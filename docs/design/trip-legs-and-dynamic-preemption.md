# Design: multi-leg trips, turn-aware dynamic pre-emption, crowd-aware routing

Status: approved for implementation (2026-11-17). This is the contract for the implementation; where code and this
document disagree after implementation, update this document in the same change.

**Why.** Testing showed three gaps:
- An ambulance stops at the scene. It should pick up the patient, take them to the nearest suitable hospital, and then return to base.
- Lane advice is a two-value rule: left turn → `L`, everything else → `R`.
- Signals are claimed at a fixed 45 s / 600 m threshold, whatever the signal is doing.

This design makes trips multi-leg, makes pre-emption depend on the turn the vehicle will take and on the signal's
current state, and picks the best, least crowded route among real alternatives.

**Verified inputs (2026-11-17)**:
- **Alternatives:** public OSRM returns 2 routes per request with `alternatives=3` (Delhi, Kolkata and Bengaluru all tested).
- **No lane data:** OSRM returns no `lanes` data for these cities (0 of 311 intersections), so lane counts and turn lanes must be estimated.
- **Maneuver types seen:** turn, new name, end of road, fork, roundabout, rotary and exit roundabout/rotary, with modifiers straight, slight, plain or sharp left/right, and uturn.
- **Hospitals:** OSM has 2,246 named hospitals. `emergency=yes` on 179, `emergency=no` on 45, unknown on the rest. A few are `healthcare=dentist|blood_bank|clinic|doctor|alternative|birthing_centre`.

---

## 1. Trip lifecycle: legs

A dispatch is a sequence of **legs**.

| Leg | From → to | Emergency (pre-empts signals)? | Who |
|---|---|---|---|
| `RESPONSE` | home station → scene | yes | every unit |
| `TRANSPORT` | scene → chosen hospital | yes (patient on board, highest priority) | AMBULANCE on MEDICAL incidents |
| `RETURN` | scene or hospital or current position → home station | **no**: obeys signals, normal traffic | every unit |

### Status machines

**Unit status:** `AVAILABLE → DISPATCHED → ON_SCENE → [TRANSPORTING → AT_HOSPITAL] → RETURNING → AVAILABLE`, plus `OUT_OF_SERVICE`.
- The bracketed part applies to ambulances only.
- A RETURNING unit is not dispatchable in this version. That is a known limit; v2 adds dispatch from moving units.

**Incident status:** `DISPATCHED → EN_ROUTE → ON_SCENE → [TRANSPORTING → AT_HOSPITAL] → RESOLVED`, plus `CANCELLED` and `FAILED`.
- MEDICAL resolves when hospital handover completes.
- FIRE and CRIME resolve when the scene is cleared.
- The RETURN leg belongs to the dispatch and unit, not the incident's response time. The dispatch row stays open until the unit is AVAILABLE again.

**Dwell times** are configurable. The defaults are realistic assumptions, labelled as such; testers use `DIVERT_SIM_TIME_SCALE`.

| Setting | Default | Meaning |
|---|---|---|
| `DIVERT_SCENE_TIME_AMBULANCE_S` | 480 | on-scene care and loading |
| `DIVERT_SCENE_TIME_FIRE_ENGINE_S` | 1200 | firefighting / scene work |
| `DIVERT_SCENE_TIME_POLICE_S` | 600 | scene work |
| `DIVERT_HOSPITAL_HANDOVER_S` | 600 | patient handover at the emergency department |

The existing `DIVERT_SCENE_TIME_S` is replaced; keep it as a deprecated fallback alias only if trivial.

### Cancel semantics

| Phase when cancelled | Result |
|---|---|
| RESPONSE (en route) | Release all claims. Plan a RETURN from the **current position**. Incident becomes CANCELLED. |
| ON_SCENE | Same as RESPONSE: RETURN from the scene, incident CANCELLED. |
| TRANSPORT / AT_HOSPITAL | **409 `cannot_cancel_transport`**: a patient is on board. |
| RETURNING or later | Idempotent 200; nothing changes. The incident is already RESOLVED or CANCELLED. |

### Leg planning (where routes come from)

