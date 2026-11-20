"""Stage 5A wiring against real PostGIS + Redis (OSRM stubbed): crowd-aware route choice
recorded on the dispatch, active-incident zones from PostGIS, and the new pre-emption
fields kept in today's schema (event payloads) without breaking the persister."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.db.repositories.history import HistoryWriter
from divert.db.repositories.incidents import IncidentRepository
from divert.domain.enums import IncidentType
from divert.domain.geo import LatLng, destination_point, haversine_m, interpolate_by_distance
from divert.domain.route import Route
from divert.store import redis_store as keys
from tests.integration.helpers import StubRouter, api, rows, signal_near_center
from tests.support import route_through

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


class AltRouter(StubRouter):
    """OSRM stub with an alternative: the straight line and a detour 20 % to one side."""

    async def alternatives(self, origin: LatLng, destination: LatLng) -> list[Route]:
        main = await self.route(origin, destination)
        d = haversine_m(origin, destination)
        mid, _ = interpolate_by_distance([origin, destination], [0.0, d], d / 2)
        from divert.domain.geo import initial_bearing_deg  # noqa: PLC0415

        via = destination_point(mid, initial_bearing_deg(origin, destination) + 90.0, 0.2 * d)
        return [main, route_through([origin, via, destination], step_m=25.0, speed=12.0)]


async def test_dispatch_scores_alternatives_and_records_the_route_choice(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    where = signal_near_center("delhi")
    async with api(test_settings, redis, AltRouter()) as (_, client):
        r = await client.post(
            "/api/v1/cities/delhi/incidents",
            json={"type": "MEDICAL", "location": [where.lat, where.lng]},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        detail = (await client.get(f"/api/v1/incidents/{body['incident']['id']}")).json()
    choice = body["vehicle"]["route_choice"]
    assert choice["station_id"] == body["vehicle"]["station"]["id"]
    assert [c["label"] for c in choice["candidates"]] == ["A", "B"]
    assert choice["chosen"] == "A"  # the straight line is shorter and as crowded
    assert choice["reason"].startswith("Route A:")
    for cand in choice["candidates"]:
        assert cand["congested_time_s"] >= cand["base_time_s"] > 0
        assert 0.0 <= cand["crowd_index"] <= 1.0
    # the chosen route's ETA is the dispatch ETA
    chosen = next(c for c in choice["candidates"] if c["label"] == choice["chosen"])
    assert body["vehicle"]["eta_s"] == pytest.approx(chosen["congested_time_s"], abs=0.1)
    # corridor entries carry the movement and lane advice; `lane` stays L/R/C
    for c in body["vehicle"]["corridor"]:
        assert c["lane"] in {"L", "C", "R"}
        assert c["movement"]
        assert c["advice"]["text"]
    # persisted: the command (outbox payload) and the "dispatched" timeline event
    (cmd,) = [json.loads(f["data"]) for _, f in await redis.xrange(keys.STREAM_COMMANDS)]
    assert cmd["route_choice"] == choice
    assert (cmd["emergency"], cmd["leg_kind"]) == (True, "RESPONSE")
    (ev,) = await rows(
        sessionmaker,
        "SELECT payload FROM incident_events WHERE type = 'dispatched' AND incident_id = :i",
        i=body["incident"]["id"],
    )
    assert ev.payload["route_choice"] == choice
    assert detail["dispatches"][0]["route_choice"] == choice


async def test_active_incident_zones_come_from_postgis(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    where = signal_near_center("kolkata")
    async with api(test_settings, redis) as (_, client):
        fire = (
            await client.post(
                "/api/v1/cities/kolkata/incidents",
                json={"type": "FIRE", "location": [where.lat, where.lng]},
            )
        ).json()
        crime = (
            await client.post(
                "/api/v1/cities/kolkata/incidents",
                json={"type": "POLICE", "location": [where.lat, where.lng]},
            )
        ).json()
        await client.delete(f"/api/v1/cities/kolkata/incidents/{crime['incident']['id']}")
    async with sessionmaker() as s:
        zones = await IncidentRepository(s).active_incident_zones("kolkata")
        assert await IncidentRepository(s).active_incident_zones("delhi") == []
    assert [(z.incident_id, z.type) for z in zones] == [
        (fire["incident"]["id"], IncidentType.FIRE)
    ]  # the cancelled one is not a zone
    assert haversine_m(zones[0].center, where) < 0.5


async def test_preemption_section_4_6_fields_are_persisted(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    where = signal_near_center("bengaluru")
    async with api(test_settings, redis) as (_, client):
        body = (
            await client.post(
                "/api/v1/cities/bengaluru/incidents",
                json={"type": "MEDICAL", "location": [where.lat, where.lng]},
            )
        ).json()
    iid, did = body["incident"]["id"], body["vehicle"]["dispatch_id"]
    sid = (await rows(sessionmaker, "SELECT id FROM signals WHERE city_id='bengaluru' LIMIT 1"))[
        0
    ].id
    now = datetime.now(UTC).isoformat()
    claim: dict[str, Any] = {
        "ts": now, "city": "bengaluru", "incident_id": iid, "dispatch_id": did,
        "vehicle_id": body["vehicle"]["id"], "signal_id": sid, "claim_event_id": f"clm:{uuid.uuid4().hex}",
        "state": "PREEMPTED", "lane": "C", "approach_bearing": 90.0, "eta_at_claim_s": 30.0,
        "claimed_at": now, "movement": "STRAIGHT", "lane_index": 1, "lanes_total": 1,
        "required_lead_s": 31.0, "transition_s": 11.0, "queue_clear_s": 15.0,
        "phase_state_at_claim": "red_truncate", "free_left": False, "pass_no": 0, "rank": 1,
    }  # fmt: skip
    events = [
        {"type": "preemption_claimed", "event_id": claim["claim_event_id"], **claim},
        {"type": "preempt_transition", "event_id": f"ptr:{uuid.uuid4().hex}", **claim},
        {"type": "preempt_green", "event_id": f"pgr:{uuid.uuid4().hex}", **claim},
        {"type": "preempt_timeout", "event_id": f"pto:{uuid.uuid4().hex}", **claim, "hold_s": 61.0},
        {
            "type": "preemption_released", "event_id": f"rel:{uuid.uuid4().hex}", **claim,
            "reason": "timeout", "outcome": "TIMEOUT", "waited_s": 0.0, "crossed_at": None,
        },
    ]  # fmt: skip
    for ev in events:
        async with sessionmaker() as s, s.begin():
            assert await HistoryWriter(s).apply(ev)
    (row,) = await rows(
        sessionmaker, "SELECT * FROM signal_preemptions WHERE dispatch_id = :d", d=did
    )
    assert row.lane == "C"  # migration 0004: lane C is a valid value (no longer NULL)
    assert row.released_at is not None
    # every design section 4.6 column (migration 0004)
    assert (row.leg_seq, row.movement, row.lane_index, row.lanes_total, row.free_left) == (
        0, "STRAIGHT", 1, 1, False,
    )  # fmt: skip
    assert (row.required_lead_s, row.transition_s, row.queue_clear_s) == (31.0, 11.0, 15.0)
    assert (row.outcome, row.phase_state_at_claim, row.waited_s) == ("TIMEOUT", "red_truncate", 0.0)
    stored = await rows(
        sessionmaker,
        "SELECT type, payload FROM incident_events WHERE incident_id = :i ORDER BY id",
        i=iid,
    )
    by_type = {r.type: r.payload for r in stored}
    assert by_type["preemption_claimed"]["lane"] == "C"  # (and kept in the payload)
    assert by_type["preemption_claimed"]["required_lead_s"] == 31.0
    assert by_type["preemption_released"]["outcome"] == "TIMEOUT"
    assert {"preempt_transition", "preempt_green", "preempt_timeout"} <= set(by_type)
