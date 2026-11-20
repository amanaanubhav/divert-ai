# 12 · Glossary

**What this covers:** the terms used across DIVERT's code, docs and console, A to Z, each with a one- or two-sentence meaning and where it lives in the code; then every identifier format the system produces (cities, stations, junctions, units, incidents, dispatches, commands, history `event_id`s, lock tokens, request ids, cursors and Redis keys), checked against the code and the committed snapshot data. For the rules behind the terms see [Domain rules](02-domain-rules.md); for the stores see [Data stores](05-data-stores.md).

Code paths are under `backend/src/divert/` unless they start with `frontend/` or `backend/tests/`.

## Terms A–Z

### A

| Term | Meaning |
|---|---|
| **Acceleration / braking** | Per-service rates the speed model uses: AMBULANCE 2.0, FIRE_ENGINE 1.0, POLICE 2.5 m/s² accelerating; 3.0 m/s² braking for all. `services/speed_model.py` `PROFILES`. |
| **Active signal** | A junction currently `PREEMPTED` or `CONFLICT`. Only these are stored (`divert:{city}:signals:active`) and streamed; a junction missing from the set is `NORMAL`. |
| **Alias (incident type)** | An accepted alternative spelling of an incident type: `HEALTH`, `AMBULANCE` → MEDICAL; `POLICE` → CRIME. Case-insensitive. `domain/enums.py` `INCIDENT_TYPE_ALIASES`. |
| **`along_m`** | Distance in metres along a route from its start, measured by cumulative haversine distance. Vehicles, junctions (after projection onto the polyline) and position samples all use it, so they compare directly. `domain/geo.py` `cumulative_distances`, `project_onto_polyline`. |
| **AOF** | Redis append-only file persistence. Compose runs Redis with `--appendonly yes --appendfsync everysec`, so live state usually survives a Redis restart (`docker-compose.yml`). |
| **Approach bearing** | Compass bearing of the route 5 m before a junction (`APPROACH_LOOKBACK_M`); the console draws it as an arrow. `services/dispatch.py` `build_corridor`. |
| **Arrival** | The moment `along_m ≥ route length − 0.5 m` (`ARRIVAL_TOLERANCE_M`). The vehicle releases all claims and goes `ON_SCENE`. `SimulationEngine._arrive`. |
| **Arrival crawl** | The 1.0 m/s speed limit at the route end (`ARRIVAL_SPEED_MPS`), which keeps the time to stop finite. |
| **At-least-once delivery** | Every message may arrive more than once, never zero times: the outbox relay, the commands stream and the history stream all redeliver, and every consumer is idempotent. |
| **Average junction delay** | A per-city constant (Delhi 35 s, Kolkata 40 s, Bengaluru 40 s) assumed lost at a non-pre-empted signalised junction; used only for the time-saved estimate. Snapshot `traffic.avg_junction_delay_s`. |

### B

| Term | Meaning |
|---|---|
| **BBox (bounding box)** | A city's rectangle `[south, west, north, east]`. Incidents outside it are rejected (422). Inclusive on every edge. `domain/geo.py` `BBox.contains`. |
| **Braking envelope** | The speed limit curve built backwards from every point cap (junctions, route end) at 3 m/s², so a vehicle slows *before* a junction. `SpeedProfile.__init__`. |

### C

