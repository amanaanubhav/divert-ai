"""OSRM routing client with a Redis cache, timeouts, retries and metrics.

``GET {DIVERT_OSRM_URL}/route/v1/driving/{lng,lat};{lng,lat}`` with ``overview=full
geometries=geojson steps=true annotations=distance,duration,speed``. There is no
straight-line fallback: when OSRM cannot route, :class:`RoutingError` is raised and the
dispatch fails with 503 before anything is written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from divert import __version__
from divert.domain.geo import LatLng, haversine_m
from divert.domain.route import Maneuver, Route
from divert.metrics import OSRM_ERRORS, OSRM_LATENCY
from divert.store import redis_store as keys
from divert.store.guard import RedisGuard
from divert.store.streams import to_str

log = logging.getLogger(__name__)

PROFILE: Final = "driving"
PARAMS: Final = {
    "overview": "full",
    "geometries": "geojson",
    "steps": "true",
    "annotations": "distance,duration,speed",
    "alternatives": "false",
}
RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
MANEUVER_SNAP_M: Final = 250.0


class RoutingError(Exception):
    """OSRM could not produce a route (unreachable, error status, NoRoute...)."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def _coords(points: Sequence[LatLng]) -> str:
    return ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in points)


def _locate_maneuvers(
    points: Sequence[LatLng],
    cumulative: Sequence[float],
    steps: Sequence[dict[str, Any]],
    scale: float,
) -> list[Maneuver]:
    """Place each step's maneuver on our polyline: start from OSRM's cumulative step
    distance (rescaled to our haversine length) and snap to the nearest vertex nearby."""
    out: list[Maneuver] = []
    acc = 0.0
    lo = 0
    for step in steps:
        man = step.get("maneuver") or {}
        loc = man.get("location")
        expected = acc * scale
        acc += float(step.get("distance", 0.0))
        if not loc or len(loc) != 2:
            continue
        where = LatLng(float(loc[1]), float(loc[0]))
        best_i, best_d = -1, float("inf")
        for i in range(lo, len(points)):
            if cumulative[i] > expected + MANEUVER_SNAP_M:
                break
            if cumulative[i] < expected - MANEUVER_SNAP_M:
                continue
            d = haversine_m(where, points[i])
            if d < best_d:
                best_i, best_d = i, d
        along = cumulative[best_i] if best_i >= 0 else min(expected, cumulative[-1])
        if best_i >= 0:
            lo = best_i
        mod = man.get("modifier")
        out.append(
            Maneuver(
                along_m=along,
                type=str(man.get("type", "")),
                modifier=str(mod) if mod is not None else None,
                location=where,
            )
        )
    return out


def parse_osrm_route(body: dict[str, Any]) -> Route:
    """Turn an OSRM /route response into a :class:`Route` (raises RoutingError)."""
    if body.get("code") != "Ok" or not body.get("routes"):
        raise RoutingError(
            f"OSRM: {body.get('code')} {body.get('message', '')}".strip(), kind="no_route"
        )
    r = body["routes"][0]
    coords = r.get("geometry", {}).get("coordinates") or []
    points = [LatLng(float(lat), float(lng)) for lng, lat in coords]
    if len(points) < 2:
        raise RoutingError("OSRM returned a degenerate route", kind="no_route")
    speeds: list[float] = []
    steps: list[dict[str, Any]] = []
    for leg in r.get("legs", []):
        ann = leg.get("annotation") or {}
        dist = ann.get("distance") or []
        dur = ann.get("duration") or []
        spd = ann.get("speed") or []
        for i in range(len(dist)):
            if i < len(spd) and spd[i]:
                speeds.append(float(spd[i]))
            elif i < len(dur) and dur[i]:
                speeds.append(float(dist[i]) / float(dur[i]))
            else:
                speeds.append(0.0)
        steps.extend(leg.get("steps") or [])
    distance = float(r.get("distance", 0.0))
    duration = float(r.get("duration", 0.0))
    if len(speeds) != len(points) - 1:
        # annotation/geometry mismatch (should not happen with overview=full): use the mean
        mean = distance / duration if duration > 0 else 10.0
        log.warning("osrm annotation mismatch; using mean speed", extra={"mean_mps": mean})
        speeds = [mean] * (len(points) - 1)
    route = Route.build(points, speeds, osrm_distance_m=distance, osrm_duration_s=duration)
    scale = route.length_m / distance if distance > 0 else 1.0
    maneuvers = _locate_maneuvers(route.points, route.cumulative, steps, scale)
    return Route(
        points=route.points,
        cumulative=route.cumulative,
        segment_speeds_mps=route.segment_speeds_mps,
        maneuvers=tuple(maneuvers),
        osrm_distance_m=distance,
        osrm_duration_s=duration,
    )


