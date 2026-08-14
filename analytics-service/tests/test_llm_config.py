"""LLM configuration: environment-driven, and incapable of leaking a key.

Two things are being protected here. The first is that Stage 5 keeps working
with no model configured at all - a missing API key must never be able to stop
anomaly detection. The second is that the key, once configured, has exactly one
destination.
"""

from __future__ import annotations

import pytest

from analytics.config import LLMSettings, Settings

FAKE_KEY = "sk-test-not-a-real-key-000000"


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    for name in (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL",
        "LLM_TEMPERATURE", "LLM_TIMEOUT_SECONDS", "LLM_MAX_OUTPUT_TOKENS",
        "LLM_JSON_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


# =============================================================================
# Configuration comes from the environment
# =============================================================================


def test_a_key_alone_is_enough_to_start(monkeypatch):
    """Provider, base URL and model all have defaults; the secret does not."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    settings = LLMSettings.from_env()

    assert settings.provider == "groq"
    assert settings.base_url == "https://api.groq.com/openai/v1"
    assert settings.model


def test_switching_provider_needs_no_code_change(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    settings = LLMSettings.from_env()

    assert settings.base_url == "https://api.openai.com/v1"


def test_an_unknown_provider_must_supply_its_own_base_url(monkeypatch):
    """Rather than being silently pointed somewhere plausible."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_PROVIDER", "some-local-runtime")

    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        LLMSettings.from_env()

    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")

    assert LLMSettings.from_env().base_url == "http://localhost:11434/v1"


def test_temperature_defaults_to_zero(monkeypatch):
    """Same evidence, closely consistent output. Sampling variety buys nothing."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    assert LLMSettings.from_env().temperature == 0.0


def test_overrides_are_read(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("LLM_JSON_MODE", "object")

    settings = LLMSettings.from_env()

    assert settings.model == "some-model"
    assert settings.temperature == 0.2
    assert settings.timeout_seconds == 15.0
    assert settings.json_mode == "object"


def test_a_blank_variable_is_treated_as_unset(monkeypatch):
    """Compose substitutes "" for a variable missing from .env.

    Without this rule every downstream cast fails at import time - the same bug
    that crash-looped the mock API in Stage 1.
    """
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_TEMPERATURE", "")

    settings = LLMSettings.from_env()

    assert settings.model
    assert settings.base_url == "https://api.groq.com/openai/v1"
    assert settings.temperature == 0.0


# =============================================================================
# The key
# =============================================================================


def test_a_missing_key_fails_loudly_and_usefully(monkeypatch):
    with pytest.raises(RuntimeError, match="LLM_API_KEY is not set"):
        LLMSettings.from_env()


def test_there_is_no_default_api_key():
    """A default secret is a hardcoded secret - the same rule as the database
    password and the n8n encryption key."""
    import inspect

    from analytics import config

    source = inspect.getsource(config.LLMSettings)

    assert 'LLM_API_KEY", "' not in source
    assert "api_key: str = " not in source


def test_describe_reveals_nothing_about_the_key(monkeypatch):
    """Not even a prefix. A prefix is still a fact about a credential."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    described = LLMSettings.from_env().describe()

    assert FAKE_KEY not in described
    assert "sk-" not in described
    assert "key" not in described.lower()


def test_the_key_is_never_part_of_a_database_connection_string(monkeypatch):
    """Different secrets, different destinations, no crossover."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    database = Settings(
        host="postgres", port=5432, database="salesops",
        user="salesops", password="db-password",
    )

    assert FAKE_KEY not in database.dsn
    assert "db-password" not in LLMSettings.from_env().describe()


# =============================================================================
# Stage 5 does not depend on any of this
# =============================================================================


def test_detection_settings_load_with_no_llm_configured(monkeypatch):
    """The pipeline degrades to 'no explanations', never to 'no detections'."""
    monkeypatch.setenv("ANALYTICS_DB_PASSWORD", "db-password")

    settings = Settings.from_env()

    assert settings.database == "salesops"


def test_importing_the_detector_does_not_require_a_model():
    """A missing key must not be able to break the Stage 5 import path."""
    import importlib

    for module in ("analytics.detector", "analytics.runner", "analytics.repository"):
        importlib.import_module(module)
