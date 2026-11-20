"""Fetch real OpenStreetMap data for the supported cities (Overpass API).

Reproducible: the exact Overpass query, the bounding box, the OSM base timestamp and
the fetch time are stored in every output file. Nothing is invented: when Overpass
cannot be reached on any endpoint after retries the fetch raises and writes nothing.

Per city:
  * ``node[highway=traffic_signals]`` -> clustered (single linkage, 40 m) into
    junction-level signals that keep their member OSM node ids;
  * ``amenity=hospital`` -> AMBULANCE bases (named ones only);
  * ``amenity=fire_station`` -> FIRE_ENGINE bases; ``amenity=police`` -> POLICE bases.

The ``traffic`` block (hourly congestion factors, average junction delay) is NOT OSM
data: it is a hand-set approximation documented in docs/data-sources.md that must be
calibrated (TomTom Traffic Index, traffic-police counts) before absolute ETAs are trusted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from divert.domain.geo import LatLng, haversine_m

log = logging.getLogger(__name__)

# Public Overpass instances, tried in rotation (the main instance is often at capacity).
OVERPASS_ENDPOINTS: Final = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
NL: Final = chr(10)
TILE_DEG: Final = 0.25  # signal queries are tiled so each stays well under gateway timeouts
USER_AGENT: Final = "divert-osm-fetch/2.0 (+https://www.openstreetmap.org/copyright)"
LICENCE: Final = "ODbL - (c) OpenStreetMap contributors"
CLUSTER_RADIUS_M: Final = 40.0
DATA_DIR: Final = Path(__file__).resolve().parent / "data"

# Hourly congestion factor = share of OSRM free-flow speed that traffic allows in that
# local hour (index 0 = 00:00-00:59 IST). Approximate diurnal shape for Indian metros
# (AM peak ~09-11, PM peak ~17-20). NOT measured - calibrate before relying on ETAs.
_DELHI_HOURLY: Final = [
    0.90, 0.92, 0.93, 0.93, 0.92, 0.88, 0.80, 0.68, 0.56, 0.50, 0.52, 0.58,
    0.60, 0.60, 0.59, 0.57, 0.54, 0.49, 0.46, 0.48, 0.56, 0.66, 0.76, 0.85,
]  # fmt: skip
_KOLKATA_HOURLY: Final = [
    0.88, 0.90, 0.92, 0.92, 0.91, 0.86, 0.76, 0.64, 0.52, 0.46, 0.47, 0.52,
    0.55, 0.55, 0.54, 0.52, 0.49, 0.45, 0.43, 0.46, 0.53, 0.63, 0.73, 0.82,
]  # fmt: skip
_BENGALURU_HOURLY: Final = [
    0.88, 0.91, 0.92, 0.92, 0.90, 0.84, 0.72, 0.58, 0.47, 0.43, 0.46, 0.52,
    0.55, 0.55, 0.53, 0.50, 0.46, 0.42, 0.40, 0.43, 0.50, 0.60, 0.72, 0.82,
]  # fmt: skip

CITY_PROFILES: Final[dict[str, dict[str, Any]]] = {
    "delhi": {
        "name": "Delhi",
        "timezone": "Asia/Kolkata",
        "bbox": {"south": 28.40, "west": 76.84, "north": 28.88, "east": 77.35},
        "center": [28.6139, 77.2090],
        "zoom": 12,
        "traffic": {"hourly_congestion": _DELHI_HOURLY, "avg_junction_delay_s": 35.0},
    },
    "kolkata": {
        "name": "Kolkata",
        "timezone": "Asia/Kolkata",
        "bbox": {"south": 22.45, "west": 88.25, "north": 22.65, "east": 88.50},
        "center": [22.5726, 88.3639],
        "zoom": 13,
        "traffic": {"hourly_congestion": _KOLKATA_HOURLY, "avg_junction_delay_s": 40.0},
    },
    "bengaluru": {
        "name": "Bengaluru",
        "timezone": "Asia/Kolkata",
        "bbox": {"south": 12.83, "west": 77.46, "north": 13.14, "east": 77.78},
        "center": [12.9716, 77.5946],
        "zoom": 12,
        "traffic": {"hourly_congestion": _BENGALURU_HOURLY, "avg_junction_delay_s": 40.0},
    },
}

# amenity -> (service, id prefix)
STATION_KINDS: Final = {
    "hospital": ("AMBULANCE", "H"),
    "fire_station": ("FIRE_ENGINE", "F"),
    "police": ("POLICE", "P"),
}
_DUPLICATE_STATION_M: Final = 150.0


class OverpassError(RuntimeError):
    """Overpass could not deliver a valid answer on any endpoint."""


def _bbox_str(b: dict[str, float]) -> str:
    return f"({b['south']},{b['west']},{b['north']},{b['east']})"


def build_queries(bbox: dict[str, float], tile_deg: float = TILE_DEG) -> list[str]:
    """One Overpass query per tile of at most ``tile_deg`` degrees.

    Each query returns the tile's traffic-signal nodes (``out body``) and its
    hospitals / fire stations / police stations (equality tag filters, which use
    Overpass's tag index; ``out center`` gives nodes their coordinates and ways or
    relations a centre point). Results are merged by OSM (type, id) before
    clustering, so tile borders neither duplicate nor split anything.
    """
    ny = max(1, math.ceil(round((bbox["north"] - bbox["south"]) / tile_deg, 9)))
    nx = max(1, math.ceil(round((bbox["east"] - bbox["west"]) / tile_deg, 9)))
    dy = (bbox["north"] - bbox["south"]) / ny
    dx = (bbox["east"] - bbox["west"]) / nx
    queries: list[str] = []
    for iy in range(ny):
        for ix in range(nx):
            b = _bbox_str(
                {
                    "south": round(bbox["south"] + iy * dy, 6),
                    "west": round(bbox["west"] + ix * dx, 6),
                    "north": round(bbox["south"] + (iy + 1) * dy, 6),
                    "east": round(bbox["west"] + (ix + 1) * dx, 6),
                }
            )
            queries.append(
                NL.join(
                    (
                        "[out:json][timeout:240];",
                        f'node["highway"="traffic_signals"]{b};',
                        "out body;",
                        "(",
                        f'  nwr["amenity"="hospital"]{b};',
                        f'  nwr["amenity"="fire_station"]{b};',
                        f'  nwr["amenity"="police"]{b};',
                        ");",
                        "out center;",
                    )
                )
            )
    return queries


def run_overpass(
    query: str,
    *,
    endpoints: tuple[str, ...] = OVERPASS_ENDPOINTS,
    rounds: int = 6,
    backoff_s: float = 15.0,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any], str]:
    """POST the query to each endpoint in turn, ``rounds`` times with backoff.

    Returns (payload, endpoint). Raises OverpassError when every attempt failed.
    """
    last_error: Exception | None = None
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(240.0, connect=20.0))
    try:
        for attempt in range(rounds):
            for endpoint in endpoints:
                try:
                    resp = http.post(
                        endpoint, data={"data": query}, headers={"User-Agent": USER_AGENT}
                    )
                    resp.raise_for_status()
                    payload: dict[str, Any] = resp.json()
                    if not isinstance(payload.get("elements"), list):
                        raise OverpassError(f"unexpected payload from {endpoint}")
                    remark = str(payload.get("remark") or "")
                    if "error" in remark.lower() or "timed out" in remark.lower():
                        raise OverpassError(f"Overpass remark from {endpoint}: {remark}")
                    log.info("overpass ok", extra={"endpoint": endpoint, "attempt": attempt})
                    return payload, endpoint
                except (httpx.HTTPError, ValueError, OverpassError) as exc:
                    last_error = exc
                    log.warning("overpass failed: %s: %s", endpoint, exc)
            if attempt + 1 < rounds:
                time.sleep(backoff_s * 2**attempt)
    finally:
        if own:
            http.close()
    raise OverpassError(f"Overpass unreachable after {rounds} rounds: {last_error}")


def element_position(el: dict[str, Any]) -> LatLng | None:
    if "lat" in el and "lon" in el:
        return LatLng(float(el["lat"]), float(el["lon"]))
    center = el.get("center")
    if isinstance(center, dict):
        return LatLng(float(center["lat"]), float(center["lon"]))
    return None


def element_name(tags: dict[str, str]) -> str | None:
    for key in ("name:en", "name", "official_name", "junction:name", "short_name"):
        value = tags.get(key, "").strip()
        if value:
            return value
    return None


@dataclass
class _Cluster:
    ids: list[int] = field(default_factory=list)
    points: list[LatLng] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


def cluster_signals(
    nodes: list[tuple[int, float, float, str | None]], radius_m: float = CLUSTER_RADIUS_M
) -> list[dict[str, Any]]:
    """Single-linkage clustering of signal nodes within ``radius_m`` (grid accelerated).

    A junction with several signal heads (one OSM node per approach) becomes one
    signal whose id is ``J<smallest member node id>`` and whose position is the
    member centroid. Member ids are kept so the junction can be traced back to OSM.
    """
    ordered = sorted(nodes, key=lambda n: n[0])
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell_deg = radius_m / 111_000.0  # a grid cell is >= radius in both axes at these latitudes
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (_, lat, lng, _) in enumerate(ordered):
        grid[(int(lat // cell_deg), int(lng // cell_deg))].append(idx)
    for idx, (_, lat, lng, _) in enumerate(ordered):
        cx, cy = int(lat // cell_deg), int(lng // cell_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other <= idx:
                        continue
                    o = ordered[other]
                    if haversine_m(LatLng(lat, lng), LatLng(o[1], o[2])) <= radius_m:
                        ra, rb = find(idx), find(other)
                        if ra != rb:
                            parent[max(ra, rb)] = min(ra, rb)

    clusters: dict[int, _Cluster] = defaultdict(_Cluster)
    for idx, (osm_id, lat, lng, name) in enumerate(ordered):
        c = clusters[find(idx)]
        c.ids.append(osm_id)
        c.points.append(LatLng(lat, lng))
        if name:
            c.names.append(name)

    out: list[dict[str, Any]] = []
    for c in clusters.values():
        lat = sum(p.lat for p in c.points) / len(c.points)
        lng = sum(p.lng for p in c.points) / len(c.points)
        name = max(sorted(set(c.names)), key=c.names.count) if c.names else None
        out.append(
            {
                "id": f"J{min(c.ids)}",
                "name": name,
                "pos": [round(lat, 6), round(lng, 6)],
                "osm": sorted(c.ids),
            }
        )
    out.sort(key=lambda s: str(s["id"]))
    return out


def extract_stations(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stations: list[dict[str, Any]] = []
    seen: list[tuple[str, str, LatLng]] = []
    for el in sorted(elements, key=lambda e: (str(e["type"]), int(e["id"]))):
        tags: dict[str, str] = el.get("tags", {})
        amenity = tags.get("amenity")
        if amenity not in STATION_KINDS:
            continue
        pos = element_position(el)
        if pos is None:
            continue
        service, prefix = STATION_KINDS[amenity]
        name = element_name(tags)
        if service == "AMBULANCE" and not name:
            continue  # hospitals become ambulance bases only when named
        # The same facility is often mapped twice (node + area): keep the first.
        key = (name or "").casefold()
        if key and any(
            s == service and n == key and haversine_m(p, pos) < _DUPLICATE_STATION_M
            for s, n, p in seen
        ):
            continue
        seen.append((service, key, pos))
        stations.append(
            {
                "id": f"{prefix}{str(el['type'])[0]}{el['id']}",
                "service": service,
                "name": name,
                "pos": [round(pos.lat, 6), round(pos.lng, 6)],
                "osm": f"{el['type']}/{el['id']}",
            }
        )
    return stations


def _cached_overpass(
    query: str, client: httpx.Client | None, cache_dir: Path | None
) -> tuple[dict[str, Any], str]:
    """run_overpass with an optional raw-response cache (makes long fetches resumable)."""
    path = None
    if cache_dir is not None:
        path = cache_dir / f"{hashlib.sha256(query.encode()).hexdigest()[:20]}.json"
        if path.is_file():
            cached: dict[str, Any] = json.loads(path.read_text("utf-8"))
            return cached["payload"], str(cached["endpoint"])
    payload, endpoint = run_overpass(query, client=client)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"endpoint": endpoint, "payload": payload}), "utf-8")
    return payload, endpoint


def build_city(
    city_id: str, *, client: httpx.Client | None = None, cache_dir: Path | None = None
) -> dict[str, Any]:
    profile = CITY_PROFILES[city_id]
    queries = build_queries(profile["bbox"])
    log.info("querying overpass for %s (%d sub-queries)", city_id, len(queries))
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    endpoints_used: list[str] = []
    osm_bases: list[str] = []
    for query in queries:
        payload, endpoint = _cached_overpass(query, client, cache_dir)
        endpoints_used.append(endpoint)
        base = payload.get("osm3s", {}).get("timestamp_osm_base")
        if base:
            osm_bases.append(str(base))
        for el in payload["elements"]:
            merged[(str(el["type"]), int(el["id"]))] = el
    elements = list(merged.values())
    bbox = profile["bbox"]

    def inside(lat: float, lng: float) -> bool:
        return bool(bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lng <= bbox["east"])

    signal_nodes = [
        (int(el["id"]), float(el["lat"]), float(el["lon"]), element_name(el.get("tags", {})))
        for el in elements
        if el["type"] == "node"
        and el.get("tags", {}).get("highway") == "traffic_signals"
        and inside(float(el["lat"]), float(el["lon"]))
    ]
    signals = cluster_signals(signal_nodes)
    stations = [s for s in extract_stations(elements) if inside(*s["pos"])]
    counts = {
        "traffic_signal_nodes": len(signal_nodes),
        "signals": len(signals),
        "stations": {
            svc: sum(1 for s in stations if s["service"] == svc)
            for svc in ("AMBULANCE", "FIRE_ENGINE", "POLICE")
        },
    }
    if not signals or not stations:
        raise OverpassError(f"{city_id}: Overpass returned no signals/stations - refusing to write")
    log.info("%s counts %s", city_id, counts)
    return {
        "id": city_id,
        "name": profile["name"],
        "timezone": profile["timezone"],
        "center": profile["center"],
        "bbox": bbox,
        "zoom": profile["zoom"],
        "traffic": profile["traffic"],
        "meta": {
            "source": "OpenStreetMap via Overpass API",
            "endpoints": sorted(set(endpoints_used)),
            "queries": queries,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "osm_base": max(osm_bases) if osm_bases else None,
            "licence": LICENCE,
            "cluster_radius_m": CLUSTER_RADIUS_M,
            "counts": counts,
            "traffic_profile_note": (
                "traffic.hourly_congestion and traffic.avg_junction_delay_s are approximations, "
                "not OSM data; calibrate with real measurements (e.g. TomTom Traffic Index, "
                "traffic-police junction counts)."
            ),
        },
        "stations": stations,
        "signals": signals,
    }


def fetch_cities(
    city_ids: list[str], out_dir: Path = DATA_DIR, cache_dir: Path | None = None
) -> dict[str, Path]:
    """Fetch every city first, then write all files (all-or-nothing per run)."""
    unknown = sorted(set(city_ids) - set(CITY_PROFILES))
    if unknown:
        raise ValueError(f"unknown cities {unknown}; choose from {sorted(CITY_PROFILES)}")
    built: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
        for i, city_id in enumerate(city_ids):
            if i:
                time.sleep(5)  # be polite to the public Overpass instances
            built[city_id] = build_city(city_id, client=client, cache_dir=cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for city_id, data in built.items():
        path = out_dir / f"{city_id}.json"
        path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n", "utf-8")
        written[city_id] = path
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch OSM snapshots for DIVERT cities")
    parser.add_argument("cities", nargs="*", default=list(CITY_PROFILES))
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--cache-dir", type=Path, default=None, help="keep raw Overpass responses (resumable)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        written = fetch_cities(list(args.cities), args.out, args.cache_dir)
    except (OverpassError, ValueError) as exc:
        log.error("fetch failed, nothing written: %s", exc)
        return 1
    for city_id, path in written.items():
        log.info("wrote %s (%d KiB) for %s", path, path.stat().st_size // 1024, city_id)
    return 0
