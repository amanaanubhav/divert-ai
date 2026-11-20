"""OSM fetcher: tiling, clustering, station extraction, retry/fallback, no-write-on-failure.

All Overpass traffic is mocked with respx; the element lists below are synthetic test
inputs (not presented as real data).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from divert.cities import osm
from divert.domain.geo import LatLng, destination_point, haversine_m

ORIGIN = LatLng(22.5726, 88.3639)


def _node_at(
    osm_id: int, p: LatLng, name: str | None = None
) -> tuple[int, float, float, str | None]:
    return (osm_id, p.lat, p.lng, name)


def test_build_queries_tiles_cover_bbox_exactly() -> None:
    bbox = osm.CITY_PROFILES["delhi"]["bbox"]
    queries = osm.build_queries(bbox)
    assert len(queries) == 6  # 0.48 x 0.51 deg -> 2 x 3 tiles of <= 0.25 deg
    for q in queries:
        assert 'node["highway"="traffic_signals"]' in q
        for amenity in ("hospital", "fire_station", "police"):
            assert f'nwr["amenity"="{amenity}"]' in q
        assert q.rstrip().endswith("out center;")
    assert "(28.4,76.84," in queries[0]  # south-west tile starts at the bbox corner
    assert ",28.88,77.35)" in queries[-1]  # north-east tile ends at the bbox corner
    assert len(osm.build_queries(osm.CITY_PROFILES["kolkata"]["bbox"])) == 1


def test_cluster_signals_single_linkage_40m() -> None:
    a = ORIGIN
    b = destination_point(a, 90, 30)  # 30 m from a -> same junction
    c = destination_point(b, 90, 35)  # 35 m from b (65 m from a) -> chained into it
    far = destination_point(a, 0, 500)  # separate junction
    nodes = [
        _node_at(30, c),
        _node_at(10, a, "Park Circus"),
        _node_at(20, b, "Park Circus"),
        _node_at(40, far),
    ]
    clusters = osm.cluster_signals(nodes)
    assert [c_["id"] for c_ in clusters] == ["J10", "J40"]
    junction = clusters[0]
    assert junction["osm"] == [10, 20, 30]
    assert junction["name"] == "Park Circus"
    centroid = LatLng(*junction["pos"])
    assert haversine_m(centroid, b) < 15  # centroid of a, b, c sits near b
    assert clusters[1]["osm"] == [40]
    assert clusters[1]["name"] is None


def test_cluster_keeps_nodes_just_over_radius_apart() -> None:
    a = ORIGIN
    b = destination_point(a, 45, 40.5)
    assert len(osm.cluster_signals([_node_at(1, a), _node_at(2, b)])) == 2
    c = destination_point(a, 45, 39.5)
    assert len(osm.cluster_signals([_node_at(1, a), _node_at(3, c)])) == 1


def _el(kind: str, osm_id: int, p: LatLng, **tags: str) -> dict[str, Any]:
    el: dict[str, Any] = {"type": kind, "id": osm_id, "tags": tags}
    if kind == "node":
        el |= {"lat": p.lat, "lon": p.lng}
    else:
        el["center"] = {"lat": p.lat, "lon": p.lng}
    return el


def test_extract_stations_rules() -> None:
    p = ORIGIN
    els = [
        _el("node", 1, p, amenity="hospital", name="City Hospital"),
        _el("way", 2, destination_point(p, 0, 60), amenity="hospital", name="City Hospital"),
        _el("node", 3, destination_point(p, 0, 900), amenity="hospital"),  # unnamed -> skipped
        _el("way", 4, destination_point(p, 90, 900), amenity="fire_station"),  # unnamed ok
        _el("node", 5, destination_point(p, 180, 900), amenity="police", **{"name:en": "PS"}),
        _el("node", 6, destination_point(p, 270, 900), amenity="school", name="ignored"),
    ]
    stations = osm.extract_stations(els)
    by_id = {s["id"]: s for s in stations}
    # node+area duplicate of the same named facility within 150 m -> one station
    assert set(by_id) == {"Hn1", "Fw4", "Pn5"}
    assert by_id["Hn1"]["service"] == "AMBULANCE"
    assert by_id["Fw4"]["service"] == "FIRE_ENGINE"
    assert by_id["Fw4"]["osm"] == "way/4"
    assert by_id["Pn5"]["service"] == "POLICE"
    assert by_id["Pn5"]["name"] == "PS"


def _payload(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z"}, "elements": elements}


@respx.mock
def test_run_overpass_falls_back_to_next_endpoint() -> None:
    first = respx.post(osm.OVERPASS_ENDPOINTS[0]).mock(return_value=httpx.Response(502))
    second = respx.post(osm.OVERPASS_ENDPOINTS[1]).mock(
        return_value=httpx.Response(200, json=_payload([]))
    )
    payload, endpoint = osm.run_overpass("q", rounds=1, backoff_s=0)
    assert endpoint == osm.OVERPASS_ENDPOINTS[1]
    assert payload["elements"] == []
    assert first.called
    assert second.called


@respx.mock
def test_run_overpass_rejects_runtime_error_remark() -> None:
    for ep in osm.OVERPASS_ENDPOINTS:
        respx.post(ep).mock(
            return_value=httpx.Response(
                200, json={"elements": [], "remark": "runtime error: Query timed out"}
            )
        )
    with pytest.raises(osm.OverpassError):
        osm.run_overpass("q", rounds=2, backoff_s=0)


@respx.mock
def test_fetch_writes_nothing_when_overpass_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(osm.time, "sleep", lambda _s: None)
    for ep in osm.OVERPASS_ENDPOINTS:
        respx.post(ep).mock(return_value=httpx.Response(504))
    assert osm.main(["kolkata", "--out", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_fetch_builds_snapshot_with_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(osm.time, "sleep", lambda _s: None)
    elements = [
        {"type": "node", "id": 11, "lat": ORIGIN.lat, "lon": ORIGIN.lng,
         "tags": {"highway": "traffic_signals"}},
        {"type": "node", "id": 12, "lat": 30.0, "lon": 90.0,  # outside bbox -> dropped
         "tags": {"highway": "traffic_signals"}},
        _el("node", 21, destination_point(ORIGIN, 0, 2000), amenity="police", name="PS"),
    ]  # fmt: skip
    respx.post(osm.OVERPASS_ENDPOINTS[0]).mock(
        return_value=httpx.Response(200, json=_payload(elements))
    )
    cache = tmp_path / "cache"
    written = osm.fetch_cities(["kolkata"], tmp_path, cache_dir=cache)
    data = __import__("json").loads(written["kolkata"].read_text("utf-8"))
    assert data["timezone"] == "Asia/Kolkata"
    assert data["meta"]["licence"] == "ODbL - (c) OpenStreetMap contributors"
    assert data["meta"]["counts"]["signals"] == 1
    assert data["meta"]["counts"]["stations"]["POLICE"] == 1
    assert data["meta"]["queries"] == osm.build_queries(osm.CITY_PROFILES["kolkata"]["bbox"])
    assert len(data["traffic"]["hourly_congestion"]) == 24
    assert [s["id"] for s in data["signals"]] == ["J11"]
    # the cache makes a re-run work offline
    respx.reset()
    respx.post(osm.OVERPASS_ENDPOINTS[0]).mock(return_value=httpx.Response(502))
    again = osm.fetch_cities(["kolkata"], tmp_path / "again", cache_dir=cache)
    assert again["kolkata"].is_file()
