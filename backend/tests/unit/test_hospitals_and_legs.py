"""Hospital selection (design 1.1), leg messages, re-route arithmetic and the planner's
pure helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from divert.config import Settings
from divert.domain.enums import DestinationKind, LegKind, ServiceType
from divert.domain.geo import LatLng, destination_point, haversine_m
from divert.domain.hospitals import (
    HospitalOption,
    choose_hospital,
    ed_tolerance_s,
    is_eligible,
)
from divert.domain.models import Station
from divert.services.legs import (
    AT_HOME_M,
    LegPlan,
    PendingLeg,
    destination_json,
    is_behind_plan,
    is_home,
    leg_planned_command,
    reroute_origin_m,
    splice,
)
from divert.services.planner import LegPlanner, PlanRequest, backoff_s
from divert.services.routing import SAME_POINT, RoutingError
from tests.support import city, corridor_at, route_through, straight_route

C = city("delhi").center


def hospital(
    sid: str,
    *,
    emergency: str | None = None,
    healthcare: str | None = None,
    name: str = "H",
    speciality: str | None = None,
) -> Station:
    return Station(
        sid, "delhi", ServiceType.AMBULANCE, name, C, f"node/{abs(hash(sid)) % 10**6}",
        emergency=emergency, healthcare=healthcare, speciality=speciality,
    )  # fmt: skip


def opt(sid: str, cost: float | None, **kw: str | None) -> HospitalOption:
    return HospitalOption(hospital(sid, **kw), 100.0, cost_s=cost, eta_s=cost)


# ---------------------------------------------------------------------- eligibility


@pytest.mark.parametrize(
    ("emergency", "healthcare", "eligible"),
    [
        ("yes", "hospital", True),
        (None, None, True),
        (None, "clinic", True),
        (None, "centre", True),
        ("no", "hospital", False),
        (None, "dentist", False),
        (None, "blood_bank", False),
        (None, "alternative", False),
        (None, "doctor", False),
        (None, "birthing_centre", False),
    ],
)
def test_eligibility_excludes_no_emergency_and_non_acute(
    emergency: str | None, healthcare: str | None, eligible: bool
) -> None:
    assert is_eligible(hospital("H", emergency=emergency, healthcare=healthcare)) is eligible


def test_unnamed_hospital_is_not_eligible() -> None:
    assert not is_eligible(hospital("H", name=""))


# real healthcare:speciality values and names from the cached Overpass payloads (B6)
@pytest.mark.parametrize(
    ("speciality", "eligible"),
    [
        ("ophthalmology", False),
        ("ophthalmology;optometry", False),
        ("Neuro Ophthalmology; Pediatric Ophthalmology ; Ophthalmology", False),
        ("Eye Care; Dental Care", False),
        ("dental_oral_maxillo_facial_surgery", False),
        ("dentistry;prosthodontist;pediatric_dentist", False),
        ("Cataract", False),
        ("dermatology", False),
        ("fertility", False),
        ("diagnostic_radiology;radiology;pathology;clinical_pathology", False),
        ("ayurveda", False),
        ("physiotherapy", False),
        ("physiotherapy;emergency", True),  # one acute speciality keeps it eligible
        ("general;dentistry;gynaecology;orthopaedics", True),
        ("otolaryngology;maternity", True),
        ("multi speciality", True),
        ("cardiology", True),
        ("trauma", True),
    ],
)
def test_specialities_that_cannot_take_an_emergency_patient_are_skipped(
    speciality: str, eligible: bool
) -> None:
    assert is_eligible(hospital("H", name="X Hospital", speciality=speciality)) is eligible
    # ... unless the hospital is a confirmed emergency department
    assert is_eligible(hospital("H", name="X", speciality=speciality, emergency="yes"))


@pytest.mark.parametrize(
    ("name", "eligible"),
    [
        ("Netaji Eye Hospital", False),
        ("Aurobindo Netralaya", False),
        ("Netradama Eye Hospital", False),
        ("Maulana Azad Institute of Dental Sciences", False),
        ("Dr Rafi Ahmed Dental College & Hospital", False),
        ("Dayal Eye & Maternity Centre", False),
        ("Government Ayurvedic Hospital", False),
        ("Unani Hospital", False),
        # after the word "hospital" the keyword names an extra unit of a general hospital
        ("Leela Hospital and Diagnostic Center", True),
        ("Sheetla Hospital And Eye Institute Private Ltd", True),
        ("Sunand Hospital & IVF Centre", True),
        # general care named too
        ("NRS Medical & Dental Specialists Centre", True),
        ("Delhi Eye Multi Speciality Centre", True),
        # no keyword ("Netaji" is not "netra"; "Ardent" is not "dent")
        ("Netaji Subhas Hospital", True),
        ("Ardent Ganpati Hospital", True),
        ("Government Hospital", True),
    ],
)
def test_untagged_hospitals_fall_back_to_name_keywords(name: str, eligible: bool) -> None:
    assert is_eligible(hospital("H", name=name)) is eligible
    assert is_eligible(hospital("H", name=name, emergency="yes"))


def test_a_speciality_tag_takes_precedence_over_the_name() -> None:
    assert is_eligible(hospital("H", name="Nangia Hospital Eye", speciality="general"))
    assert not is_eligible(hospital("H", name="City Hospital", speciality="ophthalmology"))


# ------------------------------------------------------------------------- choice


def test_confirmed_ed_preferred_within_tolerance() -> None:
    choice = choose_hospital([opt("A", 600.0), opt("B", 740.0, emergency="yes")])
    assert ed_tolerance_s(600.0) == 750.0  # 1.25 x best
    assert choice.chosen.station.id == "B"
    assert "confirmed emergency department" in choice.reason


def test_ed_beyond_tolerance_loses_to_the_fastest() -> None:
    choice = choose_hospital([opt("A", 600.0), opt("B", 760.0, emergency="yes")])
    assert choice.chosen.station.id == "A"
    assert "above the" in choice.reason


def test_short_trips_use_the_180_s_floor() -> None:
    # best 60 s: 1.25 x 60 = 75 s, but an ED within 180 s is still preferred
    assert (
        choose_hospital([opt("A", 60.0), opt("B", 175.0, emergency="yes")]).chosen.station.id == "B"
    )
    assert (
        choose_hospital([opt("A", 60.0), opt("B", 185.0, emergency="yes")]).chosen.station.id == "A"
    )


def test_no_ed_takes_the_fastest_and_unroutable_options_are_skipped() -> None:
    choice = choose_hospital([opt("A", None), opt("B", 300.0), opt("C", 200.0)])
    assert choice.chosen.station.id == "C"
    doc = choice.to_json()
    assert doc["chosen"] == "C"
    assert [c["station_id"] for c in doc["candidates"]] == ["A", "B", "C"]
    assert doc["candidates"][0]["cost_s"] is None
    assert doc["ed_tolerance_s"] == 250.0
    with pytest.raises(ValueError, match="no hospital"):
        choose_hospital([opt("A", None)])


def test_ties_break_on_distance_then_id() -> None:
    a = HospitalOption(hospital("B"), 200.0, cost_s=300.0)
    b = HospitalOption(hospital("A"), 200.0, cost_s=300.0)
    c = HospitalOption(hospital("C"), 100.0, cost_s=300.0)
    assert choose_hospital([a, b]).chosen.station.id == "A"
    assert choose_hospital([a, b, c]).chosen.station.id == "C"


# ------------------------------------------------------------------- leg messages


def test_leg_planned_command_round_trips_into_a_leg_plan() -> None:
    route = straight_route(C, n=10)
    corridor = corridor_at(route, [3, 7])
    dest = route.destination
    cmd = leg_planned_command(
        city_id="delhi", dispatch_id="d-1", incident_id="i-1", vehicle_id="AMB-1", seq=1,
        kind=LegKind.TRANSPORT, route=route, corridor=corridor,
        destination=destination_json(DestinationKind.HOSPITAL, dest, "H1", "AIIMS"),
        predicted_eta_s=123.44, route_choice={"chosen": "A"},
        hospital={"id": "H1", "name": "AIIMS"}, planned_at=datetime(2026, 11, 20, tzinfo=UTC),
    )  # fmt: skip
    assert cmd["type"] == "leg_planned"
    assert cmd["command_id"] == "leg:d1:1"  # keyed by (dispatch_id, seq)
    assert cmd["emergency"] is True
    plan = LegPlan.from_command(cmd)
    assert (plan.seq, plan.kind, plan.emergency) == (1, LegKind.TRANSPORT, True)
    assert plan.dest_kind is DestinationKind.HOSPITAL
    assert plan.dest_station_id == "H1"
    assert plan.predicted_eta_s == 123.4
    assert [c.signal_id for c in plan.corridor] == ["S3", "S7"]
    assert plan.route.length_m == pytest.approx(route.length_m, abs=0.01)
    ret = LegPlan.from_command({**cmd, "kind": "RETURN", "emergency": None})
    assert ret.emergency is False


def test_pending_leg_json_round_trip() -> None:
    p = PendingLeg(2, LegKind.RETURN, C, requested_t=10.0, start_at=30.0, attempts=3, error="x")
    assert PendingLeg.from_json(p.to_json()) == p


def test_is_home_within_30_m() -> None:
    assert is_home(C, destination_point(C, 90.0, AT_HOME_M - 1.0))
    assert not is_home(C, destination_point(C, 90.0, AT_HOME_M + 1.0))


# Real data (stage 5 fix F1): Vimhans (station Hw168898336, OSM point) and the point public
# OSRM snaps it to - where the e2e TRANSPORT to Vimhans ended (dispatch_legs route end).
VIMHANS = LatLng(28.567979, 77.251595)
VIMHANS_ROAD = LatLng(28.567703, 77.251405)


def test_a_station_mapped_off_the_road_is_home_at_its_road_point() -> None:
    assert haversine_m(VIMHANS, VIMHANS_ROAD) > AT_HOME_M  # 35.8 m: the OSM point alone fails
    assert not is_home(VIMHANS_ROAD, VIMHANS)
    assert is_home(VIMHANS_ROAD, VIMHANS, VIMHANS_ROAD)
    assert is_home(destination_point(VIMHANS_ROAD, 120.0, AT_HOME_M - 1.0), VIMHANS, VIMHANS_ROAD)
    far = destination_point(VIMHANS_ROAD, 120.0, AT_HOME_M + 1.0)
    assert haversine_m(far, VIMHANS) > AT_HOME_M
    assert not is_home(far, VIMHANS, VIMHANS_ROAD)


# ----------------------------------------------------------------------- re-route


def test_behind_plan_needs_both_the_share_and_the_seconds() -> None:
    # 25 % and 60 s: a 600 s plan needs > 150 s late; a 100 s plan needs > 60 s late
    assert not is_behind_plan(400.0, 340.0, 600.0, share=0.25, min_s=60.0)  # 140 s late
    assert is_behind_plan(400.0, 360.0, 600.0, share=0.25, min_s=60.0)  # 160 s late
    assert not is_behind_plan(100.0, 55.0, 100.0, share=0.25, min_s=60.0)  # 55 s late
    assert is_behind_plan(100.0, 65.0, 100.0, share=0.25, min_s=60.0)  # 65 s late


def test_reroute_origin_is_ahead_and_needs_room() -> None:
    assert reroute_origin_m(0.0, 2.0, 2000.0) == 100.0  # at least 100 m ahead
    assert reroute_origin_m(0.0, 15.0, 2000.0) == 225.0  # ~15 s ahead at 15 m/s
    assert reroute_origin_m(1450.0, 10.0, 2000.0) is None  # < 500 m would be left


def test_splice_keeps_the_driven_part_and_renumbers_passes() -> None:
    old = straight_route(C, bearing=90.0, n=20)  # 1000 m east
    old_cor = corridor_at(old, [4, 16], prefix="J")  # J4 at 200 m (kept), J16 dropped
    at = old.cumulative[8] + 10.0  # 410 m
    origin = destination_point(old.points[8], 90.0, 10.0)
    new = route_through(
        [origin, destination_point(origin, 0.0, 300.0), destination_point(origin, 45.0, 700.0)]
    )
    # the new route crosses J4 again (a second pass) and a new junction N
    j4 = corridor_at(new, [5], prefix="J")[0]
    j4 = type(j4)("J4", j4.pos, j4.along_m, j4.offset_m, j4.lane, j4.approach_bearing)
    new_cor = (j4, *corridor_at(new, [15], prefix="N"))
    route, cor = splice(old, old_cor, at, new, new_cor)
    assert route.points[0] == old.points[0]
    assert route.cumulative[8] == pytest.approx(old.cumulative[8])  # along_m stays valid
    assert route.length_m == pytest.approx(at + new.length_m, abs=0.5)
    assert haversine_m(route.destination, new.destination) < 0.01
    assert [(c.signal_id, c.pass_no) for c in cor] == [("J4", 0), ("J4", 1), ("N15", 0)]
    offset = route.length_m - new.length_m
    assert cor[1].along_m == pytest.approx(new_cor[0].along_m + offset)
    assert [c.key for c in cor] == ["J4", "J4#1", "N15"]


# ------------------------------------------------------------------------ planner


def test_backoff_doubles_from_2_s_to_60_s() -> None:
    assert [backoff_s(n, 2.0, 60.0) for n in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]


def test_plan_request_parses_the_engine_message() -> None:
    req = PlanRequest.from_message(
        {
            "type": "plan_request", "city": "delhi", "dispatch_id": "d", "incident_id": "i",
            "vehicle_id": "AMB-1", "service": "AMBULANCE", "station_id": "H1", "seq": 1,
            "kind": "TRANSPORT", "origin": [28.6, 77.2], "home": [28.5, 77.1], "start_at": 5.0,
        }
    )  # fmt: skip
    assert req.key == ("d", 1)
    assert req.kind is LegKind.TRANSPORT
    assert req.start_at == 5.0


class _View:
    """The planner's window on the engine: records what the planner tells it."""

    def __init__(self) -> None:
        self.failed: list[str] = []
        self.not_needed: list[tuple[str, int]] = []
        self.waiting = True

    def wants(self, dispatch_id: str, seq: int) -> bool:
        return self.waiting

    def plan_failed(self, dispatch_id: str, seq: int, error: str) -> bool:
        self.failed.append(error)
        self.waiting = len(self.failed) < 3  # stop the retry loop after three failures
        return True

    def leg_not_needed(self, dispatch_id: str, seq: int) -> bool:
        self.not_needed.append((dispatch_id, seq))
        self.waiting = False
        return True


