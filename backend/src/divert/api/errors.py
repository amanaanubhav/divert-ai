"""Error model: every error response is ``{"error": {code, message, details}, "request_id"}``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

from divert.logging import request_id_var
from divert.services.dispatch import DispatchError

log = logging.getLogger("divert.api")


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": request_id_var.get(),
    }


def _json_safe(errors: list[Any]) -> list[dict[str, Any]]:
    out = []
    for e in errors:
        out.append(
            {
                "loc": [str(x) for x in e.get("loc", [])],
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
        )
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(DispatchError)
    async def _dispatch_error(_: Request, exc: DispatchError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body(
                "validation_error",
                "request validation failed",
                {"errors": _json_safe(list(exc.errors()))},
            ),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            error_body(f"http_{exc.status_code}", str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RedisError)
    async def _redis(_: Request, exc: RedisError) -> JSONResponse:
        # only live-state reads reach here (everything else degrades); PG is unaffected
        log.warning("live state unavailable", extra={"error": repr(exc)})
        return JSONResponse(
            error_body("live_state_unavailable", "live state (Redis) is unavailable; retry"),
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(error_body("internal_error", "internal server error"), status_code=500)