class OsrmRouter:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 8.0,
        retries: int = 2,
        cache: Redis | None = None,
        cache_ttl_s: int = 3600,
        client: httpx.AsyncClient | None = None,
        backoff_s: float = 0.25,
        cache_guard: RedisGuard | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.cache = cache
        self.cache_ttl_s = cache_ttl_s
        self.cache_guard = cache_guard
        self.backoff_s = backoff_s
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0)),
            headers={"User-Agent": f"divert/{__version__} (+emergency corridor simulation)"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def route(self, origin: LatLng, destination: LatLng) -> Route:
        coords = _coords([origin, destination])
        body = await self._cached(coords)
        if body is None:
            body = await self._fetch(coords)
            await self._store(coords, body)
        return parse_osrm_route(body)

    async def _cached(self, coords: str) -> dict[str, Any] | None:
        if self.cache is None or self.cache_ttl_s <= 0:
            return None
        cache, key = self.cache, keys.osrm_route_cache(PROFILE, coords)
        try:
            if self.cache_guard is None:
                raw = await cache.get(key)
            else:
                raw = await self.cache_guard.call(lambda: cache.get(key))
        except RedisError as exc:  # cache is an optimisation; never fail a dispatch on it
            log.warning("osrm cache read failed", extra={"error": repr(exc)})
            return None
        if raw is None:
            return None
        data = json.loads(to_str(raw))
        return data if isinstance(data, dict) else None

    async def _store(self, coords: str, body: dict[str, Any]) -> None:
        if self.cache is None or self.cache_ttl_s <= 0 or body.get("code") != "Ok":
            return
        cache, key = self.cache, keys.osrm_route_cache(PROFILE, coords)
        value, ttl = json.dumps(body, separators=(",", ":")), self.cache_ttl_s
        try:
            if self.cache_guard is None:
                await cache.set(key, value, ex=ttl)
            else:
                await self.cache_guard.call(lambda: cache.set(key, value, ex=ttl))
        except RedisError as exc:
            log.warning("osrm cache write failed", extra={"error": repr(exc)})

    async def _fetch(self, coords: str) -> dict[str, Any]:
        url = f"{self.base_url}/route/v1/{PROFILE}/{coords}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)) * (1 + random.random()))  # noqa: S311
            started = time.perf_counter()
            try:
                resp = await self.client.get(url, params=PARAMS)
            except httpx.TransportError as exc:
                OSRM_LATENCY.labels(outcome="transport_error").observe(
                    time.perf_counter() - started
                )
                OSRM_ERRORS.labels(kind="transport").inc()
                last = exc
                continue
            OSRM_LATENCY.labels(outcome=str(resp.status_code)).observe(
                time.perf_counter() - started
            )
            if resp.status_code in RETRYABLE_STATUS:
                OSRM_ERRORS.labels(kind=f"http_{resp.status_code}").inc()
                last = RoutingError(f"OSRM HTTP {resp.status_code}", kind="unavailable")
                continue
            try:
                body = resp.json()
            except ValueError as exc:
                OSRM_ERRORS.labels(kind="bad_json").inc()
                raise RoutingError("OSRM returned invalid JSON", kind="bad_response") from exc
            if not isinstance(body, dict):
                OSRM_ERRORS.labels(kind="bad_json").inc()
                raise RoutingError("OSRM returned a non-object", kind="bad_response")
            if resp.status_code >= 400 or body.get("code") != "Ok":
                OSRM_ERRORS.labels(kind=str(body.get("code", resp.status_code))).inc()
                raise RoutingError(
                    f"OSRM {resp.status_code}: {body.get('code')} {body.get('message', '')}",
                    kind="no_route",
                )
            return body
        raise RoutingError(
            f"OSRM unavailable after {self.retries + 1} attempts: {last!r}", kind="unavailable"
        )