| Term | Meaning |
|---|---|
| **Callsign** | A unit's id, e.g. `AMB-Hn12052451552-1`. See [ID formats](#unit--vehicle-id-callsign). |
| **Candidate station** | One of the `DIVERT_CANDIDATE_STATIONS` (3) nearest active stations of the right service with a free unit; each is routed and ranked by ETA. `DispatchService._plans`. |
| **Circuit breaker** | `RedisGuard` (`store/guard.py`): every request-path Redis call has a 0.5 s deadline, and after a failure Redis is skipped for 5 s, so a Redis outage never stalls the API. |
| **Claim** | A vehicle's hold on one corridor junction, owned **per vehicle id**. Made when the ETA to the junction ≤ 45 s or it is ≤ 600 m ahead; released 20 m after passing, and always on arrival, cancel or error. Stored per (signal, vehicle) in `divert:{city}:claims:<signal>`. `services/preemption.py`. |
| **Claimant** | One vehicle claiming a junction, with its service and current ETA; the input to conflict resolution. `preemption.Claimant`. |
| **Cleared** | The end of the scene: after `DIVERT_SCENE_TIME_S` (120 s) on scene, the vehicle is removed, the incident becomes `RESOLVED` and the unit `AVAILABLE`. History event `cleared`. |
| **Command** | A message from the API to the worker on the `divert:commands` stream: `start_dispatch` or `cancel_dispatch`. `store/events.py` `CommandType`. |
| **Conflict** | Two or more vehicles claiming one junction. The lowest ETA wins (`PREEMPTED`); the others get `CONFLICT` and the 15 km/h junction cap. ETAs within 5 s tie, broken by AMBULANCE > FIRE_ENGINE > POLICE, then vehicle id. `preemption.resolve`. |
| **`CONFLICT`** | A `SignalState`: a junction with two or more claimants, or one losing vehicle's claim. In history, a claim keeps `CONFLICT` if it ever lost. |
| **Congestion factor** | A city's share of OSRM free-flow speed allowed in a given local hour, 24 values per city (e.g. Delhi 0.46 at 18:00). Hand-set, not measured. Snapshot `traffic.hourly_congestion`; `TrafficProfile`. |
| **Consumer group** | A Redis Streams feature that shares a stream among consumers and tracks unacknowledged (pending) entries. Groups: `worker` on `divert:commands`, `persister` on `divert:history`. `store/redis_store.py` `GROUP_WORKER`, `GROUP_PERSISTER`. |
| **Corridor** | The ordered list of active junctions within `DIVERT_CORRIDOR_M` (30 m) of a dispatch's route, each with `along_m`, `offset_m`, lane advisory and approach bearing. Fixed at dispatch. `build_corridor`. |
| **CORS** | Browser cross-origin rules. DIVERT allows only the explicit `DIVERT_CORS_ORIGINS`; `*` is refused at startup. |

### D

| Term | Meaning |
|---|---|
| **Degraded** | `/readyz` field: `true` when Redis or the worker heartbeat is not OK. The API stays ready (200) as long as PostgreSQL answers. `api/v1/health.py`. |
| **Dispatch** | One unit sent to one incident: route, corridor, predicted ETA, outcome. Row in `dispatches`; ends with `end_reason` `CLEARED`, `CANCELLED` or `FAILED`. |
| **DLQ (dead-letter queue)** | `divert:history:dlq`: history events the persister could not apply after `DIVERT_PERSISTER_MAX_RETRIES` (5) deliveries, or that can never apply. Inspect or replay with `divert dlq [--replay] [--id <stream id>]`. |
| **`dt`** | Simulated seconds per tick: `DIVERT_SIM_TICK_S × DIVERT_SIM_TIME_SCALE` (default 0.5 × 1). |

### E

