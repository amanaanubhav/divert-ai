# DIVERT API contract (v1)

Base path `/api/v1`; ops endpoints at the root. OpenAPI is served at `/docs` and
`/openapi.json` (generated from the code; this document is the normative contract).

## Conventions

| Topic | Rule |
|---|---|
| Coordinates | **always `[lat, lng]`** (WGS84 decimal degrees), in requests and responses |
| Time | ISO 8601 with offset (UTC), e.g. `2026-11-17T06:30:00.123+00:00` |
| Durations / distances | seconds (`*_s`), metres (`*_m`), metres per second (`*_mps`) |
| Ids | incidents and dispatches: UUID; vehicles: unit callsign (`AMB-n123-1`, `FIRE-...`, `PCR-...`); signals: `J<osm node id>`; stations: `<H|F|P><n|w|r><osm id>` (H = hospital-based ambulance, F = fire station, P = police; n/w/r = OSM node/way/relation, e.g. `Hn12052451552`, `Hw124760551`) |
| Cities | `delhi`, `kolkata`, `bengaluru` (`DIVERT_CITIES`) |
| Request id | `X-Request-ID` is echoed (or generated) on every response and included in logs and error bodies |
| Auth | if `DIVERT_API_KEY` is set, `POST`/`DELETE` require header `X-API-Key: <key>` (401 otherwise); reads are open |
| Rate limit | `POST`/`DELETE`: fixed window, `DIVERT_RATE_LIMIT_PER_MIN` per minute per API key (or client address); 429 + `Retry-After` |
| CORS | origins from `DIVERT_CORS_ORIGINS` (explicit list; credentials allowed; no wildcard) |
| Unknown JSON fields | rejected (422) on request bodies; clients must ignore unknown response fields |

### Error body (every 4xx/5xx)

```json
{
  "error": {"code": "invalid_incident", "message": "unknown incident type 'BOGUS'; valid: ...", "details": {"field": "type", "valid_types": ["AMBULANCE", "CRIME", "FIRE", "HEALTH", "MEDICAL", "POLICE"]}},
  "request_id": "5b1d3c0e8f6a4b0e9c1d2e3f4a5b6c7d"
}
```

| HTTP | `error.code` |
|---|---|
| 401 | `unauthorized` |
| 404 | `city_not_found`, `not_found`, `http_404` |
| 409 | `no_unit_available`, `invalid_state`, `cannot_cancel_transport` |
| 422 | `invalid_incident` (type / outside city), `validation_error` (schema, filters, cursor) |
| 429 | `rate_limited` (`details.retry_after_s`) |
| 500 | `internal_error` |
| 503 | `routing_unavailable` |

## Ops

### `GET /healthz`
`200 {"status": "ok"}` - liveness (process up).

### `GET /readyz`
```json
{"ready": true, "degraded": false,
 "checks": {"postgres": "ok", "redis": "ok",
            "worker": {"status": "ok", "heartbeat_age_s": 0.4, "instance": "worker-7f9-1", "tick": 1834, "vehicles": 3}}}
```
`200` (`ready: true`) when **PostgreSQL** answers, else `503`. Redis and the worker are
reported but do not affect readiness - they set `degraded: true`:

- `checks.redis` is `ok` | `error: <Type>` (probed through the request-path circuit breaker,
  so it answers in at most `DIVERT_REDIS_REQUEST_TIMEOUT_S`). With Redis down the API still
  accepts dispatches (the command waits in the PostgreSQL outbox and is relayed when Redis
  returns) and serves history/analytics; only live views fail fast
  (`503 live_state_unavailable`, WebSocket close `1013`). Taking the API out of rotation
  would make dispatch unavailable too, so readiness deliberately ignores Redis.
- `checks.worker.status` is `ok` | `stale` (heartbeat older than
  `DIVERT_HEARTBEAT_MAX_AGE_S`) | `missing` | `unknown` (Redis down) | `error: ...`
  (dispatches are still accepted; their commands wait in the stream/outbox).

