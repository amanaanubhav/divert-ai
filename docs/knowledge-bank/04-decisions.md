# 04 · Architecture decisions (ADRs)

**What this covers:** the major design choices in DIVERT, written as Architecture Decision Records. Each record has a Context, a Decision, its Consequences, the Alternatives considered, and Evidence: the code and tests that implement it. The repository has no formal ADR log. These records were rebuilt from the code, its docstrings and the existing docs ([architecture.md](../architecture.md), [deployment.md](../deployment.md), [data-sources.md](../data-sources.md), [vision.md](../vision.md)). An alternative marked *(inferred)* is not recorded in the repository. It is listed only to explain the trade-off.

## Index

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001-postgresql--postgis-as-the-system-of-record) | PostgreSQL + PostGIS as the system of record | Implemented |
| [002](#adr-002-redis-for-hot-rebuildable-state) | Redis for hot, rebuildable state | Implemented |
| [003](#adr-003-transactional-outbox-for-worker-commands) | Transactional outbox for worker commands | Implemented |
| [004](#adr-004-a-single-writer-leader-worker) | A single-writer leader worker | Implemented (no fencing tokens, no sharding) |
| [005](#adr-005-redis-streams-with-consumer-groups-rather-than-kafka-or-rabbitmq) | Redis Streams with consumer groups | Implemented |
| [006](#adr-006-select-for-update-skip-locked-to-reserve-units) | `SELECT … FOR UPDATE SKIP LOCKED` to reserve units | Implemented |
| [007](#adr-007-deterministic-event-ids-an-idempotent-persister-and-a-dlq) | Deterministic event ids, an idempotent persister and a DLQ | Implemented |
| [008](#adr-008-a-kinematic-eta-model-behind-an-etamodel-protocol) | A kinematic ETA model behind an `EtaModel` protocol | Implemented |
| [009](#adr-009-osrm-with-a-redis-route-cache-and-no-straight-line-fallback) | OSRM with a Redis route cache and no straight-line fallback | Implemented; self-hosting not yet run |
| [010](#adr-010-committed-real-osm-snapshot-files-rather-than-live-fetching) | Committed real-OSM snapshots | Implemented |
| [011](#adr-011-per-vehicle-signal-claim-fields-with-one-multiexec-per-city-per-tick) | Per-vehicle claim fields with one `MULTI/EXEC` per city per tick | Implemented |
| [012](#adr-012-a-request-path-redis-guard-and-circuit-breaker) | A request-path Redis guard and circuit breaker | Implemented |
| [013](#adr-013-an-nginx-template-with-runtime-dns-resolution-for-the-api-upstream) | An nginx template with runtime DNS resolution | Implemented |
| [014](#adr-014-openstreetmap-standard-tiles-as-the-default-basemap) | OSM standard tiles as the default basemap | Implemented (dev/demo only) |
| [015](#adr-015-keyset-pagination-for-incident-history) | Keyset pagination for history | Implemented |

---

## ADR-001: PostgreSQL + PostGIS as the system of record

**Context.**

- DIVERT keeps reference data for three cities: 2,718 stations and 1,104 junction signals in the committed snapshots (§5 of [Data stores](05-data-stores.md)). It also keeps the full incident history and the analytics built on it.
- Dispatch needs spatial queries: the nearest stations of a service that still have a free unit (KNN), and the signals within 30 m of a route.
- Some invariants must hold even under concurrency. For example, a unit can never be on two active dispatches.

**Decision.**

- Use PostgreSQL 16 + PostGIS 3.4 as the only system of record.
- Every spatial column is `geography(Point|LineString, 4326)` with a GiST index (`backend/src/divert/db/models.py`).
- Enumerations are `VARCHAR + CHECK`, not native enums (`backend/src/divert/db/base.py` `str_enum`).
- Constraint names follow a fixed convention (`NAMING_CONVENTION`), and timestamps are `timestamptz` with the session timezone set to UTC (`db/session.py` `create_engine`).
- The schema is managed by Alembic, and a test checks that the ORM and the migrations do not drift.

**Consequences.**

- (+) Distances and buffers are in metres. `ORDER BY geom <-> point` and `ST_DWithin` use the GiST indexes.
- (+) The database enforces the invariants: `ck_units_busy_has_dispatch`, the partial unique index `uq_dispatches_active_unit`, and the deferrable FK `fk_units_current_dispatch_id_dispatches`.
- (+) History and analytics are plain SQL. Keyset pagination and BRIN-indexed tracks make them cheap ([ADR-015](#adr-015-keyset-pagination-for-incident-history)).
- (−) PG is on the dispatch request path. If PG is down, new dispatches fail and `/readyz` returns 503.
- (−) The first migration runs `CREATE EXTENSION postgis`, so the role needs that right, or the extension must be created beforehand.
- (−) PG scales vertically, with a single primary.
- (−) Exact along-route distances are recomputed in Python (`domain/geo.py`) so they use the same metric as the simulation. PostGIS only finds the candidates.

**Alternatives considered.**

- `geometry` instead of `geography`: rejected because geography gives metres on the spheroid (`db/models.py` docstring).
- Native PG `ENUM` types: rejected because adding a value would need `ALTER TYPE` (`db/base.py` `str_enum` docstring, [data-model.md](../data-model.md)).
- A document store, or a separate geo service *(inferred)*.

**Evidence.** `tests/integration/test_migrations.py` (`test_models_match_migration`, `test_spatial_and_time_indexes`, `test_downgrade_to_base_and_upgrade_again`); CI runs `alembic check` and `downgrade base` (`.github/workflows/ci.yml`); `tests/integration/test_reference_repo.py`.

## ADR-002: Redis for hot, rebuildable state

**Context.**

- Live vehicle state changes on every tick, every 0.5 s (`config.py` `sim_tick_s`).
- Browsers need a fan-out of those changes.
- Design rule 4 forbids database calls on the simulation hot path ([architecture.md](../architecture.md)).

**Decision.**

- Use Redis 7 as a single primary, with AOF `everysec` and `maxmemory-policy noeviction` (`docker-compose.yml`).
- Redis holds:
  - Live vehicles, routes, per-vehicle claims, active signals, per-city tick and the finished set.
  - The leader lock, heartbeat and sim clock.
  - The command and history streams and the DLQ.
  - Pub/sub, the OSRM cache and the rate-limit counters.
- Every key is defined in `backend/src/divert/store/redis_store.py`.
- None of it is the system of record. A new leader can rebuild every active dispatch from PG (`worker.py` `Worker._rebuild_from_pg`).

**Consequences.**

- (+) Writes are fast. A whole city's tick is one transaction ([ADR-011](#adr-011-per-vehicle-signal-claim-fields-with-one-multiexec-per-city-per-tick)), and pub/sub feeds the WebSockets.
- (+) Losing Redis data is survivable.
- (−) A Redis outage freezes the map and the simulation, because the leader cannot hold its lease.
- (−) Unpersisted entries in `divert:history` and the DLQ are lost if Redis loses its data.
- (−) With `noeviction`, writes fail when memory is full. Caches therefore carry TTLs.
- (−) **Redis Cluster is not supported.** The tick transaction mixes hash-tagged city keys with global streams ([architecture.md](../architecture.md) §9).

**Alternatives considered.**

- Live state in PostgreSQL: rejected by design rule 4.
- In-process memory only *(inferred)*: a standby could not take over. The leader mirrors its full state every tick precisely so that it can (`worker.py` module docstring).

**Evidence.** `tests/integration/test_pipeline.py::test_new_leader_rebuilds_from_postgres_when_redis_lost_live_state`, `tests/unit/test_redis_keys.py`.

## ADR-003: Transactional outbox for worker commands

**Context.** A dispatch commits PG rows and must also tell the worker to start the vehicle, through Redis. That is a dual write.

- If the commit succeeds but the `XADD` fails, or the process dies in between, the unit stays `DISPATCHED` and no vehicle ever moves.
- If the `XADD` happens first and the transaction then rolls back, a phantom vehicle appears.

**Decision.**

1. In the same transaction as the incident and dispatch, insert an `outbox` row with `topic='commands'` and the command as payload (`db/repositories/incidents.py` `IncidentRepository.create_dispatch`, `mark_cancelled`).
2. After the commit, a background task runs the fast path `OutboxRelay.send_now`. It `XADD`s the command to `divert:commands` with `outbox_id` added, then stamps `sent_at`.
3. A relay task in every api replica (`OutboxRelay.run`) re-sends any row still unsent after `outbox_relay_min_age_s` = 2 s. It runs every `outbox_relay_interval_s` = 2 s, in id order, under `FOR UPDATE SKIP LOCKED`, and stops at the first Redis error so order is kept.

**Consequences.**

- (+) No command is lost once the dispatch is committed.
- (+) The 201 never waits on Redis.
- (−) Delivery is at-least-once, so the worker must be idempotent. `SimulationEngine.start_dispatch` acks and ignores a dispatch it already runs or that is in the finished set (`divert:{city}:finished`, kept 24 h).
- (−) When the fast path fails, a command arrives about 2–4 s later.
- (−) Relayed rows are pruned after `outbox_retention_days` = 7.
- (−) A row stamped `sent_at` whose stream entry is later lost (Redis data loss) is not re-sent. Only a later leader take-over rebuilds that dispatch from PG.

**Alternatives considered.**

- `XADD` directly after commit *(inferred)*: this is the dual-write problem described above.
- CDC through logical decoding *(inferred)*: needs more infrastructure.

**Evidence.** `services/outbox.py` module docstring; `tests/integration/test_pipeline.py::test_outbox_relay_delivers_commands_when_redis_blipped`, `::test_concurrent_relays_send_each_row_once`.

## ADR-004: A single-writer leader worker

**Context.**

- The prototype lost updates because several writers did read-modify-write on shared Redis state (`tests/unit/test_regressions.py` module docstring, guarantee 3).
- Resolving conflicts between vehicles at a junction needs one consistent view of every claim.

**Decision.**

- Exactly one worker leads, through a Redis lease (`store/locks.py` `LeaderLock`):
  - Acquired with `SET divert:worker:leader <token> NX PX 5000` (`leader_lock_ttl_ms`).
  - Renewed every TTL/3 with a `WATCH/MULTI` compare-and-set.
  - Treated as valid locally only until `renewal + TTL − TTL/5`.
  - Released on SIGTERM.
- The leader is the only writer of live state. A hot standby retries every TTL/3.
- The engine (`services/simulation.py` `SimulationEngine`) is pure and synchronous. Its full state is mirrored to Redis on every tick.

**Consequences.**

- (+) No locking inside the engine, and conflict resolution is deterministic.
- (+) Fail-over within about TTL + TTL/3 (≈ 5–6.7 s).
- (−) **One leader simulates all cities.** The CPU ceiling is one event loop, and the standby sits idle.
- (−) Simulated time freezes during a fail-over.
- (−) **No fencing tokens.** An overlap between two leaders is bounded but not excluded. Whole-field writes and deterministic event ids keep it harmless (`store/locks.py` docstring).
- (−) Take-over needs PostgreSQL.

**Alternatives considered.**

- Shard by city, with one lock per city group: the documented next step, **not implemented** ([architecture.md](../architecture.md) §9, [deployment.md](../deployment.md)).
- Fencing tokens: acknowledged as absent (`store/locks.py`).
- Lua scripts for the lock: not needed, because `WATCH/MULTI` does the compare-and-set (`store/locks.py` docstring).
- Kubernetes Lease objects *(inferred)*.

**Evidence.** `tests/unit/test_worker_leader.py` (`test_lease_expires_locally_before_the_key`, `test_only_one_leader_and_standby_takes_over_from_redis_state`, `test_new_leader_reclaims_commands_the_dead_leader_never_applied`); `tests/unit/test_resilience.py::test_new_leader_resumes_the_tick_counter`.

## ADR-005: Redis Streams with consumer groups, rather than Kafka or RabbitMQ

**Context.** DIVERT needs two queues with acknowledgement and redelivery:

- api → worker, for commands.
- worker → persister, for history. This one needs horizontally scaled consumers.

Redis is already required for live state.

**Decision.**

- Two streams, each with a consumer group: `divert:commands` (group `worker`) and `divert:history` (group `persister`). Poison messages go to a third stream, `divert:history:dlq` (`store/redis_store.py`).
- Each entry has one field, `data`, holding JSON with `type` and `v` (the schema version). Unknown types are ignored (`store/events.py` docstring).
- Consumers ack only after the effect is durable. `XAUTOCLAIM` takes over entries left pending by a crashed consumer (`store/streams.py`).
- Streams are trimmed approximately, with `MAXLEN ~` at 100,000 (commands) and 1,000,000 (history).
- The worker's history `XADD` and command `XACK` run **inside** the tick `MULTI/EXEC`, together with the state they describe.

**Consequences.**

- (+) No extra broker to run.
- (+) State, events and acks commit atomically. That is only possible because the queue and the state live in the same Redis.
- (−) Durability is bounded by Redis AOF (`everysec`).
- (−) Approximate trimming can drop entries that were never read if the persisters are down for long.
- (−) Nothing removes stale consumers from a group.
- (−) Global streams rule out Redis Cluster.

**Alternatives considered.**

- Kafka or RabbitMQ *(inferred; not discussed in the repository)*: either adds infrastructure and would break the single transaction that commits state, events and acks together.
- Plain pub/sub: used only for the WebSocket fan-out, because it is fire-and-forget. The hub forces clients to resync after a reconnect (`api/ws_hub.py`).

**Evidence.** `store/streams.py`, `store/live.py` `write_city_batch`; `tests/unit/test_regressions.py::test_one_multi_exec_per_city_per_tick_and_no_reads`.

## ADR-006: SELECT FOR UPDATE SKIP LOCKED to reserve units

**Context.** Concurrent dispatches near the same station must never double-book a unit. They also must not queue behind each other's row locks.

**Decision.**

- The KNN station search (`db/repositories/reference.py` `nearest_stations_with_free_units`) is only a hint, and takes no lock.
- Inside the dispatch transaction, `ReferenceRepository.lock_free_unit` runs `SELECT … FROM units WHERE station_id = :s AND status = 'AVAILABLE' ORDER BY ordinal LIMIT 1 FOR UPDATE SKIP LOCKED`.
- If that station has no free unit, the next-best plan is tried. If none is left, the request gets **409** (`services/dispatch.py` `DispatchService._dispatch`).
- The unit `UPDATE` is also guarded by `status = 'AVAILABLE'`, and `uq_dispatches_active_unit` is the backstop.
- The outbox relay uses the same `SKIP LOCKED` pattern.

**Consequences.**

- (+) No blocking and no double booking. Concurrent requests get different units or a 409.
- (−) A request can get 409 just before another dispatch ends and frees a unit.

**Alternatives considered** *(all inferred)*:

- Optimistic concurrency with a version column and retries.
- `SERIALIZABLE` isolation.
- A Redis lock per unit, which would put Redis on the critical path (contrary to [ADR-012](#adr-012-a-request-path-redis-guard-and-circuit-breaker)).

**Evidence.** `tests/integration/test_dispatch_api.py::test_concurrent_dispatches_never_share_a_unit`, `::test_no_free_unit_returns_409`.

## ADR-007: Deterministic event ids, an idempotent persister and a DLQ

**Context.** Delivery is at-least-once everywhere. A leader fail-over can replay events, and two leaders can briefly overlap ([ADR-004](#adr-004-a-single-writer-leader-worker)).

**Decision.**

- Every history event carries `event_id = event_id(prefix, *parts)` (`store/events.py`). Examples: `enr:<dispatch>`, `pos:<dispatch>:<n>`, `clm:<dispatch>:<signal>`, `arr:<dispatch>`. Dashes are stripped from UUIDs, and ids longer than 64 characters become `prefix:h:<sha256…>`.
- The persister (`db/repositories/history.py` `HistoryWriter.apply`) applies events idempotently:
  - It inserts into `incident_events` with `ON CONFLICT (event_id) DO NOTHING`, and applies side effects only when the row was new.
  - `signal_preemptions` are keyed by the claim's event id. A conflict sets `CONFLICT`; a release keeps the earliest `released_at`.
  - Positions are keyed by `(dispatch_id, ts)`.
  - Status changes only move forward (`_FORWARD_FROM`). A unit is freed only while it still points at this dispatch.
- A batch is applied in one transaction and acked after `COMMIT`. If the batch fails, each message is retried on its own.
- A `BadEvent` (schema violation) goes straight to `divert:history:dlq`. Other failures go there after `persister_max_retries` = 5 deliveries. `divert dlq --replay` re-queues them (`cli.py` `dead_letters`).

**Consequences.**

- (+) Replays and duplicates are harmless.
- (+) A poison message cannot block the stream.
- (−) The DLQ needs a person to act on it.
- (−) The DLQ lives in Redis, so it is lost if Redis loses its data.
- (−) Positions de-duplicate only exact replays, because their timestamp is wall-clock.

**Alternatives considered** *(both inferred)*:

- Random event ids: replays would create duplicates.
- Exactly-once delivery across Redis and PG: not achievable without a distributed transaction.

**Evidence.** `tests/integration/test_pipeline.py::test_persister_dead_letters_poison_and_retries_failures`, `::test_full_lifecycle_is_persisted`.

## ADR-008: A kinematic ETA model behind an `EtaModel` protocol

**Context.**

- The prototype used a RandomForest trained on synthetic data ([vision.md](../vision.md) §3C). No real incident history exists to train a model on.
- The predicted ETA must match what the simulation will actually do.

**Decision.**

- `services/eta.py` defines the `EtaModel` protocol: a `name` plus `estimate(EtaQuery) -> EtaEstimate`.
- The shipped `KinematicEtaModel` (`name = "kinematic-v3"` since stage 5B - non-emergency RETURN legs are replayed tick by tick against the fixed signal plans; `kinematic-v2` in stage 5A; `kinematic-v1` before) integrates the remaining route with the same speed model the simulation uses (`services/speed_model.py` `build_profile`). It assumes every corridor junction ahead will be pre-empted.
- Congestion comes from the city-local hour of the simulation clock (`services/dispatch.py` `DispatchService.sim_clock`).
- The model name is stored in `dispatches.eta_model`, and `predicted_eta_s` and `actual_travel_s` are kept so a future model can be trained.
- The RandomForest was removed.

**Consequences.**

- (+) Transparent, deterministic and testable against simulated arrivals (`tests/unit/test_speed_eta.py`).
- (+) Another model can be injected through `create_app(eta_model=…)` and `Worker(eta_model=…)`.
- (−) Not learned from data. Congestion factors and junction delays are hand-set approximations ([data-sources.md](../data-sources.md)).
- (−) Conflicts at junctions make the actual time longer than predicted.

**Alternatives considered.**

- Keep the RandomForest: rejected because it was trained on synthetic data ([vision.md](../vision.md)).
- Use the raw OSRM duration *(inferred)*: it ignores the emergency speed advantage, junction slow-downs and pre-emption.

**Evidence.** `services/eta.py` module docstring; `tests/unit/test_speed_eta.py`.

## ADR-009: OSRM with a Redis route cache and no straight-line fallback

**Context.** Dispatch needs road geometry, per-segment speeds and turn manoeuvres (the lane advice uses these). The original roadmap said "OSRM street routing instead of straight lines" ([vision.md](../vision.md) §4).

**Decision.** `services/routing.py` `OsrmRouter`:

- Calls `GET /route/v1/driving/...` with `overview=full`, `geometries=geojson`, `steps=true`, `annotations=distance,duration,speed`.
- Timeout `osrm_timeout_s` = 8 s (connect at most 3 s).
- `osrm_retries` = 2 retries on transport errors and on HTTP 429/500/502/503/504, with exponential backoff and jitter. `NoRoute` and other 4xx responses are not retried.
- Successful responses are cached in Redis under `divert:osrm:driving:<sha256[:32]>` for `osrm_cache_ttl_s` = 3600 s, through the request-path guard.
- If every candidate fails, `RoutingUnavailable` returns **503** and nothing is written.

**Consequences.**

- (+) Failures are honest: no fake routes.
- (+) The cache cuts latency and load on the public server.
- (−) OSRM is on the request path. An outage blocks every new dispatch.
- (−) The default public demo server is fair use only, with no SLA. **Self-hosting is prepared (`infra/osrm/prepare.sh`, compose profile `osrm`) but has not been run.** The data directory is empty.
- (−) The car profile has no emergency-vehicle exemptions.

**Alternatives considered.**

- A straight-line fallback: explicitly rejected ([data-sources.md](../data-sources.md) §3, [simulation-model.md](../simulation-model.md) §1).
- Other routers such as GraphHopper or Valhalla *(inferred)*.

**Evidence.** `tests/unit/test_routing.py` (`test_request_shape_retry_and_cache`, `test_unavailable_after_retries`, `test_no_route_is_not_retried`); `tests/integration/test_dispatch_api.py::test_routing_failure_returns_503_and_writes_nothing`; `tests/network/test_osrm_public_smoke.py` (weekly in CI).

## ADR-010: Committed real-OSM snapshot files rather than live fetching

**Context.**

- Public Overpass instances are slow and rate-limited.
- The reference data must be reproducible and reviewable.
- Tests and startup must work offline.

**Decision.**

- `divert fetch-data` (`backend/src/divert/cities/osm.py`) runs offline. It splits each bbox into tiles of at most 0.25°, clusters `traffic_signals` nodes within `CLUSTER_RADIUS_M` = 40 m, keeps only named hospitals, and records provenance in `meta`: queries, endpoints, `fetched_at`, `osm_base`, licence and counts.
- A fetch either writes every city or nothing.
- The JSON files are committed under `backend/src/divert/cities/data/`.
- `divert seed` (`db/seed.py` `seed_city`) upserts them idempotently. It deactivates items that disappeared and never deletes them.

**Consequences.**

- (+) Reproducible, works offline, and changes show up as reviewable diffs.
- (+) ODbL provenance is recorded in each file.
- (−) The data goes stale until someone re-fetches it. The current snapshot was fetched on 2026-11-16.
- (−) About 460 KB of JSON lives in git.
- (−) The `traffic` block in the same file is hand-set, not OSM data.

**Alternatives considered.**

- Fetch at startup or at runtime: rejected because Overpass is unreliable and the policy is "nothing is invented, fail loudly" ([data-sources.md](../data-sources.md)).
- Ship a database dump *(inferred)*.

**Evidence.** `tests/unit/test_city_data.py`, `tests/unit/test_osm_fetch.py`, `tests/integration/test_seed.py`.

## ADR-011: Per-vehicle signal-claim fields with one `MULTI/EXEC` per city per tick

**Context.** This fixes the prototype's lost-update bug.

- Signal state was a shared value that writers read, modified and wrote back.
- Concurrent dispatches therefore overwrote each other's updates.
- A vehicle leaving a junction reset the junction for another vehicle of the same type (`tests/unit/test_regressions.py`, guarantees 2 and 3).

**Decision.**

- **Claims are owned per vehicle id.**
  - `divert:{city}:claims:<signal>` is a HASH with one field per vehicle.
  - `divert:{city}:vclaims:<vehicle>` is a SET of signals, used as a release-all index.
  - Vehicles and active signals are HASHes with one field per vehicle or per signal.
- The engine resolves each signal from all of its claims (`services/preemption.py` `resolve`):
  - The lowest ETA wins.
  - ETAs within `conflict_tie_s` = 5 s count as a tie, broken by AMBULANCE > FIRE_ENGINE > POLICE, then by vehicle id.
- Every vehicle's claims are released on arrival, cancel and error (`SimulationEngine._release_all`).
- `store/live.py` `write_city_batch` writes all of a city's deltas in **one `MULTI/EXEC`**, together with that city's history `XADD`s, command `XACK`s and `PUBLISH`es. It writes whole fields and members and never reads.

**Consequences.**

- (+) No lost updates.
- (+) A leaving vehicle never resets another vehicle's claim.
- (+) State, events and acks commit together. A failed write is kept and merged into the next tick (`merge_batches`).
- (−) The transaction touches global streams, so Redis Cluster is not supported.
- (−) The transaction grows with the number of vehicles in a city.

**Alternatives considered.**

- Claims keyed by signal or vehicle type: this was the original bug.
- A Lua script that does the read-modify-write inside Redis *(inferred)*.
- `WATCH`-based optimistic locking per signal *(inferred)*.

**Evidence.** `tests/unit/test_regressions.py::test_arrival_next_to_a_signal_releases_every_claim`, `::test_exception_mid_route_releases_every_claim`, `::test_cancel_releases_every_claim`, `::test_concurrent_dispatches_never_lose_updates`, `::test_leaving_vehicle_keeps_junction_preempted_for_same_type_vehicle`, `::test_one_multi_exec_per_city_per_tick_and_no_reads`.

## ADR-012: A request-path Redis guard and circuit breaker

**Context.** With Redis down, a dispatch used to take about 47 s: socket timeouts multiplied by client retries (`store/guard.py` docstring). An emergency dispatch must not depend on something that is only an optimisation or a live view.

**Decision.**

- The api uses a request-path client with 0.5 s socket and connect timeouts and no retries (`store/redis_store.py` `create_redis(request_path=True)`).
- Every call goes through `RedisGuard.call` (`store/guard.py`):
  - `asyncio.wait_for` with `redis_request_timeout_s` = 0.5 s.
  - Any failure opens the circuit for `redis_circuit_cooldown_s` = 5 s.
  - After the cooldown, the next call is a half-open trial.
- Each caller degrades in its own way:
  - The rate limiter fails open.
  - The OSRM cache is treated as a miss.
  - The sim clock falls back to wall time.
  - The post-commit `XADD` is left to the outbox relay.
  - Live reads return 503.
- `/readyz` depends on PostgreSQL only and reports Redis problems as `degraded` (`api/v1/health.py` `readyz`).

**Consequences.**

- (+) During a Redis outage a dispatch costs at most one 0.5 s deadline per 5 s cooldown.
- (+) api pods stay in rotation.
- (−) Rate limiting is off while Redis is down.
- (−) Live views are unavailable.
- (−) A degraded instance still reports ready, so monitoring must watch `degraded` and `divert_redis_circuit_open`.

**Alternatives considered.**

- The patient client with retries: this was the previous behaviour.
- Including Redis in readiness *(inferred)*: it would take every api pod out of rotation during a Redis outage, even though the outbox can carry dispatches through it.

**Evidence.** `tests/unit/test_resilience.py::test_guard_bounds_a_hanging_call_and_then_short_circuits`, `::test_guard_half_open_trial_closes_or_reopens`; `tests/integration/test_resilience.py::test_dispatch_and_history_keep_working_while_redis_is_down`, `::test_websocket_is_refused_quickly_while_redis_is_down`.

## ADR-013: An nginx template with runtime DNS resolution for the API upstream

**Context.** A static `proxy_pass http://api:8000` is resolved once, when nginx starts. That causes three problems:

- nginx fails to start if the api does not exist yet.
- It keeps a stale IP after the api container or pod is recreated.
- It never spreads load over scaled replicas.

**Decision.**

- `frontend/nginx/default.conf.template` is rendered by the image entrypoint. `envsubst` is limited by `NGINX_ENVSUBST_FILTER` to `DIVERT_API_UPSTREAM` and `NGINX_LOCAL_RESOLVERS` (`frontend/Dockerfile`).
- The config uses `resolver ${NGINX_LOCAL_RESOLVERS} valid=10s ipv6=off`, `set $api_upstream http://${DIVERT_API_UPSTREAM}` and `proxy_pass $api_upstream`.
- In compose the upstream is `api:8000`. In k8s it is `divert-api.$(POD_NAMESPACE).svc.cluster.local:80`.

**Consequences.**

- (+) nginx starts before the api exists, follows a recreated api within 10 s, and spreads requests over every address the name returns.
- (−) The resolver ignores DNS search domains, so k8s must use a fully qualified name. Adjust `cluster.local` if the cluster domain differs.
- (−) nginx drops inherited `add_header` directives in any location that declares its own, so the security headers are repeated per location.

**Alternatives considered.**

- A static upstream: this causes the problems above.
- Putting the API origin in the bundle and relying on CORS: not chosen. The browser talks only to its own origin, so no CORS is needed ([deployment.md](../deployment.md), `frontend/src/config.ts`). `VITE_API_BASE_URL` remains as an opt-in.

**Evidence.** `frontend/nginx/default.conf.template` header comment; [architecture.md](../architecture.md) §8 ("API container/pod recreated").

## ADR-014: OpenStreetMap standard tiles as the default basemap

**Context.** The ops console needs a basemap that works without an account. CARTO basemaps now return an "API key required" placeholder when no key is given ([deployment.md](../deployment.md)).

**Decision.**

- The default light layer is `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, with OSM attribution, at `maxZoom` 19 (`frontend/src/config.ts` `config.tiles`).
- Without a dark URL, dark mode applies a CSS filter to the light tiles.
- The build args `VITE_TILE_URL`, `VITE_TILE_URL_DARK` and `VITE_TILE_ATTRIBUTION` override it. compose maps them from `DIVERT_WEB_TILE_*`.

**Consequences.**

- (+) Works out of the box.
- (−) **For development and demos only**, under the OSMF tile usage policy. Production must set a keyed provider or self-hosted tiles.
- (−) The tile URL is fixed at build time, so the image must be rebuilt to change it.
- (−) The CSP must allow `img-src https:`.

**Alternatives considered.**

- CARTO: the earlier choice, which now needs a key. The root `README.md` and a comment in `frontend/src/lib/theme.ts` still mention CARTO.
- Self-hosted tiles: recommended for production, but not provided.

**Evidence.** `frontend/src/config.ts`, `frontend/Dockerfile`, `.env.example`.

## ADR-015: Keyset pagination for incident history

**Context.** Incident history grows without bound. Operators browse it newest first, with filters. `OFFSET` gets slower the deeper you page, and pages shift when new incidents arrive.

**Decision.**

- Order by `(reported_at DESC, id DESC)`.
- The cursor is base64url of the JSON `[reported_at, id]` (`db/repositories/incidents.py` `encode_cursor`, `decode_cursor`).
- The next page uses `WHERE (reported_at, id) < (:ts, :id)`. The query fetches `limit + 1` rows to know whether another page exists.
- The supporting index is `ix_incidents_city_reported (city_id, reported_at DESC, id DESC)`.
- `limit` is 1..200 (default 50) and `cursor` is at most 200 characters (`api/v1/history.py` `list_incidents`). A bad cursor returns 422.

**Consequences.**

- (+) Every page costs about the same, and pages stay stable while new incidents are inserted.
- (−) No jumping to page N and no total count.
- (−) The cursor is opaque but not signed. A client can craft any position, which is harmless.

**Alternatives considered.** `OFFSET`/`LIMIT` *(inferred)*.

**Evidence.** [api.md](../api.md) "History"; `tests/integration/test_api_reads.py`.

## Related

- [Architecture](03-architecture.md): the components these decisions shape, and their failure modes.
- [Data stores](05-data-stores.md): the tables, keys and streams referenced above.
- [Domain rules](02-domain-rules.md): pre-emption, speed and ETA rules in detail.
- [Runtime workflows](07-runtime-workflows.md): the outbox, fail-over and rebuild step by step.
- Reference: [architecture.md](../architecture.md), [data-model.md](../data-model.md), [simulation-model.md](../simulation-model.md), [data-sources.md](../data-sources.md), [vision.md](../vision.md).
