"""Shared fixtures.

Every test runs against a temporary data directory and a pinned history end
date. Pinning matters: the service anchors its history to *today* by default,
so without a fixed end date the assertions below would drift with the calendar.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# Fixed so the generated dataset is byte-identical on every run.
TEST_SEED = 20260101
TEST_HISTORY_DAYS = 90
TEST_END_DATE = date(2026, 8, 9)
TEST_START_DATE = TEST_END_DATE - timedelta(days=TEST_HISTORY_DAYS - 1)


def build_settings(data_dir: Path, **overrides) -> Settings:
    """Settings pointing at `data_dir`, with the history pinned."""
    values = {
        "data_dir": data_dir,
        "seed": TEST_SEED,
        "history_days": TEST_HISTORY_DAYS,
        "history_end_date": TEST_END_DATE,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A client whose app has bootstrapped a fresh order book.

    Entering the context manager runs the lifespan, which is what generates and
    persists the history.
    """
    with TestClient(create_app(settings)) as test_client:
        yield test_client