### `GET /metrics`
Prometheus text format. API metrics: `divert_http_requests_total{method,route,status}`,
`divert_http_request_duration_seconds`, `divert_dispatches_total{city,service,outcome}`,
`divert_dispatch_duration_seconds`, `divert_osrm_request_duration_seconds{outcome}`,
`divert_osrm_errors_total{kind}`, `divert_outbox_backlog`, `divert_outbox_relayed_total{path}`,
`divert_active_vehicles{city}`, `divert_worker_heartbeat_age_seconds`,
`divert_history_stream_lag`, `divert_history_stream_pending`, `divert_ws_clients{city}`,
`divert_rate_limited_total`, `divert_redis_circuit_open{guard}` (1 while request-path Redis
calls are short-circuited), `divert_redis_circuit_trips_total{guard}`. Worker (`:9100/metrics`): `divert_tick_duration_seconds`,
`divert_tick_overruns_total`, `divert_worker_is_leader`, `divert_active_vehicles{city}`,
`divert_active_signals{city}`, `divert_preemptions_total{city,state}`,
`divert_preemption_conflicts_total{city}`, `divert_worker_commands_total{type,outcome}`,
`divert_history_events_total{type}`, `divert_preemption_releases_total{city,outcome}`,
`divert_preemption_greens_total{city,kind}` (`green_extend` | `red_truncate`),
`divert_signals_starved{city}`, `divert_legs_planned_total{city,kind}`,
`divert_leg_plan_failures_total{city,kind,reason}`, `divert_leg_plan_duration_seconds{kind}`,
`divert_reroutes_total{city,outcome}` (`replaced` | `kept` | `failed`). The API also exports
`divert_route_candidates` (candidates scored per dispatch). Persister (`:9100/metrics`):
`divert_persister_events_total{type,outcome}`, `divert_persister_lag_seconds`,
`divert_dlq_messages_total{stream}`, `divert_history_stream_lag`, `divert_history_stream_pending`.

## Cities

### `GET /api/v1/cities`
```json
{"cities": [{"id": "delhi", "name": "Delhi", "timezone": "Asia/Kolkata",
             "center": [28.6139, 77.209], "bbox": [28.4, 76.84, 28.88, 77.35], "zoom": 12}]}
```
`bbox` is `[south, west, north, east]`.

### `GET /api/v1/cities/{city}`
City summary plus:
```json
{"traffic": {"hourly_congestion": [0.95, "... 24 values, index = local hour"], "avg_junction_delay_s": 35.0},
 "stations": [{"id": "Pw351374286", "name": "Police Station Connaught Place", "service": "POLICE",
               "location": [28.63, 77.21], "osm": "way/351374286", "total_units": 2, "free_units": 1}],
 "signals": [{"id": "J2800811770", "name": null, "location": [28.61, 77.23], "osm_node_ids": [2800811770, 2800811771]}]}
```
`404 city_not_found` for an unknown city. Unit availability is live; stations/signals are
the seeded OSM snapshot (active only).

## Incidents (dispatch)

### `POST /api/v1/cities/{city}/incidents`
Request:
```json
{"type": "POLICE", "location": [28.6271, 77.2166], "description": "optional, <= 1000 chars"}
```
`type` is case-insensitive: `MEDICAL` (aliases `HEALTH`, `AMBULANCE`), `FIRE`, `CRIME`
(alias `POLICE`). Service: MEDICAL -> AMBULANCE, FIRE -> FIRE_ENGINE, CRIME -> POLICE.

