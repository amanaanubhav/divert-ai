# Simulation model

Code: `services/routing.py`, `services/route_choice.py`, `services/traffic.py`,
`services/corridor.py`, `domain/movement.py`, `domain/lanes.py`, `domain/signal_plan.py`,
`services/speed_model.py`, `services/eta.py`, `services/preemption.py`,
`services/junctions.py`, `services/obey.py`, `services/legs.py`, `services/planner.py`,
`domain/hospitals.py`, `services/simulation.py`. Everything here is a *model*: the
constants are documented defaults, not measurements (see "Calibration"). The contract is
[design/trip-legs-and-dynamic-preemption.md](design/trip-legs-and-dynamic-preemption.md)
(stage A: §2-§4 on the RESPONSE leg; stage B: §1 trip legs and §5 persistence).

## 1. Route candidates

`GET {DIVERT_OSRM_URL}/route/v1/driving/{lng},{lat};{lng},{lat}?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed,nodes&alternatives=3`

(`nodes`: the OSM node id of every vertex, used to find a signal cluster's member nodes on
the route. A zero-length answer, with origin and destination snapped to the same road point,
is a routing error of kind `same_point`.)

* httpx timeout `DIVERT_OSRM_TIMEOUT_S` (connect <= 3 s); retries `DIVERT_OSRM_RETRIES` on
  transport errors and HTTP 429/5xx with exponential backoff + jitter; `NoRoute` / 4xx are
  not retried. **No straight-line fallback**: if no candidate can be routed the dispatch
  returns 503 and nothing is written.
* `alternatives` = `DIVERT_ROUTE_ALTERNATIVES` (3; public OSRM returns 2 for these cities).
  When fewer than 2 routes come back and the trip is longer than 1.5 km, up to
  `DIVERT_ROUTE_DETOURS` (2) **via-point detours** are requested: via = the midpoint
  offset perpendicular by +-20 % of the straight distance, `continue_straight=true` (no
  U-turn at the via). A detour whose 11 sample points all lie within 25 m of an earlier
  candidate is a duplicate and dropped. Candidates longer than 1.6 x the shortest are
  dropped before scoring.
* Responses are cached in Redis for `DIVERT_OSRM_CACHE_TTL_S`; the key hashes the OSRM base
  URL, the request parameters and the coordinates (switching servers never serves stale
  routes).
* The geometry becomes the polyline; consecutive duplicate vertices (zero-length segments)
  are merged so polyline, PostGIS LineString and speeds line up one-to-one.
* Per-segment free-flow speed = OSRM `annotation.speed` (else distance/duration).
* Step maneuvers (type, modifier, roundabout `exit`, road `name`) are placed on the polyline
  by OSRM's cumulative step distance, snapped to the nearest vertex within 250 m.
  **OSRM intersections** (road `bearings`, `in`, `out`) are placed on their vertex (exact
  coordinate match, else nearest vertex within the step): they are the junction geometry.

## 2. Distances

All distances are great-circle haversine on a sphere of R = 6 371 008.8 m (IUGG mean
radius; < 0.5 % from WGS84 at city scale). Positions along a route are interpolated by
**cumulative haversine distance** (`along_m`); signals are projected onto the polyline
(local equirectangular projection per segment, haversine offset) to get their `along_m`
in the same metric. The network smoke test checks our route length is within 5 % of
OSRM's.

## 3. Corridor, movement and lane advice

PostGIS `ST_DWithin(signal.geom, route, DIVERT_CORRIDOR_M = 30 m)` on the GiST index of
`signals.geom` (geography, metres). Each candidate is projected onto the polyline **once
per pass** (runs of segments within the buffer; runs < 200 m apart are one pass), so a
route through a junction twice has entries `J` (pass 0) and `J#1` (pass 1).

