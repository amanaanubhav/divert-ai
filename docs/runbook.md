# Runbook

Commands assume the compose stack (`docker compose exec ...`); on Kubernetes use
`kubectl -n divert exec deploy/divert-api -- ...` with the same commands.

```bash
R="docker compose exec -T redis redis-cli"
PSQL="docker compose exec -T postgres psql -U divert -d divert -c"
```

## Health at a glance

```bash
curl -s localhost:8000/readyz | jq        # ready = postgres; degraded = redis / worker heartbeat
$R GET divert:worker:leader               # "<instance>:<token>" of the current leader
$R HGETALL divert:worker:heartbeat
$R XINFO GROUPS divert:history            # persister lag / pending
$R XINFO GROUPS divert:commands           # worker lag / pending
$R XLEN divert:history:dlq
$PSQL "SELECT count(*) FROM outbox WHERE sent_at IS NULL"
```

## Alerts and actions

### No worker leader / heartbeat stale (`/readyz` `degraded: true`)
Vehicles freeze on the map; dispatches are still accepted (commands queue).
1. `docker compose ps worker` / `kubectl get pods -l app.kubernetes.io/name=divert-worker`.
2. Logs: `docker compose logs --tail=200 worker` - look for `became leader`,
   `leadership lost`, `redis error`.
3. If the lock is held by a dead instance it expires within `DIVERT_LEADER_LOCK_TTL_MS`;
   only if a hung process keeps renewing, restart that pod. **Never** delete the lock key
   while its owner is alive.
4. After take-over the log line `became leader` reports `restored` (from Redis) and
   `rebuilt` (from PostgreSQL) vehicle counts.

### Persister lag growing (`divert_persister_lag_seconds`, `history_stream_lag`)
1. Check PostgreSQL health and slow queries; `docker compose logs persister`.
2. Scale persisters (`docker compose up -d --scale persister=3`); the group shares work.
3. Live operation is unaffected; history catches up when the backlog drains.

### DLQ not empty (`divert_dlq_messages_total`)
```bash
cd backend && uv run divert dlq                 # list: reason, deliveries, raw event
uv run divert dlq --replay --id 1763...-0       # re-queue one after fixing the cause
uv run divert dlq --replay                      # re-queue all
```
Replays are safe: every write is idempotent on the event id.

### Outbox backlog > 0 for minutes (`divert_outbox_backlog`)
Commands are committed in PostgreSQL but not reaching Redis. Check Redis from the API
pods (`/readyz`), then the API logs for `outbox relay round failed`. The relay runs in
every API replica every `DIVERT_OUTBOX_RELAY_INTERVAL_S`; rows are re-sent in id order.
Inspect: `$PSQL "SELECT id, attempts, last_error, created_at FROM outbox WHERE sent_at IS NULL ORDER BY id"`.

### OSRM errors / 503 on dispatch
`divert_osrm_errors_total{kind}`; `curl "$DIVERT_OSRM_URL/route/v1/driving/77.209,28.6139;77.22,28.62"`.
The public demo rate-limits; production must self-host (deployment.md). Nothing is
written on a 503 - the caller can retry.

### Redis unreachable (`/readyz` `checks.redis: error`, `divert_redis_circuit_open` = 1)
The API stays ready on purpose: dispatches still return 201 (the command waits in the
outbox), history and analytics work; live endpoints return `503 live_state_unavailable`,
WebSockets close with 1013 and the map freezes (the worker cannot tick without Redis).
1. `docker compose ps redis` / your managed Redis console; `redis-cli -u $DIVERT_REDIS_URL ping`.
2. Once Redis answers, the circuit closes on the next call (<= `DIVERT_REDIS_CIRCUIT_COOLDOWN_S`),
   the outbox relay sends queued commands (`divert_outbox_backlog` -> 0) and the worker
   leader resumes (AOF) or rebuilds from PostgreSQL.
3. Log lines: `redis unavailable; circuit open` / `redis reachable again; circuit closed`.

### Redis lost its data
The next leader rebuilds active dispatches from PostgreSQL automatically (positions
and simulated elapsed time resume from the last persisted sample). Force a take-over by restarting the workers.
Un-acked commands lost with Redis are re-sent by the outbox relay only if they were never
stamped `sent_at`; for a dispatch whose command was lost after stamping, the rebuild
covers it (it is active in PostgreSQL).

### A unit looks stuck (DISPATCHED/ON_SCENE but no live vehicle)
```bash
$PSQL "SELECT u.id, u.status, d.id AS dispatch, d.dispatched_at, d.arrived_at
       FROM units u JOIN dispatches d ON d.id = u.current_dispatch_id
       WHERE d.dispatched_at < now() - interval '1 hour'"
$R HKEYS divert:{delhi}:vehicles
```
If the dispatch is active in PostgreSQL but not live, restart the workers (take-over
rebuilds it). To end it administratively, cancel the incident
(`DELETE /api/v1/cities/{city}/incidents/{id}`); the worker reports `cancelled` and the
persister frees the unit.

### Signal stuck PREEMPTED
Claims are per vehicle; a signal stays non-NORMAL only while some live vehicle claims it.
```bash
$R HGETALL divert:{delhi}:signals:active
$R HGETALL "divert:{delhi}:claims:J2800811770"     # which vehicle holds it
```
If the holder is not in `divert:{city}:vehicles`, the next leader take-over clears it
(stale active signals are deleted on the first tick).

## Routine operations

| Task | Command |
|---|---|
| migrate + seed | `docker compose run --rm migrate` / k8s Job |
| refresh OSM data | `uv run divert fetch-data --cache-dir ../.work/osm-cache`, commit JSON, re-seed |
| re-seed reference data | `uv run divert seed` (idempotent; never resets live units) |
| retention | automatic hourly (persister): positions > `DIVERT_POSITION_RETENTION_DAYS`, relayed outbox > `DIVERT_OUTBOX_RETENTION_DAYS` |
| take a unit out of service | `$PSQL "UPDATE units SET status='OUT_OF_SERVICE' WHERE id='AMB-...' AND status='AVAILABLE'"` |
| backup / restore | managed PITR (deployment.md); Redis needs no backup (rebuildable) |
| speed up the demo | `DIVERT_SIM_TIME_SCALE=10 docker compose up -d worker` |
