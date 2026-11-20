# 01 · Specification

**What this covers:** what DIVERT is for and what it must do. It states the purpose, the problem, the users, the scope, the three cities and their data, numbered functional requirements (FR) and non-functional requirements (NFR) each traced to the code that implements it and the test that proves it, a one-line-per-endpoint API summary, the acceptance criteria (including the three regression guarantees), and the known limits. The exact rules behind each requirement are in [Domain rules](02-domain-rules.md). Code pointers are `path` + symbol; test pointers are `file::test_name`.

## 1. Purpose and vision

**DIVERT (Dynamic Intelligent Vehicle Emergency Routing & Traffic): because every second counts** ([vision.md](../vision.md)).

Emergency vehicles lose critical minutes in urban traffic. DIVERT links emergency-vehicle dispatch with the traffic-signal network to create a **green corridor that moves with the vehicle**. When an incident is reported, it sends the fastest free unit of the right service, routes it on real streets, and pre-empts each signalised junction on the route just before the vehicle reaches it. It releases each junction right after the vehicle passes.

As built today, DIVERT is a **simulator and operations console**: vehicles, their movement and the signal heads are simulated; stations and junctions are real OpenStreetMap data for Delhi, Kolkata and Bengaluru. The vision's later stages (real telemetry, physical signal controllers, learned ETAs) are future scope ([section 10](#10-known-limits-and-future-scope)).

## 2. The problem

| Problem | How DIVERT addresses it (as built) |
|---|---|
| Emergency vehicles queue at red lights and in congestion | Pre-empts every corridor junction ahead of the vehicle (claim at ETA ≤ 45 s or ≤ 600 m) and releases it 20 m after the vehicle passes |
| A static "route lock" holds signals far longer than needed | A rolling claim per junction, owned per vehicle, released as soon as it is passed and always on arrival, cancel or error |
| Two emergency vehicles can need the same junction | Deterministic conflict resolution: lowest ETA wins; within 5 s, AMBULANCE > FIRE_ENGINE > POLICE |
| Static ETAs ignore congestion and vehicle type | A physics-based ETA from OSRM segment speeds, hourly city congestion, an emergency advantage, per-vehicle acceleration and braking, and junction slow-downs |
| Other traffic does not know which lane to clear | A lane advisory per junction for India's left-hand traffic |
| Nobody can tell afterwards what happened or what it saved | Full incident history (timeline, route, pre-emptions, sampled track), analytics, and an "estimated time saved" figure |

## 3. Users and personas