| Term | Meaning |
|---|---|
| **Emergency advantage** | The factor 1.3 by which an emergency vehicle beats congested traffic speed (sirens, traffic yielding). `speed_model.EMERGENCY_ADVANTAGE`. |
| **`EN_ROUTE`** | Vehicle and incident status after the worker starts the dispatch, until arrival. |
| **Estimated time saved** | `est_time_saved_s` = junctions passed while `PREEMPTED` for the vehicle × the city's average junction delay. Always labelled an estimate. `SimulationEngine._arrive`. |
| **ETA** | Estimated time of arrival, in simulated seconds. `predicted_eta_s` is fixed at dispatch; `eta_s` is refreshed every tick; `eta_at_claim_s` is the ETA to a junction when it was claimed. |
| **`EtaModel` / `KinematicEtaModel`** | The ETA interface (a `Protocol`) and its shipped implementation `kinematic-v3`: emergency legs integrate the speed envelope assuming every corridor junction is pre-empted; RETURN legs are replayed tick by tick against the fixed signal plans. `services/eta.py`. |
| **ETA MAPE** | Mean absolute percentage error of predicted vs actual travel time, `avg(abs(actual − predicted) / actual)`, reported as `dispatches.eta_mape` in the analytics summary (`db/repositories/analytics.py` `summary`). |
| **`event_id`** | The deterministic id of a history event (max 64 characters), used as the idempotency key in PostgreSQL. See [event_id formats](#history-event_id). |

### F – G

| Term | Meaning |
|---|---|
| **Failed** | A dispatch ended by an error: a crash while stepping, an ETA error, an invalid start command, a failed rebuild, or a newer dispatch of the same unit. Incident `FAILED` with `failure_reason`. |
| **Fencing token** | A monotonically increasing number a lock holder sends with each write so stale holders are rejected. **DIVERT has none**; its lease bounds but cannot fully exclude two leaders (`store/locks.py`). |
| **Finished set** | `divert:{city}:finished`, a sorted set of dispatch ids the worker has ended, kept 86 400 s (`DIVERT_FINISHED_DISPATCH_TTL_S`), which makes a redelivered `start_dispatch` a no-op. |
| **GiST** | PostgreSQL's generalised search tree index, used on every geography column for KNN and `ST_DWithin`. |
| **Green corridor** | DIVERT's goal: signals turning green just ahead of the vehicle and back right after it, moving with it. |

### H

| Term | Meaning |
|---|---|
| **Hash tag** | The `{city}` part of per-city Redis keys (e.g. `divert:{delhi}:vehicles`), which would keep one city's keys in one Redis Cluster slot. Redis Cluster is still not supported (global streams in the same transaction). |
| **Haversine** | Great-circle distance on a sphere of radius 6 371 008.8 m (`EARTH_RADIUS_M`). All DIVERT distances use it; the frontend uses the same model (`frontend/src/lib/geo.ts`). |
| **Heartbeat** | `divert:worker:heartbeat`: the leader's instance, time, tick, vehicle count and sim time, written every tick. `/readyz` calls the worker `stale` when it is older than `DIVERT_HEARTBEAT_MAX_AGE_S` (10 s). |
| **History event** | A worker message on `divert:history` describing something that happened (`en_route`, `preemption_claimed`, `arrived`, …). The persister writes it to PostgreSQL. `store/events.py` `HistoryEventType`. |

### I – K

| Term | Meaning |
|---|---|
| **Idempotent** | Applying something twice has the same effect as once. DIVERT relies on it for redelivered commands and events, re-seeding and cancel. |
| **Incident** | A reported emergency at a point in a city, with a type and a status. Row in `incidents`. |
| **Incident type** | `MEDICAL`, `FIRE` or `CRIME` (`IncidentType`); `reported_type` keeps the spelling the caller sent. |
| **Junction** | A signalised road junction: one or more OSM `traffic_signals` nodes clustered within 40 m. DIVERT's unit of pre-emption, stored in the `signals` table. Also called *signal* in the code and API. |
| **Junction cap** | The speed limit at a corridor junction: 35 km/h if pre-empted for this vehicle, otherwise 15 km/h. `speed_model.junction_cap_mps`. |
| **Keyset pagination** | Paging by the last row's sort key (`reported_at`, `id`) instead of an offset; the cursor is opaque. `IncidentRepository.list_incidents`. |
| **KNN** | k-nearest-neighbour search. DIVERT orders stations by the PostGIS `<->` distance operator on the GiST index to find the nearest stations with a free unit. `nearest_stations_with_free_units`. |

### L

| Term | Meaning |
|---|---|
| **Lane advisory** | The lane other traffic should clear at a junction: `L` when the vehicle turns left there, `R` (outer lane) for straight, right turns and U-turns. India drives on the left. `domain/route.py` `lane_advisory`. |
| **Leader** | The one worker instance holding the leader lock. It alone ticks the simulation and writes live state, for **all** cities. |
| **Leader lock / lease** | The Redis key `divert:worker:leader`, set with `SET NX PX` for `DIVERT_LEADER_LOCK_TTL_MS` (5000 ms) and renewed every TTL/3. The leader trusts its lease only until last renewal + TTL − TTL/5. `store/locks.py` `LeaderLock`. |
| **Live state** | Hot, rebuildable state in Redis written only by the leader: vehicles, routes, claims, active signals, tick. |

### M – N

| Term | Meaning |
|---|---|
| **Maneuver** | An OSRM turn instruction (type, modifier, location), placed on the route by distance; used for the lane advisory. `domain/route.py` `Maneuver`. |
| **Motion profile** | The `SpeedProfile` the simulation moves a vehicle with: real claim states (only its `PREEMPTED` junctions get 35 km/h). Contrast with the ETA model's plan profile, which assumes all are pre-empted. `SimulationEngine._motion_profile`. |
| **MULTI/EXEC** | A Redis transaction. Each city's tick (state deltas, history events, command acks, pub/sub messages) is written in exactly one. `store/live.py` `write_city_batch`. |
| **`NORMAL`** | A junction with no claim. Never stored or streamed. |

### O

| Term | Meaning |
|---|---|
| **ODbL** | Open Database Licence. OpenStreetMap data, and DIVERT's derived snapshots, are licensed under it; attribution "© OpenStreetMap contributors" is required. |
| **`offset_m`** | Distance from a junction to the route line; corridor junctions have `offset_m ≤ DIVERT_CORRIDOR_M`. |
| **`ON_SCENE`** | Status after arrival, until the scene time ends (vehicle, incident and unit). |
| **OSRM** | Open Source Routing Machine, the road router. DIVERT calls `/route/v1/driving` and uses its geometry, per-segment speeds and maneuvers. Default: the public demo server (development only). |
| **Outbox (transactional outbox)** | The `outbox` table. A worker command is inserted in the same transaction as the incident and dispatch, then XADDed to Redis right after commit; a relay re-sends rows still unsent after 2 s. `services/outbox.py`. |
| **`OUT_OF_SERVICE`** | Unit status set when re-seeding deactivates its station. No code path reverses it. |
| **Overpass** | The OpenStreetMap query API used to fetch signals and stations. `cities/osm.py`. |

### P

| Term | Meaning |
|---|---|
| **Passed** | A corridor junction the vehicle is more than 20 m beyond. It is released (reason `passed`) and never claimed again. |
| **Persister** | The process that consumes `divert:history` (group `persister`) and writes PostgreSQL idempotently; also runs retention. `persister.py`. |
| **Position sample** | A `vehicle_position` event every `DIVERT_POSITION_SAMPLE_S` (5) simulated seconds, kept `DIVERT_POSITION_RETENTION_DAYS` (30) days. |
| **Pre-emption** | Giving a traffic signal to an approaching emergency vehicle. In DIVERT a junction is pre-empted for a vehicle when it holds the winning claim on it. |
| **`PREEMPTED`** | `SignalState` of a junction with one claimant, and of the winning claim on a contested junction. |
| **`preempted_passed`** | Count of corridor junctions a vehicle has passed while `PREEMPTED` for it; the multiplier of the time-saved estimate. |
| **Priority (service)** | Tie-break order inside the 5 s tie window: AMBULANCE (0) > FIRE_ENGINE (1) > POLICE (2). `SERVICE_PRIORITY`. |
| **Pub/sub channel** | `divert:events:<city>`: the leader publishes tick and lifecycle messages; each API process fans them out to its WebSocket clients. |

### R

| Term | Meaning |
|---|---|
| **Rebuild** | On take-over, the new leader re-creates from PostgreSQL any active dispatch that Redis lost, resuming from its last position sample and simulated `elapsed_s`. `Worker._rebuild_from_pg`. |
| **Release / release all** | Dropping one claim (passed) or every claim of a vehicle (arrival, clear, cancel, error). Regression guarantee 2. |
| **Resolution** | Per-tick outcome of conflict resolution for one junction: state, winner and each claimant's state. `preemption.Resolution`. |

### S

| Term | Meaning |
|---|---|
| **Scene time** | Simulated seconds a vehicle stays `ON_SCENE` before it is cleared: `DIVERT_SCENE_TIME_S`, default 120. |
| **Segment cap** | Speed limit on one route segment: `max(1, min(v_max, max(1, v_osrm) × congestion × 1.3))` m/s. `speed_model.segment_cap_mps`. |
| **Service** | Which kind of unit answers an incident: `AMBULANCE`, `FIRE_ENGINE`, `POLICE` (`ServiceType`). |
| **Signal** | In the code and API, a *junction* (see above), not a single signal head. |
| **Sim clock (simulation clock)** | The time the simulation runs at. Equal to wall time at time scale 1; otherwise it advances `dt` per tick and is kept in `divert:sim:clock`. Congestion uses its local hour. |
| **SKIP LOCKED** | PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`: lock a row others have not locked, skipping theirs instead of waiting. Concurrent dispatches therefore never get the same unit. `ReferenceRepository.lock_free_unit`. |
| **Snapshot (data)** | A committed city file `cities/data/<city>.json`: OSM stations and junctions plus metadata and the traffic profile. |
| **Snapshot (stream)** | The first WebSocket message: all vehicles with routes and all active signals of the city. |
| **Standby** | A worker instance waiting for the leader lock; it retries every TTL/3. |
| **Station** | A base for units: a named hospital (ambulances), fire station or police station from OSM. |
| **Stream (Redis)** | An append-only log. DIVERT uses `divert:commands` (API → worker), `divert:history` (worker → persister) and `divert:history:dlq`. |

### T

| Term | Meaning |
|---|---|
| **Take-over** | What a worker does on becoming leader: load cities, resume the sim clock and tick number, restore vehicles and claims from Redis, rebuild lost ones from PostgreSQL. `Worker.take_over`. |
| **Tick** | One simulation step: every vehicle moves `dt`, ETAs refresh, claims update and resolve, and each city's batch is written in one `MULTI/EXEC`. Every `DIVERT_SIM_TICK_S` (0.5 s) of wall time. Also the WebSocket message carrying the result. |
| **Tick number** | A monotonic counter incremented once per tick for the whole engine, stored per city in `divert:{city}:tick`; a new leader resumes from the highest value. Not dense per city. |
| **Tie window** | `DIVERT_CONFLICT_TIE_S` (5 s): claimants this close to the best ETA are decided by service priority. |
| **Time scale** | `DIVERT_SIM_TIME_SCALE` (1, max 100): simulated seconds per wall-clock second. Set the same value on API and worker. |

### U – X

| Term | Meaning |
|---|---|
| **Unit** | One vehicle of a station (2 per station by default). Status `AVAILABLE`, `DISPATCHED`, `ON_SCENE` or `OUT_OF_SERVICE`. |
| **Vehicle** | A unit while the simulation moves it; identified by the unit's callsign. Live status `EN_ROUTE` or `ON_SCENE`. |
| **Wall clock** | Real UTC time. Event timestamps are wall clock; durations (`elapsed_s`, `travel_s`) are simulated seconds. |
| **XACK / XAUTOCLAIM** | Redis Streams commands: acknowledge an entry; take over entries another consumer read but never acknowledged. The leader reclaims commands idle for the lease TTL; persisters reclaim idle history entries. |

## ID formats

### City id

Lower-case: `delhi`, `kolkata`, `bengaluru` (`DIVERT_CITIES`). The API path parameter must match `^[a-z0-9_-]{1,32}$` (`api/deps.py` `CityId`); Redis keys require `^[A-Za-z0-9_.-]{1,64}$` (`store/redis_store.py` `_ID_RE`).

### Station id

`<service letter><OSM element letter><OSM id>`, built by `extract_stations` in `cities/osm.py`:

| Part | Values |
|---|---|
| Service letter | `H` hospital → AMBULANCE, `F` fire station → FIRE_ENGINE, `P` police → POLICE (`STATION_KINDS`) |
| OSM element letter | `n` node, `w` way, `r` relation (first letter of the OSM type) |
| OSM id | the element's numeric id |

Example from the Kolkata snapshot: `Pn651795823`, "Jadavpur Police Station", `osm: "node/651795823"`. Every station id in the three snapshots matches its `osm` field and service (`test_station_ids_encode_osm_element`).

| Prefix | Delhi | Kolkata | Bengaluru |
|---|---:|---:|---:|
| `Hn` / `Hw` / `Hr` | 700 / 145 / 2 | 223 / 70 / 1 | 863 / 215 / 8 |
| `Fn` / `Fw` / `Fr` | 23 / 19 / 0 | 3 / 3 / 1 | 4 / 18 / 0 |
| `Pn` / `Pw` / `Pr` | 154 / 81 / 7 | 30 / 14 / 0 | 69 / 65 / 0 |

### Junction (signal) id

`J<smallest member OSM node id>` (`cluster_signals` in `cities/osm.py`). The members are kept in `signals.osm_node_ids`. Example: Kolkata `J10792907223` clusters nodes 10792907223, 10792907224, 10792907225. All 1104 junctions in the snapshots satisfy this rule (`test_signal_ids_trace_back_to_osm_nodes`). Up to 8 nodes form one junction (Delhi, Bengaluru).

### Unit / vehicle id (callsign)

`<prefix>-<station id>-<ordinal>` (`db/seed.py` `callsign`), with prefix from `CALLSIGN_PREFIX` and ordinal 1 … `DIVERT_UNITS_PER_STATION`:

| Service | Prefix | Example |
|---|---|---|
| AMBULANCE | `AMB` | `AMB-Hn12052451552-1` |
| FIRE_ENGINE | `FIRE` | `FIRE-Fw78595558-2` |
| POLICE | `PCR` | `PCR-Pn651795823-1` |

The station id keeps its service letter, so the callsign is `AMB-Hn…`, not `AMB-n…`. The live vehicle uses the unit's callsign as its id. Units are unique per `(station_id, ordinal)`.

### Incident, dispatch and outbox ids

| Id | Format | Source |
|---|---|---|
| Incident id | UUID v4 | `uuid.uuid4()` in `DispatchService._dispatch` |
| Dispatch id | UUID v4 | same |
| Outbox id | `BIGINT` identity, added to the relayed command as `outbox_id` | `db/models.py` `Outbox.id`; `services/outbox.py` `publish` |

### Command ids

Commands carry a `command_id` (informational; the worker de-duplicates by `dispatch_id`, not by this field):

| Command | `command_id` |
|---|---|
| `start_dispatch` | `start:<dispatch id hex>` |
| `cancel_dispatch` | `cancel:<dispatch id hex>` |

`<… hex>` is the UUID's 32 hex digits without hyphens (`uuid.hex`).

### History `event_id`

Deterministic, so a replay after failover is de-duplicated by `ON CONFLICT (event_id)`. Worker ids are built by `event_id(prefix, *parts)` in `store/events.py`: parts are joined with `:`, and **hyphens are removed** from each part, so a dispatch UUID appears as 32 hex digits. If the result is longer than 64 characters it becomes `<prefix>:h:<sha256 hex prefix>`, cut to 64 characters. The API's own events use `uuid.hex` directly.

| Prefix | Event type | Format | Written by |
|---|---|---|---|
| `rep:` | `reported` | `rep:<incident hex>` | API, `IncidentRepository.create_dispatch` |
| `dsp:` | `dispatched` | `dsp:<dispatch hex>` | API, same |
| `cxr:` | `cancel_requested` | `cxr:<incident hex>` | API, `IncidentRepository.mark_cancelled` |
| `enr:` | `en_route` | `enr:<dispatch hex>` | worker, `SimulationEngine._announce` |
| `pos:` | `vehicle_position` | `pos:<dispatch hex>:<sample n>` (n = 0, 1, 2, …) | worker, `_sample` (stored in `vehicle_positions`, keyed by `(dispatch_id, ts)`) |
| `clm:` | `preemption_claimed` | `clm:<dispatch hex>:<signal id>` (also the `signal_preemptions.event_id` of the claim) | worker, `_update_claims` |
| `cfl:` | `preemption_conflict` | `cfl:<dispatch hex>:<signal id>:<n>` (n-th time this claim entered `CONFLICT`, from 1) | worker, `_resolve_city` |
| `rel:` | `preemption_released` | `rel:<dispatch hex>:<signal id>` | worker, `_release` |
| `arr:` | `arrived` | `arr:<dispatch hex>` | worker, `_arrive` |
| `clr:` | `cleared` | `clr:<dispatch hex>` | worker, `_end` |
| `cxl:` | `cancelled` | `cxl:<dispatch hex>` | worker, `_end`, `cancel_dispatch` |
| `fail:` | `failed` | `fail:<dispatch hex>` | worker, `_end`, `Worker._fail_unstarted`, `Worker._rebuild_from_pg` |

Example from a real engine run (dispatch `00000000-0000-0000-0000-000000000001`, junction `J1`): `clm:00000000000000000000000000000001:J1`.

### Other identifiers

| Identifier | Format | Source |
|---|---|---|
| Worker / persister instance id | `DIVERT_INSTANCE_ID`, else `<hostname>-<pid>`; also the stream consumer name | `logging.py` `default_instance_id` |
| Leader lock token | `<instance id>:<16 hex characters>` (value of `divert:worker:leader`) | `LeaderLock.__init__` |
| Request id | `X-Request-ID` if it matches `^[A-Za-z0-9._-]{1,64}$`, else a new UUID v4 hex | `api/middleware.py` `RequestContextMiddleware` |
| History cursor | URL-safe base64, padding stripped, of the JSON `[reported_at ISO 8601, incident UUID]` | `db/repositories/incidents.py` `encode_cursor` |
| Redis stream entry id | Redis `<milliseconds>-<sequence>`, e.g. used by `divert dlq --replay --id` | Redis |
| OSM reference | `node/<id>`, `way/<id>`, `relation/<id>` (station `osm` field) | snapshots |
| OSRM cache key | `divert:osrm:driving:<first 32 hex of sha256(coordinates)>` | `redis_store.osrm_route_cache` |
| Rate-limit key | `divert:rl:mutate:<first 16 hex of sha256(client)>:<epoch seconds ÷ 60>` | `redis_store.rate_limit` |

Per-city live keys follow `divert:{<city>}:<name>[:<id>]`, e.g. `divert:{delhi}:claims:J2800811770` or `divert:{delhi}:route:AMB-Hn1-1`. The full keyspace is in [Data stores](05-data-stores.md).

## Related

- [01 · Specification](01-specification.md)
- [02 · Domain rules](02-domain-rules.md)
- [05 · Data stores](05-data-stores.md): tables and Redis keys behind these ids
- [06 · Data flows](06-data-flows.md) and [07 · Runtime workflows](07-runtime-workflows.md): where each event is produced and consumed
- [11 · Configuration](11-configuration.md): the `DIVERT_*` settings named here
- [api.md](../api.md), [data-model.md](../data-model.md), [data-sources.md](../data-sources.md)
