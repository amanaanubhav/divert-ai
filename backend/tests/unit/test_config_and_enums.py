from __future__ import annotations

import pytest
from pydantic import ValidationError

from divert.config import Settings
from divert.domain.enums import (
    SERVICE_FOR_INCIDENT,
    IncidentType,
    ServiceType,
    normalize_incident_type,
)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIVERT_CITIES", "Delhi, kolkata")
    monkeypatch.setenv("DIVERT_CORS_ORIGINS", "https://a.example,https://b.example")
    monkeypatch.setenv("DIVERT_CORRIDOR_M", "25")
    monkeypatch.setenv("DIVERT_DATABASE_URL", "postgresql+asyncpg://u:s3cret@db/divert")
    s = Settings()
    assert s.cities == ["delhi", "kolkata"]
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert s.corridor_m == 25
    assert "s3cret" not in repr(s)  # secrets are SecretStr
    assert s.sqlalchemy_url.endswith("@db/divert")


def test_defaults_match_spec() -> None:
    s = Settings()
    assert s.osrm_url == "https://router.project-osrm.org"
    assert (s.corridor_m, s.preempt_lead_s, s.preempt_distance_m) == (30, 45, 600)
    assert (s.sim_tick_s, s.sim_time_scale, s.scene_time_s) == (0.5, 1.0, 120)
    assert s.units_per_station == 2
    assert s.api_key is None


def test_wildcard_cors_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIVERT_CORS_ORIGINS", "*")
    with pytest.raises(ValidationError, match="explicit origins"):
        Settings()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MEDICAL", IncidentType.MEDICAL),
        ("health", IncidentType.MEDICAL),
        (" Ambulance ", IncidentType.MEDICAL),
        ("fire", IncidentType.FIRE),
        ("CRIME", IncidentType.CRIME),
        ("POLICE", IncidentType.CRIME),  # regression guarantee 1: POLICE alias
        ("police", IncidentType.CRIME),
    ],
)
def test_incident_type_aliases(raw: str, expected: IncidentType) -> None:
    assert normalize_incident_type(raw) is expected


@pytest.mark.parametrize("raw", ["", "FLOOD", "POLICE_STATION", "123"])
def test_unknown_incident_type_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="unknown incident type"):
        normalize_incident_type(raw)


def test_every_incident_type_has_a_service() -> None:
    assert set(SERVICE_FOR_INCIDENT) == set(IncidentType)
    assert set(SERVICE_FOR_INCIDENT.values()) == set(ServiceType)
