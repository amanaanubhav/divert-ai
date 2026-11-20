"""Request schemas (responses are documented in docs/api.md and covered by tests)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Lat = Annotated[float, Field(ge=-90, le=90)]
Lng = Annotated[float, Field(ge=-180, le=180)]


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    type: str = Field(
        min_length=0,
        max_length=32,
        description="MEDICAL | FIRE | CRIME (case-insensitive; aliases HEALTH, AMBULANCE, POLICE)",
        examples=["MEDICAL", "POLICE"],
    )
    location: tuple[Lat, Lng] = Field(
        description="[lat, lng] (WGS84)", examples=[[28.6139, 77.209]]
    )
    description: str | None = Field(default=None, max_length=1000)
