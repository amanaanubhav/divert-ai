"""OSRM routing client with a Redis cache, timeouts, retries and metrics.

``GET {DIVERT_OSRM_URL}/route/v1/driving/{lng,lat};{lng,lat}`` with ``overview=full
geometries=geojson steps=true annotations=distance,duration,speed,nodes
alternatives=DIVERT_ROUTE_ALTERNATIVES``. Each returned route keeps its step maneuvers
(type, modifier, roundabout exit, road name), its OSRM intersections (road bearings,
in/out) - the junction geometry movements and signal phases are built from - and the OSM
node id of every vertex (which vertices belong to a signal cluster).

A zero-length answer (origin and destination snap to the same road point) raises
:class:`RoutingError` with ``kind=SAME_POINT``.

:meth:`OsrmRouter.alternatives` returns the candidates for crowd-aware route choice
(design §2): OSRM's alternatives, plus up to DIVERT_ROUTE_DETOURS *via-point detours*
when fewer than 2 routes come back and the trip is longer than 1.5 km (via = the
midpoint offset perpendicular by +-20 % of the straight distance, snapped by OSRM).

There is no straight-line fallback: when OSRM cannot route, :class:`RoutingError` is
raised and the dispatch fails with 503 before anything is written. The cache key covers
the OSRM base URL and the request parameters, so switching servers never serves the
previous server's routes.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
from divert.domain.geo import (
    LatLng,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    interpolate_by_distance,
    project_onto_polyline,
)
from divert.domain.route import Intersection, Maneuver, Route
from divert.metrics import OSRM_ERRORS, OSRM_LATENCY
from divert.store import redis_store as keys
from divert.store.guard import RedisGuard
from divert.store.streams import to_str

log = logging.getLogger(__name__)

PROFILE: Final = "driving"
BASE_PARAMS: Final = {
    "overview": "full",
    "geometries": "geojson",
    "steps": "true",
    # nodes: the OSM node id of every vertex - finds a signal cluster's member nodes
    "annotations": "distance,duration,speed,nodes",
}
RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
# RoutingError.kind of a zero-length answer: origin and destination snap to the same road
# point (e.g. a RETURN to a station mapped off the road, asked from its road point)
SAME_POINT: Final = "same_point"
MANEUVER_SNAP_M: Final = 250.0

# via-point detours (design §2; assumptions)
DETOUR_MIN_TRIP_M: Final = 1500.0
DETOUR_OFFSET_SHARE: Final = 0.20
DUPLICATE_SAMPLES: Final = 12  # points of a candidate compared against an earlier one
DUPLICATE_MAX_GAP_M: Final = 25.0  # ...all this close -> the same route


class RoutingError(Exception):
    """OSRM could not produce a route (unreachable, error status, NoRoute...)."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def _coords(points: Sequence[LatLng]) -> str:
    return ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in points)


def _snap(
    points: Sequence[LatLng],
    cumulative: Sequence[float],
    where: LatLng,
    *,
    expected: float,
    lo: int,
    window_m: float,
) -> int:
    """Index of the vertex nearest ``where`` within ``window_m`` of ``expected`` (or -1)."""
    best_i, best_d = -1, float("inf")
    for i in range(lo, len(points)):
        if cumulative[i] > expected + window_m:
            break
        if cumulative[i] < expected - window_m:
            continue
        d = haversine_m(where, points[i])
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def _loc(raw: Any) -> LatLng | None:
    if not raw or len(raw) != 2:
        return None
    return LatLng(float(raw[1]), float(raw[0]))


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


