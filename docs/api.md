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
| 409 | `no_unit_available`, `invalid_state` |
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
`divert_history_events_total{type}`. Persister (`:9100/metrics`):
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
    "failed_at": null, "failure_reason": null
  },
  "vehicle": {
    "id": "PCR-Pw351374286-1", "dispatch_id": "6c0f...", "service": "POLICE", "status": "DISPATCHED",
    "station": {"id": "Pw351374286", "name": "Police Station Connaught Place", "location": [28.63, 77.21], "straight_line_m": 812.4},
    "route": [[28.63, 77.21], ["..."], [28.6271, 77.2166]],
    "distance_m": 1090.2, "eta_s": 73.4, "eta_model": "kinematic-v1",
    "corridor": [{"signal_id": "J6720393130", "name": null, "pos": [28.628, 77.214], "along_m": 655.1,
                  "offset_m": 3.2, "lane": "R", "approach_bearing": 171.4}]
  }
}
```
`corridor` lists the junctions within `DIVERT_CORRIDOR_M` of the route in route order;
`lane` is the advisory (`L` left turn, `R` straight/right/U - India drives on the left).
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
`200 {"incident": {... "status": "CANCELLED", "cancelled_at": "..."}}`. Idempotent (a
cancelled incident returns 200 again). `404 not_found` (unknown id or other city),
`409 invalid_state` if already RESOLVED/FAILED. The worker recalls the vehicle
(`vehicle_removed`, reason `cancelled`) and the unit becomes AVAILABLE once that is
persisted.

## History (PostgreSQL)

### `GET /api/v1/cities/{city}/incidents`
Query: `status` (repeat or comma list of OPEN, DISPATCHED, EN_ROUTE, ON_SCENE, RESOLVED,
CANCELLED, FAILED), `type` (aliases accepted), `from`, `to` (ISO 8601 on `reported_at`,
`from` inclusive, `to` exclusive; naive = UTC), `limit` 1..200 (default 50), `cursor`.
```json
{"items": [{"id": "...", "city": "delhi", "type": "CRIME", "status": "RESOLVED", "...": "incident fields as above"}],
 "next_cursor": "WyIyMDI2LTExLTE3VDA2OjMwOjAwKzAwOjAwIiwgIjMxZTYuLi4iXQ"}
```
Newest first; keyset pagination on `(reported_at, id)`: pass `next_cursor` back as
`cursor`; `null` = last page. Bad status/type/cursor -> `422 validation_error`.

### `GET /api/v1/incidents/{incident_id}`
Incident fields plus:
```json
{"dispatches": [{"id": "...", "vehicle_id": "PCR-...", "service": "POLICE",
                 "station": {"id": "...", "name": "...", "location": [28.63, 77.21]},
                 "route": [[28.63, 77.21], "..."], "distance_m": 1090.2,
                 "predicted_eta_s": 73.4, "actual_travel_s": 75.0, "est_time_saved_s": 35.0,
                 "eta_model": "kinematic-v1", "corridor": ["corridor entries as above"],
                 "dispatched_at": "...", "arrived_at": "...", "ended_at": "...", "end_reason": "CLEARED"}],
 "preemptions": [{"event_id": "clm:6c0f...:J6720393130", "dispatch_id": "...", "signal_id": "J6720393130",
                  "state": "PREEMPTED", "lane": "R", "approach_bearing": 171.4, "eta_at_claim_s": 44.6,
                  "claimed_at": "...", "released_at": "..."}],
 "timeline": [{"event_id": "rep:31e6...", "dispatch_id": null, "type": "reported", "ts": "...", "payload": {}},
              {"type": "dispatched"}, {"type": "en_route"}, {"type": "preemption_claimed"},
              {"type": "preemption_released"}, {"type": "arrived"}, {"type": "cleared"}]}
```
`state` is `PREEMPTED`, or `CONFLICT` if the claim lost a conflict at any time.
`est_time_saved_s` is an **estimate** (pre-empted junctions passed x city average delay).
Timeline types: `reported`, `dispatched`, `cancel_requested`, `en_route`,
`preemption_claimed`, `preemption_conflict`, `preemption_released`, `arrived`, `cleared`,
`cancelled`, `failed`. `404 not_found`.

### `GET /api/v1/incidents/{incident_id}/track?max_points=500`
```json
{"incident_id": "...", "points": [{"dispatch_id": "...", "ts": "...", "pos": [28.63, 77.21], "speed_mps": 8.2, "along_m": 120.5}]}
```
Positions sampled every `DIVERT_POSITION_SAMPLE_S` simulated seconds, evenly thinned to at
most `max_points` (2..5000; first and last kept). Retained `DIVERT_POSITION_RETENTION_DAYS`.

### `GET /api/v1/cities/{city}/analytics/summary?from=&to=`
Window on `reported_at` (default: last 24 h; max 366 days).
```json
{"city": "delhi", "from": "...", "to": "...",
 "incidents": {"total": 12, "by_type": {"MEDICAL": 7, "FIRE": 2, "CRIME": 3},
               "by_status": {"OPEN": 0, "DISPATCHED": 0, "EN_ROUTE": 1, "ON_SCENE": 0, "RESOLVED": 10, "CANCELLED": 1, "FAILED": 0}},
 "response_time_s": {"MEDICAL": {"count": 6, "p50_s": 312.0, "p90_s": 540.2, "mean_s": 330.1}},
 "dispatches": {"total": 12, "arrived": 10, "avg_predicted_eta_s": 301.2, "avg_actual_travel_s": 309.8, "eta_mape": 0.031},
 "preemptions": {"total": 41, "conflicts": 2, "distinct_signals": 30, "top_signals": [{"signal_id": "J...", "count": 4}]},
 "est_time_saved_s": {"total": 1225.0, "note": "estimate: pre-empted junctions passed x city average junction delay"}}
