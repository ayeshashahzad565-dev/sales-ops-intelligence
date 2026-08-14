"""The provider boundary.

One interface, one real implementation, one fake.

The real one speaks the OpenAI chat-completions dialect, which Groq, OpenAI,
Together, OpenRouter and most local runtimes all serve. That is why there is a
single provider rather than three: the differences between these services are a
base URL and a model name, and building a class hierarchy over that would be
inventing structure to justify itself. If a provider with a genuinely different
protocol is ever needed, `LLMProvider` is the seam to add it at.

The fake is not a test convenience bolted on afterwards - it is how the entire
stage is tested. Every behaviour that matters (validation, failure isolation,
idempotency, eligibility, the Stage 6 boundary) is exercised through it, so the
suite runs offline, costs nothing, and gives the same answer every time. The
real provider is then responsible for exactly one thing the fake cannot check:
that a live model returns something the validator accepts.

Errors are deliberately coarse. Everything a provider can do wrong arrives as
`ProviderError`, because the caller's response is the same in every case: record
the failure, leave the Stage 6 decision alone, move to the next anomaly.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import httpx

from analytics.llm.models import GenerationMetadata, RootCauseHypothesis

logger = logging.getLogger(__name__)

# Structured-output modes, most to least constrained.
JSON_MODE_SCHEMA = "schema"    # provider enforces our JSON Schema
JSON_MODE_OBJECT = "object"    # provider guarantees valid JSON, shape unenforced


class ProviderError(RuntimeError):
    """The provider did not return a usable response.

    Timeout, transport failure, HTTP error, empty body, unparseable JSON. One
    type because one reaction: this anomaly's analysis failed, nothing else
    changes.
    """


class LLMProvider(Protocol):
    """Everything the rest of Stage 7 needs from a language model."""

    name: str
    model: str

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict,
    ) -> tuple[RootCauseHypothesis, GenerationMetadata]:
        """Return a validated hypothesis, or raise ProviderError."""
        ...


class OpenAICompatibleProvider:
    """Chat-completions provider: Groq, OpenAI, or anything speaking the dialect.

    Structured output is negotiated, not assumed. The request asks for
    `json_schema` first because it is the strongest guarantee available; if the
    provider rejects that response_format - support varies by model, and by the
    week - it retries once with `json_object` and records which mode actually
    produced the answer.

    That fallback is capability negotiation at the transport layer, and it is not
    the same thing as repairing bad output. Whichever mode is used, the response
    is validated identically afterwards; the mode only changes how much was
    guaranteed before validation ran, which is why it is persisted alongside the
    hypothesis.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 2000,
        json_mode: str = JSON_MODE_SCHEMA,
        provider_name: str = "openai-compatible",
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.name = provider_name
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._json_mode = json_mode

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict,
    ) -> tuple[RootCauseHypothesis, GenerationMetadata]:
        started = time.monotonic()

        modes = [self._json_mode]
        if self._json_mode == JSON_MODE_SCHEMA:
            modes.append(JSON_MODE_OBJECT)

        last_error: Exception | None = None
        for mode in modes:
            try:
                payload, mode_used = self._request(
                    system_prompt, user_message, json_schema, mode
                )
                break
            except _UnsupportedResponseFormat as exc:
                logger.warning(
                    "%s/%s rejected response_format=%s (%s); retrying with json_object",
                    self.name, self.model, mode, exc,
                )
                last_error = exc
                continue
        else:
            raise ProviderError(
                f"{self.name}/{self.model} accepted no supported response format: {last_error}"
            ) from last_error

        latency_ms = int((time.monotonic() - started) * 1000)
        return self._parse(payload, mode_used, latency_ms)

    # -- transport ------------------------------------------------------------

    def _request(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict,
        mode: str,
    ) -> tuple[dict, str]:
        body: dict = {
            "model": self.model,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        if mode == JSON_MODE_SCHEMA:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "root_cause_hypothesis",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    # The key travels in the header and nowhere else. It is never
                    # logged, never persisted, and never part of an error message.
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name}/{self.model} timed out after {self._timeout:.0f}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}/{self.model} transport error: {exc}") from exc

        if response.status_code == 400 and mode == JSON_MODE_SCHEMA:
            # Most compatible services report an unsupported response_format as a
            # 400. Distinguished from a genuine bad request so the retry is only
            # attempted for the reason it exists.
            detail = _safe_detail(response)
            if "response_format" in detail or "json_schema" in detail:
                raise _UnsupportedResponseFormat(detail)

        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name}/{self.model} returned HTTP {response.status_code}: "
                f"{_safe_detail(response)}"
            )

        try:
            return response.json(), mode
        except ValueError as exc:
            raise ProviderError(
                f"{self.name}/{self.model} returned a non-JSON envelope"
            ) from exc

    # -- response -------------------------------------------------------------

    def _parse(
        self,
        payload: dict,
        mode: str,
        latency_ms: int,
    ) -> tuple[RootCauseHypothesis, GenerationMetadata]:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name}/{self.model} returned no choices")

        content = (choices[0].get("message") or {}).get("content")
        if not content or not content.strip():
            raise ProviderError(f"{self.name}/{self.model} returned an empty message")

        try:
            parsed = json.loads(content)
        except ValueError as exc:
            # Malformed JSON is a failure, not something to be coaxed into shape.
            # Repairing it would mean guessing at content the model did not
            # produce, which is precisely the fabrication this stage forbids.
            raise ProviderError(
                f"{self.name}/{self.model} returned content that is not valid JSON"
            ) from exc

        try:
            hypothesis = RootCauseHypothesis.model_validate(parsed)
        except Exception as exc:
            raise ProviderError(
                f"{self.name}/{self.model} returned JSON that does not match the "
                f"hypothesis schema: {exc}"
            ) from exc

        usage = payload.get("usage") or {}
        metadata = GenerationMetadata(
            provider=self.name,
            model=payload.get("model") or self.model,
            prompt_version="",  # set by the caller, which owns the prompt
            json_mode=mode,
            request_id=payload.get("id"),
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
            latency_ms=latency_ms,
        )
        return hypothesis, metadata