def _locate_steps(
    points: Sequence[LatLng],
    cumulative: Sequence[float],
    steps: Sequence[dict[str, Any]],
    scale: float,
) -> tuple[list[Maneuver], list[Intersection]]:
    """Place each step's maneuver and intersections on our polyline: start from OSRM's
    cumulative step distance (rescaled to our haversine length) and snap to the nearest
    vertex nearby (OSRM geometry has a vertex at every intersection node)."""
    maneuvers: list[Maneuver] = []
    intersections: list[Intersection] = []
    exact: dict[tuple[float, float], list[int]] = {}
    for i, p in enumerate(points):
        exact.setdefault((round(p.lat, 6), round(p.lng, 6)), []).append(i)
    acc = 0.0
    lo = 0
    for step in steps:
        man = step.get("maneuver") or {}
        start = acc * scale
        acc += float(step.get("distance", 0.0))
        end = acc * scale
        where = _loc(man.get("location"))
        if where is not None:
            i = _snap(points, cumulative, where, expected=start, lo=lo, window_m=MANEUVER_SNAP_M)
            along = cumulative[i] if i >= 0 else min(start, cumulative[-1])
            lo = max(lo, i)
            mod = man.get("modifier")
            name = step.get("name")
            maneuvers.append(
                Maneuver(
                    along_m=along,
                    type=str(man.get("type", "")),
                    modifier=str(mod) if mod is not None else None,
                    location=where,
                    exit=_opt_int(man.get("exit")),
                    name=str(name) if name else None,
                )
            )
        ilo = lo
        for raw in step.get("intersections") or []:
            loc = _loc(raw.get("location"))
            bearings = raw.get("bearings") or []
            if loc is None or not bearings:
                continue
            hits = [k for k in exact.get((round(loc.lat, 6), round(loc.lng, 6)), []) if k >= ilo]
            if hits:
                i = hits[0]
            else:  # not a vertex of ours (rounding): nearest vertex within this step
                window = MANEUVER_SNAP_M + max(0.0, end - start)
                i = _snap(points, cumulative, loc, expected=start, lo=ilo, window_m=window)
                if i < 0:
                    continue
            ilo = i
            intersections.append(
                Intersection(
                    along_m=cumulative[i],
                    location=loc,
                    bearings=tuple(float(b) for b in bearings),
                    in_idx=_opt_int(raw.get("in")),
                    out_idx=_opt_int(raw.get("out")),
                )
            )
    return maneuvers, intersections


def _leg_node_ids(legs: Sequence[dict[str, Any]], n_points: int) -> list[int]:
    """OSM node id per geometry vertex (``annotation.nodes``; legs share their via
    vertex). A leg's first and last vertices are snapped points, not nodes - OSRM lists
    a neighbouring node there - so they get 0 (unknown). Empty when OSRM sent no nodes or
    the counts do not line up."""
    ids: list[int] = []
    for k, leg in enumerate(legs):
        ann = leg.get("annotation") or {}
        nodes = ann.get("nodes") or []
        if not nodes or len(nodes) != len(ann.get("distance") or []) + 1:
            return []
        own = [0, *(int(n) for n in nodes[1:-1]), 0]
        ids.extend(own if k == 0 else own[1:])
    return ids if len(ids) == n_points else []


def _merge_ids(points: Sequence[LatLng], ids: Sequence[int]) -> tuple[int, ...]:
    """Node ids for the vertices :meth:`Route.build` keeps (a run of identical
    consecutive points becomes one vertex: the first known id of the run)."""
    if not ids:
        return ()
    out: list[int] = []
    for i, (p, n) in enumerate(zip(points, ids, strict=True)):
        if i and points[i - 1] == p:
            out[-1] = out[-1] or n
        else:
            out.append(n)
    return tuple(out)


def _parse_one(r: dict[str, Any]) -> Route:
    coords = r.get("geometry", {}).get("coordinates") or []
    points = [LatLng(float(lat), float(lng)) for lng, lat in coords]
    if len(points) < 2:
        raise RoutingError("OSRM returned a degenerate route", kind="no_route")
    if len(set(points)) < 2:  # a Route needs two distinct points
        raise RoutingError(
            "OSRM: origin and destination snap to the same road point", kind=SAME_POINT
        )
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
    maneuvers, intersections = _locate_steps(route.points, route.cumulative, steps, scale)
    node_ids = _merge_ids(points, _leg_node_ids(r.get("legs", []), len(points)))
    return Route(
        points=route.points,
        cumulative=route.cumulative,
        segment_speeds_mps=route.segment_speeds_mps,
        maneuvers=tuple(maneuvers),
        osrm_distance_m=distance,
        osrm_duration_s=duration,
        intersections=tuple(intersections),
        node_ids=node_ids if len(node_ids) == len(route.points) else (),
    )


def parse_osrm_routes(body: dict[str, Any]) -> list[Route]:
    """Every route of an OSRM /route response (raises RoutingError when there is none)."""
    if body.get("code") != "Ok" or not body.get("routes"):
        raise RoutingError(
            f"OSRM: {body.get('code')} {body.get('message', '')}".strip(), kind="no_route"
        )
    return [_parse_one(r) for r in body["routes"]]


def parse_osrm_route(body: dict[str, Any]) -> Route:
    """The first (OSRM's preferred) route of a response."""
    return parse_osrm_routes(body)[0]


