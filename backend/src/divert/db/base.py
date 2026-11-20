"""Declarative base with a deterministic constraint naming convention."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def str_enum(enum_cls: type[StrEnum], name: str, length: int = 16) -> sa.Enum:
    """VARCHAR + CHECK (not a native PG enum): adding a value is a one-line migration."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def utc_now_column(*, nullable: bool = False, onupdate: bool = False) -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now(),
        onupdate=sa.func.now() if onupdate else None,
    )