- **RESPONSE** is planned by the API at dispatch, as today, but with crowd-aware alternatives (§2).
- **TRANSPORT and RETURN** are planned **when the previous leg ends**, so their routes use the congestion at the time they start.
  - Planning runs in a `LegPlanner` asyncio task inside the **leader worker**, so the simulation engine stays pure.
  - The engine emits a `plan_request` (dispatch_id, next leg kind, origin, time) in its tick batch.
  - The planner then does three things:
    1. Choose the hospital (§1.1) and route candidates (§2).
    2. Compute corridor signals and movements (§3).
    3. Write the leg to PostgreSQL **in one transaction with an outbox row**, then XADD a `leg_planned` command.
  - The leader applies `leg_planned` idempotently, keyed by (dispatch_id, seq).
  - This is the same transactional-outbox pattern as dispatch, so a planned leg is never lost and never applied twice.
- **Dwell overlaps planning.** The vehicle dwells (scene/handover time) while planning runs. If a plan is not ready when the dwell ends, the vehicle waits in place and the UI shows "planning route".
- **OSRM failure:** retry with exponential backoff (2 s doubling to 60 s max, unlimited). Emit `leg_plan_failed` events and a metric. Never teleport, and never fall back to a straight line.
- **Failover:** a new leader re-issues `plan_request` for any vehicle that is waiting for a plan. It also rebuilds legs from `dispatch_legs`.

### 1.1 Hospital selection (TRANSPORT)

1. **Eligible:** AMBULANCE stations, i.e. OSM `amenity=hospital` that are named and active, **excluding** `emergency=no` and `healthcare ∈ {dentist, blood_bank, alternative, doctor, birthing_centre}`.
2. **Candidates:** PostGIS KNN, the 6 nearest eligible hospitals to the scene.
3. **Route** all candidates concurrently with crowd-aware scoring (§2), then take the best cost per hospital.
4. **Prefer a confirmed ED:** choose the best `emergency=yes` hospital if its cost is within `max(180 s, 1.25 × best cost)`. Otherwise choose the best overall.
5. **Record** the choice and the alternatives considered (`hospital_choice` JSON) for explainability.
6. **Data:** the snapshot gains `emergency` (`yes|no|null`) and `healthcare` for hospitals.
   - Extract them from the cached Overpass payloads in `.work/osm-cache`; that needs no network.
   - The cache payloads include all tags; the snapshot is regenerated by the existing fetch code from cache.
   - The seed upserts the new station columns.

---

## 2. Route choice: best and least crowded

**Candidates** for every leg:
- OSRM `alternatives=3`. Today's client sends `alternatives=false`, so this changes.
- If fewer than 2 routes come back and the trip is longer than 1.5 km, add up to 2 **via-point detours**: via = the midpoint offset perpendicular by ±20% of the straight distance, snapped by OSRM.
- Drop any candidate longer than 1.6 × the shortest.
- Set by `DIVERT_ROUTE_ALTERNATIVES` (default 3) and `DIVERT_ROUTE_DETOURS` (default 2).

**Cost** = predicted travel time under a **TrafficModel** (protocol), plus junction delays, plus penalties.

**Road class** comes from the OSRM free-flow speed annotation, which reflects the OSM highway class:

| Class | Free-flow speed | Lanes per direction (estimate) |
|---|---|---|
| EXPRESSWAY | ≥ 75 km/h | 3 |
| ARTERIAL | 50–75 km/h | 3 if ≥ 60 km/h, else 2 |
| COLLECTOR | 32–50 km/h | 2 |
| LOCAL | < 32 km/h | 1 |

**Congestion factor** = the city hourly factor (existing table) applied **by class**:
- The peak drop applies fully to ARTERIAL and EXPRESSWAY, 70% to COLLECTOR and 50% to LOCAL.
- Formula: `f_class = 1 − sensitivity × (1 − f_city)`.
- This is an assumption, documented, and the hook where a live feed plugs in.

**Live crowd from DIVERT's own state:**
- **Active incident zones:**

  | Incident type | Radius | Speed factor |
  |---|---|---|
  | FIRE | 250 m | × 0.3 (appliances block lanes) |
  | CRIME | 150 m | × 0.6 |
  | MEDICAL | 50 m | × 0.85 |

  Exempt the vehicle's own destination zone.
- **Corridor contention:** for each corridor junction that another active emergency vehicle is expected to cross within ±20 s, add the expected conflict delay (`tie window + transition` ≈ 10 s).

