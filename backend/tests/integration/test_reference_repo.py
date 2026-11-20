"""Reference repository on real PostGIS + the committed OSM data.

PostGIS answers (geodesic, spheroid) are cross-checked against DIVERT's own
haversine math over every row, with tolerances far above the < 0.5 % sphere/spheroid
difference and far below anything that would change a dispatch decision.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.cities.loader import load_snapshot
from divert.config import Settings
from divert.db.models import Unit as UnitRow
from divert.db.repositories.reference import ReferenceRepository
from divert.domain.enums import ServiceType, UnitStatus
from divert.domain.geo import LatLng, cumulative_distances, haversine_m, project_onto_polyline
from divert.domain.hospitals import is_eligible
from divert.domain.models import Signal

pytestmark = pytest.mark.integration


@pytest.fixture(params=["delhi", "kolkata", "bengaluru"])
def city_id(request: pytest.FixtureRequest, test_settings: Settings) -> str:
    if request.param not in test_settings.cities:
        pytest.skip(f"{request.param} not configured")
    return str(request.param)


async def test_city_reference_roundtrip(session: AsyncSession, city_id: str) -> None:
    repo = ReferenceRepository(session)
    ref = await repo.load_city_reference(city_id)
    assert ref is not None
    snap = load_snapshot(city_id)
    assert ref.city.bbox == snap.city.bbox
    assert ref.city.traffic == snap.city.traffic
    assert haversine_m(ref.city.center, snap.city.center) < 0.01
    assert len(ref.stations) == len(snap.stations)
    assert len(ref.signals) == len(snap.signals)
    by_id = {s.id: s for s in snap.signals}
    for sig in ref.signals:  # coordinates survive the geography round trip (< 1 cm)
        assert haversine_m(sig.pos, by_id[sig.id].pos) < 0.01
        assert sig.osm_node_ids == by_id[sig.id].osm_node_ids
    assert await repo.get_city("atlantis") is None
    assert city_id in [c.id for c in await repo.list_cities()]


@pytest.mark.parametrize("service", list(ServiceType))
async def test_knn_matches_brute_force(
    session: AsyncSession, city_id: str, service: ServiceType
) -> None:
    repo = ReferenceRepository(session)
    snap = load_snapshot(city_id)
    incident = snap.city.center
    got = await repo.nearest_stations_with_free_units(city_id, service, incident, limit=3)
    brute = sorted(
        (haversine_m(incident, s.pos), s.id) for s in snap.stations if s.service is service
    )
    assert len(got) == min(3, len(brute))
    dists = [c.distance_m for c in got]
    assert dists == sorted(dists)
    for cand, (bd, _) in zip(got, brute, strict=False):
        assert cand.station.service is service
        assert cand.free_units == 2
        # spheroid (PostGIS) vs sphere (haversine): within 0.5 %
        assert cand.distance_m == pytest.approx(haversine_m(incident, cand.station.pos), rel=5e-3)
        assert cand.distance_m <= bd * 1.005 + 1.0
    # every returned station is among the true top-3 unless it is a near tie
    kth = brute[min(3, len(brute)) - 1][0]
    assert all(c.distance_m <= kth * 1.005 + 1.0 for c in got)


async def test_nearest_hospitals_skip_non_emergency_specialities(
    session: AsyncSession, city_id: str
) -> None:
    """Stage 5 fix-2 B6: the seeded speciality reaches the TRANSPORT filter; the KNN
    over-fetch still returns the 6 nearest ELIGIBLE hospitals (brute force over the
    snapshot), at the densest eye-hospital cluster as well as at the city centre."""
    repo = ReferenceRepository(session)
    snap = load_snapshot(city_id)
    eligible = [s for s in snap.stations if s.service is ServiceType.AMBULANCE and is_eligible(s)]
    restricted = [
        s for s in snap.stations
        if s.service is ServiceType.AMBULANCE and s.name and s.speciality
        and not is_eligible(s)
    ]  # fmt: skip
    assert restricted, "every city has tagged eye / dental hospitals"
    seeded = await repo.get_station(restricted[0].id)
    assert seeded is not None
    assert seeded.speciality == restricted[0].speciality
    for scene in (snap.city.center, restricted[0].pos):
        got = await repo.nearest_hospitals(city_id, scene, limit=6)
        brute = sorted(eligible, key=lambda s: (haversine_m(scene, s.pos), s.id))[:6]
        assert len(got) == 6
        assert all(is_eligible(c.station) for c in got)
        assert restricted[0].id not in {c.station.id for c in got}
        kth = haversine_m(scene, brute[-1].pos)
        assert all(c.distance_m <= kth * 1.005 + 1.0 for c in got)


async def test_knn_skips_stations_without_free_units(session: AsyncSession, city_id: str) -> None:
    repo = ReferenceRepository(session)
    center = load_snapshot(city_id).city.center
    first = await repo.nearest_stations_with_free_units(city_id, ServiceType.POLICE, center, 2)
    nearest = first[0].station.id
    await session.execute(
        sa.update(UnitRow)
        .where(UnitRow.station_id == nearest)
        .values(status=UnitStatus.OUT_OF_SERVICE)
    )
    again = await repo.nearest_stations_with_free_units(city_id, ServiceType.POLICE, center, 2)
    assert nearest not in {c.station.id for c in again}
    assert again[0].station.id == first[1].station.id
    avail = {a.station.id: a for a in await repo.station_availability(city_id)}
    assert (avail[nearest].total_units, avail[nearest].free_units) == (2, 0)


def _route_through_real_signals(signals: list[Signal]) -> list[LatLng]:
    """A test polyline visiting three real junctions that are 200 m - 3 km apart."""
    for s0 in signals:
        n1 = sorted((haversine_m(s0.pos, s.pos), s.id, s) for s in signals if s.id != s0.id)
        n1 = [t for t in n1 if 200 <= t[0] <= 3000]
        if not n1:
            continue
        s1 = n1[0][2]
        n2 = sorted(
            (haversine_m(s1.pos, s.pos), s.id, s)
            for s in signals
            if s.id not in {s0.id, s1.id}
            and haversine_m(s0.pos, s.pos) > haversine_m(s0.pos, s1.pos)
        )
        n2 = [t for t in n2 if 200 <= t[0] <= 3000]
        if n2:
            return [s0.pos, s1.pos, n2[0][2].pos]
    raise AssertionError("no three junctions 200 m - 3 km apart in the city data")


@pytest.mark.parametrize("within_m", [30.0, 120.0])
async def test_corridor_matches_brute_force(
    session: AsyncSession, city_id: str, within_m: float
) -> None:
    repo = ReferenceRepository(session)
    signals = await repo.list_signals(city_id)
    route = _route_through_real_signals(signals)
    cum = cumulative_distances(route)
    got = await repo.corridor_candidates(city_id, route, within_m)
    got_ids = [c.signal.id for c in got]
    assert len(got_ids) == len(set(got_ids))

    proj = {s.id: project_onto_polyline(s.pos, route, cum) for s in signals}
    must = {sid for sid, p in proj.items() if p.offset_m <= within_m - 0.5}
    may = {sid for sid, p in proj.items() if p.offset_m <= within_m + 0.5}
    assert must <= set(got_ids) <= may
    for c in got:  # PostGIS offset agrees with our projection
        assert c.offset_m == pytest.approx(proj[c.signal.id].offset_m, rel=5e-3, abs=0.2)
    # the three junctions the route was built through are on it
    on_route = {s.id for s in signals if s.pos in route}
    assert on_route <= set(got_ids)
    # roughly ordered along the route (ST_LineLocatePoint) - consistent with haversine along_m
    along = [proj[sid].along_m for sid in got_ids]
    assert all(b >= a - 5.0 for a, b in itertools.pairwise(along))


async def test_skip_locked_never_hands_out_the_same_unit(
    sessionmaker: async_sessionmaker[AsyncSession], city_id: str
) -> None:
    async with sessionmaker() as probe:
        station_id = (await ReferenceRepository(probe).list_stations(city_id))[0].id

    holders: list[AsyncSession] = []
    try:
        for _ in range(3):
            s = sessionmaker()
            await s.begin()
            holders.append(s)
        a = await ReferenceRepository(holders[0]).lock_free_unit(station_id)
        b = await ReferenceRepository(holders[1]).lock_free_unit(station_id)
        c = await ReferenceRepository(holders[2]).lock_free_unit(station_id)
        assert a is not None
        assert b is not None
        assert a.id != b.id  # second transaction skipped the row the first one locked
        assert c is None  # both units of the station are locked -> no free unit, no wait
        await holders[0].rollback()  # releasing A's lock frees its unit again
        d = await ReferenceRepository(holders[2]).lock_free_unit(station_id)
        assert d is not None
        assert d.id == a.id
    finally:
        for s in holders:
            await s.rollback()
            await s.close()


async def test_concurrent_lockers_get_distinct_units(
    sessionmaker: async_sessionmaker[AsyncSession], city_id: str
) -> None:
    async with sessionmaker() as probe:
        station_id = (await ReferenceRepository(probe).list_stations(city_id))[-1].id
    start = asyncio.Event()

    async def grab() -> str | None:
        async with sessionmaker() as s:
            await s.begin()
            await start.wait()
            unit = await ReferenceRepository(s).lock_free_unit(station_id)
            await asyncio.sleep(0.2)  # hold the row lock while the others try
            await s.rollback()
            return None if unit is None else unit.id

    tasks = [asyncio.create_task(grab()) for _ in range(6)]
    await asyncio.sleep(0.05)
    start.set()
    results = await asyncio.gather(*tasks)
    won = [r for r in results if r is not None]
    assert len(won) == 2
    assert len(set(won)) == 2


async def test_spatial_queries_can_use_gist_indexes(session: AsyncSession, city_id: str) -> None:
    """With sequential scans disabled the planner must find an index path, proving the
    GiST indexes serve KNN (<->) and ST_DWithin on geography."""
    center = load_snapshot(city_id).city.center
    pt = f"SRID=4326;POINT({center.lng} {center.lat})"
    await session.execute(sa.text("SET LOCAL enable_seqscan = off"))
    knn = " | ".join(
        r[0]
        for r in await session.execute(
            sa.text(
                "EXPLAIN SELECT id FROM stations ORDER BY geom <-> ST_GeogFromText(:p) LIMIT 3"
            ),
            {"p": pt},
        )
    )
    assert "ix_stations_geom" in knn
    dwithin = " | ".join(
        r[0]
        for r in await session.execute(
            sa.text(
                "EXPLAIN SELECT id FROM signals WHERE ST_DWithin(geom, ST_GeogFromText(:p), 30)"
            ),
            {"p": pt},
        )
    )
    assert "ix_signals_geom" in dwithin
