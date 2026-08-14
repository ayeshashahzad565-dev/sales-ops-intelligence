"""The delivery boundary.

One interface, one real implementation, one fake - the same shape as Stage 7's
LLM provider, for the same reasons.

The real one POSTs JSON to a configured webhook URL, which is what Slack, Teams,
Discord, PagerDuty, Zapier, an internal relay and most email gateways all accept.
Choosing a webhook over an SMTP client is deliberate: it can be exercised
end-to-end locally without a mail server, without a production messaging
workspace, and without anyone's inbox being used as a test fixture.

Every failure is classified, never just reported
------------------------------------------------
A provider that returned "something went wrong" would hand the retry decision
back to the caller as a string-matching exercise. Instead each attempt comes back
as `retryable_failure` or `permanent_failure`:

    retryable   timeout, connection failure, 429, 5xx
    permanent   400, 401, 403, 404, 422, malformed response

Retrying a timeout is free and often works. Retrying a 401 three times only
delays the moment somebody notices the credential is wrong.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import httpx

from analytics.notifications.models import DeliveryOutcome, DeliveryResult, Notification

logger = logging.getLogger(__name__)

#: 408 and 425 are timing failures; 429 is explicit backpressure; 5xx is theirs,
#: not ours. Everything else in the 4xx range describes a request that will be
#: just as wrong the second time.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


class NotificationProvider(Protocol):
    """Everything Stage 8 needs from a delivery channel."""

    name: str
    channel: str

    def send(self, notification: Notification) -> DeliveryResult:
        """Attempt delivery. Never raises - classify and return."""
        ...


class WebhookProvider:
    """Delivers by POSTing the notification payload as JSON.

    The URL is configuration and is never persisted, never logged, and never
    included in an error message: a webhook URL is frequently a bearer credential
    in its own right (Slack's certainly is), so it is treated as one.
    """

    channel = "webhook"

    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float = 15.0,
        name: str = "webhook",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = webhook_url
        self._timeout = timeout_seconds
        self.name = name
        self._headers = headers or {}

    def send(self, notification: Notification) -> DeliveryResult:
        body = {
            "subject": notification.subject,
            "recipient": notification.recipient,
            "severity": notification.severity,
            "calendar_date": notification.calendar_date.isoformat(),
            "notification": notification.payload,
        }

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    self._url,
                    headers={"Content-Type": "application/json", **self._headers},
                    json=body,
                )
        except httpx.TimeoutException:
            return self._failure(
                DeliveryOutcome.RETRYABLE_FAILURE,
                f"timed out after {self._timeout:.0f}s",
                started,
            )
        except httpx.HTTPError as exc:
            # Transport-level: DNS, connection refused, TLS. Worth another go.
            return self._failure(
                DeliveryOutcome.RETRYABLE_FAILURE,
                f"transport error: {type(exc).__name__}",
                started,
            )

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code < 300:
            return DeliveryResult(
                outcome=DeliveryOutcome.SUCCESS,
                provider=self.name,
                status_code=response.status_code,
                provider_message_id=_message_id(response),
                latency_ms=latency_ms,
            )

        outcome = (
            DeliveryOutcome.RETRYABLE_FAILURE
            if response.status_code in RETRYABLE_STATUS_CODES
            else DeliveryOutcome.PERMANENT_FAILURE
        )
        return DeliveryResult(
            outcome=outcome,
            provider=self.name,
            status_code=response.status_code,
            error_message=f"HTTP {response.status_code}: {_safe_detail(response)}",
            latency_ms=latency_ms,
        )

    def _failure(self, outcome: DeliveryOutcome, message: str, started: float) -> DeliveryResult:
        return DeliveryResult(
            outcome=outcome,
            provider=self.name,
            error_message=message,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class RecordingProvider:
    """A provider that records what it was asked to send, without a network.

    The whole Stage 8 suite runs through this. Configure it to succeed, to fail
    retryably, to fail permanently, or to fail for the first N attempts and then
    succeed - which is how retry convergence is tested without waiting on
    anything.
    """

    channel = "webhook"
    name = "recording"

    def __init__(
        self,
        outcome: DeliveryOutcome = DeliveryOutcome.SUCCESS,
        status_code: int | None = 200,
        error_message: str | None = None,
        fail_first: int = 0,
    ) -> None:
        self._outcome = outcome
        self._status_code = status_code
        self._error_message = error_message
        #: Fail this many attempts, then start succeeding.
        self._fail_first = fail_first
        #: Every notification handed over, so tests can assert on real content.
        self.sent: list[Notification] = []
        self.attempts = 0

    def send(self, notification: Notification) -> DeliveryResult:
        self.attempts += 1
        self.sent.append(notification)

        if self._fail_first and self.attempts <= self._fail_first:
            return DeliveryResult(
                outcome=DeliveryOutcome.RETRYABLE_FAILURE,
                provider=self.name,
                status_code=503,
                error_message="temporary failure (fixture)",
                latency_ms=1,
            )

        if self._outcome is DeliveryOutcome.SUCCESS:
            return DeliveryResult(
                outcome=DeliveryOutcome.SUCCESS,
                provider=self.name,
                status_code=self._status_code,
                provider_message_id=f"recording-{self.attempts}",
                latency_ms=1,
            )

        return DeliveryResult(
            outcome=self._outcome,
            provider=self.name,
            status_code=self._status_code,
            error_message=self._error_message or "delivery failed (fixture)",
            latency_ms=1,
        )


def _message_id(response: httpx.Response) -> str | None:
    """A provider's own id for the delivery, when it offers one."""
    for header in ("X-Message-Id", "X-Request-Id", "Message-Id"):
        if header in response.headers:
            return response.headers[header]
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        for key in ("id", "message_id", "messageId", "ts"):
            value = body.get(key)
            if isinstance(value, (str, int)):
                return str(value)
    return None


def _safe_detail(response: httpx.Response, limit: int = 200) -> str:
    """A short failure description with nothing sensitive in it.

    Deliberately does NOT include the request URL. The webhook URL is often the
    credential, and an error message is the easiest place for one to end up in a
    database row or a log line.
    """
    try:
        body = response.json()
        message = body.get("error") or body.get("message") or json.dumps(body)
    except Exception:
        message = response.text
    return (str(message) or "")[:limit]