**Junction delay** per corridor junction:
- **Emergency leg:** STRAIGHT 0 s, SLIGHT 1 s, LEFT 3 s (0 if FREE_LEFT), RIGHT 6 s, SHARP 8 s, U_TURN 12 s, ROUNDABOUT 4 + 2 × exit s.
- **Non-emergency leg:** the expected signal wait, `red_share × red_s / 2` from the phase plan (§4.1), plus the same turn delay.

**Pick and record:**
- Pick the minimum cost.
- Within 5% of each other, prefer fewer signalised right/U turns, then fewer contention junctions, then the shorter distance.
- Record every candidate in `route_choice` JSON on the leg: distance, base time, congested time, junction delay, penalties, crowd index and reason.
  - Crowd index = length-weighted mean of (1 − speed factor), 0 meaning free and 1 meaning jammed.
- The UI shows the reason, e.g. "Route B: 1.8 min faster than A (A: Ring Road congested, 2 extra right turns)".

**ETA:**
- The chosen route's ETA uses the same speed model as the simulation, including class-aware congestion and incident zones.
- The ±10% predicted-vs-actual guarantee must still hold, with tests.

**Dynamic re-routing**, evaluated by the LegPlanner for emergency legs only:
- **Triggers:**
  - (a) A new FIRE or CRIME zone intersects the remaining path.
  - (b) The vehicle has run more than 25% and more than 60 s behind plan.
- **Action:** route from the current position, and switch only if the new route saves ≥ 60 s (hysteresis).
- **Cap:** at most 2 re-routes per leg.
- **Applying it:** the switch arrives as a `route_replaced` command keyed by (dispatch_id, seq, reroute_no). The vehicle continues from its current position. Claims not on the new path are released, and the next tick re-evaluates the remaining ones.

---

## 3. Movement and lane advice at each junction

For every corridor junction on a leg, the planner computes a **Movement**.

### Turn classification

**Bearings:**
- The approach bearing is taken over the 30 m of route before the junction's along-position, and the exit bearing over the 30 m after.
- If an OSRM intersection lies within 15 m of the junction, prefer its `bearings[in]` (reversed) and `bearings[out]`. That intersection's bearing count is the junction's **leg count**.

**Turn angle** `δ = normalise(exit − approach)` in (−180°, 180°]. Negative means a left turn; bearings run clockwise.

| \|δ\| | Movement |
|---|---|
| ≤ 20° | STRAIGHT |
| 20–45° | SLIGHT_LEFT / SLIGHT_RIGHT |
| 45–135° | LEFT / RIGHT |
| 135–170° | SHARP_LEFT / SHARP_RIGHT |
| ≥ 170° | U_TURN |

**Special cases:**
- **Roundabout/rotary:** if the OSRM maneuver is `roundabout|rotary`, the movement is ROUNDABOUT, with `exit` from OSRM and the exit side from δ. India drives on the left, so circulation is clockwise.
- **OSRM disagreement:** OSRM modifiers corroborate the classification. If OSRM disagrees with the geometry by more than one class, geometry wins and a debug log is written.

**Free left:**
- Indian junctions often have a slip road that bypasses the signal. A LEFT/SLIGHT_LEFT movement is FREE_LEFT if both:
  - the route's closest approach to the junction centre (`offset_m`) is > 12 m, and
  - the bearing change completes ≥ 10 m before the junction's along-position.
- FREE_LEFT junctions are **not claimed**: no pre-emption, no disruption to cross traffic. They are shown as "free left".

### Lane model

- Lanes per direction come from the road class of the approach segment (§2). They are numbered from the **kerb**: lane 1 is the leftmost, because India drives on the left.

| Movement | Target lane | Note |
|---|---|---|
| LEFT / SLIGHT_LEFT / SHARP_LEFT / FREE_LEFT | 1 (kerb) | |
| STRAIGHT (emergency leg) | n (rightmost through lane) | Traffic yields to the left. On a 1-lane road: "centre, traffic pulls left". |
| STRAIGHT (RETURN leg) | middle lane `ceil(n/2)` | |
| RIGHT / SHARP_RIGHT / U_TURN | n (median side) | Needs the approach's protected phase |
| SLIGHT_RIGHT | n | |
| ROUNDABOUT, exit 1 | 1 | |
| ROUNDABOUT, exit ≥ 2 | n (inner), move out after the second-to-last exit | |

