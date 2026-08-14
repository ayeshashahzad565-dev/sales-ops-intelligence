"""Runtime configuration, read from the environment.

Deliberately a plain dataclass rather than pydantic-settings: there are five
values, none of them secret, and an explicit `from_env` keeps the dependency
list short. Tests construct `Settings` directly instead of setting env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_DATA_DIR = "/data"
DEFAULT_SEED = 20260101
DEFAULT_HISTORY_DAYS = 90


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _env(name: str) -> str | None:
    """Read an environment variable, treating blank as unset.

    Docker Compose substitutes an empty string for any variable missing from
    `.env`, so `os.getenv(name, default)` returns `""` rather than the default
    and every downstream `int()`/`fromisoformat()` blows up at import time. It
    also lets `MOCK_API_HISTORY_END_DATE=` mean "anchor to today", which is the
    documented way to leave it unset.
    """
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    """Everything the service needs to know at startup."""

    data_dir: Path
    seed: int
    history_days: int
    history_end_date: date

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from `MOCK_API_*` environment variables.

        Any variable left blank falls back to its default, so a `.env` that
        predates these settings still starts.

        `MOCK_API_HISTORY_END_DATE` is the only one worth explaining. The
        history is anchored to *today* by default so a freshly started demo
        shows recent dates. Pinning it makes the generated dataset byte-for-byte
        reproducible, which is what the test suite does.
        """
        raw_end = _env("MOCK_API_HISTORY_END_DATE")

        return cls(
            data_dir=Path(_env("MOCK_API_DATA_DIR") or DEFAULT_DATA_DIR),
            seed=int(_env("MOCK_API_SEED") or DEFAULT_SEED),
            history_days=int(_env("MOCK_API_HISTORY_DAYS") or DEFAULT_HISTORY_DAYS),
            history_end_date=date.fromisoformat(raw_end) if raw_end else _utc_today(),
        )

    @property
    def orders_path(self) -> Path:
        return self.data_dir / "orders.jsonl"

    @property
    def anomalies_path(self) -> Path:
        return self.data_dir / "anomalies.json"