There is no user or role model in the code: one optional shared API key protects writes, and reads are open ([NFR-07](#7-non-functional-requirements)). The personas below describe who the console, API and test suites serve.

| Persona | Goals | Main surfaces |
|---|---|---|
| **Dispatcher / ops operator** | Report an incident by clicking the map, see which unit goes and when it arrives, watch junctions turn green, cancel a call, review past incidents and city statistics | Ops console: **Live** tab (map, dispatch panel, incidents with cancel, fleet, stats, stream status), **History** tab (filters, detail drawer, track replay), **Analytics** tab ([10 · Frontend codebase](10-codebase-frontend.md)) |
| **Ops / platform engineer** | Keep the stack healthy, follow failovers, drain backlogs | `/healthz`, `/readyz`, `/metrics`, the `divert` CLI (`migrate`, `seed`, `fetch-data`, `dlq`), [runbook.md](../runbook.md) |
| **Tester / QA** | Reproduce scenarios, check the rules and guarantees, drive the API directly | REST + WebSocket API ([api.md](../api.md)), `DIVERT_SIM_TIME_SCALE` to speed up time, backend unit/integration/network suites, frontend Vitest suite ([08 · Engineering workflows](08-engineering-workflows.md)) |
| **Developer** | Change rules safely | this bank, [09 · Backend codebase](09-codebase-backend.md) |

## 4. Scope

### In scope (implemented)

- Three cities: Delhi, Kolkata, Bengaluru (`DIVERT_CITIES`, `Settings.cities` in `backend/src/divert/config.py`).
- Incident types MEDICAL, FIRE, CRIME (with aliases), answered by AMBULANCE, FIRE_ENGINE, POLICE units.
- One unit per incident, chosen among the 3 nearest stations with a free unit, ranked by predicted ETA.
- Road routing through OSRM; corridor detection with PostGIS; lane advisory.
- A live simulation of every dispatched vehicle (0.5 s tick), signal pre-emption and conflict resolution.
- Lifecycle to the scene and back to availability; operator cancellation.
- Durable history (PostgreSQL/PostGIS), analytics, a live WebSocket stream, and an ops console.
- High availability for the simulation (leader + hot standby) and survival of Redis outages for dispatch.
- Reference data from real OpenStreetMap snapshots, refreshable with a command.

### Out of scope (not implemented)

| Not implemented | Evidence |
|---|---|
| Real vehicle telemetry (GPS, transponders) | vehicles are simulated (`SimulationEngine`); [vision.md](../vision.md) §2 "simulated today" |
| Control of physical signal controllers or LED lane boards (IoT/MQTT) | signal states exist only in Redis and history; vision "Future scope" |
| Simulation of other traffic or of red-light stops | congestion is one city-wide hourly factor; a non-pre-empted junction is a 15 km/h slow-down ([Domain rules §5.3](02-domain-rules.md#53-junction-caps)) |
| Multi-unit dispatch, manual unit choice, or adding units to an incident | `DispatchService._dispatch` creates exactly one dispatch; no endpoint adds another |
| Mid-route rerouting | the route is fixed at dispatch; vision "adaptive rerouting" is future scope |
| A learned ETA model | `EtaModel` protocol exists; only `KinematicEtaModel` ships |
| User accounts, roles, per-user audit | a single optional `DIVERT_API_KEY` |
| Real fleet data | units are 2 per station by assumption (`DIVERT_UNITS_PER_STATION`) |
| Emergency-vehicle road exemptions (wrong-way, medians) | OSRM car profile ([simulation-model.md](../simulation-model.md) §9) |

## 5. Cities and reference data

All values below come from the committed snapshots `backend/src/divert/cities/data/<city>.json` (`bbox`, `center`, `zoom`, `timezone`, `meta.counts`, `meta.fetched_at`, `meta.osm_base`, `meta.queries`) and were re-counted from the `stations` and `signals` arrays.

| | Delhi | Kolkata | Bengaluru |
|---|---|---|---|
| City id | `delhi` | `kolkata` | `bengaluru` |
| Bounding box (S, W, N, E) | 28.40, 76.84, 28.88, 77.35 | 22.45, 88.25, 22.65, 88.50 | 12.83, 77.46, 13.14, 77.78 |
| Centre (lat, lng) / map zoom | 28.6139, 77.209 / 12 | 22.5726, 88.3639 / 13 | 12.9716, 77.5946 / 12 |
| Time zone | Asia/Kolkata | Asia/Kolkata | Asia/Kolkata |
| Overpass sub-queries (tiles ≤ 0.25°) | 6 | 1 | 4 |
| OSM `traffic_signals` nodes | 1276 | 67 | 1385 |
| Junctions (40 m clusters) | 500 | 28 | 576 |
| Junctions with more than one node | 366 | 18 | 410 |
| Junctions with a name | 2 | 0 | 27 |
| AMBULANCE bases (named hospitals) | 847 | 294 | 1086 |
| FIRE_ENGINE stations | 42 | 7 | 22 |
| POLICE stations | 242 | 44 | 134 |
| Stations in total | 1131 | 345 | 1242 |
| Units seeded (stations × 2, derived) | 2262 | 690 | 2484 |
| Average junction delay | 35 s | 40 s | 40 s |
| `meta.fetched_at` (UTC) | 2026-11-16T20:41:07 | 2026-11-16T20:34:27 | 2026-11-16T20:37:14 |
| `meta.osm_base` (UTC) | 2026-11-16T20:39:38 | 2026-11-16T20:33:36 | 2026-11-16T20:35:36 |

**Provenance.** Signals and stations come from OpenStreetMap through the Overpass API (`meta.source`; endpoints in `meta.endpoints`; the exact queries in `meta.queries`), fetched by `backend/src/divert/cities/osm.py` (`divert fetch-data`, `make fetch-data`). Processing rules (`cluster_signals`, `extract_stations`, `CLUSTER_RADIUS_M = 40.0`, `_DUPLICATE_STATION_M = 150.0`, `TILE_DEG = 0.25`):

- `highway=traffic_signals` nodes are clustered by single linkage within 40 m into one junction, id `J<smallest member node id>`; all member node ids are kept.
- `amenity=hospital` → AMBULANCE base (**named hospitals only**), `amenity=fire_station` → FIRE_ENGINE, `amenity=police` → POLICE. A named facility mapped twice (node + area within 150 m) is kept once.
- If Overpass cannot answer, the fetch fails and writes nothing (`test_fetch_writes_nothing_when_overpass_is_down`).

**Licence.** `meta.licence` = "ODbL - (c) OpenStreetMap contributors". The snapshots are a Derivative Database under the ODbL and are shared under the same licence ([data-sources.md](../data-sources.md)). The console shows OSM attribution on the map (`frontend/src/config.ts` `config.tiles.attribution`). OSRM itself is BSD-2-Clause ([infra/osrm/README.md](../../infra/osrm/README.md)).

**Not from OSM.** `traffic.hourly_congestion` and `traffic.avg_junction_delay_s` are hand-set approximations (`meta.traffic_profile_note`), listed in [Domain rules §5.2](02-domain-rules.md#52-hourly-congestion-factors). Centres and zooms are presentation defaults.

**Coverage caveat.** OSM tags only 67 signal nodes in the whole Kolkata box, so Kolkata corridors hold few or no pre-emptable junctions. This reflects OSM coverage, not the city.

## 6. Functional requirements

Backend tests live in `backend/tests/`; frontend tests in `frontend/src/`. "No dedicated test found" means the behaviour is in the code but no test asserts it directly.

| ID | Requirement | Implemented in | Proved by |
|---|---|---|---|
| FR-01 | List the served cities and return one city's detail: traffic profile, active stations with live total/free unit counts, active junctions. Unknown city → `404 city_not_found`. | `backend/src/divert/api/v1/cities.py`, `services/reference.py` `ReferenceCache`, `db/repositories/reference.py` `station_availability` | `integration/test_api_reads.py::test_cities_and_city_detail`; `integration/test_reference_repo.py::test_city_reference_roundtrip` |
| FR-02 | Accept an incident type in any case with aliases (`HEALTH`, `AMBULANCE` → MEDICAL; `POLICE` → CRIME) and map it to its service. | `domain/enums.py` `normalize_incident_type`, `INCIDENT_TYPE_ALIASES`, `SERVICE_FOR_INCIDENT` | `unit/test_config_and_enums.py::test_incident_type_aliases`, `::test_every_incident_type_has_a_service`; `unit/test_regressions.py::test_police_and_other_aliases_normalise`, `::test_police_alias_maps_to_a_police_unit`; `integration/test_dispatch_api.py::test_police_alias_dispatches_a_police_unit` |
| FR-03 | Reject an invalid report with 422 before anything is created: unknown type (`invalid_incident`, lists `valid_types`), location outside the city box (`invalid_incident`, gives `bbox`), bad body or unknown fields (`validation_error`). | `services/dispatch.py` `DispatchService.create_incident`; `schemas/incidents.py` `IncidentCreate` | `unit/test_regressions.py::test_unknown_type_is_rejected_before_anything_is_created`; `unit/test_config_and_enums.py::test_unknown_incident_type_rejected`; `integration/test_dispatch_api.py::test_invalid_reports_return_422_and_create_nothing`; `frontend/src/features/dispatch/DispatchPanel.test.tsx` ("refuses a location outside the city bbox") |
| FR-04 | Find the `DIVERT_CANDIDATE_STATIONS` (3) nearest active stations of the service that have a free unit (KNN on a GiST index). None → `409 no_unit_available`. | `db/repositories/reference.py` `ReferenceRepository.nearest_stations_with_free_units` | `integration/test_reference_repo.py::test_knn_matches_brute_force`, `::test_knn_skips_stations_without_free_units`, `::test_spatial_queries_can_use_gist_indexes`; `integration/test_dispatch_api.py::test_no_free_unit_returns_409` |
| FR-05 | Route each candidate with OSRM (cached, retried on transport errors and 429/5xx, not on `NoRoute`/4xx). No straight-line fallback: if nothing routes → `503 routing_unavailable` and nothing is written. | `services/routing.py` (`RETRYABLE_STATUS`, `RoutingError`); `DispatchService._plans` | `unit/test_routing.py::test_request_shape_retry_and_cache`, `::test_unavailable_after_retries`, `::test_no_route_is_not_retried`, `::test_parse_merges_duplicates_and_locates_maneuvers`; `integration/test_dispatch_api.py::test_routing_failure_returns_503_and_writes_nothing`; `network/test_osrm_public_smoke.py::test_public_osrm_routes_between_real_points` (opt-in) |
| FR-06 | Rank routed candidates by predicted ETA at the simulation clock (then straight-line distance, then station id) and take the best station that still has a free unit. | `DispatchService._plans`, `DispatchService.sim_clock`, `DispatchService._dispatch` | ETA clock: `unit/test_resilience.py::test_dispatch_eta_uses_the_sim_clock_when_time_is_scaled`, `::test_sim_clock_falls_back_to_wall_time`; `integration/test_resilience.py::test_dispatch_eta_is_predicted_at_the_simulation_clock`. Ranking order: no dedicated test found |
| FR-07 | Compute the corridor: active junctions within `DIVERT_CORRIDOR_M` (30 m) of the route, each with `along_m`, `offset_m`, approach bearing and lane advisory (`L` left turn; `R` straight, right, U-turn). | `db/repositories/reference.py` `corridor_candidates`; `services/dispatch.py` `build_corridor`; `domain/route.py` `lane_advisory` | `integration/test_reference_repo.py::test_corridor_matches_brute_force`; `unit/test_routing.py::test_corridor_projection_lane_and_bearing`, `::test_lane_advisory_left_hand_traffic`; `unit/test_geo.py::test_projection_recovers_along_and_offset` |
| FR-08 | Commit the dispatch atomically: lock one free unit (`FOR UPDATE SKIP LOCKED`, next-best station if taken), insert incident + dispatch + `reported`/`dispatched` events + outbox command, mark the unit `DISPATCHED`, return `201` with incident, unit, route, ETA and corridor. A unit is never given two dispatches. | `DispatchService._dispatch`; `db/repositories/incidents.py` `IncidentRepository.create_dispatch`; `ReferenceRepository.lock_free_unit` | `integration/test_dispatch_api.py::test_police_alias_dispatches_a_police_unit`, `::test_concurrent_dispatches_never_share_a_unit`; `integration/test_reference_repo.py::test_skip_locked_never_hands_out_the_same_unit`, `::test_concurrent_lockers_get_distinct_units` |
| FR-09 | Deliver worker commands through a transactional outbox: XADD right after commit, plus a relay that re-sends unsent rows older than `DIVERT_OUTBOX_RELAY_MIN_AGE_S` (2 s) every `DIVERT_OUTBOX_RELAY_INTERVAL_S` (2 s). The 201 never waits on Redis. | `services/outbox.py` `OutboxRelay`; `DispatchService._after_commit` | `integration/test_pipeline.py::test_outbox_relay_delivers_commands_when_redis_blipped`, `::test_concurrent_relays_send_each_row_once`; `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down` |
| FR-10 | Simulate each dispatched vehicle along its route every tick with the speed model (segment caps, congestion, junction caps, braking envelope). | `services/simulation.py` `SimulationEngine.tick`, `_advance`; `services/speed_model.py` `SpeedProfile`; `worker.py` `Worker.tick_once` | `unit/test_speed_eta.py` (all tests, e.g. `::test_braking_envelope_before_a_junction`, `::test_unpreempted_junction_slows_the_vehicle`); `unit/test_simulation_engine.py::test_lifecycle_events_and_deterministic_ids` |
| FR-11 | Pre-empt corridor junctions per vehicle: claim at ETA ≤ 45 s or ≤ 600 m ahead, release > 20 m past; resolve contested junctions (lowest ETA; 5 s tie → AMBULANCE > FIRE_ENGINE > POLICE → vehicle id); the loser gets the 15 km/h cap. | `services/preemption.py` `PreemptionRules`, `resolve`; `SimulationEngine._update_claims`, `_resolve_city` | `unit/test_preemption.py` (all); `unit/test_simulation_engine.py::test_conflict_lower_eta_wins_and_loser_is_slowed`; `unit/test_regressions.py::test_leaving_vehicle_keeps_junction_preempted_for_same_type_vehicle` |
| FR-12 | Run the dispatch lifecycle: `en_route` on start, `arrived` at the route end (incident `ON_SCENE`, unit `ON_SCENE`), `cleared` after `DIVERT_SCENE_TIME_S` (120 s) on scene (incident `RESOLVED`, unit `AVAILABLE`). | `SimulationEngine._announce`, `_arrive`, `_end`; `db/repositories/history.py` `HistoryWriter` | `unit/test_simulation_engine.py::test_lifecycle_events_and_deterministic_ids`; `integration/test_pipeline.py::test_full_lifecycle_is_persisted` |
| FR-13 | Cancel an incident: `DELETE` sets `CANCELLED` at once, is idempotent, `409 invalid_state` once `RESOLVED`/`FAILED`, `404` for an unknown id or another city's incident; the worker recalls the vehicle and releases its claims; the unit becomes `AVAILABLE` only after the worker confirms. A cancel that overtakes its start blocks the late start. | `DispatchService.cancel_incident`; `IncidentRepository.mark_cancelled`; `SimulationEngine.cancel_dispatch` | `integration/test_dispatch_api.py::test_cancel_writes_cancel_command_and_is_idempotent`; `integration/test_pipeline.py::test_cancel_frees_the_unit_after_the_worker_confirms`; `unit/test_regressions.py::test_cancel_releases_every_claim`; `unit/test_simulation_engine.py::test_cancel_before_start_reports_cancelled_and_blocks_late_start` |
| FR-14 | Serve live state from Redis: vehicles in a city (optionally with route and corridor) and the non-NORMAL signals. While Redis is down, answer `503 live_state_unavailable` with `Retry-After: 5`. | `api/v1/vehicles.py`, `api/v1/signals.py`, `store/live.py`; `api/errors.py` | `integration/test_api_reads.py::test_live_endpoints_read_worker_state`; `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down` |
| FR-15 | Stream a city over WebSocket: a `snapshot` first, then `tick`, `vehicle_added`, `vehicle_removed`, `incident_updated`; answer `ping` with `pong`; close `4404` for an unknown city and `1013` when the client must reconnect (too many clients, slow consumer, Redis unavailable). The tick number stays monotonic across leader changes. | `api/v1/stream.py`, `api/ws_hub.py`; `store/events.py` `StreamMessageType`; `frontend/src/api/stream.ts` | `integration/test_api_reads.py::test_websocket_snapshot_then_live_messages`; `integration/test_resilience.py::test_websocket_is_refused_quickly_while_redis_is_down`; `unit/test_resilience.py::test_new_leader_resumes_the_tick_counter`; `frontend/src/api/stream.test.ts` |
| FR-16 | Persist the history idempotently: timeline events, pre-emptions, sampled positions (every `DIVERT_POSITION_SAMPLE_S` = 5 simulated s), dispatch outcomes, status transitions; dead-letter poison messages. | `persister.py` `Persister`; `db/repositories/history.py` `HistoryWriter` | `integration/test_pipeline.py::test_full_lifecycle_is_persisted`, `::test_persister_dead_letters_poison_and_retries_failures`; `unit/test_store_and_api_bits.py::test_poison_history_message_goes_to_dlq_without_touching_pg` |
| FR-17 | Query the history: list a city's incidents newest first with status/type/date filters and keyset pagination (limit 1–200, default 50); incident detail (dispatches, route, pre-emptions, timeline); the sampled track thinned to `max_points` (2–5000, default 500). | `api/v1/history.py`; `services/history.py` `HistoryService`; `IncidentRepository.list_incidents`, `incident_detail`, `track` | `integration/test_api_reads.py::test_history_list_keyset_pagination_and_filters`; `integration/test_pipeline.py::test_full_lifecycle_is_persisted`; `frontend/src/features/history/HistoryView.test.tsx`, `replay.test.ts` |
| FR-18 | Summarise a city over a window (default last 24 h, max 366 days): counts by type and status, response-time p50/p90/mean per type, predicted vs actual travel and ETA MAPE, pre-emptions/conflicts/busiest junctions, total estimated time saved. | `api/v1/analytics.py`; `db/repositories/analytics.py` `summary`; `services/history.py` `MAX_ANALYTICS_RANGE`, `DEFAULT_ANALYTICS_RANGE` | `integration/test_pipeline.py::test_full_lifecycle_is_persisted` (summary assertions); `frontend/src/features/analytics/AnalyticsView.test.tsx` |
| FR-19 | Report an estimated time saved per dispatch: junctions passed while pre-empted × the city's average junction delay, always labelled an estimate. | `SimulationEngine._arrive`, `_release`; `frontend/src/features/fleet/FleetPanel.tsx` | `unit/test_simulation_engine.py::test_lifecycle_events_and_deterministic_ids`; `unit/test_speed_eta.py::test_simulated_arrival_within_10pct_of_dispatch_eta` (`preempted_passed == 4`); `frontend/src/features/fleet/FleetPanel.test.tsx` |
| FR-20 | Load reference data from the OSM snapshots: validate them, seed idempotently, deactivate (never delete) vanished stations and junctions, take their free units out of service, never reset a live unit's status. Refresh from Overpass on demand. | `cities/loader.py` `parse_snapshot`; `db/seed.py` `seed_city`; `cities/osm.py`; `cli.py` (`migrate`, `seed`, `fetch-data`) | `unit/test_city_data.py` (all); `unit/test_osm_fetch.py` (all); `integration/test_seed.py::test_initial_seed_matches_snapshots`, `::test_reseed_is_idempotent_and_keeps_live_unit_status`, `::test_vanished_station_is_deactivated_not_deleted` |
| FR-21 | Expose operations endpoints: liveness, readiness (PostgreSQL decides; Redis and worker only set `degraded`), Prometheus metrics. | `api/v1/health.py`; `metrics.py` | `integration/test_api_reads.py::test_readyz_metrics_healthz`; `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down` |
| FR-22 | Let operators list and replay dead-lettered history events. | `cli.py` `dead_letters` (`divert dlq [--replay] [--id]`) | Dead-lettering: `integration/test_pipeline.py::test_persister_dead_letters_poison_and_retries_failures`. The CLI list/replay: no dedicated test found |
| FR-23 | Provide the ops console: city switcher, live map with junctions, stations, routes, animated vehicles, pulsing pre-empted and amber conflicting junctions; dispatch by map click with a haversine preview; incident list with cancel; fleet panel; history with replay; analytics. | `frontend/src/features/*` ([10 · Frontend codebase](10-codebase-frontend.md)) | `frontend/src/features/dispatch/DispatchPanel.test.tsx`, `fleet/FleetPanel.test.tsx`, `history/HistoryView.test.tsx`, `analytics/AnalyticsView.test.tsx`, `frontend/src/store/live.test.ts` |

All backend paths in this table are under `backend/src/divert/` unless they start with `frontend/`; test paths are under `backend/tests/` unless they start with `frontend/`.

## 7. Non-functional requirements

| ID | Requirement | Value and source | Proved by / evidence |
|---|---|---|---|
| NFR-01 | **Tick rate.** The leader advances every vehicle once per tick. | `DIVERT_SIM_TICK_S` = 0.5 s (range >0–5) → 2 ticks/s (`Settings.sim_tick_s`). A late tick is counted in `divert_tick_overruns_total` and the schedule resets (`Worker._lead`). Time scale `DIVERT_SIM_TIME_SCALE` 1 (range >0–100) multiplies simulated time per tick. | `unit/test_regressions.py::test_one_multi_exec_per_city_per_tick_and_no_reads`; `unit/test_config_and_enums.py::test_defaults_match_spec`. No test asserts wall-clock tick timing. |
| NFR-02 | **ETA accuracy.** A vehicle not delayed by a conflict arrives within ±10 % of the ETA predicted at dispatch. | Same envelope used by `KinematicEtaModel` and `SpeedProfile.step` ([Domain rules §6](02-domain-rules.md#6-eta-model)) | `unit/test_speed_eta.py::test_simulated_arrival_within_10pct_of_dispatch_eta` (all services, 3 hours); `integration/test_pipeline.py::test_full_lifecycle_is_persisted` (`actual_travel_s ≈ predicted_eta_s`, rel 0.10). Against real OSRM: route length within 5 % of OSRM's, ETA between 0.3× and 3× OSRM duration (`network/test_osrm_public_smoke.py`). **Real-world accuracy is not measured**; analytics reports `eta_mape` for calibration. |
| NFR-03 | **Durability: no lost dispatches or history.** | PostgreSQL is the system of record. The dispatch and its command commit in one transaction (outbox). The worker acks a command in the same `MULTI/EXEC` as its effects (`write_city_batch`); a failed tick write is kept and merged into the next. The persister acks only after commit; a message failing `DIVERT_PERSISTER_MAX_RETRIES` (5) deliveries goes to `divert:history:dlq`. A new leader rebuilds any active dispatch Redis lost from PostgreSQL. Redis runs with AOF `appendfsync everysec` (`docker-compose.yml`). | `integration/test_pipeline.py::test_outbox_relay_delivers_commands_when_redis_blipped`, `::test_new_leader_rebuilds_from_postgres_when_redis_lost_live_state`, `::test_persister_dead_letters_poison_and_retries_failures`; `unit/test_worker_leader.py::test_failed_tick_write_is_retried_merged_into_the_next`, `::test_new_leader_reclaims_commands_the_dead_leader_never_applied`; `unit/test_regressions.py::test_cancel_releases_every_claim` (no pending commands) |
| NFR-04 | **Failover.** Exactly one worker leads; a standby takes over within the lease. | Lease `DIVERT_LEADER_LOCK_TTL_MS` = 5000 ms (min 500), renewed every TTL/3; the leader trusts itself only until last renewal + TTL − TTL/5 (`LeaderLock.margin_s`); standbys retry every TTL/3; un-acked commands are reclaimed after TTL idle (`XAUTOCLAIM`); SIGTERM releases the lock at once. Simulated time is frozen while nobody leads. | `unit/test_worker_leader.py::test_lock_acquire_renew_release`, `::test_lease_expires_locally_before_the_key`, `::test_only_one_leader_and_standby_takes_over_from_redis_state`; `unit/test_resilience.py::test_new_leader_resumes_the_tick_counter`; `integration/test_resilience.py::test_rebuild_after_a_no_leader_freeze_keeps_simulated_travel_time` |
| NFR-05 | **Idempotency.** Every consumer tolerates redelivery. | Deterministic `event_id`s (≤ 64 chars) with `ON CONFLICT` writes; positions keyed `(dispatch_id, ts)`; forward-only status moves; `start_dispatch` ignored for running or finished dispatches (finished set kept 86 400 s, `DIVERT_FINISHED_DISPATCH_TTL_S`); cancel idempotent; seed idempotent. | `unit/test_simulation_engine.py::test_start_is_idempotent_and_finished_dispatches_never_restart`; `unit/test_store_and_api_bits.py::test_event_ids_are_deterministic_and_bounded`; `integration/test_pipeline.py::test_full_lifecycle_is_persisted` (re-applying the stream changes nothing); `integration/test_seed.py::test_reseed_is_idempotent_and_keeps_live_unit_status` |
| NFR-06 | **Latency bounds (where defined).** A Redis outage never stalls a dispatch. | Request-path Redis deadline `DIVERT_REDIS_REQUEST_TIMEOUT_S` 0.5 s (max 10) with a circuit breaker open for `DIVERT_REDIS_CIRCUIT_COOLDOWN_S` 5 s (`RedisGuard`); OSRM timeout `DIVERT_OSRM_TIMEOUT_S` 8 s (connect ≤ 3 s), `DIVERT_OSRM_RETRIES` 2, backoff 0.25 s × 2^(n−1) × (1 + random); PostgreSQL statement timeout 15 000 ms, pool timeout 10 s; `/metrics` refresh ≤ 2 s. **No end-to-end latency SLO is defined in code**; alert thresholds (e.g. tick p95 > 0.25 s) are only suggested in [deployment.md](../deployment.md). | `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down` (each POST < 3.0 s with Redis dead, 0.3 s deadline); `unit/test_resilience.py::test_guard_bounds_a_hanging_call_and_then_short_circuits`, `::test_guard_half_open_trial_closes_or_reopens` |
| NFR-07 | **Security.** Writes can be keyed and are rate-limited; browsers are restricted to listed origins. | **API key:** when `DIVERT_API_KEY` is set, `POST`/`DELETE` need `X-API-Key` (constant-time compare, `401 unauthorized`); reads are open (`require_api_key`). **Rate limit:** fixed 60 s window, `DIVERT_RATE_LIMIT_PER_MIN` 60 per API key or client address (`X-Forwarded-For` only if `DIVERT_TRUST_PROXY_HEADERS`), `429 rate_limited` + `Retry-After`; rejected 422s still count; fails **open** if Redis is down (`rate_limit_mutations`). **CORS:** explicit `DIVERT_CORS_ORIGINS` (default `http://localhost:5173`, `http://localhost:8080`), `*` rejected at startup, credentials allowed, methods GET/POST/DELETE/OPTIONS, headers Content-Type/X-API-Key/X-Request-ID (`create_app` in `main.py`). Secrets are `SecretStr`. Redis key parts must match `^[A-Za-z0-9_.-]{1,64}$`. The console's `VITE_API_KEY` is baked into the bundle and is not a secret (`frontend/src/config.ts`). | `integration/test_dispatch_api.py::test_rate_limit_and_api_key`; `unit/test_store_and_api_bits.py::test_fixed_window_rate_limit`, `::test_api_key_on_mutations`; `unit/test_config_and_enums.py::test_wildcard_cors_rejected`, `::test_settings_from_env`; `unit/test_redis_keys.py::test_invalid_ids_rejected` |
| NFR-08 | **Data retention.** | Positions pruned after `DIVERT_POSITION_RETENTION_DAYS` 30; relayed outbox rows after `DIVERT_OUTBOX_RETENTION_DAYS` 7; pruning every `DIVERT_RETENTION_INTERVAL_S` 3600 s by one persister (PostgreSQL advisory lock), 10 000 rows per batch. Incidents, dispatches, events and pre-emptions are **kept forever** (no pruning code). Redis: `divert:commands` trimmed to ~100 000 entries, `divert:history` to ~1 000 000; OSRM cache 3600 s; finished-dispatch set 86 400 s. | `integration/test_pipeline.py::test_retention_prunes_old_positions_and_relayed_outbox` |
| NFR-09 | **Availability under partial failure.** Dispatch keeps working while Redis is down; the live simulation keeps running while PostgreSQL is down. | `/readyz` is 200 while PostgreSQL answers, even with Redis down (`degraded: true`); commands wait in the outbox; the worker never touches PostgreSQL on the hot path (it only reads it on take-over). | `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down`, `::test_websocket_is_refused_quickly_while_redis_is_down` |
| NFR-10 | **Scalability.** | API stateless, N replicas; WebSocket limit `DIVERT_WS_MAX_CLIENTS_PER_CITY` 500 per process, per-client queue `DIVERT_WS_QUEUE_SIZE` 256; persisters scale through the consumer group; **one worker leader simulates all cities**. | Design statement in `worker.py` docstring and [architecture.md](../architecture.md) §9; no load test in the repo. |
| NFR-11 | **Observability.** | Prometheus metrics on the API (`/metrics`) and on worker/persister (`DIVERT_METRICS_PORT` 9100); JSON logs; `X-Request-ID` echoed or generated on every response. | `integration/test_api_reads.py::test_readyz_metrics_healthz` |
| NFR-12 | **Data provenance.** No invented geography; every snapshot records source, queries, timestamps and licence. | `meta` block of each snapshot; `cities/osm.py` | `unit/test_city_data.py::test_snapshot_is_valid_and_traceable`, `::test_signal_ids_trace_back_to_osm_nodes`, `::test_station_ids_encode_osm_element` |

## 8. API surface

One line per endpoint. The normative contract, bodies and error codes are in [api.md](../api.md). OpenAPI is generated at `/docs` and `/openapi.json`. Writes are marked ✎ (API key if configured, rate-limited).

| Method and path | Purpose | Contract |
|---|---|---|
| `GET /healthz` | Liveness | [api.md · Ops](../api.md#ops) |
| `GET /readyz` | Readiness: PostgreSQL; Redis and worker heartbeat reported as `degraded` | [api.md · Ops](../api.md#ops) |
| `GET /metrics` | Prometheus metrics | [api.md · Ops](../api.md#ops) |
| `GET /api/v1/cities` | Served cities with centre, bbox, zoom | [api.md · Cities](../api.md#cities) |
| `GET /api/v1/cities/{city}` | City detail: traffic profile, stations with free units, junctions | [api.md · Cities](../api.md#cities) |
| ✎ `POST /api/v1/cities/{city}/incidents` | Report an incident and dispatch the fastest free unit | [api.md · Incidents](../api.md#incidents-dispatch) |
| ✎ `DELETE /api/v1/cities/{city}/incidents/{incident_id}` | Cancel an incident; the worker recalls the vehicle | [api.md · Incidents](../api.md#incidents-dispatch) |
| `GET /api/v1/cities/{city}/incidents` | Incident history with filters and keyset pagination | [api.md · History](../api.md#history-postgresql) |
| `GET /api/v1/incidents/{incident_id}` | Incident detail: dispatches, route, pre-emptions, timeline | [api.md · History](../api.md#history-postgresql) |
| `GET /api/v1/incidents/{incident_id}/track` | Sampled vehicle positions | [api.md · History](../api.md#history-postgresql) |
| `GET /api/v1/cities/{city}/analytics/summary` | Counts, response times, ETA error, pre-emptions, time saved | [api.md · History](../api.md#history-postgresql) |
| `GET /api/v1/cities/{city}/vehicles` | Live vehicles (`include_route=true` adds route and corridor) | [api.md · Live state](../api.md#live-state-redis-written-by-the-worker-leader) |
| `GET /api/v1/cities/{city}/signals/active` | Live `PREEMPTED` and `CONFLICT` junctions | [api.md · Live state](../api.md#live-state-redis-written-by-the-worker-leader) |
| `WS /api/v1/cities/{city}/stream` | Live stream: snapshot, tick, vehicle and incident events | [api.md · WebSocket](../api.md#websocket-get-apiv1citiescitystream) |

## 9. Acceptance criteria

A release is acceptable when all of the following hold. Each line names the check that proves it; CI runs the backend unit and integration suites and the frontend suite ([08 · Engineering workflows](08-engineering-workflows.md)).

### 9.1 The three regression guarantees

These port three bugs from the prototype into permanent tests (`backend/tests/unit/test_regressions.py` module docstring; [architecture.md](../architecture.md) §7).

| # | Guarantee | Acceptance criteria | Tests |
|---|---|---|---|
| 1 | **`POLICE` alias.** `POLICE` (any case, padded) is CRIME and dispatches a police unit; an unknown type is rejected before anything is created. | `POST {"type": "police", …}` → 201 with `incident.type = "CRIME"`, `reported_type` as sent, `vehicle.service = "POLICE"`, a `PCR-…` unit. `POST {"type": "BOGUS", …}` → 422 `invalid_incident` whose `valid_types` include `POLICE`; no row, no command, no Redis key, no OSRM call. | `unit/test_regressions.py::test_police_and_other_aliases_normalise`, `::test_police_alias_maps_to_a_police_unit`, `::test_unknown_type_is_rejected_before_anything_is_created`; `integration/test_dispatch_api.py::test_police_alias_dispatches_a_police_unit`, `::test_invalid_reports_return_422_and_create_nothing` |
| 2 | **All signal claims released on every exit path.** | After arrival next to a junction, after an exception part-way along the route, and after a cancel: the vehicle lists no claims, no `divert:{city}:claims:*` or `vclaims:*` key remains, the active-signal hash is empty, and a `preemption_released` exists for each claim (reason `arrived` for the junction at the scene). The failed case emits exactly one `failed` event carrying the error. | `unit/test_regressions.py::test_arrival_next_to_a_signal_releases_every_claim`, `::test_exception_mid_route_releases_every_claim`, `::test_cancel_releases_every_claim` |
| 3 | **No lost updates under concurrency.** | With four vehicles dispatched at once (two ambulances sharing a junction): no vehicle moves backwards or reappears after removal; every claim field belongs to a vehicle that lists it; both ambulances hold the shared junction at the same time; event ids are unique; all four clear. When one ambulance leaves the shared junction, it stays `PREEMPTED` for the other. Each tick is exactly one `MULTI/EXEC` per city with no reads inside, and never spans two cities; a foreign hash field survives. Ten simultaneous reports at one spot never share a unit. | `unit/test_regressions.py::test_concurrent_dispatches_never_lose_updates`, `::test_leaving_vehicle_keeps_junction_preempted_for_same_type_vehicle`, `::test_one_multi_exec_per_city_per_tick_and_no_reads`; `integration/test_dispatch_api.py::test_concurrent_dispatches_never_share_a_unit` |

### 9.2 Other acceptance criteria

| # | Criterion | Check |
|---|---|---|
| AC-04 | A full lifecycle is persisted in order: `reported`, `dispatched`, `en_route`, …, `arrived`, `cleared`; `dispatched_at ≤ en_route_at ≤ on_scene_at ≤ resolved_at`; end reason `CLEARED`; unit back to `AVAILABLE`; every pre-emption released; actual travel within 10 % of predicted; replaying the stream changes nothing; DLQ empty. | `integration/test_pipeline.py::test_full_lifecycle_is_persisted` |
| AC-05 | OSRM failure → 503 and nothing written; no free unit → 409. | `integration/test_dispatch_api.py::test_routing_failure_returns_503_and_writes_nothing`, `::test_no_free_unit_returns_409` |
| AC-06 | Cancel is idempotent and frees the unit only after the worker confirms. | `integration/test_dispatch_api.py::test_cancel_writes_cancel_command_and_is_idempotent`; `integration/test_pipeline.py::test_cancel_frees_the_unit_after_the_worker_confirms` |
| AC-07 | With Redis dead: POST still 201 in < 3 s, `/readyz` 200 with `degraded: true`, live views 503, history works, commands wait in the outbox and are relayed later; WebSocket closes 1013. | `integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down`, `::test_websocket_is_refused_quickly_while_redis_is_down` |
| AC-08 | Leader failover: one leader at a time; the standby resumes vehicles, the tick counter and un-acked commands; a rebuild after a no-leader freeze keeps simulated travel time. | `unit/test_worker_leader.py` (all); `integration/test_pipeline.py::test_new_leader_rebuilds_from_postgres_when_redis_lost_live_state`; `integration/test_resilience.py::test_rebuild_after_a_no_leader_freeze_keeps_simulated_travel_time` |
| AC-09 | Simulated arrival within ±10 % of the dispatch ETA for every service; a non-pre-empted junction slows the vehicle; a junction at the destination is not claimed early. | `unit/test_speed_eta.py::test_simulated_arrival_within_10pct_of_dispatch_eta`, `::test_unpreempted_junction_slows_the_vehicle`, `::test_signal_at_the_destination_is_not_claimed_early` |
| AC-10 | Defaults match the specification: corridor 30 m, lead 45 s, distance 600 m, tick 0.5 s, scale 1, scene 120 s, 2 units per station, no API key, public OSRM URL. | `unit/test_config_and_enums.py::test_defaults_match_spec` |
| AC-11 | Migrations upgrade, match the models, and downgrade to base and back; the seed matches the snapshots. | `integration/test_migrations.py` (all); `integration/test_seed.py` (all) |

## 10. Known limits and future scope

### Known limits and gaps (from the code and docs)

| Limit | Detail | Source |
|---|---|---|
| **One leader for all cities** | A single worker leader simulates every city; scaling beyond one CPU would need sharding by city (one lock per city group), which is not built. | `backend/src/divert/worker.py` docstring; [architecture.md](../architecture.md) §9 |
| **No fencing tokens** | The leader lease bounds but cannot fully exclude a two-leader overlap; per-field writes and deterministic event ids limit the damage. | `backend/src/divert/store/locks.py` docstring |
| **Redis Cluster unsupported** | Per-city keys share a hash tag, but the tick transaction also writes the global `divert:history` stream and acks `divert:commands`. Deployments use a single-primary Redis. | `backend/src/divert/store/redis_store.py`, `store/live.py` docstrings; [deployment.md](../deployment.md) |
| **Kubernetes manifests never applied to a cluster** | `infra/k8s/base` has only been rendered and validated offline (`kubectl kustomize` + `kubeconform -strict`, as stated in deployment.md). No CI job, Makefile target or script in the repo renders, validates or applies it. | [deployment.md](../deployment.md); `.github/workflows/ci.yml`, `Makefile` |
| **Self-hosted OSRM not yet run** | The default `DIVERT_OSRM_URL` is the public demo server (fair use, no SLA). `infra/osrm/prepare.sh` has not been run in this checkout: `infra/osrm/data/` is empty, which the compose `osrm` profile needs. | `Settings.osrm_url`; [infra/osrm/README.md](../../infra/osrm/README.md) |
| **OSM map tiles for development only** | The console defaults to `tile.openstreetmap.org` tiles, acceptable for development and demos under the OSMF policy; production needs a keyed provider or self-hosted tiles (`VITE_TILE_URL`). | `frontend/src/config.ts`; `.env.example` |
| Congestion and junction delays are uncalibrated | Hand-set approximations; absolute ETAs and time-saved figures are not trustworthy until calibrated. | snapshot `meta.traffic_profile_note` |
| Time saved is a heuristic | Junctions passed while pre-empted × average delay; the speed model itself never stops at a red light. | [Domain rules §9.3](02-domain-rules.md#93-estimated-time-saved) |
| Sparse Kolkata signals | 67 OSM signal nodes → 28 junctions. | Kolkata snapshot `meta.counts` |
| Fleet is assumed | 2 units per station; not every hospital runs ambulances. | `Settings.units_per_station` |
| `OUT_OF_SERVICE` is one-way | No code path returns such a unit to `AVAILABLE`. | `backend/src/divert/db/seed.py` |
| `OPEN` incident status unused | It is the database default but incidents are created `DISPATCHED`. | `IncidentRepository.create_dispatch` |
| Junction cap checked per tick | A vehicle can cross a junction slightly above its cap between tick positions. | `SpeedProfile.step` |
| No alert rules shipped | Prometheus and Grafana are provisioned; alert thresholds exist only as suggestions in docs. | `infra/observability/` |
| Rate limiter fails open | Deliberate: a dispatch is never refused because the limiter is down. | `backend/src/divert/api/ratelimit.py` |
| History grows without bound | Only positions and relayed outbox rows are pruned. | `prune_positions`, `prune_outbox` |

### Future scope (from [vision.md](../vision.md) §5 and code seams)

- **IoT signal control** (e.g. MQTT) for real signal heads and lane boards.
- **Civilian navigation SDK**: "Emergency vehicle approaching - keep clear".
- **Adaptive rerouting** when a blockage is detected mid-route.
- **Predictive deployment**: use the stored incident history to pre-position units.
- **A learned ETA model** behind the existing `EtaModel` protocol, trained on `predicted_eta_s` vs `actual_travel_s` and the position tracks.
- **Calibration** of congestion and junction delay (TomTom Traffic Index, traffic-police counts, DIVERT's own `eta_mape`).
- **Sharding the worker by city** and moving to per-city streams if Redis Cluster is ever needed.

## Related

- [02 · Domain rules](02-domain-rules.md): the exact rules behind FR-02 to FR-19
- [03 · Architecture](03-architecture.md) and [04 · Decisions](04-decisions.md): how and why it is built this way
- [07 · Runtime workflows](07-runtime-workflows.md): each requirement as a step-by-step flow
- [08 · Engineering workflows](08-engineering-workflows.md): running the tests named here
- [09 · Backend codebase](09-codebase-backend.md), [10 · Frontend codebase](10-codebase-frontend.md)
- [11 · Configuration](11-configuration.md): every setting named here
- [12 · Glossary](12-glossary.md)
- [vision.md](../vision.md), [api.md](../api.md), [data-sources.md](../data-sources.md), [deployment.md](../deployment.md)