**Lane-change planning:**
- Moving k lanes needs `k × max(30 m, v × 3 s + v × 2 s)`.
- The advice says where to be in the target lane (`advise_at_m`): `junction_along − max(required, 150 m)`, clipped to the previous junction.
- **Look-ahead:** if the next junction's required lane-change distance does not fit between junctions, junction 1's lane is chosen to suit junction 2. Example: straight at J1 then left 80 m later at J2 → lane 1 through J1.

**Output** (`LaneAdvice`): `movement`, `lane_index`, `lanes_total`, `side` (`L|C|R`, kept for backward compatibility; C is new), `advise_at_m`, `free_left`, and `text`, e.g. "Right lane (3 of 3): protected right turn".

---

## 4. Signal control and dynamic pre-emption

### 4.1 Signal phase model (synthetic and deterministic, documented as such)

Real Indian signal timing plans are not available. Each junction runs a **fixed-time split-phase plan**:
- **Phases:** one phase per approach. Each approach gets its own green for all of its movements (split phasing, common in Indian cities), so every movement, including right turns, is protected.
- **Approaches** come from the junction's leg count, learned from OSRM intersections and cached in Redis per junction. The default is 4.
- **Cycle length:** 3 legs 90 s, 4 legs 120 s, 5+ legs 150 s. Each cycle is split equally after amber (3 s) and all-red (2 s) per phase.
- **Minimum green:** 10 s, for pedestrian safety.
- **Offset:** a deterministic per-junction offset (a stable hash of the junction id modulo the cycle), so `phase_at(junction, t)` is a pure function and needs no stored state.
- **Approach matching:** a vehicle's approach maps to the phase whose bearing is nearest its approach bearing. With unknown geometry, phases are spaced evenly from the first observed approach.

### 4.2 When to claim (dynamic lead)

A vehicle on an emergency leg claims junction J when its ETA to J ≤ the **required lead**:

```
required_lead = T_transition + T_queue + margin(5 s)
```

**T_transition** is the time until the vehicle's phase can be green:
- 0 if its phase is green now. This is **green extension**: hold it.
- Otherwise: the remaining minimum green of the current phase, plus amber 3 s, plus all-red 2 s. This is **early green / red truncation**.

**T_queue** is the time to discharge the queue in the vehicle's approach lane(s):
- `queue_veh = arrival_rate × red_elapsed`
- `T_queue = queue_veh × 2.0 s / lanes`, using a saturation headway of 2.0 s per vehicle per lane.
- `arrival_rate` comes from class and congestion: ARTERIAL 0.20, COLLECTOR 0.12, LOCAL 0.05 vehicles per second per lane, × `(2 − f_class)`.
- All of these are assumptions, documented.

**Bounds**, which replace the fixed 45 s / 600 m rule:
- Always claim once within `max(stopping_distance(v) + 50 m, 150 m)`, where stopping distance = v² / (2 × 3.0 m/s²).
- Never claim earlier than `DIVERT_PREEMPT_MAX_LEAD_S` (default 90 s).
- These replace `DIVERT_PREEMPT_LEAD_S` and `DIVERT_PREEMPT_DISTANCE_M` (remove both, or keep them as deprecated aliases for the bounds).

**Re-evaluation:** the required lead is recomputed every tick (the ETA changes). A claim, once made, is kept until release.

**Other cases:**
- **Multiple passes:** the claim key is `(vehicle, junction, pass_no)`, so a route through the same junction twice works.
- **Destination inside the junction box:** if the leg ends within the junction box, the junction is claimed only if the vehicle must cross it. The existing destination-junction fix stays.

### 4.3 Signal states

| State | Meaning |
|---|---|
| `NORMAL` | running its fixed plan |
| `TRANSITIONING` | clearing the current phase (rest of minimum green, then amber, then all-red) toward the claimed phase; shows a countdown |
| `PREEMPTED` | the claimed phase is held green for the winner |
| `QUEUED` | a second, incompatible claimant waits; green is expected after the winner clears plus 5 s transition |
| `RECOVERING` | after release, going back to the plan: amber + all-red, then the most-starved approach gets green first, for `recovery_s` |