def detour_vias(origin: LatLng, destination: LatLng, count: int) -> list[LatLng]:
    """Midpoint offset perpendicular by +-20 % of the straight distance (left, right, ...)."""
    straight = haversine_m(origin, destination)
    if count <= 0 or straight <= 0:
        return []
    mid, _ = interpolate_by_distance([origin, destination], [0.0, straight], straight / 2.0)
    brg = initial_bearing_deg(origin, destination)
    out: list[LatLng] = []
    for k in range(count):
        side = -90.0 if k % 2 == 0 else 90.0
        scale = 1 + k // 2  # 20 %, 20 %, 40 %, 40 % ...
        out.append(destination_point(mid, brg + side, DETOUR_OFFSET_SHARE * straight * scale))
    return out


def is_duplicate(candidate: Route, existing: Sequence[Route]) -> bool:
    """Same road path as an earlier candidate (every sample point within 25 m of it)."""
    n = DUPLICATE_SAMPLES
    samples = [
        interpolate_by_distance(candidate.points, candidate.cumulative, candidate.length_m * k / n)[
            0
        ]
        for k in range(1, n)
    ]
    for other in existing:
        if all(
            project_onto_polyline(p, other.points, other.cumulative).offset_m <= DUPLICATE_MAX_GAP_M
            for p in samples
        ):
            return True
    return False


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
        alternatives: int = 3,
        detours: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.cache = cache
        self.cache_ttl_s = cache_ttl_s
        self.cache_guard = cache_guard
        self.backoff_s = backoff_s
        self.max_alternatives = alternatives
        self.max_detours = detours
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0)),
            headers={"User-Agent": f"divert/{__version__} (+emergency corridor simulation)"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    def _params(self, alternatives: int, *, via: bool = False) -> dict[str, str]:
        params = dict(BASE_PARAMS)
        params["alternatives"] = str(alternatives) if alternatives > 1 else "false"
        if via:
            params["continue_straight"] = "true"  # no U-turn at the via point
        return params

    async def route(self, origin: LatLng, destination: LatLng) -> Route:
        """OSRM's preferred route (no alternatives requested)."""
        body = await self._get([origin, destination], self._params(1))
        return parse_osrm_route(body)

    async def alternatives(self, origin: LatLng, destination: LatLng) -> list[Route]:
        """Route candidates for crowd-aware choice (see module doc); never empty."""
        body = await self._get([origin, destination], self._params(self.max_alternatives))
        routes = parse_osrm_routes(body)[: max(1, self.max_alternatives)]
        if len(routes) >= 2 or haversine_m(origin, destination) <= DETOUR_MIN_TRIP_M:
            return routes
        vias = detour_vias(origin, destination, self.max_detours)
        results = await asyncio.gather(
            *(self._get([origin, v, destination], self._params(1, via=True)) for v in vias),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, RoutingError):  # a detour is optional
                log.info("detour not routable", extra={"error": str(res)})
                continue
            if isinstance(res, BaseException):
                raise res
            detour = dataclasses.replace(parse_osrm_route(res), source="detour")
            if not is_duplicate(detour, routes):
                routes.append(detour)
        return routes

    async def _get(self, points: Sequence[LatLng], params: dict[str, str]) -> dict[str, Any]:
        coords = _coords(points)
        key = keys.osrm_route_cache(PROFILE, coords, scope=self._scope(params))
        body = await self._cached(key)
        if body is None:
            body = await self._fetch(coords, params)
            await self._store(key, body)
        return body

    def _scope(self, params: dict[str, str]) -> str:
        """Everything besides the coordinates that changes the response."""
        return self.base_url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

    async def _cached(self, key: str) -> dict[str, Any] | None:
        if self.cache is None or self.cache_ttl_s <= 0:
            return None
        cache = self.cache
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

    async def _store(self, key: str, body: dict[str, Any]) -> None:
        if self.cache is None or self.cache_ttl_s <= 0 or body.get("code") != "Ok":
            return
        cache = self.cache
        value, ttl = json.dumps(body, separators=(",", ":")), self.cache_ttl_s
        try:
            if self.cache_guard is None:
                await cache.set(key, value, ex=ttl)
            else:
                await self.cache_guard.call(lambda: cache.set(key, value, ex=ttl))
        except RedisError as exc:
            log.warning("osrm cache write failed", extra={"error": repr(exc)})

    async def _fetch(self, coords: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}/route/v1/{PROFILE}/{coords}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)) * (1 + random.random()))  # noqa: S311
            started = time.perf_counter()
            try:
                resp = await self.client.get(url, params=params)
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
