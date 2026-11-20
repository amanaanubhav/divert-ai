"""Pure-ASGI request context: request id (X-Request-ID in/out), metrics, access log."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from divert.logging import request_id_var
from divert.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("divert.access")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _RID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["type"] == "http":
                route = scope.get("route")
                path = getattr(route, "path", "unmatched")
                method = scope.get("method", "GET")
                elapsed = time.perf_counter() - started
                if path != "/metrics":
                    HTTP_REQUESTS.labels(method=method, route=path, status=str(status)).inc()
                    HTTP_LATENCY.labels(method=method, route=path).observe(elapsed)
                if path not in ("/healthz", "/readyz", "/metrics"):
                    log.info(
                        "request",
                        extra={
                            "method": method,
                            "path": scope.get("path"),
                            "route": path,
                            "status": status,
                            "duration_ms": round(elapsed * 1000, 1),
                        },
                    )
            request_id_var.reset(token)