`CONFLICT` stays as the per-vehicle label for a losing claimant, for API compatibility.

### 4.4 Several claimants at one junction

- **Compatible:** claimants whose approach maps to the **same phase** are compatible and are all PREEMPTED. With split phasing, everything from one approach moves together.
- **Incompatible:** these are sequenced. Each is given a priority rank:
  - 0: AMBULANCE on TRANSPORT (patient on board)
  - 1: AMBULANCE on RESPONSE
  - 2: FIRE_ENGINE
  - 3: POLICE
- **Winner:** the claimant with the earliest ETA, unless a higher-priority claimant's ETA is within `DIVERT_PRIORITY_WINDOW_S` (default 10 s) of it; then the higher priority wins. Ties go to the vehicle id.
- **Losers** get QUEUED with an expected green time, and receive a **speed advisory**: slow down to reach the stop line at the expected green (minimum 2 m/s), rather than stopping hard. If that's impossible, they stop at the stop line.
- **Re-sequencing:** the order is recomputed each tick. A new higher-priority arrival can re-sequence QUEUED claimants, but never the vehicle already PREEMPTED within 3 s of crossing.

### 4.5 Release and failsafes

- **Release** once the vehicle is past the junction box. The box size is 20 m for 3–4 legs and 30 m for 5+ legs (replacing the fixed 20 m). The junction then goes RECOVERING.
- **Max hold:** if the winner has not crossed within `DIVERT_PREEMPT_MAX_HOLD_S` (default 60 s) of green, release with outcome `TIMEOUT`, and let the vehicle re-claim later. This prevents gridlock behind a stopped vehicle.
- **Leg change, cancel, re-route or failure:** release all claims of that vehicle. The existing guarantee holds: every exit path releases every claim.
- **Starvation:** report a metric for junctions that have been pre-empted for more than 50% of the last 5 minutes. Emergency vehicles still win; this is for operators.
- **Non-emergency legs (RETURN) never claim.** They obey `phase_at`:
  - Arriving at red for their approach, they brake (3.0 m/s²) to the stop line, wait for green, then accelerate.
  - If a junction is pre-empted for another approach, they wait until it recovers.

### 4.6 Recording

Each claim produces a `signal_preemptions` row with:
- `leg_seq`, `movement`, `lane_index`, `lanes_total`, `free_left`
- `required_lead_s`, `transition_s`, `queue_clear_s`
- `green_at`, `crossed_at`, `released_at`, `waited_s`
- `outcome` (`CLEARED|TIMEOUT|CANCELLED|REROUTED`)
- `phase_state_at_claim` (green-extend vs red-truncate)

---

## 5. Data and API changes

### Migration `0004_trip_legs`

- **New table `dispatch_legs`:** id, dispatch_id FK, seq, kind, emergency, origin and destination (geography points), `destination_station_id` (nullable), `route` (LineString), `distance_m`, `predicted_eta_s`, `segment_speeds_mps`, `route_choice` (jsonb), `hospital_choice` (jsonb, nullable), `eta_model`, `planned_at`, `started_at`, `ended_at`, `actual_travel_s`. Unique on (dispatch_id, seq).
  - The existing `dispatches.route` and `predicted_eta_s` stay as the RESPONSE leg, for compatibility.
- **incidents:** add `hospital_station_id` (FK, nullable), `transport_started_at` and `at_hospital_at`. Add `TRANSPORTING` and `AT_HOSPITAL` to the status check/enum.
- **units:** add `TRANSPORTING`, `AT_HOSPITAL` and `RETURNING`.
  - The CHECK that ties status to `current_dispatch_id` must allow these.
  - The unit is freed (AVAILABLE, `current_dispatch_id` null) when the RETURN leg ends.
- **dispatches:** status covers the whole trip, ending `COMPLETED` when the unit is back.
- **signal_preemptions:** add the §4.6 columns.
- **vehicle_positions:** add `leg_seq`.
- **stations:** add `emergency` and `healthcare`. The seed upserts them from the snapshot.

### History events

New events, each with a deterministic id and an idempotent persister handler:
- `leg_planned`, `leg_started`, `leg_ended`, `leg_plan_failed`, `rerouted`
- `transport_started`, `arrived_hospital`, `handover_complete`
- `returning`, `back_in_service`
- `preempt_transition`, `preempt_green`, `preempt_timeout`