```
Response time = (dispatch time - report time) + simulated travel time to the scene,
percentiles via `percentile_cont`. Only types with arrivals appear in `response_time_s`.

## Live state (Redis, written by the worker leader)

While Redis is unreachable these endpoints answer immediately with
`503 live_state_unavailable` (`Retry-After: 5`) instead of waiting on timeouts.

### `GET /api/v1/cities/{city}/vehicles?include_route=false`
```json
{"city": "delhi", "tick": 1834, "sim_ts": "...",
 "vehicles": [{"id": "PCR-...", "dispatch_id": "...", "incident_id": "...", "service": "POLICE",
               "station_id": "...", "incident_type": "CRIME", "status": "EN_ROUTE",
               "pos": [28.629, 77.213], "heading": 171.4, "speed_mps": 9.7, "eta_s": 41.2,
               "predicted_eta_s": 73.4, "along_m": 580.2, "distance_m": 1090.2, "elapsed_s": 32.5,
               "started_at": "...", "destination": [28.6271, 77.2166], "claims": ["J6720393130"],
               "preempted_passed": 1}]}
```
`status`: `EN_ROUTE` | `ON_SCENE`. `preempted_passed` = corridor junctions this vehicle has
passed while they were pre-empted for it (live est. time saved = `preempted_passed` x
`traffic.avg_junction_delay_s`). With `include_route=true` each vehicle also has
`route` (`[[lat, lng], ...]`) and `corridor`.

### `GET /api/v1/cities/{city}/signals/active`
Only non-NORMAL signals:
```json
{"city": "delhi", "signals": [{"signal_id": "J6720393130", "state": "PREEMPTED", "pos": [28.628, 77.214],
  "vehicle_id": "PCR-...", "service": "POLICE", "lane": "R", "approach_bearing": 171.4, "eta_s": 12.3,
  "claimants": [{"vehicle_id": "PCR-...", "service": "POLICE", "eta_s": 12.3, "state": "PREEMPTED"}]}]}
```
`state`: `PREEMPTED` (one claimant) or `CONFLICT` (contested; `vehicle_id` is the winner,
the others' claimant `state` is `CONFLICT`).

## WebSocket `GET /api/v1/cities/{city}/stream`

Server -> client JSON text frames; every message has `type` and `v` (schema version, 1).
Clients must ignore unknown `type`s and fields.

| `type` | When | Fields |
|---|---|---|
| `snapshot` | first message after connect | `city`, `tick`, `sim_ts`, `vehicles` (as `/vehicles?include_route=true`), `signals` (as `/signals/active`) |
| `tick` | every worker tick while the city has vehicles (0.5 s) | `city`, `tick`, `sim_ts`, `ts`, `vehicles` [`id`, `dispatch_id`, `incident_id`, `service`, `status`, `pos`, `heading`, `speed_mps`, `eta_s`, `along_m`, `distance_m`, `preempted_passed`], `signals` (the complete non-NORMAL set, as above; a signal missing from it is NORMAL) |
| `vehicle_added` | vehicle starts | `city`, `vehicle` (tick fields + `station_id`, `incident_type`, `predicted_eta_s`, `started_at`), `route`, `corridor`, `destination` |
| `vehicle_removed` | cleared / cancelled / failed | `city`, `vehicle_id`, `dispatch_id`, `incident_id`, `reason` |
| `incident_updated` | DISPATCHED, EN_ROUTE, ON_SCENE, RESOLVED, CANCELLED, FAILED | `city`, `incident_id`, `dispatch_id`, `vehicle_id`, `status`, `ts`, optional `incident`, `eta_s`, `travel_s`, `reason` |
| `pong` | reply to client `{"type": "ping"}` | - |

`tick` increases by one per worker tick and stays monotonic across worker leader
changes (a new leader resumes from the last committed `divert:{city}:tick`). It is not a
dense sequence: ticks of a city with no vehicles are not sent, and it restarts at 1 only
if Redis lost its data. Order by (`tick`, `sim_ts`) if you need both properties.

Close codes: `4404` unknown city; `1013` try again (too many clients, slow consumer,
missed messages after a Redis reconnect, or live state - Redis - unavailable) - reconnect
with backoff to receive a fresh snapshot.
