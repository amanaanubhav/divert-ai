"""FastAPI dependencies: context, DB session, city lookup, API key."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Path, Request
from fastapi.requests import HTTPConnection
from sqlalchemy.ext.asyncio import AsyncSession

from divert.api.context import AppContext
from divert.api.errors import ApiError
from divert.domain.models import City


def get_ctx(conn: HTTPConnection) -> AppContext:
    ctx: AppContext = conn.app.state.ctx
    return ctx


CtxDep = Annotated[AppContext, Depends(get_ctx)]


async def get_session(ctx: CtxDep) -> AsyncIterator[AsyncSession]:
    async with ctx.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

CityId = Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,32}$", description="city id, e.g. delhi")]


async def get_city(city: CityId, ctx: CtxDep) -> City:
    found = await ctx.reference.city(city) if city in ctx.settings.cities else None
    if found is None:
        raise ApiError(
            404, "city_not_found", f"unknown city {city!r}", {"cities": ctx.settings.cities}
        )
    return found


CityDep = Annotated[City, Depends(get_city)]


async def require_api_key(
    ctx: CtxDep, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
) -> None:
    """Mutating endpoints: enforced only when DIVERT_API_KEY is set."""
    expected = ctx.settings.api_key
    if expected is None or not expected.get_secret_value():
        return
    if x_api_key is None or not hmac.compare_digest(
        x_api_key.encode(), expected.get_secret_value().encode()
    ):
        raise ApiError(
            401,
            "unauthorized",
            "missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def client_id(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
