"""The execution boundary.

One interface, one implementation - and the implementation is honest about
being a development recorder.

Why there is no real provider here
----------------------------------
Stage 8 ships a real `WebhookProvider` because a webhook is a real destination:
Slack, Teams, PagerDuty and most relays accept one, and pointing it at any of
them makes the delivery genuinely happen. Remediation has no equivalent. There
is no ticketing system, no case management system and no operations platform in
this project, so a "real" provider would have to be a fake one wearing a
convincing name - a mock ERP, an imaginary CRM - and the demonstration it
produced would be a demonstration of nothing.

So the provider records the request and returns. It says so in its name, in its
result (`external_side_effect=False`), in the payload it is handed, and in the
audit view. A reader is never left inferring whether anything real happened.

The boundary is the deliverable
-------------------------------
`RemediationProvider` is the whole contract: one method, a validated request in,
a classified result out. A real provider - a Jira client, a ServiceNow client,
an internal case API - implements that method and changes nothing else. The
state machine, the retry budget, the idempotency key and the audit trail are all
outside it, which is where they belong: they are properties of the system, not
of whichever ticketing product an organisation happens to run.
"""

from __future__ import annotations

import logging
from typing import Protocol

from analytics.remediation.models import (
    ExecutionOutcome,
    ExecutionResult,
    RemediationRequest,
)

logger = logging.getLogger(__name__)


class RemediationProvider(Protocol):
    """Everything Stage 9 needs from an execution target."""

    name: str

    def execute(self, request: RemediationRequest) -> ExecutionResult:
        """Perform the action. Never raises - classify and return."""
        ...


class RecordingRemediationProvider:
    """Records what it was asked to do. Contacts nothing.

    This is the development provider, and the only one. It is deterministic by
    construction: the same request produces the same result, and the reference
    it returns is derived from a counter rather than from anything a remote
    system decided.

    Configure it to succeed, to fail retryably, to fail permanently, or to fail
    for the first N calls and then succeed - which is how retry convergence is
    tested without waiting on anything.
    """

    name = "recording-remediation"

    def __init__(
        self,
        outcome: ExecutionOutcome = ExecutionOutcome.SUCCESS,
        error_message: str | None = None,
        fail_first: int = 0,
    ) -> None:
        self._outcome = outcome
        self._error_message = error_message
        #: Fail this many calls, then start succeeding.
        self._fail_first = fail_first
        #: Every request handed over, so tests can assert on real content and so
        #: "the provider was called exactly once" is a fact rather than a hope.
        self.received: list[RemediationRequest] = []
        self.calls = 0

    def execute(self, request: RemediationRequest) -> ExecutionResult:
        self.calls += 1
        self.received.append(request)

        logger.info(
            "Recorded remediation %s (%s, %s) locally. Nothing outside this "
            "warehouse was contacted.",
            request.remediation_id, request.action_type, request.calendar_date,
        )

        if self._fail_first and self.calls <= self._fail_first:
            return ExecutionResult(
                outcome=ExecutionOutcome.RETRYABLE_FAILURE,
                provider=self.name,
                error_message="temporary failure (fixture)",
                latency_ms=1,
                external_side_effect=False,
            )

        if self._outcome is ExecutionOutcome.SUCCESS:
            return ExecutionResult(
                outcome=ExecutionOutcome.SUCCESS,
                provider=self.name,
                # Deliberately not shaped like a ticket id. "TICKET-4821" in an
                # audit trail invites somebody to go looking for it.
                provider_reference=f"local-record-{self.calls}",
                latency_ms=1,
                external_side_effect=False,
            )

        return ExecutionResult(
            outcome=self._outcome,
            provider=self.name,
            error_message=self._error_message or "execution failed (fixture)",
            latency_ms=1,
            external_side_effect=False,
        )

    @property
    def requests_for(self) -> dict[int, int]:
        """How many times each remediation id was handed over.

        The direct expression of the guarantee this stage makes: every value
        here must be 1 for an action that executed successfully.
        """
        counts: dict[int, int] = {}
        for request in self.received:
            counts[request.remediation_id] = counts.get(request.remediation_id, 0) + 1
        return counts
