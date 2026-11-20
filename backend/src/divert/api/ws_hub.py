"""Per-process WebSocket fan-out: ONE Redis pub/sub subscription per city per API
process, shared by every WebSocket client of that city through bounded queues.

A client whose queue overflows is a slow consumer: it receives ``None`` (the hub's
"resync" sentinel) and the endpoint closes it (1013) - the browser reconnects and gets a
fresh snapshot, so no client can make the API buffer without bound. After a Redis
reconnect every client gets the sentinel too, because messages may have been missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from divert.metrics import WS_CLIENTS
from divert.store import redis_store as keys
from divert.store.streams import to_str

log = logging.getLogger(__name__)

Item = str | None  # raw JSON text, or None = "you missed messages: resync"


class HubFull(Exception):
    pass


class CityHub:
    def __init__(self, redis: Redis, *, queue_size: int = 256, max_clients: int = 500) -> None:
        self.redis = redis
        self.queue_size = queue_size
        self.max_clients = max_clients
        self._clients: dict[str, set[asyncio.Queue[Item]]] = {}
        self._readers: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._closed = False

    def client_count(self, city_id: str) -> int:
        return len(self._clients.get(city_id, ()))

    async def subscribe(self, city_id: str) -> asyncio.Queue[Item]:
        clients = self._clients.setdefault(city_id, set())
        if len(clients) >= self.max_clients:
            raise HubFull(city_id)
        q: asyncio.Queue[Item] = asyncio.Queue(maxsize=self.queue_size)
        clients.add(q)
        WS_CLIENTS.labels(city=city_id).set(len(clients))
        task = self._readers.get(city_id)
        if task is None or task.done():
            self._ready[city_id] = asyncio.Event()
            self._readers[city_id] = asyncio.create_task(
                self._reader(city_id), name=f"ws-hub-{city_id}"
            )
        with contextlib.suppress(TimeoutError):
            # wait until the SUBSCRIBE is active so nothing published after the snapshot is lost
            await asyncio.wait_for(self._ready[city_id].wait(), timeout=2.0)
        return q

    def unsubscribe(self, city_id: str, q: asyncio.Queue[Item]) -> None:
        clients = self._clients.get(city_id)
        if clients is not None:
            clients.discard(q)
            WS_CLIENTS.labels(city=city_id).set(len(clients))

    def _fan_out(self, city_id: str, item: Item) -> None:
        for q in list(self._clients.get(city_id, ())):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # slow consumer: drop its backlog and tell it to resync
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    async def _reader(self, city_id: str) -> None:
        channel = keys.events_channel(city_id)
        backoff = 0.5
        first = True
        while not self._closed:
            pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(channel)
                self._ready[city_id].set()
                if not first:
                    self._fan_out(city_id, None)  # we may have missed messages
                first = False
                backoff = 0.5
                while not self._closed:
                    msg = await pubsub.get_message(timeout=1.0)
                    if msg is not None and msg.get("type") == "message":
                        self._fan_out(city_id, to_str(msg["data"]))
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                log.warning(
                    "pubsub reader error; reconnecting", extra={"city": city_id, "error": repr(exc)}
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        self._closed = True
        for t in self._readers.values():
            t.cancel()
        await asyncio.gather(*self._readers.values(), return_exceptions=True)
        self._readers.clear()
