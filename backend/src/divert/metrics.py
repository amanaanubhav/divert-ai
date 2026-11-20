"""Prometheus metrics (one registry per process; every service exposes ``/metrics``).

api       -> GET /metrics on the API port (gauges for backlog/lag refreshed per scrape)
worker    -> :DIVERT_METRICS_PORT/metrics
persister -> :DIVERT_METRICS_PORT/metrics
"""

from __future__ import annotations

from typing import Final

from prometheus_client import Counter, Gauge, Histogram

NS: Final = "divert"

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"], namespace=NS
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    namespace=NS,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DISPATCHES = Counter(
    "dispatches_total", "Dispatch attempts by outcome", ["city", "service", "outcome"], namespace=NS
)
DISPATCH_LATENCY = Histogram(
    "dispatch_duration_seconds",
    "POST /incidents end-to-end latency",
    namespace=NS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
OSRM_LATENCY = Histogram(
    "osrm_request_duration_seconds",
    "OSRM route request latency",
    ["outcome"],
    namespace=NS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
)
OSRM_ERRORS = Counter("osrm_errors_total", "OSRM failures", ["kind"], namespace=NS)
ACTIVE_VEHICLES = Gauge(
    "active_vehicles", "Vehicles live in the simulation", ["city"], namespace=NS
)
ACTIVE_SIGNALS = Gauge("active_signals", "Signals not NORMAL", ["city"], namespace=NS)
TICK_DURATION = Histogram(
    "tick_duration_seconds",
    "Worker tick (all cities) compute + Redis write time",
    namespace=NS,
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)
TICK_OVERRUNS = Counter(
    "tick_overruns_total", "Ticks that took longer than the period", namespace=NS
)
PREEMPTIONS = Counter(
    "preemptions_total", "Signal claims by initial state", ["city", "state"], namespace=NS
)
PREEMPTION_CONFLICTS = Counter(
    "preemption_conflicts_total", "Claims that became CONFLICT", ["city"], namespace=NS
)
PREEMPTION_OUTCOMES = Counter(
    "preemption_releases_total",
    "Claims released, by outcome (CLEARED|TIMEOUT|CANCELLED|REROUTED)",
    ["city", "outcome"],
    namespace=NS,
)
PREEMPTION_PHASE_STATE = Counter(
    "preemption_greens_total",
    "Claims that got their green, by green_extend vs red_truncate",
    ["city", "kind"],
    namespace=NS,
)
SIGNALS_STARVED = Gauge(
    "signals_starved",
    "Junctions pre-empted for more than 50% of the last 5 minutes (operator signal)",
    ["city"],
    namespace=NS,
)
ROUTE_CANDIDATES = Histogram(
    "route_candidates",
    "Route candidates scored per dispatch (all stations)",
    namespace=NS,
    buckets=(1, 2, 3, 4, 6, 9, 12, 15),
)
LEGS_PLANNED = Counter(
    "legs_planned_total", "Trip legs planned by the LegPlanner", ["city", "kind"], namespace=NS
)
LEG_PLAN_FAILURES = Counter(
    "leg_plan_failures_total",
    "LegPlanner attempts that failed (retried with backoff)",
    ["city", "kind", "reason"],
    namespace=NS,
)
LEG_PLAN_DURATION = Histogram(
    "leg_plan_duration_seconds",
    "LegPlanner: request to leg_planned sent (successful attempt)",
    ["kind"],
    namespace=NS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
REROUTES = Counter(
    "reroutes_total",
    "Re-route evaluations by outcome (replaced|kept|failed)",
    ["city", "outcome"],
    namespace=NS,
)
WORKER_IS_LEADER = Gauge("worker_is_leader", "1 if this worker holds the leader lock", namespace=NS)
WORKER_HEARTBEAT_AGE = Gauge(
    "worker_heartbeat_age_seconds", "Age of the leader heartbeat (api view)", namespace=NS
)
COMMANDS = Counter("worker_commands_total", "Commands applied", ["type", "outcome"], namespace=NS)
HISTORY_EVENTS = Counter(
    "history_events_total", "History events emitted by the worker", ["type"], namespace=NS
)
PERSISTED = Counter(
    "persister_events_total", "History events persisted", ["type", "outcome"], namespace=NS
)
PERSISTER_LAG = Gauge(
    "persister_lag_seconds", "Now minus the event time of the last persisted event", namespace=NS
)
HISTORY_STREAM_LAG = Gauge(
    "history_stream_lag", "Entries not yet delivered to the persister group", namespace=NS
)
HISTORY_PENDING = Gauge(
    "history_stream_pending", "Entries delivered but not acked (persister)", namespace=NS
)
DLQ_MESSAGES = Counter("dlq_messages_total", "Messages moved to a DLQ", ["stream"], namespace=NS)
OUTBOX_BACKLOG = Gauge("outbox_backlog", "Outbox rows not yet relayed to Redis", namespace=NS)
OUTBOX_RELAYED = Counter("outbox_relayed_total", "Outbox rows relayed", ["path"], namespace=NS)
WS_CLIENTS = Gauge("ws_clients", "Open WebSocket clients", ["city"], namespace=NS)
RATE_LIMITED = Counter(
    "rate_limited_total", "Requests rejected by the rate limit", ["scope"], namespace=NS
)
REDIS_CIRCUIT_OPEN = Gauge(
    "redis_circuit_open", "1 while request-path Redis calls are short-circuited", ["guard"],
    namespace=NS,
)  # fmt: skip
REDIS_CIRCUIT_TRIPS = Counter(
    "redis_circuit_trips_total", "Times the request-path Redis circuit opened", ["guard"],
    namespace=NS,
)  # fmt: skip