class _UnsupportedResponseFormat(RuntimeError):
    """Internal: the provider does not support the requested response_format."""


class StaticProvider:
    """A provider that returns whatever it was told to, without a network.

    Used by the whole test suite. Give it a payload to return, or an exception to
    raise, and every Stage 7 behaviour becomes deterministic and offline.
    """

    name = "mock"

    def __init__(
        self,
        payload: dict | str | None = None,
        error: Exception | None = None,
        model: str = "mock-model-v1",
        latency_ms: int = 1,
    ) -> None:
        self._payload = payload
        self._error = error
        self.model = model
        self._latency_ms = latency_ms
        #: Every (system_prompt, user_message) pair it was called with, so tests
        #: can assert on what the model would actually have been shown.
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict,
    ) -> tuple[RootCauseHypothesis, GenerationMetadata]:
        self.calls.append((system_prompt, user_message))

        if self._error is not None:
            raise self._error

        payload = self._payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise ProviderError("mock provider returned invalid JSON") from exc

        if payload is None:
            raise ProviderError("mock provider returned an empty response")

        try:
            hypothesis = RootCauseHypothesis.model_validate(payload)
        except Exception as exc:
            raise ProviderError(f"mock provider payload failed validation: {exc}") from exc

        return hypothesis, GenerationMetadata(
            provider=self.name,
            model=self.model,
            prompt_version="",
            json_mode=JSON_MODE_SCHEMA,
            request_id="mock-request",
            prompt_tokens=len(system_prompt.split()) + len(user_message.split()),
            completion_tokens=64,
            latency_ms=self._latency_ms,
        )


def _safe_detail(response: httpx.Response, limit: int = 400) -> str:
    """A short, credential-free description of a failed response."""
    try:
        body = response.json()
        message = (body.get("error") or {}).get("message") or json.dumps(body)
    except Exception:
        message = response.text
    return (message or "")[:limit]


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
