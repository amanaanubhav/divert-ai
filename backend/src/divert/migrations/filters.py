"""Autogenerate/compare filters shared by env.py and the drift test."""

from __future__ import annotations

from typing import Any


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Manage only our own tables: PostGIS (and the postgis/postgis image's tiger and
    topology extensions) own reflected tables that are absent from our metadata."""
    return not (type_ == "table" and reflected and compare_to is None)
