"""Runtime configuration. Environment only (``DIVERT_`` prefix); see .env.example."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIVERT_", env_file=None, extra="ignore", frozen=True, populate_by_name=True
    )

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    instance_id: str = Field(default="", description="defaults to hostname-pid at runtime")

    # --- PostgreSQL / PostGIS (system of record) ---------------------------------
    database_url: SecretStr = SecretStr("postgresql+asyncpg://divert:divert@localhost:5432/divert")
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_s: float = Field(default=10.0, gt=0)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)
    db_echo: bool = False

    # --- Redis (hot state + messaging) -------------------------------------------
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_socket_timeout_s: float = Field(default=5.0, gt=0)
    redis_request_timeout_s: float = Field(
        default=0.5, gt=0, le=10, description="API request-path Redis deadline (no retries)"
    )
    redis_circuit_cooldown_s: float = Field(
        default=5.0, gt=0, description="skip request-path Redis this long after a failure"
    )

    # --- OSRM ----------------------------------------------------------------------
    osrm_url: str = "https://router.project-osrm.org"
    osrm_timeout_s: float = Field(default=8.0, gt=0)
    osrm_retries: int = Field(default=2, ge=0)
    osrm_cache_ttl_s: int = Field(default=3600, ge=0)

    # --- Reference data --------------------------------------------------------------
    cities: Annotated[list[str], NoDecode] = ["delhi", "kolkata", "bengaluru"]
    units_per_station: int = Field(default=2, ge=1, le=20)

    # --- Dispatch / routing (design §2) -------------------------------------------------
    candidate_stations: int = Field(default=3, ge=1, le=10)
    route_alternatives: int = Field(
        default=3, ge=1, le=5, description="OSRM alternatives per request (1 = none)"
    )
    route_detours: int = Field(
        default=2, ge=0, le=4, description="via-point detours when OSRM returns < 2 routes"
    )

    # --- Corridor / pre-emption (design §4) ---------------------------------------------
    corridor_m: float = Field(default=30.0, gt=0, le=200)
    preempt_max_lead_s: float = Field(
        default=90.0,
        gt=0,
        validation_alias=AliasChoices(
            "preempt_max_lead_s", "DIVERT_PREEMPT_MAX_LEAD_S", "DIVERT_PREEMPT_LEAD_S"
        ),
        description="never claim with an ETA above this (DIVERT_PREEMPT_LEAD_S: deprecated alias)",
    )
    preempt_min_claim_distance_m: float = Field(
        default=150.0,
        gt=0,
        validation_alias=AliasChoices(
            "preempt_min_claim_distance_m",
            "DIVERT_PREEMPT_MIN_CLAIM_DISTANCE_M",
            "DIVERT_PREEMPT_DISTANCE_M",
        ),
        description="always claim within max(stopping distance + 50 m, this) "
        "(DIVERT_PREEMPT_DISTANCE_M: deprecated alias)",
    )
    preempt_release_past_m: float = Field(default=20.0, ge=0, description="box, 3-4 legs")
    preempt_release_past_large_m: float = Field(default=30.0, ge=0, description="box, 5+ legs")
    preempt_max_hold_s: float = Field(default=60.0, gt=0)
    preempt_recovery_s: float = Field(
        default=20.0, ge=0, description="green for the most-starved approach after a release"
    )
    priority_window_s: float = Field(
        default=10.0,
        ge=0,
        validation_alias=AliasChoices(
            "priority_window_s", "DIVERT_PRIORITY_WINDOW_S", "DIVERT_CONFLICT_TIE_S"
        ),
        description="a better priority rank this close to the earliest ETA wins "
        "(DIVERT_CONFLICT_TIE_S: deprecated alias)",
    )

    # --- Simulation ----------------------------------------------------------------------
    sim_tick_s: float = Field(default=0.5, gt=0, le=5)
    sim_time_scale: float = Field(default=1.0, gt=0, le=100)
    # Dwell times (design §1). Defaults are realistic ASSUMPTIONS, not measurements:
    # on-scene care + loading, firefighting, police scene work, ED handover.
    scene_time_ambulance_s: float = Field(default=480.0, ge=0)
    scene_time_fire_engine_s: float = Field(default=1200.0, ge=0)
    scene_time_police_s: float = Field(default=600.0, ge=0)
    hospital_handover_s: float = Field(default=600.0, ge=0)
    scene_time_s: float | None = Field(
        default=None,
        ge=0,
        description="DEPRECATED: one scene time for every service; used only for the "
        "services whose DIVERT_SCENE_TIME_<SERVICE>_S is not set",
    )
    position_sample_s: float = Field(default=5.0, gt=0)

    # --- Trip legs: planning + dynamic re-routing (design §1, §2) -------------------------
    leg_plan_backoff_s: float = Field(
        default=2.0, gt=0, description="first OSRM retry delay; doubles per failure"
    )
    leg_plan_backoff_max_s: float = Field(default=60.0, gt=0)
    hospital_candidates: int = Field(default=6, ge=1, le=20)
    reroute_min_saving_s: float = Field(default=60.0, ge=0)
    reroute_max_per_leg: int = Field(default=2, ge=0, le=10)
    reroute_behind_share: float = Field(
        default=0.25, ge=0, description="re-route when this share AND reroute_behind_s late"
    )
    reroute_behind_s: float = Field(default=60.0, ge=0)
    position_retention_days: int = Field(default=30, ge=1)

    # --- Worker / persister / outbox ---------------------------------------------------
    leader_lock_ttl_ms: int = Field(default=5_000, ge=500)
    heartbeat_max_age_s: float = Field(default=10.0, gt=0)
    persister_batch: int = Field(default=200, ge=1)
    persister_max_retries: int = Field(default=5, ge=1)
    persister_retry_idle_ms: int = Field(default=5_000, ge=100)
    persister_block_ms: int = Field(default=1_000, ge=10)
    retention_interval_s: float = Field(default=3600.0, gt=0)
    outbox_retention_days: int = Field(default=7, ge=1)
    outbox_relay_interval_s: float = Field(default=2.0, gt=0)
    outbox_relay_min_age_s: float = Field(default=2.0, ge=0)
    command_block_ms: int = Field(default=200, ge=10)
    finished_dispatch_ttl_s: int = Field(default=86_400, ge=60)
    metrics_port: int = Field(default=9100, ge=0, le=65535, description="worker/persister; 0=off")

    # --- API ------------------------------------------------------------------------------
    api_key: SecretStr | None = None
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:8080",
    ]
    rate_limit_per_min: int = Field(default=60, ge=1)
    ws_max_clients_per_city: int = Field(default=500, ge=1)
    ws_queue_size: int = Field(default=256, ge=8)
    reference_cache_ttl_s: float = Field(default=300.0, ge=0)
    trust_proxy_headers: bool = Field(
        default=False, description="use X-Forwarded-For for rate limits"
    )

    @field_validator("cities", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("cities")
    @classmethod
    def _lower_cities(cls, v: list[str]) -> list[str]:
        return [c.lower() for c in v]

    @model_validator(mode="after")
    def _no_wildcard_cors(self) -> Settings:
        if "*" in self.cors_origins:
            raise ValueError("DIVERT_CORS_ORIGINS must list explicit origins (credentials on)")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url.get_secret_value()

    def scene_time_for(self, service: str) -> float:
        """On-scene dwell of a service; the deprecated DIVERT_SCENE_TIME_S applies only to
        services whose own setting was not given."""
        field = {
            "AMBULANCE": "scene_time_ambulance_s",
            "FIRE_ENGINE": "scene_time_fire_engine_s",
            "POLICE": "scene_time_police_s",
        }[str(service)]
        if field not in self.model_fields_set and self.scene_time_s is not None:
            return self.scene_time_s
        return float(getattr(self, field))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