`201 Created`:
```json
{
  "incident": {
    "id": "31e6a6c9-83e3-4f3e-97bf-3954fe3bca02", "city": "delhi", "type": "CRIME",
    "reported_type": "POLICE", "status": "DISPATCHED", "description": null,
    "location": [28.6271, 77.2166],
    "reported_at": "2026-11-17T06:30:00.123+00:00", "dispatched_at": "2026-11-17T06:30:00.123+00:00",
    "en_route_at": null, "on_scene_at": null, "resolved_at": null, "cancelled_at": null,
    "failed_at": null, "failure_reason": null,
    "hospital_station_id": null, "transport_started_at": null, "at_hospital_at": null
  },
  "vehicle": {
    "id": "PCR-Pw351374286-1", "dispatch_id": "6c0f...", "service": "POLICE", "status": "DISPATCHED",
    "station": {"id": "Pw351374286", "name": "Police Station Connaught Place", "location": [28.63, 77.21], "straight_line_m": 812.4},
    "route": [[28.63, 77.21], ["..."], [28.6271, 77.2166]],
    "distance_m": 1090.2, "eta_s": 73.4, "eta_model": "kinematic-v3",
    "corridor": [{"signal_id": "J6720393130", "name": null, "pos": [28.628, 77.214], "along_m": 655.1,
                  "offset_m": 3.2, "lane": "R", "approach_bearing": 171.4, "pass_no": 0,
                  "movement": "RIGHT", "turn_deg": 88.2, "exit_bearing": 259.6, "roundabout_exit": null,
                  "free_left": false, "legs": 4, "junction_bearings": [0.0, 90.0, 180.0, 270.0],
                  "advice": {"movement": "RIGHT", "lane_index": 2, "lanes_total": 2, "side": "R",
                             "advise_at_m": 505.1, "free_left": false, "road_class": "COLLECTOR",
                             "lookahead": false, "text": "Right lane (2 of 2): protected right turn"}}],
    "route_choice": {"station_id": "Pw351374286", "chosen": "A", "tie_break": null,
                     "reason": "Route A: 0.4 min faster than B (B: 1 extra right turn)",
                     "candidates": [{"label": "A", "source": "osrm", "distance_m": 1090.2,
                                     "base_time_s": 61.0, "congested_time_s": 73.4,
                                     "junction_delay_s": 6.0, "penalties_s": 0.0, "cost_s": 79.4,
                                     "crowd_index": 0.31, "signals": 1, "right_turns": 1,
                                     "contention_junctions": [], "incident_zones": [],
                                     "congested_road": "Janpath"}, "..."]}
  }
}
```
`corridor` lists every pass of the route through a junction within `DIVERT_CORRIDOR_M`, in
route order (`pass_no` > 0 when the route crosses the same junction again), with the
movement there and the lane advice (lanes from the kerb; `side` / `lane` is `L`, `C` or
`R` - India drives on the left). `route_choice` explains which of the scored candidates
(OSRM alternatives and detours, all stations) was chosen and why; it is also stored in the
`dispatched` timeline event and returned in the incident detail (`dispatches[].route_choice`
and `legs[0].route_choice`). This response is the **RESPONSE leg** (seq 0) only; the
TRANSPORT and RETURN legs are planned by the worker when the previous leg ends (see
*Trip legs* below).
`eta_s` uses the congestion factor of the city-local hour of the **simulation clock** (the
same clock the worker simulates with; it differs from wall time when
`DIVERT_SIM_TIME_SCALE` != 1), falling back to wall time if the clock is unavailable.
The response does not wait on Redis: the worker command is relayed after the commit
(transactional outbox), so `201` also holds while Redis is down.

Errors: `422 invalid_incident` (unknown type -> `details.valid_types`; outside the city ->
`details.bbox`), `422 validation_error` (body schema), `409 no_unit_available`,
`503 routing_unavailable` (OSRM failed; **nothing was written**), `401`, `429`.
Validation happens before any database, routing or Redis access.

### `DELETE /api/v1/cities/{city}/incidents/{incident_id}`
`200 {"incident": {... "status": "CANCELLED", "cancelled_at": "..."}}`. What happens
depends on the incident's status (design section 1):

| Incident status | Result |
|---|---|
| `DISPATCHED`, `EN_ROUTE` (RESPONSE leg) | `200`, CANCELLED. The worker releases every claim, the vehicle brakes to a stop (3 m/s^2) and drives back to its station from **there** (a RETURN leg: never a teleport). |
| `ON_SCENE` | `200`, CANCELLED. The dwell ends; a TRANSPORT already requested is superseded; RETURN from the scene. |
| `TRANSPORTING`, `AT_HOSPITAL` | `409 cannot_cancel_transport` (`details.status`): a patient is on board. Nothing changes. |
| `CANCELLED`, `RESOLVED` | `200`, unchanged (idempotent): the unit is already driving back, or back. |
| `FAILED` | `409 invalid_state`. |

`404 not_found` (unknown id or other city). The unit stays busy (`RETURNING`) until its
RETURN leg ends; then it is AVAILABLE and the dispatch `COMPLETED` (`end_reason`
`CANCELLED`). The status check uses PostgreSQL, which trails the worker by up to one
persister round: a cancel that reaches the worker after the patient was picked up is
refused there (`cancel_rejected` event) and the incident status is restored.

