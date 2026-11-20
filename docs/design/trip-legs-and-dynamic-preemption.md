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

---

## Deviations and implementation notes (stage A, 2026-11-17)

Stage A implemented §2-§4 on the existing RESPONSE leg (no new legs, no migration 0004).
Where the code differs from, or had to decide something this document leaves open:

| # | Topic | What the code does | Why |
|---|---|---|---|
| 1 | §4.3 signal states | Junction states are `JunctionState` (NORMAL, TRANSITIONING, PREEMPTED, QUEUED, RECOVERING). `SignalState` stays the per-claim label (PREEMPTED = served, CONFLICT = queued) persisted in `signal_preemptions.state`. Junction state = TRANSITIONING until the served phase is green, then QUEUED if an incompatible claimant waits, else PREEMPTED. | The `signal_preemptions.state` CHECK only allows the old values until stage B's migration; a separate enum keeps the ORM model equal to the database. |
| 2 | §3 free left | "The route is already turning left 10 m before the junction" (heading at `along − 10 m` more than 15° left of the approach chord over `[along − 60, along − 30]`), plus `offset_m > 12 m`. | A slip road that rejoins the cross road after the junction's projection never *completes* its turn 10 m before it, so the literal rule never fires on real slip roads. |
| 3 | §3 OSRM corroboration | Same class: geometry. One class apart: OSRM's modifier is used. More than one class: geometry wins, debug log. OSRM intersections listing fewer than 2 roads (depart / arrive points) are ignored. | Reading of "corroborate"; the arrive point would otherwise give a leg count of 1. |
| 4 | §3 lanes | Approach free-flow speed = the fastest OSRM segment in the 30 m before the junction (OSRM folds turn penalties into the last segments). The first junction assumes the vehicle starts in lane 1. One-lane roads: `side` L for lefts, R for rights / U-turns, C for straight. Look-ahead only re-assigns STRAIGHT movements; turns keep their lane. Lane-change speed = the approach's free-flow speed. | Unspecified details. |
| 5 | §4.1 plans | Junctions with fewer than 3 legs use 2 phases and the 90 s cycle; many-legged junctions stretch the cycle so each phase keeps its 10 s minimum green. Unknown geometry: the first observed approach of the junction in the worker's lifetime (rebuilt after failover); junction geometry rides on the corridor data (`legs`, `junction_bearings`) and lives in the worker's in-memory registry, not in Redis. | Unspecified details; no extra Redis state needed in stage A. |
| 6 | §4.2 queue | `T_queue = rate/lane × lanes × red_elapsed × 2.0 s / lanes`, rate for EXPRESSWAY = ARTERIAL's 0.20; while the phase is green, the green already elapsed is subtracted (residual queue). `red_elapsed` comes from the fixed plan. | EXPRESSWAY missing from the table; a green phase has a partly discharged queue. |
| 7 | §4.2 destination in the box | Stop line = junction centre − box / 2; "must cross" = the leg ends past the stop line. A junction whose stop line the leg never reaches is not claimed. | Concrete reading of "the vehicle must cross it". |
| 8 | §4.2 multi-pass | Claim key `(vehicle, junction, pass_no)` in events (`clm:<dispatch>:<signal>:<pass>:<attempt>` for later passes / re-claims); in Redis a vehicle holds one pass of a junction at a time (the next pass is claimed after the previous is released). | Keeps the per-(signal, vehicle) Redis claim fields and their release-all guarantee unchanged. |
| 9 | §4.4 advisory | The advisory also applies to a served claimant still TRANSITIONING that would reach the stop line before its green. Advisories are applied in the next tick (0.5 s lag). Expected green of the k-th queued phase = max(previous green + 10 s minimum green, previous phase's last vehicle clearing) + 5 s. | A winner should not enter on red either. |
| 10 | §4.5 recovery | `DIVERT_PREEMPT_RECOVERY_S` = 20 s (no default given). Most-starved approach = the phase whose last *planned* green before the pre-emption began ended earliest. After the recovery green: amber + all-red, then the plan resumes wherever it is. A TIMEOUT re-claim is allowed after clearance + recovery + clearance (30 s). Starvation is the gauge `divert_signals_starved{city}`. | Unspecified details. |
| 11 | §4.5 non-emergency motion | Stops for red, amber, all-red and pre-emption for another approach, if it can still stop (`v²/2b ≤ distance + 1 m`); otherwise (amber dilemma) it carries on. Moves off one tick after the green. Junction cap 15 km/h, no 1.3 advantage. A vehicle never waits inside an earlier junction's box (back-to-back junctions). | Unspecified details. |
| 12 | §2 ETA for non-emergency legs | Kinematic time + Σ expected waits (`red_share × red / 2`) of the signalised junctions ahead - an expectation, not the deterministic wait. | Stage B must validate the ±10 % on RETURN legs (§7) and may switch to a phase-aware ETA. |
| 13 | §2 contention | Other vehicles' junction ETAs come from a new `signal_etas` field of the live vehicle documents (one `HGETALL`, skipped when Redis is down). Contention delay = 10 s (`CONTENTION_DELAY_S`). | Cheapest source; no extra writes. |
| 14 | §2 scoring | `base_time_s` = the kinematic ETA at free flow (not OSRM's duration). SLIGHT_RIGHT is not a "signalised right turn" for the tie-break. Incident zones apply to every segment that comes within the radius. Labels A, B, ... follow OSRM's order, detours last. | Consistent with the ETA model; slight rights do not cross oncoming traffic. |
| 15 | §5 persistence (stage A) | No migration. `signal_preemptions` gets its existing columns (a `C` lane is stored as NULL; the ORM column is pinned to L/R); every §4.6 field rides in the event payloads (`incident_events.payload`). `route_choice` is in the start command (outbox payload), the `dispatched` event payload, the POST response and the incident detail (`dispatches[].route_choice`). New history events `preempt_transition`, `preempt_green`, `preempt_timeout` are timeline-only. | Stage B's migration 0004 adds the columns. |
| 16 | Live JSON | `signals/active` also lists RECOVERING junctions (legacy fields from the last winner, no claimants) so the current frontend keeps rendering them. Claimants keep the legacy `state` (PREEMPTED / CONFLICT) and gain `phase_state`, `movement`, `rank`, `queue_pos`, `expected_green_at`. `next_movement` also carries `pass_no` and `advise_at_m`. | Backward compatibility; the current frontend shows QUEUED / TRANSITIONING junctions as pre-empted (no amber styling) until its stage. |
| 17 | Settings | `DIVERT_PREEMPT_LEAD_S` / `DIVERT_PREEMPT_DISTANCE_M` are deprecated aliases of `DIVERT_PREEMPT_MAX_LEAD_S` / `DIVERT_PREEMPT_MIN_CLAIM_DISTANCE_M`; `DIVERT_CONFLICT_TIE_S` of `DIVERT_PRIORITY_WINDOW_S`. New: `DIVERT_PREEMPT_RELEASE_PAST_LARGE_M` (30), `DIVERT_PREEMPT_MAX_HOLD_S` (60), `DIVERT_PREEMPT_RECOVERY_S` (20), `DIVERT_ROUTE_ALTERNATIVES` (3), `DIVERT_ROUTE_DETOURS` (2). The ETA model is `kinematic-v2`. | |

## Deviations and implementation notes (stage B, 2026-11-20)

Stage B implemented §1 (legs, LegPlanner, hospitals, cancel) and §5 (migration `0004_trip_legs`,
persistence, API / WS / analytics), plus stage A's open items. Where the code differs from,
or had to decide something this document leaves open:

| # | Topic | What the code does | Why |
|---|---|---|---|
| B1 | §5 vehicles `destination` | `destination` keeps its v1 type `[lat, lng]` (the current leg's end) on every vehicle document; the object `{kind, station_id, name, location}` is **`destination_info`** (tick, `/vehicles`, snapshot, `vehicle_added.vehicle`, `leg_changed`). | The deployed UI feeds `destination` to the map as a point; a type change is breaking and the API rule is that breaking changes bump `v`. |
| B2 | §1 RETURN from the own station | A unit that ends a leg within 30 m (`legs.AT_HOME_M`, assumption) of its station gets **no RETURN leg**: it is back in service when the dwell ends (`returning` then `back_in_service`). Typical case: a hospital-based ambulance takes the patient to its own ED (the nearest hospital to the scene is often the one it came from). | A zero-length route cannot be planned (the planner retried forever before this rule). |
| B3 | §1 statuses | The unit (and dispatch) become RETURNING when the unit is **released** - scene cleared, handover complete or cancelled - not when the RETURN leg starts; the incident is RESOLVED at that moment. `dispatches.status` has no EN_ROUTE (DISPATCHED covers it). | The unit no longer serves the incident while it waits for its RETURN plan. |
| B4 | §1 cancel en route | All claims released at once (outcome `CANCELLED`), the vehicle loses its emergency flag and brakes at 3 m/s² to `along + v²/(2·3)`; the RETURN is planned from that halt point and starts when the vehicle has stopped. | "Plan a RETURN from the current position" without a teleport or a U-turn at speed. |
| B5 | §1 cancel race | The API checks PostgreSQL, which trails the worker by up to one persister round. If the cancel reaches a vehicle that already has its patient on board (or was already released), the worker emits a new **`cancel_rejected`** history event and the persister restores the incident status (TRANSPORTING / AT_HOSPITAL / RESOLVED). Cancel on RESOLVED / CANCELLED answers `200` unchanged. | The design lists `409` for TRANSPORT, but the API can only see the persisted status. |
| B6 | §1.1 ED preference | Read literally: a confirmed ED wins if its **cost <= max(180 s, 1.25 × best cost)**; ties by cost, straight-line distance, id. `hospital_choice` = `{chosen, reason, ed_tolerance_s, candidates[]}`. | "within max(180 s, 1.25 × best cost)" names a cost bound. |
| B7 | §1 leg numbering and events | RESPONSE is seq 0; the next leg gets the previous number + 1 when it is **requested**; a leg superseded before it started (cancel on scene with TRANSPORT pending) keeps its number (`leg_ended` SUPERSEDED), so seqs may have gaps. `leg_started` is emitted for TRANSPORT / RETURN; the RESPONSE leg starts with the dispatch (`en_route`, `started_at` written by the API). `leg_planned` is timeline-only in the persister (the planner wrote the row with its outbox command). | Deterministic ids per (dispatch, seq) for idempotency. |
| B8 | §2 ETA | `kinematic-v3`: RETURN legs are **replayed tick by tick** with the simulation's own obey-the-signal code against the deterministic fixed plans (`services/obey.py`), instead of `kinematic-v2`'s expected wait. The prediction of record of TRANSPORT / RETURN is re-made **at departure** (`leg_started`, overwriting the planner's value in `dispatch_legs.predicted_eta_s`). The ±10 % holds on all leg kinds in unit and integration tests; another vehicle's pre-emption of a junction ahead can still delay a RETURN. | Stage A deviation 12 asked stage B to prove ±10 % on RETURN; the expectation missed it on short legs. |
| B9 | §2 re-routing | The engine detects the triggers, the planner decides. Re-route point = ~15 s ahead (>= 100 m), none when < 500 m would be left, 60 s cool-down between evaluations (assumptions); `route_replaced` is ignored if the vehicle is already within 5 m of that point; the new route is spliced in (`legs.splice`), passed junctions keep their keys, a junction crossed again gets the next pass number. Re-routed legs are excluded from the ETA accuracy metric. | Planning takes time; the vehicle must never jump. |
| B10 | §1 planning state | The pending request lives in the vehicle's live JSON (`pending`); a plan that arrived during the dwell is kept in memory only (`ready`) - after a failover the request is re-issued and answered from the `dispatch_legs` row. The planner's XADD loop stops when the API outbox relay has stamped the row or the leader no longer waits for it. | One source of truth (PostgreSQL) for planned legs. |
| B11 | Stage A gaps | Lane `C` is stored (`ck_signal_preemptions_lane` allows it); `leg_seq` is real on claims, positions and events; junction geometry is cached in the Redis HASH `divert:{city}:junctions` (written in the tick transaction, seeded on take-over); the ±10 % on RETURN is proven (B8). | |
| B12 | §5 analytics | `legs` = predicted-vs-actual per leg kind (MAPE and worst error); `handover_to_back_in_service_s` from the `handover_complete` / `back_in_service` events (so it also covers B2); `hospitals` top 20; `free_left_passed` counts free-left corridor entries of driven legs (JSONPath). | |
| B13 | §1.1 data | Snapshots regenerated **offline** from the cached Overpass payloads (`osm.py --offline`, `meta.fetched_at` kept); only `emergency` / `healthcare` were added to hospitals; ids, counts, signals and every other field are identical (`.work/stage5/stageB/snapshot_diff.log`). | No network, no fabricated data. |
| B14 | Settings | `DIVERT_SCENE_TIME_{AMBULANCE,FIRE_ENGINE,POLICE}_S`, `DIVERT_HOSPITAL_HANDOVER_S` (`DIVERT_SCENE_TIME_S` deprecated fallback), `DIVERT_LEG_PLAN_BACKOFF_S` (2) / `_MAX_S` (60), `DIVERT_HOSPITAL_CANDIDATES` (6), `DIVERT_REROUTE_MIN_SAVING_S` (60), `DIVERT_REROUTE_MAX_PER_LEG` (2), `DIVERT_REROUTE_BEHIND_SHARE` (0.25), `DIVERT_REROUTE_BEHIND_S` (60). | |
