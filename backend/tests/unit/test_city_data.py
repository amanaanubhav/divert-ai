"""The committed OSM snapshots are present, well-formed and provenance-complete."""

from __future__ import annotations

import json

import pytest

from divert.cities.loader import DATA_DIR, SnapshotError, load_snapshot, parse_snapshot
from divert.cities.osm import CITY_PROFILES, LICENCE
from divert.domain.enums import ServiceType

CITIES = sorted(CITY_PROFILES)


@pytest.mark.parametrize("city_id", CITIES)
def test_snapshot_is_valid_and_traceable(city_id: str) -> None:
    raw = json.loads((DATA_DIR / f"{city_id}.json").read_text("utf-8"))
    snap = load_snapshot(city_id)  # validates ids unique + everything inside bbox
    meta = snap.meta
    assert meta["licence"] == LICENCE
    assert meta["source"].startswith("OpenStreetMap")
    assert meta["fetched_at"]
    assert meta["osm_base"]
    assert meta["queries"], "the exact Overpass queries must be recorded"
    assert meta["counts"]["signals"] == len(snap.signals) > 0
    assert meta["counts"]["traffic_signal_nodes"] >= len(snap.signals)
    for service in ServiceType:
        n = sum(1 for s in snap.stations if s.service is service)
        assert meta["counts"]["stations"][service.value] == n
        assert n > 0, f"{city_id} has no {service} stations"
    assert snap.city.timezone == "Asia/Kolkata"
    assert len(snap.city.traffic.hourly_congestion) == 24
    assert snap.city.bbox.south == CITY_PROFILES[city_id]["bbox"]["south"]
    assert raw["id"] == city_id


@pytest.mark.parametrize("city_id", CITIES)
def test_signal_ids_trace_back_to_osm_nodes(city_id: str) -> None:
    snap = load_snapshot(city_id)
    member_ids: set[int] = set()
    for sig in snap.signals:
        assert sig.id == f"J{min(sig.osm_node_ids)}"
        assert list(sig.osm_node_ids) == sorted(sig.osm_node_ids)
        assert member_ids.isdisjoint(sig.osm_node_ids), "a node belongs to one junction"
        member_ids.update(sig.osm_node_ids)
    assert len(member_ids) == snap.meta["counts"]["traffic_signal_nodes"]


@pytest.mark.parametrize("city_id", CITIES)
def test_station_ids_encode_osm_element(city_id: str) -> None:
    prefix = {ServiceType.AMBULANCE: "H", ServiceType.FIRE_ENGINE: "F", ServiceType.POLICE: "P"}
    for st in load_snapshot(city_id).stations:
        osm_type, _, osm_id = st.osm_ref.partition("/")
        assert osm_type in {"node", "way", "relation"}
        assert st.id == f"{prefix[st.service]}{osm_type[0]}{osm_id}"
        if st.service is ServiceType.AMBULANCE:
            assert st.name, "hospitals become ambulance bases only when named"


def _minimal() -> dict[str, object]:
    return {
        "id": "x",
        "name": "X",
        "timezone": "Asia/Kolkata",
        "center": [1.5, 1.5],
        "bbox": {"south": 1, "west": 1, "north": 2, "east": 2},
        "zoom": 12,
        "traffic": {"hourly_congestion": [0.5] * 24, "avg_junction_delay_s": 30},
        "meta": {},
        "stations": [],
        "signals": [{"id": "J1", "name": None, "pos": [1.5, 1.5], "osm": [1]}],
    }


def test_parse_rejects_points_outside_bbox() -> None:
    data = _minimal()
    data["signals"] = [{"id": "J1", "name": None, "pos": [5.0, 5.0], "osm": [1]}]
    with pytest.raises(SnapshotError, match="outside bbox"):
        parse_snapshot(data)


def test_parse_rejects_duplicate_ids_and_bad_profiles() -> None:
    data = _minimal()
    data["signals"] = [
        {"id": "J1", "name": None, "pos": [1.5, 1.5], "osm": [1]},
        {"id": "J1", "name": None, "pos": [1.6, 1.6], "osm": [2]},
    ]
    with pytest.raises(SnapshotError, match="duplicate"):
        parse_snapshot(data)
    data = _minimal()
    data["traffic"] = {"hourly_congestion": [0.5] * 23, "avg_junction_delay_s": 30}
    with pytest.raises(ValueError, match="24"):
        parse_snapshot(data)


def test_missing_snapshot_is_a_clear_error(tmp_path: object) -> None:
    with pytest.raises(SnapshotError, match="fetch-data"):
        load_snapshot("atlantis")
