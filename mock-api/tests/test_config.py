"""Environment parsing.

The blank-string cases are not hypothetical: Docker Compose substitutes an
empty string for any variable referenced in `docker-compose.yml` but missing
from `.env`. A `.env` written before these settings existed will therefore hand
the container `MOCK_API_SEED=""`, and a naive `int(os.getenv(...))` crashes at
import time - before the health check has anything to report on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.config import DEFAULT_HISTORY_DAYS, DEFAULT_SEED, Settings


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MOCK_API_DATA_DIR",
        "MOCK_API_SEED",
        "MOCK_API_HISTORY_DAYS",
        "MOCK_API_HISTORY_END_DATE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_nothing_is_set() -> None:
    settings = Settings.from_env()

    assert settings.seed == DEFAULT_SEED
    assert settings.history_days == DEFAULT_HISTORY_DAYS
    assert str(settings.data_dir) in ("/data", "\\data")
    assert settings.history_end_date == datetime.now(UTC).date()


def test_blank_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact failure mode a stale `.env` produces."""
    monkeypatch.setenv("MOCK_API_SEED", "")
    monkeypatch.setenv("MOCK_API_HISTORY_DAYS", "")
    monkeypatch.setenv("MOCK_API_HISTORY_END_DATE", "")
    monkeypatch.setenv("MOCK_API_DATA_DIR", "")

    settings = Settings.from_env()

    assert settings.seed == DEFAULT_SEED
    assert settings.history_days == DEFAULT_HISTORY_DAYS
    assert settings.history_end_date == datetime.now(UTC).date()


def test_explicit_values_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_API_SEED", "7")
    monkeypatch.setenv("MOCK_API_HISTORY_DAYS", "30")
    monkeypatch.setenv("MOCK_API_HISTORY_END_DATE", "2026-01-31")
    monkeypatch.setenv("MOCK_API_DATA_DIR", "/tmp/orders")

    settings = Settings.from_env()

    assert settings.seed == 7
    assert settings.history_days == 30
    assert settings.history_end_date == date(2026, 1, 31)
    assert settings.data_dir.as_posix() == "/tmp/orders"


def test_surrounding_whitespace_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_API_SEED", "  42  ")
    monkeypatch.setenv("MOCK_API_HISTORY_END_DATE", " 2026-03-01 ")

    settings = Settings.from_env()

    assert settings.seed == 42
    assert settings.history_end_date == date(2026, 3, 1)


def test_data_paths_derive_from_the_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_API_DATA_DIR", "/srv/state")

    settings = Settings.from_env()

    assert settings.orders_path.as_posix() == "/srv/state/orders.jsonl"
    assert settings.anomalies_path.as_posix() == "/srv/state/anomalies.json"


def test_a_genuinely_invalid_value_still_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank means "use the default"; garbage is a misconfiguration worth crashing on."""
    monkeypatch.setenv("MOCK_API_SEED", "not-a-number")

    with pytest.raises(ValueError):
        Settings.from_env()
