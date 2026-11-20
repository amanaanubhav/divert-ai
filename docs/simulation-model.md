# Simulation model

Code: `services/routing.py`, `services/speed_model.py`, `services/eta.py`,
`services/preemption.py`, `services/simulation.py`. Everything here is a *model*:
the constants are documented defaults, not measurements (see "Calibration").

## 1. Route

`GET {DIVERT_OSRM_URL}/route/v1/driving/{lng},{lat};{lng},{lat}?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed&alternatives=false`

* httpx timeout `DIVERT_OSRM_TIMEOUT_S` (connect <= 3 s); retries `DIVERT_OSRM_RETRIES` on
  transport errors and HTTP 429/5xx with exponential backoff + jitter; `NoRoute` / 4xx are
  not retried. **No straight-line fallback**: if no candidate can be routed the dispatch
  returns 503 and nothing is written.
* Responses are cached in Redis for `DIVERT_OSRM_CACHE_TTL_S` (key = profile + coordinates).
* The geometry becomes the polyline; consecutive duplicate vertices (zero-length segments)
  are merged so polyline, PostGIS LineString and speeds line up one-to-one.
* Per-segment free-flow speed = OSRM `annotation.speed` (else distance/duration).
* Maneuvers (`steps[].maneuver` type/modifier/location) are placed on the polyline by
  OSRM's cumulative step distance, snapped to the nearest vertex within 250 m.

## 2. Distances

All distances are great-circle haversine on a sphere of R = 6 371 008.8 m (IUGG mean
radius; < 0.5 % from WGS84 at city scale). Positions along a route are interpolated by
**cumulative haversine distance** (`along_m`); signals are projected onto the polyline
(local equirectangular projection per segment, haversine offset) to get their `along_m`
in the same metric. The network smoke test checks our route length is within 5 % of
OSRM's.

## 3. Corridor

PostGIS `ST_DWithin(signal.geom, route, DIVERT_CORRIDOR_M = 30 m)` on the GiST index of
`signals.geom` (geography, metres), ordered by `ST_LineLocatePoint`; then the precise
`along_m` (projection above), the approach bearing (route bearing 5 m before the
junction) and the lane advisory.

**Lane advisory** (India drives on the left): the OSRM maneuver within 40 m (along the
route) of the junction decides - `left`, `slight left`, `sharp left` -> `L`; straight,
right turns and U-turns -> `R` (outer lane); no maneuver -> straight -> `R`.

## 4. Speed model

Vehicle profiles:

| Service | max speed | acceleration | braking |
|---|---|---|---|
| AMBULANCE | 80 km/h | 2.0 m/s^2 | 3.0 m/s^2 |
| FIRE_ENGINE | 60 km/h | 1.0 m/s^2 | 3.0 m/s^2 |
| POLICE | 90 km/h | 2.5 m/s^2 | 3.0 m/s^2 |

Speed caps along the route:

* **segment cap** = `min(v_max, max(1, v_osrm) x congestion(h) x 1.3)` where 1.3 is the
  emergency advantage (sirens, lane discipline yielding) and `congestion(h)` is the city's
  hourly factor for the local hour `h` of the simulation clock (`Asia/Kolkata`);
* **junction cap** at each corridor signal: **35 km/h** if the signal is pre-empted *for
  this vehicle*, otherwise **15 km/h** (red, or green for a competing vehicle);
* **arrival**: the route end is a stop (1 m/s crawl floor so arrival time is finite).

The point caps become a **braking envelope** by a backward pass over the knots (route
vertices + junction positions):
`limit[k] = min(cap_in, cap_out, junction_cap, sqrt(limit[k+1]^2 + 2 b (x[k+1] - x[k])))`;
the allowed speed at `x` inside interval `k` is
`min(cap_k, sqrt(limit[k+1]^2 + 2 b (x[k+1] - x)))`.

**Motion** (every tick, `dt = DIVERT_SIM_TICK_S x DIVERT_SIM_TIME_SCALE` simulated s):
`v' = min(v + a dt, allowed(x))`, `x' = x + (v + v') / 2 * dt`, `v' = min(v', allowed(x'))`.
Arrival when `x >= length - 0.5 m`.

## 5. ETA

`EtaModel` is a protocol (`estimate(EtaQuery) -> EtaEstimate`) so a learned model can
replace the default later; `dispatches.eta_model` records which model predicted each ETA.