**Movement** (`domain/movement.py`). Approach / exit bearings are the chords over the 30 m
before / after the junction; OSRM intersections in the **junction span** win and give the
**leg count**. The span runs from the first to the last of the route's vertices that are
member nodes of the junction's signal cluster (within 100 m along) and the OSRM
intersections within 15 m. The approach is the first span intersection's
`bearings[in] + 180` and the exit the last one's `bearings[out]`, because a dual
carriageway crossing is 2-4 OSRM intersections and the turn spans them. The leg count and
road bearings come from the span intersection with the most roads (ties: the entry).
Intersections listing one road are OSRM's depart / arrive points and are ignored. With no
intersection the leg count is unknown, and 4 is assumed. With
`delta = normalise(exit - approach)` in (-180, 180], negative = left:

| \|delta\| | movement |
|---|---|
| <= 20 | STRAIGHT |
| <= 45 | SLIGHT_LEFT / SLIGHT_RIGHT |
| <= 135 | LEFT / RIGHT |
| < 170 | SHARP_LEFT / SHARP_RIGHT |
| >= 170 | U_TURN |

The OSRM modifier within 40 m corroborates: one class apart, OSRM's is used; more than
one class apart, the geometry wins (debug log). `roundabout` / `rotary` -> ROUNDABOUT with
OSRM's exit. **Free left**: LEFT / SLIGHT_LEFT, route > 12 m from the junction centre and
already turning left 10 m before it (a slip road bypassing the signal): never claimed.

**Lanes** (`domain/lanes.py`; India drives on the left, lane 1 = kerb). Road class and
lanes per direction from the approach's OSRM free-flow speed (fastest segment in the 30 m
before): >= 75 km/h EXPRESSWAY 3; 50-75 ARTERIAL 3 (>= 60) or 2; 32-50 COLLECTOR 2; < 32
LOCAL 1 - estimates, OSRM returns no lane data here. Target lane: lefts, free left,
roundabout exit 1 -> 1; STRAIGHT -> n on an emergency leg (traffic yields left; one lane:
"centre, traffic pulls left"), `ceil(n/2)` otherwise; rights, U-turn, roundabout exit >= 2
-> n. Changing k lanes needs `k x max(30 m, 5 s x v)`; advise at
`along - max(that, 150 m)`, clipped to the previous junction. Look-ahead: a straight-on
junction too close to the next one takes the next one's lane. Output `LaneAdvice`
(`lane_index`, `lanes_total`, `side` L/C/R, `advise_at_m`, `free_left`, `text`); the
corridor's `lane` field is the side.

## 4. Traffic model

`TrafficModel` (protocol, `services/traffic.py`) gives a speed factor per route segment;
the ETA and the simulation apply exactly the same factors. Shipped `ModelledTraffic`:

* **class-aware congestion** `f_class = 1 - s x (1 - f_city)`: `f_city` = the city's hourly
  factor at the local hour of the simulation clock, `s` = 1.0 EXPRESSWAY/ARTERIAL, 0.7
  COLLECTOR, 0.5 LOCAL (from each segment's own OSRM speed);
* **incident zones** of the other active incidents: segments within FIRE 250 m / CRIME
  150 m / MEDICAL 50 m x 0.3 / 0.6 / 0.85 (own incident exempt). The API reads active
  incidents from PostGIS; the worker uses its active vehicles' incidents - the same set.

A live feed (TomTom / HERE / Google) plugs in as another `TrafficModel` (map-match to its
flow segments, return current/free-flow; needs a key and costs money).

## 5. Speed model

Vehicle profiles:

| Service | max speed | acceleration | braking |
|---|---|---|---|
| AMBULANCE | 80 km/h | 2.0 m/s^2 | 3.0 m/s^2 |
| FIRE_ENGINE | 60 km/h | 1.0 m/s^2 | 3.0 m/s^2 |
| POLICE | 90 km/h | 2.5 m/s^2 | 3.0 m/s^2 |

Speed caps along the route:

* **segment cap** = `min(v_max, max(1, v_osrm) x factor x 1.3)`; the 1.3 emergency
  advantage (sirens, lane discipline yielding) applies to emergency legs only;
* **junction cap** at each corridor signal: **35 km/h** if the vehicle's claim is served
  (or it is a free left), otherwise **15 km/h** (not yet claimed, queued, or any junction
  on a non-emergency leg);
* **stop line** (`junction - box/2`): 0 while the vehicle must wait there (red for a
  non-emergency vehicle; a queued emergency claimant whose advisory cannot make the green);
* **arrival**: the route end is a stop (1 m/s crawl floor so arrival time is finite).

The point caps become a **braking envelope** by a backward pass over the knots (route
vertices + junction positions + the stop line):
`limit[k] = min(cap_in, cap_out, point_cap, sqrt(limit[k+1]^2 + 2 b (x[k+1] - x[k])))`;
the allowed speed at `x` inside interval `k` is
`min(cap_k, sqrt(limit[k+1]^2 + 2 b (x[k+1] - x)))`.

**Motion** (every tick, `dt` simulated s - `DIVERT_SIM_TICK_S x DIVERT_SIM_TIME_SCALE`;
at scale 1 the time the tick actually took, see section 10 - the same `dt` that advances
the clock the signal plans read):
`v' = min(v + a dt, allowed(x))`, then with a speed advisory `v' = min(v', max(adv, v - b dt))`,
`x' = x + (v + v') / 2 * dt`, `v' = min(v', allowed(x'))`. A vehicle never passes an
active stop line (clamped and stopped within 0.3 m). Arrival when `x >= length - 0.5 m`.

## 6. ETA

`EtaModel` is a protocol (`estimate(EtaQuery) -> EtaEstimate`) so a learned model can
replace the default later; `dispatches.eta_model` records which model predicted each ETA.

The shipped `KinematicEtaModel` (`kinematic-v3`) uses the **same motion with the same
traffic factors** as the simulation:

* **emergency legs** (RESPONSE, TRANSPORT) integrate the envelope analytically from the
  current `(x, v)`: per interval, accelerate at `a`, cruise at the cap, brake at `b` to the
  next knot's limit (closed form), assuming every corridor junction ahead is pre-empted
  for the vehicle (DIVERT's plan);
* **non-emergency legs** (RETURN) are **replayed tick by tick** with the simulation's own
  obey-the-signal code (`services/obey.py`: `decide_stop` / `replay`) against the
  junctions' deterministic fixed plans (`phase_at`) - so the predicted waits at red are the
  waits the vehicle will actually have, unless another vehicle's pre-emption changes a
  junction's plan meanwhile. The planner pins the junction geometry the simulation uses
  (`PlanSignals`); while driving, the engine replays every 5 s against its registry and
  decrements in between. The replay always steps by the nominal tick
  (`DIVERT_SIM_TICK_S x DIVERT_SIM_TIME_SCALE`), so a prediction does not depend on how
  late the last tick ran. (`kinematic-v2` added the expected wait `red_share x red / 2`
  per junction instead; that is an average, and missed +-10 % on short RETURN legs.)

It returns the ETA to the destination and to every corridor entry ahead. The prediction
of record of a TRANSPORT / RETURN leg is made **at departure** (the `leg_started` event,
`dispatch_legs.predicted_eta_s`), with the traffic and signal plans of that moment. The API evaluates it at the **simulation
clock** (`divert:sim:clock`), so the congestion hour of the prediction is the hour the
worker will simulate.

Consistency tests: on stubbed routes the simulated arrival is within **+-10 %** of the ETA
predicted at departure for all services and several hours of the day, and with an active
FIRE zone and mixed road classes on the route (`tests/unit/test_speed_eta.py`,
`tests/unit/test_dynamic_preemption.py`), **on every leg kind** - RESPONSE, TRANSPORT and
RETURN with junctions on each (`tests/unit/test_trip_legs.py`); end to end with PostgreSQL,
Redis, the LegPlanner and real corridors (`tests/integration/test_pipeline.py::
test_full_lifecycle_is_persisted`, `tests/integration/test_legs.py`: every leg of a MEDICAL
chain, FIRE / CRIME RESPONSE + RETURN, a RETURN after a cancel). Queuing at a contested
junction makes actual > predicted, by design; a re-routed leg is excluded from the
accuracy metric (its route changed).

## 7. Route choice

`cost = ETA (traffic model) + junction delays + penalties` (`services/route_choice.py`):
emergency junction delays STRAIGHT 0, SLIGHT 1, LEFT 3 (free left 0), RIGHT 6, SHARP 8,
U_TURN 12, ROUNDABOUT 4 + 2 x exit s; non-emergency legs add the expected signal wait;
penalty 10 s per **contested** junction (another live emergency vehicle expected there
within +-20 s, from the `signal_etas` of the live vehicles). The cheapest wins; within 5 %,
fewer signalised RIGHT/SHARP_RIGHT/U_TURN, then fewer contested junctions, then shorter.
Per station the best candidate is chosen, then the best station by cost. Everything is
recorded as `route_choice` (candidates with distance, free-flow base time, congested time,
junction delay, penalties, cost, crowd index = length-weighted mean of `1 - factor`,
right turns, contested junctions, incident zones, most congested road; the reason, e.g.
`Route B: 1.8 min faster than A (A: Ring Road congested, 2 extra right turns)`).

## 8. Signals and pre-emption

**Plans** (`domain/signal_plan.py`, synthetic): fixed-time split phasing, one phase per
approach; cycle 90 s (<= 3 legs), 120 s (4), 150 s (5+), stretched so each phase keeps a
10 s minimum green; each slot = green + 3 s amber + 2 s all-red; offset = CRC-32(junction
id) mod cycle, so `phase_at(junction, legs, t)` is pure. A vehicle's phase is the approach
whose heading is nearest its approach bearing (OSRM bearings, else 4 approaches spaced
from the first observed approach).

**Claim** (emergency legs; `services/preemption.py`) when `ETA <= T_transition + T_queue + 5 s`:

* `T_transition` = 0 if its phase is green (green extension), else the current phase's
  remaining minimum green + amber + all-red (red truncation);
* `T_queue` = `rate x red_elapsed x 2.0 s` per lane, rate/lane ARTERIAL/EXPRESSWAY 0.20,
  COLLECTOR 0.12, LOCAL 0.05 veh/s x `(2 - f_class)` (minus the green already elapsed);
* always within `max(v^2/6 + 50 m, DIVERT_PREEMPT_MIN_CLAIM_DISTANCE_M = 150 m)`, never
  with an ETA above `DIVERT_PREEMPT_MAX_LEAD_S = 90 s`; recomputed every tick;
* not for free lefts, nor for a junction whose stop line the leg never reaches.

**Junction control** (`services/junctions.py`): a served phase is laid down as explicit
intervals (rest of minimum green, amber, all-red, then green held open-ended); states
`TRANSITIONING` -> `PREEMPTED` (`QUEUED` while an incompatible claimant waits); on
release `RECOVERING`: amber + all-red, the most-starved approach green for
`DIVERT_PREEMPT_RECOVERY_S` (20 s), amber + all-red, then the plan.

**Conflicts**: same-phase claimants are compatible (served together); otherwise the
earliest ETA wins unless a better priority rank (0 patient on board, 1 ambulance, 2 fire,
3 police) is within `DIVERT_PRIORITY_WINDOW_S` (10 s); the queue is recomputed every tick
but a served winner within 3 s of crossing is never bumped. Losers are `CONFLICT` /
QUEUED with an expected green (previous green + max(10 s min green, its clearing) + 5 s)
and a **speed advisory** `distance / time to green` (>= 2 m/s), else a stop at the stop
line. Back-to-back junctions: never wait inside an earlier junction's box.

**Release** past the box (20 m past the centre; 30 m with 5+ legs); on arrival, cancel or
error (all claims); **max hold** 60 s after green without reaching the junction ->
`TIMEOUT`, re-claim allowed after clearance + recovery. Starvation (pre-empted > 50 % of
the last 5 min) is a metric.

**Non-emergency legs** (`emergency=False`, stage B's RETURN) never claim: red, amber,
all-red or pre-empted for another approach -> brake at 3.0 m/s^2 to the stop line if it
can still stop, wait, go on green.

## 9. Time saved (estimate)

`est_time_saved_s = (# corridor junctions the vehicle passed while its claim was served) x
city average junction delay` (Delhi 35 s, Kolkata 40 s, Bengaluru 40 s). It is always
labelled an estimate: it assumes each such junction would otherwise have cost the city's
average delay.

## 10. Clock, sampling, scene

* Simulation clock (`services/sim_clock.py`): ONE clock moves the vehicles and runs the
  signal plans (`phase_at`), claim timing, dwells and the RETURN replay - every tick
  advances it by the same `dt` the vehicles move. At `DIVERT_SIM_TIME_SCALE` != 1, `dt` =
  `DIVERT_SIM_TICK_S x scale` per tick and the clock is persisted in Redis
  (`divert:sim:clock`) so it survives fail-over (an overrunning tick slows the whole
  simulation down). At scale 1 the clock follows the wall clock: `dt` = how far it is
  behind, clamped to [0, 2 x `DIVERT_SIM_TICK_S`] - a late tick simulates the time it
  took, a longer stall is caught up over the next ticks, and a backlog over 30 s
  (suspended host) is skipped, not simulated (logged; `divert_sim_clock_lag_seconds`).
  Until stage 5 fix-2 the scale-1 plans read the wall clock while vehicles moved a fixed
  0.5 s, so overrunning ticks let the plans outrun the vehicles (RETURN error -60 % at
  50 % overrun; `tests/unit/test_sim_clock.py`). Congestion uses this clock too.
* Event timestamps (`ts`) are wall clock; durations (`travel_s`, `elapsed_s`) are
  simulated seconds - so analytics stay meaningful at any time scale.
* Simulated time only advances while a leader ticks: during a leader change (up to the
  lease TTL) it is frozen. Each persisted position therefore carries its simulated
  `elapsed_s`, and a leader that rebuilds a vehicle from PostgreSQL resumes from that
  value. Junction controls are in memory; a new leader rebuilds the served ones from the
  restored claims (their `green_at`).
* The tick counter is per city and monotonic across leader changes.
* Positions are sampled every `DIVERT_POSITION_SAMPLE_S` (5) simulated seconds.
* Dwell (simulated seconds, assumptions): on scene `DIVERT_SCENE_TIME_AMBULANCE_S` 480,
  `_FIRE_ENGINE_S` 1200, `_POLICE_S` 600; hospital handover `DIVERT_HOSPITAL_HANDOVER_S`
  600 (`DIVERT_SCENE_TIME_S` is a deprecated fallback for all services). Testers shorten
  everything with `DIVERT_SIM_TIME_SCALE`. `transport_started.scene_s` records the real
  time on scene: arrival -> TRANSPORT departure (the dwell plus any wait for the plan).
* A unit that was ON_SCENE in a stage-A live document (no dwell target, no pending leg)
  is upgraded when a new leader restores it: dwell target = its service's scene time
  (`scene_s` of it already elapsed), and its next leg (TRANSPORT or RETURN) is requested
  to start when the dwell ends.

## 10a. Trip legs (design section 1)

A dispatch is RESPONSE (station -> scene, emergency) -> [TRANSPORT (scene -> hospital,
emergency, ambulance on MEDICAL)] -> RETURN (-> home station, obeys signals). The engine
(pure) and the worker's **LegPlanner** (asyncio tasks of the leader) cooperate:

1. a leg ends (arrival, or a cancel's halt): the engine emits a `plan_request` (dispatch,
   next seq, kind, origin, start time = end of the dwell) in its tick batch and keeps it in
   the vehicle's live state (`pending`);
2. the planner chooses the destination - TRANSPORT: the 6 nearest **eligible** hospitals
   (PostGIS KNN; not `emergency=no`, not dentist / blood bank / alternative / doctor /
   birthing centre; unless `emergency=yes`, not a facility whose every
   `healthcare:speciality` cannot take an emergency patient - dental, eye, skin,
   fertility, physiotherapy, diagnostics, alternative medicine - nor, when untagged, one
   whose name says so before the word "hospital": Dental, Eye, Netra, Nethralaya,
   Ayurvedic, ...) routed concurrently and scored with the crowd-aware cost; a confirmed
   ED (`emergency=yes`) wins if its cost <= max(180 s, 1.25 x the best), else the cheapest;
   RETURN: the unit's station - scores the route candidates at the start time (RETURN with
   the phase-aware replay), builds the corridor (movements, lanes), and writes the
   `dispatch_legs` row **and** the outbox row of the `leg_planned` command in one
   PostgreSQL transaction; then XADDs it (retried while Redis is down; the API's outbox
   relay is the safety net);
3. the leader applies `leg_planned` idempotently (keyed by dispatch + seq). If the dwell is
   over the leg starts at once; otherwise when the dwell ends. Waiting without a plan, the
   vehicle stays where it is (`planning: true`);
4. OSRM / PostgreSQL failures: retried with backoff 2 s doubling to 60 s, unlimited, a
   `leg_plan_failed` event per attempt - never a straight line, never a teleport;
5. failover: the new leader restores vehicles from Redis (or rebuilds them from
   `dispatch_legs` + the last position when Redis lost them) and re-issues every pending
   request; a leg already in PostgreSQL is re-sent from its row, never re-planned.

Statuses: the unit is RETURNING from the moment it is released (scene cleared, handover
complete or cancelled), and AVAILABLE only when its RETURN leg ends (`back_in_service`,
dispatch COMPLETED). A unit that ends a leg within **30 m of its own station** (e.g. the
patient went to the ambulance's own hospital) has no RETURN leg. Cancel: en route -> every
claim released, the vehicle brakes (3 m/s^2) to a stop and the RETURN starts there; on
scene -> the dwell ends, a requested TRANSPORT is superseded; with a patient on board ->
refused (`409` at the API; `cancel_rejected` if the race reaches the worker).

**Re-routing** (emergency legs; the engine detects, the planner decides): triggers are a
new FIRE / CRIME zone on the remaining path, or running more than 25 % **and** 60 s
behind the plan. The planner routes from a point ~15 s ahead (>= 100 m; not when < 500 m
would be left), and sends `route_replaced` (outbox + XADD) only if it saves >= 60 s; at
most 2 per leg, 60 s cool-down between evaluations. The leader splices the new route in at
that point (the vehicle never jumps), releases claims not on the new path (`REROUTED`) and
re-evaluates the rest at the next tick. A junction released this way and crossed again by
the new path is claimed again as a new claim instance: claim event ids carry
`r<reroute_no>` (the re-routes applied before the claim) besides the pass, the TIMEOUT
attempt and the leg, so the persister keeps the second claim and its CLEARED release
(before stage 5 fix-2 they reused the first claim's ids and were dropped as duplicates).
Route candidates are recorded with a simplified polyline (`route_choice.candidates[].route`,
Douglas-Peucker 10 m, at most 150 points) so the UI can draw the alternatives.

## 11. Limitations and calibration

* **Crowding is modelled, not measured**: class-aware time-of-day congestion, incident
  zones and DIVERT's own vehicles; no other traffic is simulated.
* **Lane counts and turn lanes are estimated** from road class (no OSM `lanes` tags in the
  snapshot, no OSRM lane data for these cities).
* **Signal plans are synthetic** (split phasing, standard timings); arrival rates, the
  saturation headway, sensitivities and zone factors are assumptions.
* Hourly congestion factors and junction delays are hand-set approximations of the
  diurnal shape - calibrate with the TomTom Traffic Index city pages and traffic-police
  junction counts, then with DIVERT's own `predicted_eta_s` vs `actual_travel_s`
  (`analytics.summary.dispatches.eta_mape`).
* OSM signal coverage is uneven (Kolkata: 67 signal nodes -> 28 junctions), so Kolkata
  corridors hold few signals (see data-sources.md).
* Vehicles follow the OSRM car profile; emergency-vehicle exemptions (wrong-way, medians)
  are not modelled.
* Hospitals have no capacity / bed data (OSM `beds` is on 12 of 2,246); a RETURNING unit
  is not dispatchable (v2).