### Live and WS contract

- **vehicles** gain:
  - `leg` {seq, kind, emergency}
  - `phase` (unit status)
  - `destination` {kind, station_id, name}
  - `next_movement` {signal_id, movement, lane_index, lanes_total, side, text, distance_m, free_left}
  - `speed_advisory_mps` (nullable)
- **signals/active** gain `state`, `phase_info` {transition_s_left, expected_green_at, hold_s}, and per-claimant `movement` and `rank`.
- **New WS messages:** `leg_changed` and `route_replaced`. Existing messages gain the fields above.
- **The incident detail** includes `legs[]` (each with `route_choice`), `hospital`, and the new timestamps.
- **Analytics** adds:
  - response time (the RESPONSE leg)
  - transport time
  - call-to-hospital time
  - time from handover to back in service
  - hospital distribution
  - pre-emption outcomes and the green-extension vs red-truncation split
  - free-left count
  - re-route count
- **Docs:** `docs/api.md`, `docs/data-model.md`, `docs/simulation-model.md` and the knowledge-bank pages (02, 05, 06, 07, 11) must be updated.

---

## 6. Frontend

- **Legs** are drawn by kind:
  - RESPONSE: solid, service colour
  - TRANSPORT: solid violet with a patient badge
  - RETURN: dashed grey
- **Hospital:** the destination hospital is highlighted.
- **Vehicle card:** phase chip, next movement shown as an arrow icon, and a lane diagram (n lanes, target lane highlighted, "be in lane by 120 m").
- **Signal popup:** state, countdown (transition left, hold) and claimant queue with movement and rank.
- **Route choice:** a "Why this route" explainer listing the candidates and scores, with alternatives drawn faint on hover.
- **Planning state:** a "Planning route…" indicator while waiting for a leg plan.
- **History:** legs in the timeline, replay across all legs, and per-leg predicted vs actual.
- **Analytics:** the new metrics from §5.

---

## 7. Tests (must exist and pass)

**Unit:**
- Turn classification over angles −180° to 180° including every boundary, plus roundabout exits, U-turns, OSRM disagreement and free-left detection.
- Lane advice for every movement × lane count 1–3, and look-ahead with closely spaced junctions.
- `phase_at` determinism.
- Transition time in green / mid-min-green / amber / all-red states, and queue discharge.
- Compatible vs incompatible claimants, priority window, re-sequencing, speed advisory.
- Max-hold timeout, release past the box, multi-pass claims.
- A RETURN vehicle stopping at red and starting on green.
- Route scoring choosing the less congested or incident-free alternative.
- Hospital selection: ED preference within tolerance; `emergency=no` and dentists excluded.
- The ±10% ETA guarantee on every leg kind.

**Regression (keep all three original guarantees):**
- The POLICE alias.
- Every claim released on every exit path, including leg change, re-route, cancel mid-RESPONSE, and failover mid-TRANSPORT.
- No lost updates under concurrency.

**Integration (real PostGIS and Redis):**
- A full MEDICAL chain from dispatch to back in service, with legs, hospital and pre-emptions persisted and the unit freed only at the end.
- FIRE/CRIME with RESPONSE then RETURN.
- Cancel in each phase, including 409 during TRANSPORT.
- Leg-plan outbox recovery when Redis is down.
- Leader failover while waiting for a plan and while on TRANSPORT.
- OSRM failure during TRANSPORT planning (retry, no teleport).

**E2E:** compose at time scale 10, all three cities, with a real public OSRM chain.

---

## 8. Known limits (state honestly in docs and UI)

- **Crowding is modelled, not measured.** There is no live traffic feed: class-aware time-of-day congestion, incident zones and DIVERT's own vehicles. A `TrafficModel` plug-in point exists for TomTom/HERE/Google (needs an API key and has a cost).
- **Lane counts and turn lanes are estimated** from road class. OSM `lanes`/`turn:lanes` are not in the snapshot, and OSRM returns no lane data here. v2 can fetch the lane tags.
- **Signal phase plans are synthetic** (split phasing, standard timings). There are no real timing plans from city traffic police.
- **Units are not dispatchable while RETURNING**, and there are no hospital capacity or bed data (OSM `beds` is on 12 of 2,246 hospitals).
