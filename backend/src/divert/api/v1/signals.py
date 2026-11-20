"""Live signal states (only non-NORMAL signals are stored/streamed)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from divert.api.deps import CityDep, CtxDep
from divert.store.live import read_active_signals

router = APIRouter(tags=["live"])


@router.get("/cities/{city}/signals/active", summary="Signals currently PREEMPTED or CONFLICT")
async def active_signals(city: CityDep, ctx: CtxDep) -> dict[str, Any]:
    signals = await ctx.guard.call(lambda: read_active_signals(ctx.redis, city.id))
    return {"city": city.id, "signals": signals}
