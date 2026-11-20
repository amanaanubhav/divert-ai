"""Stage 5 fix-2 B5: a claim that lost the junction (CONFLICT) and was served later used
to be overwritten to PREEMPTED, so analytics showed 0 conflicts. The persisted row keeps
its final state AND how often it was queued; analytics counts contested claims, served
or not; the incident detail shows the final state plus ``contested`` / ``waited_s``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.db.repositories.history import HistoryWriter
from tests.integration.helpers import api, rows, signal_near_center

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


def _claim(base: dict[str, Any], signal_id: str, **over: Any) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        **base, "ts": now, "signal_id": signal_id, "claim_event_id": f"clm:{uuid.uuid4().hex}",
        "state": "PREEMPTED", "lane": "R", "approach_bearing": 90.0, "eta_at_claim_s": 30.0,
        "claimed_at": now, "movement": "STRAIGHT", "lane_index": 1, "lanes_total": 2,
        "required_lead_s": 31.0, "transition_s": 11.0, "queue_clear_s": 15.0, "pass_no": 0,
        "rank": 1, "free_left": False, "conflicts": 0, **over,
    }  # fmt: skip


def _ev(type_: str, claim: dict[str, Any], **over: Any) -> dict[str, Any]:
    eid = claim["claim_event_id"] if type_ == "preemption_claimed" else uuid.uuid4().hex
    return {"type": type_, "event_id": eid, **claim, **over}


async def test_a_contested_claim_stays_contested_once_served(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    where = signal_near_center("delhi")
    async with api(test_settings, redis) as (_, client):
        body = (
            await client.post(
                "/api/v1/cities/delhi/incidents",
                json={"type": "MEDICAL", "location": [where.lat, where.lng]},
            )
        ).json()
        iid, did = body["incident"]["id"], body["vehicle"]["dispatch_id"]
        sids = [
            r.id for r in await rows(
                sessionmaker, "SELECT id FROM signals WHERE city_id = 'delhi' ORDER BY id LIMIT 3"
            )
        ]  # fmt: skip
        base = {"city": "delhi", "incident_id": iid, "dispatch_id": did,
                "vehicle_id": body["vehicle"]["id"]}  # fmt: skip
        # (1) queued behind a police car, served after it, crossed after a 12.5 s wait
        won = _claim(base, sids[0], state="CONFLICT")
        # (2) never contested; (3) queued and released while still queued
        clean = _claim(base, sids[1])
        lost = _claim(base, sids[2], state="CONFLICT")
        events = [
            _ev("preemption_claimed", won),
            _ev("preemption_conflict", won, conflicts=1, winner="PCR-1"),
            _ev("preempt_green", won, state="PREEMPTED", conflicts=1),
            _ev(
                "preemption_released",
                won,
                state="PREEMPTED",
                conflicts=1,
                reason="passed",
                outcome="CLEARED",
                waited_s=12.5,
                crossed_at=won["ts"],
            ),
            _ev("preemption_claimed", clean),
            _ev("preempt_green", clean),
            _ev("preemption_released", clean, reason="passed", outcome="CLEARED", waited_s=0.0),
            _ev("preemption_claimed", lost),
            # an event written before this fix carries no count: still at least one
            {k: v for k, v in _ev("preemption_conflict", lost).items() if k != "conflicts"},
            _ev(
                "preemption_released",
                lost,
                conflicts=1,
                reason="cancelled",
                outcome="CANCELLED",
                waited_s=3.0,
            ),
        ]
        for ev in events:
            async with sessionmaker() as s, s.begin():
                assert await HistoryWriter(s).apply(ev), ev["type"]
        for ev in events:  # a redelivered stream changes nothing
            async with sessionmaker() as s, s.begin():
                assert not await HistoryWriter(s).apply(ev)

        got = {
            r.event_id: (r.state, r.conflicts, r.waited_s)
            for r in await rows(
                sessionmaker,
                "SELECT event_id, state, conflicts, waited_s FROM signal_preemptions "
                "WHERE dispatch_id = :d",
                d=did,
            )
        }
        assert got == {
            won["claim_event_id"]: ("PREEMPTED", 1, 12.5),
            clean["claim_event_id"]: ("PREEMPTED", 0, 0.0),
            lost["claim_event_id"]: ("CONFLICT", 1, 3.0),
        }
        detail = (await client.get(f"/api/v1/incidents/{iid}")).json()
        pre = {p["event_id"]: p for p in detail["preemptions"]}
        w = pre[won["claim_event_id"]]
        assert (w["state"], w["contested"], w["conflicts"], w["waited_s"]) == (
            "PREEMPTED", True, 1, 12.5,
        )  # fmt: skip
        assert (
            pre[clean["claim_event_id"]]["contested"],
            pre[clean["claim_event_id"]]["conflicts"],
        ) == (False, 0)
        assert pre[lost["claim_event_id"]]["state"] == "CONFLICT"
        assert pre[lost["claim_event_id"]]["contested"] is True

        summary = (await client.get("/api/v1/cities/delhi/analytics/summary")).json()
        p = summary["preemptions"]
        assert (p["total"], p["conflicts"], p["conflicts_served"]) == (3, 2, 1)
