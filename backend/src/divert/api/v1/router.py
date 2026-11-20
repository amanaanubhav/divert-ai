"""``/api/v1`` router."""

from __future__ import annotations

from fastapi import APIRouter

from divert.api.v1 import analytics, cities, history, incidents, signals, stream, vehicles

api_router = APIRouter(prefix="/api/v1")
for module in (cities, incidents, vehicles, signals, history, analytics, stream):
    api_router.include_router(module.router)