def _planner(view: _View, error: Exception) -> LegPlanner:
    async def no_sleep(_: float) -> None:
        return None

    planner = LegPlanner(
        settings=Settings(),
        sessionmaker=cast(Any, None),
        redis=cast(Any, None),
        router=cast(Any, None),
        view=cast(Any, view),
        outbox=cast(Any, object()),
        sleep=no_sleep,
    )

    async def plan(_: PlanRequest) -> dict[str, Any]:
        raise error

    planner.plan = plan  # type: ignore[method-assign]
    return planner


def _request(kind: LegKind) -> PlanRequest:
    return PlanRequest(
        "delhi", "d", "i", "AMB-1", ServiceType.AMBULANCE, "Hw168898336", 2, kind,
        origin=VIMHANS_ROAD, home=VIMHANS, start_at=0.0,
    )  # fmt: skip


async def test_a_return_that_snaps_to_the_station_is_not_needed_and_never_retried() -> None:
    view = _View()
    same = RoutingError("OSRM: origin and destination snap to the same road point", kind=SAME_POINT)
    await _planner(view, same)._plan_with_retries(_request(LegKind.RETURN))
    assert view.not_needed == [("d", 2)]
    assert view.failed == []  # no leg_plan_failed: nothing failed, the unit is home


async def test_other_planning_errors_still_retry() -> None:
    view = _View()
    same = RoutingError("OSRM: origin and destination snap to the same road point", kind=SAME_POINT)
    await _planner(view, same)._plan_with_retries(_request(LegKind.TRANSPORT))
    assert view.not_needed == []  # a TRANSPORT is never "home"
    assert len(view.failed) == 3
    view = _View()
    down = RoutingError("OSRM unavailable", kind="unavailable")
    await _planner(view, down)._plan_with_retries(_request(LegKind.RETURN))
    assert view.not_needed == []
    assert len(view.failed) == 3
    assert all(f.startswith("routing:") for f in view.failed)
