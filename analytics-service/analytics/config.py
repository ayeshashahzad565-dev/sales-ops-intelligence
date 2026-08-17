"""Runtime configuration, read from the environment.

Mirrors the mock-api pattern deliberately, including the blank-is-unset rule:
Docker Compose substitutes an empty string for any variable referenced in
docker-compose.yml but missing from .env, so `os.getenv(name, default)` returns
"" rather than the default and every downstream cast fails at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "postgres"
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "salesops"
DEFAULT_USER = "salesops"


def _env(name: str) -> str | None:
    """Read an environment variable, treating blank as unset."""
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    """Everything the analytics service needs to start."""

    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> Settings:
        password = _env("ANALYTICS_DB_PASSWORD")
        if password is None:
            # Fail loudly at startup rather than at the first query. There is no
            # default: a default password is a hardcoded secret.
            raise RuntimeError(
                "ANALYTICS_DB_PASSWORD is not set - the analytics service cannot "
                "reach the warehouse without it"
            )

        return cls(
            host=_env("ANALYTICS_DB_HOST") or DEFAULT_HOST,
            port=int(_env("ANALYTICS_DB_PORT") or DEFAULT_PORT),
            database=_env("ANALYTICS_DB_NAME") or DEFAULT_DATABASE,
            user=_env("ANALYTICS_DB_USER") or DEFAULT_USER,
            password=password,
        )

    @property
    def dsn(self) -> str:
        """libpq connection string.

        Assembled from parts rather than accepted as one blob so the password
        never has to be embedded in a URL that might get logged.
        """
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password}"
        )

    def describe(self) -> str:
        """Connection summary safe to log - no password."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


# =============================================================================
# Stage 7: the language model
#
# Separate from Settings on purpose. Stage 5 must keep working with no LLM
# configured at all - the detector has nothing to do with a model, and a missing
# API key is not a reason for /detect to stop answering. Nothing here is read
# until an analysis is actually requested.
# =============================================================================

DEFAULT_LLM_PROVIDER = "groq"
DEFAULT_LLM_TEMPERATURE = 0.0
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 2000

#: Providers whose base URL is known. Anything else must supply LLM_BASE_URL,
#: rather than being silently pointed somewhere plausible.
_PROVIDER_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
}

#: A sensible default per provider, so a first run needs only a key. Model
#: line-ups move quickly; treat these as a starting point, not a guarantee.
#: Groq retired llama-3.3-70b-versatile, which then answered every Stage 7 call
#: with HTTP 404 rather than anything resembling a model problem. Set LLM_MODEL
#: explicitly if a default here has since gone the same way.
_PROVIDER_DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-4.1-mini",
}


