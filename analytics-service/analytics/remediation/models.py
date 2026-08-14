"""What a remediation action is, and what executing one returned.

The action vocabulary is small, closed, and made entirely of requests for human
work. That is the scope rather than a shortcoming: this project has no
downstream system that could safely issue a refund, cancel an order or suspend
an account, and building a fake one to execute against would produce a
convincing demonstration of something that does not exist.

The three actions are also mirrored in `salesops.remediation_action_types`, and
the database is the authority - the enum here exists so a typo is a Python error
rather than a foreign key violation three layers down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    """The closed action vocabulary. Every value asks a person to look at
    something; none of them changes state in an external system."""

    CREATE_INVESTIGATION = "create_investigation"
    REQUEST_REFUND_REVIEW = "request_refund_review"
    REQUEST_OPERATIONS_REVIEW = "request_operations_review"


class RemediationStatus(StrEnum):
    """proposed -> approved -> executing -> executed, and the ways out.

    `failed` is a resting state rather than a dead end: an explicit retry may
    move it back to `executing` while the attempt budget lasts. `executed`,
    `rejected` and `cancelled` are terminal, and `executed` has no outgoing
    transition at all.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionOutcome(StrEnum):
    """What one provider call achieved, and whether trying again could help.

    Same three-way split as Stage 8 delivery, for the same reason: a provider
    that returned only "it went wrong" would hand the retry decision back to the
    caller as a string-matching exercise.
    """

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class RemediationRequest:
    """One action, ready to hand to a provider.

    `payload` is what is persisted and what the provider receives. It carries
    business evidence and the authorisation that justified it - never a
    credential, an authorization header or a provider URL.
    """

    remediation_id: int
    review_id: int
    anomaly_id: int
    calendar_date: date
    severity: str
    action_type: ActionType
    payload: dict[str, Any]
    approved_by: str
    authorized_by: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """Structured, never prose.

    `external_side_effect` is stated on every result rather than inferred. A
    development provider that quietly looked like a real one would be the single
    most misleading thing this stage could contain.
    """

    outcome: ExecutionOutcome
    provider: str
    provider_reference: str | None = None
    error_message: str | None = None
    latency_ms: int | None = None
    external_side_effect: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome is ExecutionOutcome.SUCCESS

    @property
    def may_retry(self) -> bool:
        return self.outcome is ExecutionOutcome.RETRYABLE_FAILURE


@dataclass
class RemediationRunSummary:
    """What one Stage 9 execution run did. Shaped for the ingestion_runs ledger."""

    #: Authorised actions found waiting.
    eligible: int = 0
    #: Handed to the provider this run - one outcome each.
    processed: int = 0
    executed: int = 0
    failed: int = 0
    #: Already executed, still unauthorised, or out of retry budget.
    skipped: int = 0

    executed_ids: list[int] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Derived from outcomes, never from having reached the end.

        An empty queue is a success: nobody approved anything, which is the
        normal state of a healthy week rather than a fault.
        """
        if self.failed and self.executed == 0 and self.processed > 0:
            return "failed"
        if self.failed:
            return "partial"
        return "success"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "eligible": self.eligible,
            "processed": self.processed,
            "executed": self.executed,
            "failed": self.failed,
            "skipped": self.skipped,
            "executed_ids": list(self.executed_ids),
            "failures": list(self.failures),
        }


# =============================================================================
# The request payload
# =============================================================================

#: Stated on every payload. A remediation "action" in this system is a request
#: for a person to look at something - and the one thing worse than not saying
#: so would be a downstream reader assuming otherwise.
NO_EXTERNAL_EFFECT_NOTE = (
    "This action is a request for human investigation or review. Executing it "
    "records the request and notifies nobody's systems: it issues no refund, "
    "changes no order, contacts no customer and moves no money."
)

_ACTION_SUMMARY = {
    ActionType.CREATE_INVESTIGATION:
        "Investigate the cause of this revenue anomaly and record what is found.",
    ActionType.REQUEST_OPERATIONS_REVIEW:
        "Review operational systems and processes for this date.",
    ActionType.REQUEST_REFUND_REVIEW:
        "Re-examine the refunds issued on this date and confirm each was legitimate.",
}


def build_request_payload(
    review: dict,
    action_type: ActionType,
    reason_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Assemble what the provider is asked to do.

    Three blocks, in the order a reader needs them: what is being requested,
    what was observed, and who authorised it. The authorisation block is not
    decoration - an action arriving at a downstream team without a named human
    behind it is indistinguishable from an automated one.

    Nothing from Stage 7 is included. A hypothesis is a guess, and putting one
    in front of the person asked to investigate would anchor the investigation
    on it. Its id is recorded on the row for provenance instead.
    """
    return {
        "remediation_version": "stage9-v1",
        "action": {
            "action_type": str(action_type),
            "request": _ACTION_SUMMARY[action_type],
            "note": NO_EXTERNAL_EFFECT_NOTE,
        },
        "observed": {
            "calendar_date": review["calendar_date"].isoformat(),
            "severity": review["severity"],
            "routing": review["routing"],
            "decision": review["decision"],
            "decision_version": review["decision_version"],
            "decision_reason_code": review["decision_reason_code"],
            "reason_codes": list(reason_codes),
            "business_impact_tier": review.get("business_impact_tier"),
            "expected_net_revenue_usd": _optional_float(review.get("expected_net_revenue_usd")),
            "actual_net_revenue_usd": _optional_float(review.get("actual_net_revenue_usd")),
            "revenue_delta_usd": _optional_float(review.get("revenue_delta_usd")),
            "revenue_delta_pct": _optional_float(review.get("revenue_delta_pct")),
            "anomaly_score": _optional_float(review.get("anomaly_score")),
        },
        "authorization": {
            "review_id": review["review_id"],
            "approved_by": review["approved_by"],
            "approved_at": _isoformat(review["approved_at"]),
            "review_resolution": review["resolution"],
            "policy": (
                "Authorised by a human through the Stage 8 review queue. No automated "
                "process may create or execute a remediation action without it."
            ),
        },
    }


def _optional_float(value) -> float | None:
    return None if value is None else float(value)


def _isoformat(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
