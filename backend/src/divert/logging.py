"""Structured (JSON) logging with request/correlation ids carried in contextvars."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

request_id_var: ContextVar[str | None] = ContextVar("divert_request_id", default=None)

_RESERVED: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


def default_instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``extra=`` fields are merged in."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
        }
        rid = request_id_var.get()
        if rid:
            out["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def configure_logging(service: str, level: str = "INFO", *, json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(
            logging.Formatter(f"%(asctime)s %(levelname)s [{service}] %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def quiet_abandoned_dns_lookups(loop: asyncio.AbstractEventLoop) -> None:
    """While Redis' host name does not resolve (container stopped), a connect attempt that
    hits its deadline abandons the executor's ``getaddrinfo`` future; when that lookup later
    fails asyncio logs "Future exception was never retrieved" with a traceback at ERROR.
    The failure was already handled (circuit breaker), so log it as one WARNING line."""
    previous = loop.get_exception_handler()

    def handler(lp: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, socket.gaierror) and "never retrieved" in str(
            context.get("message", "")
        ):
            logging.getLogger("divert.asyncio").warning(
                "abandoned DNS lookup failed", extra={"error": repr(exc)}
            )
            return
        if previous is not None:
            previous(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)