## Trip legs

A dispatch is a sequence of legs; `seq` numbers them from 0 (gaps are possible: a leg
superseded before it started keeps its number).

| Leg | From -> to | `emergency` (pre-empts signals) | Who |
|---|---|---|---|
| `RESPONSE` (seq 0) | station -> scene | yes | every unit; planned by the API at dispatch |
| `TRANSPORT` | scene -> hospital | yes (patient on board, priority rank 0) | AMBULANCE on a MEDICAL incident |
| `RETURN` | scene / hospital / where it stopped -> its station | **no**: obeys the signals | every unit |

TRANSPORT / RETURN are planned by the worker's LegPlanner when the previous leg ends, with
the traffic at that time; the vehicle dwells meanwhile (scene time per service,
`DIVERT_HOSPITAL_HANDOVER_S` at the hospital). If the plan is not ready when the dwell ends
the vehicle waits in place (`planning: true`); routing failures are retried with backoff
(`plan_error`), never replaced by a straight line. A unit that ends a leg within 30 m of its
own station (e.g. the patient was taken to the ambulance's own hospital) needs no RETURN
leg: it is back in service when the dwell ends.

Statuses: unit `AVAILABLE -> DISPATCHED -> ON_SCENE -> [TRANSPORTING -> AT_HOSPITAL] ->
RETURNING -> AVAILABLE` (+ `OUT_OF_SERVICE`); incident `DISPATCHED -> EN_ROUTE -> ON_SCENE ->
[TRANSPORTING -> AT_HOSPITAL] -> RESOLVED` (+ `CANCELLED`, `FAILED`; MEDICAL resolves at the
handover, FIRE / CRIME when the scene is cleared); dispatch `DISPATCHED -> ON_SCENE ->
[TRANSPORTING -> AT_HOSPITAL] -> RETURNING -> COMPLETED` (or `FAILED`), open until the unit
is AVAILABLE again.

## History (PostgreSQL)

### `GET /api/v1/cities/{city}/incidents`
Query: `status` (repeat or comma list of OPEN, DISPATCHED, EN_ROUTE, ON_SCENE, TRANSPORTING,
AT_HOSPITAL, RESOLVED, CANCELLED, FAILED), `type` (aliases accepted), `from`, `to` (ISO 8601 on `reported_at`,
`from` inclusive, `to` exclusive; naive = UTC), `limit` 1..200 (default 50), `cursor`.
```json
{"items": [{"id": "...", "city": "delhi", "type": "CRIME", "status": "RESOLVED", "...": "incident fields as above"}],
 "next_cursor": "WyIyMDI2LTExLTE3VDA2OjMwOjAwKzAwOjAwIiwgIjMxZTYuLi4iXQ"}
```
Newest first; keyset pagination on `(reported_at, id)`: pass `next_cursor` back as
`cursor`; `null` = last page. Bad status/type/cursor -> `422 validation_error`.

### `GET /api/v1/incidents/{incident_id}`
Incident fields (incl. `hospital_station_id`, `transport_started_at`, `at_hospital_at`) plus:
```json
{"dispatches": [{"id": "...", "vehicle_id": "AMB-...", "service": "AMBULANCE", "status": "COMPLETED",
                 "station": {"id": "...", "name": "...", "location": [28.63, 77.21]},
                 "route": [[28.63, 77.21], "..."], "distance_m": 1090.2,
                 "predicted_eta_s": 73.4, "actual_travel_s": 75.0, "est_time_saved_s": 35.0,
                 "eta_model": "kinematic-v3", "corridor": ["corridor entries as above"], "route_choice": {"...": "as above"},
                 "dispatched_at": "...", "arrived_at": "...", "ended_at": "...", "end_reason": "CLEARED"}],
 "legs": [{"dispatch_id": "...", "seq": 1, "kind": "TRANSPORT", "emergency": true,
           "origin": [28.6271, 77.2166],
           "destination": {"kind": "HOSPITAL", "station_id": "Hn123", "name": "AIIMS", "location": [28.567, 77.21]},
           "route": [[28.6271, 77.2166], "..."], "distance_m": 6120.4,
           "predicted_eta_s": 512.3, "actual_travel_s": 520.0, "eta_error_pct": 1.5,
           "corridor": ["corridor entries as above"], "route_choice": {"...": "as above"},
           "hospital_choice": {"chosen": "Hn123", "reason": "AIIMS: confirmed emergency department, 1.2 min more than ...",
                               "ed_tolerance_s": 640.4,
                               "candidates": [{"station_id": "Hn123", "name": "AIIMS", "location": [28.567, 77.21],
                                               "emergency": "yes", "healthcare": "hospital", "straight_line_m": 5300.2,
                                               "cost_s": 584.2, "eta_s": 512.3, "distance_m": 6120.4,
                                               "route": "A", "error": null}, "..."]},
           "eta_model": "kinematic-v3", "planned_at": "...", "started_at": "...", "ended_at": "...",
           "end_reason": "ARRIVED", "reroutes": 0, "started_trip_s": 812.5, "ended_trip_s": 1332.5}],
 "hospital": {"id": "Hn123", "name": "AIIMS", "location": [28.567, 77.21], "emergency": "yes", "healthcare": "hospital"},
 "preemptions": [{"event_id": "clm:6c0f...:J6720393130", "dispatch_id": "...", "signal_id": "J6720393130",
                  "state": "PREEMPTED", "lane": "R", "approach_bearing": 171.4, "eta_at_claim_s": 44.6,
                  "claimed_at": "...", "released_at": "...", "leg_seq": 0, "movement": "RIGHT",
                  "lane_index": 2, "lanes_total": 2, "free_left": false, "required_lead_s": 21.4,
                  "transition_s": 9.0, "queue_clear_s": 7.4, "green_at": "...", "crossed_at": "...",
                  "waited_s": 0.0, "outcome": "CLEARED", "phase_state_at_claim": "red_truncate"}],
 "timeline": [{"event_id": "rep:31e6...", "dispatch_id": null, "type": "reported", "ts": "...", "payload": {}},
              {"type": "dispatched"}, {"type": "en_route"}, {"type": "preemption_claimed"},
              {"type": "preemption_released"}, {"type": "arrived"}, {"type": "leg_planned"},
              {"type": "leg_ended"}, {"type": "leg_started"}, {"type": "transport_started"},
              {"type": "arrived_hospital"}, {"type": "handover_complete"}, {"type": "returning"},
              {"type": "back_in_service"}]}
```
`dispatches[]` keep the RESPONSE leg's route / ETA (as before); `status` is the whole trip's.
`legs[]` lists every leg of every dispatch, ordered by (dispatch, `seq`): `destination.kind`
is `SCENE` | `HOSPITAL` | `STATION`; `end_reason` `ARRIVED` | `CANCELLED` | `FAILED` |
`SUPERSEDED` (planned, never started) or null while driven; `actual_travel_s` and
`*_trip_s` are **simulated** seconds (`*_trip_s` since the dispatch started);
`eta_error_pct` = (actual - predicted) / actual x 100 (the +-10 % guarantee);
`hospital_choice` only on TRANSPORT (every hospital considered, the chosen one and why);
`reroutes` counts applied re-routes (the route is then the spliced one).
`hospital` is the incident's destination hospital (null unless transported).
Preemption `state` is `PREEMPTED`, or `CONFLICT` if the claim lost a conflict at any time;
`lane` is `L` | `C` | `R`; `outcome` `CLEARED` | `TIMEOUT` | `CANCELLED` | `REROUTED`;
`phase_state_at_claim` `green_extend` | `red_truncate`; `leg_seq` is the leg the claim was on.
`est_time_saved_s` is an **estimate** (pre-empted junctions passed x city average delay).
Timeline types: `reported`, `dispatched`, `cancel_requested`, `en_route`,
`preemption_claimed`, `preemption_conflict`, `preempt_transition`, `preempt_green`,
`preempt_timeout`, `preemption_released`, `arrived`, `cleared`, `leg_planned`,
`leg_plan_failed`, `leg_started`, `leg_ended`, `rerouted`, `transport_started`,
`arrived_hospital`, `handover_complete`, `returning`, `back_in_service`, `cancelled`,
`cancel_rejected`, `failed`. `404 not_found`.

### `GET /api/v1/incidents/{incident_id}/track?max_points=500`
```json
{"incident_id": "...", "points": [{"dispatch_id": "...", "ts": "...", "pos": [28.63, 77.21], "speed_mps": 8.2, "along_m": 120.5, "leg_seq": 0}]}
```
Positions sampled every `DIVERT_POSITION_SAMPLE_S` simulated seconds, evenly thinned to at
most `max_points` (2..5000; first and last kept). Retained `DIVERT_POSITION_RETENTION_DAYS`.
`along_m` is along the leg `leg_seq` (each leg starts at 0).

### `GET /api/v1/cities/{city}/analytics/summary?from=&to=`
Window on `reported_at` (default: last 24 h; max 366 days).
```json
{"city": "delhi", "from": "...", "to": "...",
 "incidents": {"total": 12, "by_type": {"MEDICAL": 7, "FIRE": 2, "CRIME": 3},
               "by_status": {"OPEN": 0, "DISPATCHED": 0, "EN_ROUTE": 1, "ON_SCENE": 0, "TRANSPORTING": 0,
                             "AT_HOSPITAL": 0, "RESOLVED": 10, "CANCELLED": 1, "FAILED": 0}},
 "response_time_s": {"MEDICAL": {"count": 6, "p50_s": 312.0, "p90_s": 540.2, "mean_s": 330.1}},
 "dispatches": {"total": 12, "arrived": 10, "avg_predicted_eta_s": 301.2, "avg_actual_travel_s": 309.8, "eta_mape": 0.031,
                "by_status": {"DISPATCHED": 0, "ON_SCENE": 0, "TRANSPORTING": 0, "AT_HOSPITAL": 0,
                              "RETURNING": 1, "COMPLETED": 11, "FAILED": 0}},
 "transport_time_s": {"count": 5, "p50_s": 480.0, "p90_s": 700.1, "mean_s": 501.2},
 "call_to_hospital_s": {"count": 5, "p50_s": 1460.3, "p90_s": 1802.0, "mean_s": 1495.0},
 "handover_to_back_in_service_s": {"count": 5, "p50_s": 1010.0, "p90_s": 1350.4, "mean_s": 1080.2},
 "hospitals": [{"station_id": "Hn123", "name": "AIIMS", "count": 3}],
 "legs": {"RESPONSE": {"count": 10, "eta_mape": 0.021, "eta_max_error": 0.064},
          "TRANSPORT": {"count": 5, "eta_mape": 0.018, "eta_max_error": 0.041},
          "RETURN": {"count": 9, "eta_mape": 0.012, "eta_max_error": 0.035}},
 "free_left_passed": 3,
 "reroutes": 1,
 "preemptions": {"total": 41, "conflicts": 2, "distinct_signals": 30, "top_signals": [{"signal_id": "J...", "count": 4}],
                 "outcomes": {"CLEARED": 38, "TIMEOUT": 1, "CANCELLED": 2, "REROUTED": 0},
                 "green_extension": 9, "red_truncation": 30},
 "est_time_saved_s": {"total": 1225.0, "note": "estimate: pre-empted junctions passed x city average junction delay"}}
```
Response time = (dispatch time - report time) + simulated travel time of the **RESPONSE leg**,
percentiles via `percentile_cont`. Only types with arrivals appear in `response_time_s`.
Simulated seconds throughout: `transport_time_s` = TRANSPORT legs that arrived;
`call_to_hospital_s` = (dispatch - report) + simulated time from dispatch to arrival at the
hospital; `handover_to_back_in_service_s` = from `handover_complete` to `back_in_service`
(also when no RETURN leg was needed). `legs` = predicted-vs-actual per leg kind (legs that
arrived without a re-route; `eta_mape` mean, `eta_max_error` worst |actual - predicted| /
actual). `hospitals` = where patients were taken (top 20). `free_left_passed` = free-left
junction passes on driven legs (never claimed); `reroutes` = re-routes applied.
`green_extension` / `red_truncation` = claims whose phase was already green / made green.

## Live state (Redis, written by the worker leader)

While Redis is unreachable these endpoints answer immediately with
`503 live_state_unavailable` (`Retry-After: 5`) instead of waiting on timeouts.

### `GET /api/v1/cities/{city}/vehicles?include_route=false`
```json
{"city": "delhi", "tick": 1834, "sim_ts": "...",
 "vehicles": [{"id": "AMB-...", "dispatch_id": "...", "incident_id": "...", "service": "AMBULANCE",
               "station_id": "...", "incident_type": "MEDICAL", "status": "EN_ROUTE",
               "phase": "TRANSPORTING", "leg": {"seq": 1, "kind": "TRANSPORT", "emergency": true},
               "pos": [28.629, 77.213], "heading": 171.4, "speed_mps": 9.7, "eta_s": 41.2,
               "predicted_eta_s": 73.4, "along_m": 580.2, "distance_m": 1090.2, "elapsed_s": 32.5,
               "started_at": "...", "destination": [28.567, 77.21],
               "destination_info": {"kind": "HOSPITAL", "station_id": "Hn123", "name": "AIIMS",
                                    "location": [28.567, 77.21]},
               "claims": ["J6720393130"], "preempted_passed": 1, "emergency": true,
               "leg_kind": "TRANSPORT", "rank": 0,
               "next_movement": {"signal_id": "J6720393130", "pass_no": 0, "movement": "RIGHT",
                                 "lane_index": 2, "lanes_total": 2, "side": "R",
                                 "text": "Right lane (2 of 2): protected right turn",
                                 "advise_at_m": 505.1, "distance_m": 74.9, "free_left": false},
               "speed_advisory_mps": null, "planning": false, "plan_error": null,
               "incident_location": [28.6271, 77.2166], "home": [28.63, 77.21],
               "hospital": {"id": "Hn123", "name": "AIIMS", "location": [28.567, 77.21], "emergency": "yes"},
               "dwell_s": 0.0, "dwell_target_s": 0.0, "trip_s": 845.0, "reroutes": 0}]}
```
- `status` (legacy, two values): `EN_ROUTE` while driving a leg, `ON_SCENE` while stopped
  (on scene, at the hospital, or waiting for a plan). `phase` is the unit status:
  `DISPATCHED` | `ON_SCENE` | `TRANSPORTING` | `AT_HOSPITAL` | `RETURNING`.
- `leg` is the leg being driven (or the last one, while stopped); `leg_kind` / `emergency`
  repeat it (legacy). `along_m`, `distance_m`, `elapsed_s`, `eta_s`, `predicted_eta_s` are
  per leg; `trip_s` is simulated seconds since the dispatch started.
- `destination` stays `[lat, lng]` (the current leg's end, v1 type); `destination_info` is
  `{kind: SCENE | HOSPITAL | STATION, station_id, name, location}`.
- `planning: true` = stopped, dwell over, waiting for the next leg's plan ("planning
  route"); `plan_error` = the last planning failure while it retries (else null).
- `rank` = conflict priority (0 ambulance with a patient, 1 ambulance, 2 fire, 3 police).
- `hospital` is set on TRANSPORT (and kept after); `dwell_s` / `dwell_target_s` count the
  on-scene / handover dwell; `reroutes` = re-routes applied on the current leg.
- `next_movement` is the next junction ahead while driving (null when none or stopped);
  `speed_advisory_mps` is set while the vehicle must slow for its green (0 = stop at the
  stop line). Both are also in every `tick` vehicle.
- `preempted_passed` = corridor junctions passed while pre-empted for it, whole trip (live
  est. time saved = `preempted_passed` x `traffic.avg_junction_delay_s`).
- With `include_route=true` each vehicle also has `route` (`[[lat, lng], ...]`) and
  `corridor` of the **current leg**.

### `GET /api/v1/cities/{city}/signals/active`
Only non-NORMAL signals:
```json
{"city": "delhi", "signals": [{"signal_id": "J6720393130", "state": "QUEUED", "pos": [28.628, 77.214],
  "vehicle_id": "PCR-...", "service": "POLICE", "lane": "R", "approach_bearing": 171.4, "eta_s": 12.3,
  "phase_info": {"served_phase": 2, "phases": 4, "cycle_s": 120.0, "geometry": "osrm",
                 "transition_s_left": 0.0, "expected_green_at": "...", "hold_s": 8.5,
                 "state_at_claim": "red_truncate"},
  "claimants": [{"vehicle_id": "PCR-...", "service": "POLICE", "eta_s": 12.3, "state": "PREEMPTED",
                 "phase_state": "PREEMPTED", "movement": "RIGHT", "rank": 3, "lane": "R",
                 "lane_index": 2, "lanes_total": 2, "phase": 2, "queue_pos": 0, "expected_green_at": "..."},
                {"vehicle_id": "FIRE-...", "service": "FIRE_ENGINE", "eta_s": 19.0, "state": "CONFLICT",
                 "phase_state": "QUEUED", "movement": "STRAIGHT", "rank": 2, "lane": "R",
                 "lane_index": 2, "lanes_total": 2, "phase": 0, "queue_pos": 1, "expected_green_at": "..."}]}]}
```
`state` (junction): `TRANSITIONING` (clearing toward the served phase; `transition_s_left`),
`PREEMPTED` (served phase held green; `hold_s`), `QUEUED` (held, and an incompatible
claimant waits), `RECOVERING` (released, back to its plan soon; no claimants, the other
fields are the last winner's). Claimant `state` keeps the legacy per-vehicle label:
`PREEMPTED` (served) or `CONFLICT` (queued); `phase_state` is the claimant's own view.

## WebSocket `GET /api/v1/cities/{city}/stream`

Server -> client JSON text frames; every message has `type` and `v` (schema version, 1).
Clients must ignore unknown `type`s and fields.

| `type` | When | Fields |
|---|---|---|
| `snapshot` | first message after connect | `city`, `tick`, `sim_ts`, `vehicles` (as `/vehicles?include_route=true`), `signals` (as `/signals/active`) |
| `tick` | every worker tick while the city has vehicles (0.5 s) | `city`, `tick`, `sim_ts`, `ts`, `vehicles` [`id`, `dispatch_id`, `incident_id`, `service`, `status`, `phase`, `leg`, `destination`, `destination_info`, `planning`, `pos`, `heading`, `speed_mps`, `eta_s`, `preempted_passed`, `next_movement`, `speed_advisory_mps`, `along_m`, `distance_m`], `signals` (the complete non-NORMAL set, as above; a signal missing from it is NORMAL) |
| `vehicle_added` | a vehicle starts, **and again at the start of every later leg** (older clients redraw the route) | `city`, `vehicle` (tick fields + `station_id`, `incident_type`, `predicted_eta_s`, `started_at`), `route`, `corridor`, `destination` (`[lat, lng]`) of the current leg |
| `leg_changed` | a TRANSPORT / RETURN leg starts | `city`, `vehicle_id`, `dispatch_id`, `incident_id`, `phase`, `leg` {`seq`, `kind`, `emergency`}, `destination` (`[lat, lng]`), `destination_info`, `predicted_eta_s`, `route`, `corridor`, `hospital` (TRANSPORT: {`id`, `name`, `location`, `emergency`}, else null) |
| `route_replaced` | a re-route was applied (emergency legs) | `city`, `vehicle_id`, `dispatch_id`, `incident_id`, `leg`, `reroute_no` (1..2), `reason`, `saved_s`, `route` (the whole spliced route of the leg), `corridor` |
| `vehicle_removed` | back in service, or failed | `city`, `vehicle_id`, `dispatch_id`, `incident_id`, `reason` (`back_in_service` \| `failed`) |
| `incident_updated` | DISPATCHED, EN_ROUTE, ON_SCENE, TRANSPORTING, AT_HOSPITAL, RESOLVED, CANCELLED, FAILED | `city`, `incident_id`, `dispatch_id`, `vehicle_id`, `status`, `ts`, optional `incident`, `eta_s`, `travel_s`, `hospital`, `reason` (a refused cancel re-sends the real status with `reason` `cancel refused: ...`) |
| `pong` | reply to client `{"type": "ping"}` | - |

A vehicle stays on the map (and in `/vehicles`) for its whole trip - dwells, transport
and the drive back - until `vehicle_removed`. RETURN legs never claim signals.

`tick` increases by one per worker tick and stays monotonic across worker leader
changes (a new leader resumes from the last committed `divert:{city}:tick`). It is not a
dense sequence: ticks of a city with no vehicles are not sent, and it restarts at 1 only
if Redis lost its data. Order by (`tick`, `sim_ts`) if you need both properties.

Close codes: `4404` unknown city; `1013` try again (too many clients, slow consumer,
missed messages after a Redis reconnect, or live state - Redis - unavailable) - reconnect
with backoff to receive a fresh snapshot.