@dataclass(frozen=True)
class LLMSettings:
    """Everything Stage 7 needs to reach a model.

    The API key lives here and travels no further than an Authorization header.
    It is never logged, never persisted, and `describe()` exists so that the
    temptation to log the whole object has somewhere harmless to go.
    """

    provider: str
    model: str
    api_key: str
    base_url: str
    temperature: float
    timeout_seconds: float
    max_output_tokens: int
    json_mode: str

    @classmethod
    def from_env(cls) -> LLMSettings:
        provider = (_env("LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).lower()

        api_key = _env("LLM_API_KEY")
        if api_key is None:
            # Fail here, clearly, rather than sending an unauthenticated request
            # and reporting whatever the provider says about it.
            raise RuntimeError(
                "LLM_API_KEY is not set - Stage 7 cannot generate hypotheses. "
                "Set it in .env; it is never written to the database or to a workflow."
            )

        base_url = _env("LLM_BASE_URL") or _PROVIDER_BASE_URLS.get(provider)
        if base_url is None:
            raise RuntimeError(
                f"LLM_PROVIDER={provider!r} has no built-in base URL. "
                f"Set LLM_BASE_URL, or use one of: {', '.join(sorted(_PROVIDER_BASE_URLS))}."
            )

        model = _env("LLM_MODEL") or _PROVIDER_DEFAULT_MODELS.get(provider)
        if model is None:
            raise RuntimeError(f"LLM_MODEL is not set and provider {provider!r} has no default")

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            # Zero by default. The same evidence should produce closely
            # consistent output; sampling variety is worth nothing here and
            # makes two runs harder to compare.
            temperature=float(_env("LLM_TEMPERATURE") or DEFAULT_LLM_TEMPERATURE),
            timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS") or DEFAULT_LLM_TIMEOUT_SECONDS),
            max_output_tokens=int(_env("LLM_MAX_OUTPUT_TOKENS") or DEFAULT_LLM_MAX_OUTPUT_TOKENS),
            # 'schema' asks the provider to enforce our JSON Schema and falls
            # back to 'object' if the model does not support it. Set to 'object'
            # to skip the negotiation entirely.
            json_mode=(_env("LLM_JSON_MODE") or "schema").lower(),
        )

    def describe(self) -> str:
        """Configuration summary safe to log - no key, not even a prefix."""
        return (
            f"{self.provider}/{self.model} at {self.base_url} "
            f"(temperature={self.temperature}, timeout={self.timeout_seconds:.0f}s, "
            f"json_mode={self.json_mode})"
        )


# =============================================================================
# Stage 8: notification delivery
#
# Separate again, and for the same reason: Stages 5-7 must all keep working with
# no delivery channel configured. A missing webhook URL means findings are not
# delivered; it does not mean they stop being detected, decided or explained.
# =============================================================================

DEFAULT_NOTIFICATION_PROVIDER = "webhook"
DEFAULT_NOTIFICATION_TIMEOUT_SECONDS = 15.0
DEFAULT_NOTIFICATION_FROM = "salesops-pipeline"


@dataclass(frozen=True)
class NotificationSettings:
    """Everything Stage 8 needs to deliver a notification.

    `webhook_url` is treated as a credential. Slack, Teams and Discord webhook
    URLs are bearer tokens in URL form - anyone holding one can post as the
    integration - so it is never persisted, never logged, and never part of an
    error message or an audit row.
    """

    provider: str
    webhook_url: str
    timeout_seconds: float
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls) -> NotificationSettings:
        provider = (_env("NOTIFICATION_PROVIDER") or DEFAULT_NOTIFICATION_PROVIDER).lower()

        webhook_url = _env("NOTIFICATION_WEBHOOK_URL")
        if webhook_url is None:
            raise RuntimeError(
                "NOTIFICATION_WEBHOOK_URL is not set - Stage 8 cannot deliver "
                "notifications. Set it in .env; it is never written to the database, "
                "a workflow, or a log."
            )

        raw_recipients = _env("NOTIFICATION_RECIPIENTS") or ""
        recipients = tuple(
            part.strip() for part in raw_recipients.split(",") if part.strip()
        )
        if not recipients:
            raise RuntimeError(
                "NOTIFICATION_RECIPIENTS is empty - there is nobody to notify. "
                "Set a comma-separated list in .env."
            )

        return cls(
            provider=provider,
            webhook_url=webhook_url,
            timeout_seconds=float(
                _env("NOTIFICATION_TIMEOUT_SECONDS") or DEFAULT_NOTIFICATION_TIMEOUT_SECONDS
            ),
            sender=_env("NOTIFICATION_FROM") or DEFAULT_NOTIFICATION_FROM,
            recipients=recipients,
        )

    def describe(self) -> str:
        """Configuration summary safe to log.

        The webhook URL is reduced to its host. A path is where webhook secrets
        live, so the path never appears - not in a log, not in an ops endpoint,
        not in an exception.
        """
        return (
            f"{self.provider} -> {self.webhook_host} "
            f"({len(self.recipients)} recipient(s), timeout={self.timeout_seconds:.0f}s)"
        )

    @property
    def webhook_host(self) -> str:
        """Host only, never the path or query."""
        from urllib.parse import urlsplit

        parts = urlsplit(self.webhook_url)
        return parts.netloc or "unknown"
