"""WebSocket ``/api/v1/cities/{city}/stream``.

On connect: ``snapshot`` (vehicles with routes + active signals). Then every message the
worker publishes for the city: ``tick``, ``vehicle_added``, ``vehicle_removed``,
``incident_updated``. Every message has ``type`` and ``v``. Clients may send
``{"type": "ping"}`` (answered with ``pong``); anything else is ignored. A slow client is
closed with 1013 and should reconnect (it then receives a fresh snapshot).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from divert.api.context import AppContext
from divert.api.v1.vehicles import public_vehicle
from divert.api.ws_hub import HubFull, Item
from divert.store.events import StreamMessageType, message
from divert.store.live import read_active_signals, read_tick, read_vehicle_routes, read_vehicles
from divert.store.streams import dumps

router = APIRouter(tags=["live"])
log = logging.getLogger(__name__)

CLOSE_UNKNOWN_CITY = 4404
CLOSE_TRY_AGAIN = 1013


async def build_snapshot(ctx: AppContext, city_id: str) -> dict[str, Any]:
    rds, guard = ctx.redis, ctx.guard
    vehicles = [public_vehicle(v) for v in await guard.call(lambda: read_vehicles(rds, city_id))]
    ids = [str(v["id"]) for v in vehicles]
    routes = await guard.call(lambda: read_vehicle_routes(rds, city_id, ids))
    for v in vehicles:
        doc = routes.get(str(v["id"]), {})
        v["route"] = doc.get("route", {}).get("points", [])
        v["corridor"] = doc.get("corridor", [])
    tick = await guard.call(lambda: read_tick(rds, city_id))
    signals = await guard.call(lambda: read_active_signals(rds, city_id))
    return message(
        StreamMessageType.SNAPSHOT.value,
        city=city_id,
        tick=int(float(tick.get("tick", 0))),
        sim_ts=tick.get("sim_ts"),
        vehicles=vehicles,
        signals=signals,
    )


@router.websocket("/cities/{city}/stream")
async def stream(ws: WebSocket, city: str) -> None:
    ctx: AppContext = ws.app.state.ctx
    known = city in ctx.settings.cities and await ctx.reference.city(city) is not None
    await ws.accept()
    if not known:
        await ws.close(code=CLOSE_UNKNOWN_CITY, reason="unknown city")
        return
    try:  # fail fast (bounded, circuit-broken) instead of waiting on the subscription
        await ctx.guard.call(ctx.redis.ping)
    except RedisError:
        await ws.close(code=CLOSE_TRY_AGAIN, reason="live state unavailable")
        return
    try:
        queue = await ctx.hub.subscribe(city)
    except HubFull:
        await ws.close(code=CLOSE_TRY_AGAIN, reason="too many clients")
        return
    try:
        try:
            snapshot = await build_snapshot(ctx, city)
        except RedisError as err:  # live state unreachable: client retries with backoff
            log.warning("snapshot unavailable", extra={"city": city, "error": repr(err)})
            await ws.close(code=CLOSE_TRY_AGAIN, reason="live state unavailable")
            return
        await ws.send_text(dumps(snapshot))
        sender = asyncio.create_task(_forward(ws, queue))
        receiver = asyncio.create_task(_receive(ws))
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                log.warning("websocket closed with error", extra={"city": city, "error": repr(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        ctx.hub.unsubscribe(city, queue)
        with contextlib.suppress(Exception):
            await ws.close()


async def _forward(ws: WebSocket, queue: asyncio.Queue[Item]) -> None:
    while True:
        item = await queue.get()
        if item is None:  # missed messages (slow client or Redis reconnect)
            await ws.close(code=CLOSE_TRY_AGAIN, reason="resync: reconnect for a fresh snapshot")
            return
        await ws.send_text(item)


async def _receive(ws: WebSocket) -> None:
    while True:
        raw = await ws.receive_text()
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            if json.loads(raw).get("type") == "ping":
                await ws.send_text(dumps(message("pong")))
