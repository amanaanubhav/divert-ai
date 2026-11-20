"""Shared test setup: unit tests never see the developer's DIVERT_* environment."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from divert.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("DIVERT_") and not name.startswith("DIVERT_TEST_"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
