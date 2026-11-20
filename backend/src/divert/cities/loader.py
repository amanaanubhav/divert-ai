"""Load and validate the committed OSM city snapshots (cities/data/<id>.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from divert.domain.enums import ServiceType
from divert.domain.geo import BBox, LatLng
from divert.domain.models import City, Signal, Station, TrafficProfile

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True, slots=True)
class CitySnapshot:
    city: City
    stations: tuple[Station, ...]
    signals: tuple[Signal, ...]
    meta: dict[str, Any]


class SnapshotError(ValueError):
    pass


def _latlng(raw: Any, what: str) -> LatLng:
    if not (isinstance(raw, list | tuple) and len(raw) == 2):
        raise SnapshotError(f"{what}: expected [lat, lng], got {raw!r}")
    return LatLng(float(raw[0]), float(raw[1]))


def parse_snapshot(data: dict[str, Any]) -> CitySnapshot:
    city_id = str(data["id"])
    b = data["bbox"]
    bbox = BBox(float(b["south"]), float(b["west"]), float(b["north"]), float(b["east"]))
    traffic = data["traffic"]
    city = City(
        id=city_id,
        name=str(data["name"]),
        timezone=str(data.get("timezone", "Asia/Kolkata")),
        center=_latlng(data["center"], f"{city_id}.center"),
        bbox=bbox,
        zoom=int(data["zoom"]),
        traffic=TrafficProfile(
            hourly_congestion=tuple(float(f) for f in traffic["hourly_congestion"]),
            avg_junction_delay_s=float(traffic["avg_junction_delay_s"]),
        ),
    )
    stations = tuple(
        Station(
            id=str(s["id"]),
            city_id=city_id,
            service=ServiceType(s["service"]),
            name=s.get("name"),
            pos=_latlng(s["pos"], f"station {s['id']}"),
            osm_ref=str(s["osm"]),
            emergency=s.get("emergency"),
            healthcare=s.get("healthcare"),
            speciality=s.get("speciality"),
        )
        for s in data["stations"]
    )
    signals = tuple(
        Signal(
            id=str(s["id"]),
            city_id=city_id,
            name=s.get("name"),
            pos=_latlng(s["pos"], f"signal {s['id']}"),
            osm_node_ids=tuple(int(n) for n in s["osm"]),
        )
        for s in data["signals"]
    )
    for kind, items in (("station", stations), ("signal", signals)):
        ids = [i.id for i in items]
        if len(ids) != len(set(ids)):
            raise SnapshotError(f"{city_id}: duplicate {kind} ids")
        outside = [i.id for i in items if not bbox.contains(i.pos)]
        if outside:
            raise SnapshotError(f"{city_id}: {kind}s outside bbox: {outside[:5]}")
    if not bbox.contains(city.center):
        raise SnapshotError(f"{city_id}: center outside bbox")
    return CitySnapshot(city=city, stations=stations, signals=signals, meta=dict(data["meta"]))


def load_snapshot(city_id: str, data_dir: Path = DATA_DIR) -> CitySnapshot:
    path = data_dir / f"{city_id}.json"
    if not path.is_file():
        raise SnapshotError(f"no snapshot for {city_id!r} at {path}; run `divert fetch-data`")
    snap = parse_snapshot(json.loads(path.read_text("utf-8")))
    if snap.city.id != city_id:
        raise SnapshotError(f"{path} contains city {snap.city.id!r}")
    return snap


def available_cities(data_dir: Path = DATA_DIR) -> list[str]:
    return sorted(p.stem for p in data_dir.glob("*.json"))