The shipped `KinematicEtaModel` integrates the **same envelope analytically** from the
current `(x, v)`: per interval, accelerate at `a`, cruise at the cap, brake at `b` to the
next knot's limit (closed form), assuming every corridor junction ahead will be
pre-empted for the vehicle (that is DIVERT's plan). It returns the ETA to the destination
and to every corridor signal ahead; the ETA at dispatch (`predicted_eta_s`) ranks the
candidate stations. The API evaluates it at the **simulation clock** (`divert:sim:clock`,
section 8) - not the wall clock - so the congestion hour of the prediction is the hour
the worker will simulate. (Before this was fixed, at time scale 10 a prediction made at
04:31 IST wall time for a sim clock of 08:00 IST was off by up to +32 %.)

Consistency test: on stubbed routes the simulated arrival is within **+-10 %** of the ETA
predicted at dispatch for all services and several hours of the day
(`tests/unit/test_speed_eta.py`); end to end on the compose stack with a real OSRM route
in Delhi: predicted 73.4 s, simulated 75.0 s. Conflicts (15 km/h through a contested
junction) make actual > predicted, by design.

## 6. Pre-emption

Per vehicle per tick, for each corridor signal it has not passed:

* **claim** when `ETA_to_signal <= DIVERT_PREEMPT_LEAD_S (45 s)` or the signal is within
  `DIVERT_PREEMPT_DISTANCE_M (600 m)` along the route;
* **release** when the vehicle is more than `DIVERT_PREEMPT_RELEASE_PAST_M (20 m)` past it;
* **release all** of the vehicle's claims on arrival, cancellation and any error.

Claims are owned **per vehicle id** (never per vehicle type). Resolution per signal,
every tick: the claim with the lowest ETA is `PREEMPTED`, every other `CONFLICT`; claims
within `DIVERT_CONFLICT_TIE_S (5 s)` of the best ETA are a tie broken by service
priority AMBULANCE > FIRE_ENGINE > POLICE, then by vehicle id. Signal state: `NORMAL`
(no claim - never stored or streamed), `PREEMPTED` (one claimant) or `CONFLICT`
(contested). A vehicle whose claim is `CONFLICT` gets the 15 km/h junction cap.

## 7. Time saved (estimate)

`est_time_saved_s = (# corridor junctions the vehicle passed while PREEMPTED for it) x
city average junction delay` (Delhi 35 s, Kolkata 40 s, Bengaluru 40 s). It is always
labelled an estimate: it assumes each such junction would otherwise have cost the city's
average delay.

## 8. Clock, sampling, scene

* Simulation clock: wall clock when `DIVERT_SIM_TIME_SCALE = 1`; otherwise it advances by
  `dt` per tick and is persisted in Redis (`divert:sim:clock`) so it survives fail-over.
  Congestion uses the city's local hour of this clock.
* Event timestamps (`ts`) are wall clock; durations (`travel_s`, `elapsed_s`) are
  simulated seconds - so analytics stay meaningful at any time scale.
* Simulated time only advances while a leader ticks: during a leader change (up to the
  lease TTL) it is frozen. Each persisted position therefore carries its simulated
  `elapsed_s`, and a leader that rebuilds a vehicle from PostgreSQL resumes from that
  value - never from wall-clock differences, which would count the freeze (x time scale)
  as travel time. An ON_SCENE vehicle resumes with its persisted `actual_travel_s`.
* The tick counter is per city and monotonic across leader changes (a new leader resumes
  from `divert:{city}:tick`).
* Positions are sampled every `DIVERT_POSITION_SAMPLE_S` (5) simulated seconds.
* A vehicle stays ON_SCENE for `DIVERT_SCENE_TIME_S` (120) simulated seconds, then is
  cleared (incident RESOLVED, unit AVAILABLE).

## 9. Limitations and calibration

* No other traffic is simulated; congestion is a city-wide hourly factor.
* Hourly congestion factors and junction delays are hand-set approximations of the
  diurnal shape - calibrate with the TomTom Traffic Index city pages and traffic-police
  junction counts, then with DIVERT's own `predicted_eta_s` vs `actual_travel_s`
  (`analytics.summary.dispatches.eta_mape`).
* OSM signal coverage is uneven (Kolkata: 67 signal nodes -> 28 junctions), so Kolkata
  corridors hold few signals (see data-sources.md).
* Vehicles follow the OSRM car profile; emergency-vehicle exemptions (wrong-way, medians)
  are not modelled.
