"""What gets delivered, and what a delivery attempt returned.

The notification body is the interesting part of this module. It has one job
beyond being readable: never letting a hypothesis be mistaken for a finding.

Stage 5 and Stage 6 produce facts - this day's revenue, how far it moved, what
the rules concluded. Stage 7 produces a guess. Both arrive in the same message,
and the reader is someone scanning it between other work. So the message is
built in three labelled blocks rather than as prose:

    OBSERVED        measured, from the warehouse
    HYPOTHESIS      a model's suggestion, hedged
    NOT CONFIRMED   what the data cannot show

A reader who stops after the first block has read only true things.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class DeliveryOutcome(StrEnum):
    """What one attempt achieved, and whether trying again could help.

    The distinction drives the whole retry policy. A timeout is worth another
    attempt; an invalid recipient never will be, and retrying it three times
    only delays the moment somebody notices it is wrong.
    """

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


class HypothesisStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DeliveryResult:
    """Structured, never prose.

    A provider that returned "something went wrong" would put the retry decision
    back in the caller's hands as a string-matching exercise.
    """

    outcome: DeliveryOutcome
    provider: str
    status_code: int | None = None
    provider_message_id: str | None = None
    error_message: str | None = None
    latency_ms: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is DeliveryOutcome.SUCCESS

    @property
    def may_retry(self) -> bool:
        return self.outcome is DeliveryOutcome.RETRYABLE_FAILURE


@dataclass(frozen=True)
class Notification:
    """One rendered notification, ready to hand to a provider.

    `payload` is what is persisted and transmitted. It carries business evidence
    and nothing else: no credential, no authorization header, no provider URL.
    """

    anomaly_id: int
    decision_id: int
    hypothesis_id: int | None
    calendar_date: date

    decision_version: str
    severity: str
    routing: str
    decision: str
    notification_allowed: bool
    human_review_required: bool

    channel: str
    recipient: str
    subject: str
    payload: dict[str, Any]
    hypothesis_status: HypothesisStatus


@dataclass
class RoutingRunSummary:
    """What one Stage 8 run did. Shaped for the ingestion_runs ledger."""

    #: Actionable Stage 6 decisions found.
    eligible: int = 0
    #: Handled this run - one outcome each.
    processed: int = 0
    notifications_sent: int = 0
    notifications_failed: int = 0
    reviews_created: int = 0
    #: Already delivered or already queued.
    skipped: int = 0

    failures: list[dict] = field(default_factory=list)
    notified_dates: list[str] = field(default_factory=list)
    queued_dates: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Derived from outcomes, never from having reached the end.

        An empty eligible set is a success: Stage 6 found nothing actionable,
        which is the normal state of a healthy week and not a fault.
        """
        succeeded = self.notifications_sent + self.reviews_created
        if self.notifications_failed and succeeded == 0 and self.processed > 0:
            return "failed"
        if self.notifications_failed:
            return "partial"
        return "success"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "eligible": self.eligible,
            "processed": self.processed,
            "notifications_sent": self.notifications_sent,
            "notifications_failed": self.notifications_failed,
            "reviews_created": self.reviews_created,
            "skipped": self.skipped,
            "failures": list(self.failures),
            "notified_dates": list(self.notified_dates),
            "queued_dates": list(self.queued_dates),
        }


# =============================================================================
# Rendering
# =============================================================================

#: Section labels. Constants because the tests assert on them, and because the
#: separation they create is the point of the message rather than decoration.
OBSERVED = "OBSERVED"
HYPOTHESIS = "HYPOTHESIS"
NOT_CONFIRMED = "NOT CONFIRMED"

_SEVERITY_PREFIX = {
    "minor": "Minor",
    "major": "Major",
    "critical": "CRITICAL",
}


def build_subject(row: dict) -> str:
    prefix = _SEVERITY_PREFIX.get(row["severity"], row["severity"].title())
    delta = row.get("revenue_delta_usd")
    movement = ""
    if delta is not None:
        direction = "down" if float(delta) < 0 else "up"
        movement = f" - revenue {direction} {_money(abs(float(delta)))}"
    return f"[{prefix}] Revenue anomaly {row['calendar_date'].isoformat()}{movement}"


def build_payload(row: dict, hypothesis: dict | None, reason_codes: tuple[str, ...]) -> dict:
    """Assemble the notification body.

    Deliberately not the whole database row. A notification that dumps every
    column is a notification nobody reads, and the columns a reader needs are
    the ones that say what moved and by how much.
    """
    observed = {
        "calendar_date": row["calendar_date"].isoformat(),
        "severity": row["severity"],
        "routing": row["routing"],
        "decision": row["decision"],
        "decision_version": row["decision_version"],
        "reason_codes": list(reason_codes),
        "business_impact_tier": row.get("business_impact_tier"),
        "actual_net_revenue_usd": _optional_float(row.get("actual_net_revenue_usd")),
        "expected_net_revenue_usd": _optional_float(row.get("expected_net_revenue_usd")),
        "revenue_delta_usd": _optional_float(row.get("revenue_delta_usd")),
        "revenue_delta_pct": _optional_float(row.get("revenue_delta_pct")),
        "average_order_value_usd": _optional_float(row.get("average_order_value_usd")),
        "refund_rate": _optional_float(row.get("refund_rate")),
        "orders_count": row.get("orders_count"),
        "anomaly_score": _optional_float(row.get("anomaly_score")),
        "signal_count": row.get("signal_count"),
    }

    if hypothesis is None:
        # Section 18: say so plainly rather than inventing an explanation, and
        # never let a missing analysis look like an absence of concern.
        analysis: dict[str, Any] = {
            "status": str(HypothesisStatus.UNAVAILABLE),
            "note": (
                "AI analysis unavailable for this anomaly. The deterministic evidence "
                "above stands on its own; no explanation has been generated."
            ),
        }
    else:
        analysis = {
            "status": str(HypothesisStatus.AVAILABLE),
            "summary": hypothesis["summary"],
            "primary_hypothesis": hypothesis["primary_hypothesis"],
            "confidence": hypothesis["confidence"],
            "supporting_evidence": [
                item.get("metric") for item in hypothesis.get("supporting_evidence") or []
            ],
            "recommended_checks": list(hypothesis.get("recommended_checks") or []),
            "model": hypothesis.get("model_name"),
            "caveat": (
                "This is a generated hypothesis, not a confirmed cause. It has not "
                "been verified against any system outside this warehouse."
            ),
        }

    return {
        "notification_version": "stage8-v1",
        "title": build_subject(row),
        OBSERVED: observed,
        HYPOTHESIS: analysis,
        NOT_CONFIRMED: _not_confirmed(hypothesis),
        "text": render_text(row, hypothesis, reason_codes),
    }


def _not_confirmed(hypothesis: dict | None) -> dict:
    """What the warehouse cannot show.

    Comes from the model's own `missing_evidence` when there is one, because
    that is the model saying what it could not check. With no analysis, the
    statement is simply that no cause has been established.
    """
    if hypothesis is None:
        return {
            "statement": (
                "No cause has been established. This warehouse contains order "
                "transactions, exchange rates and derived KPIs only."
            ),
            "missing_evidence": [],
        }

    return {
        "statement": (
            "The hypothesis above is not confirmed. The following evidence would be "
            "needed to test it and is not held in this warehouse."
        ),
        "missing_evidence": list(hypothesis.get("missing_evidence") or []),
    }


def render_text(row: dict, hypothesis: dict | None, reason_codes: tuple[str, ...]) -> str:
    """A readable version of the same content, for channels that show text."""
    lines: list[str] = [build_subject(row), ""]

    lines.append(f"{OBSERVED}:")
    lines.append(
        f"  {row['calendar_date'].isoformat()} was graded {row['severity'].upper()} "
        f"by the deterministic rules (routing: {row['routing']})."
    )
    delta = row.get("revenue_delta_usd")
    pct = row.get("revenue_delta_pct")
    if delta is not None and pct is not None:
        direction = "below" if float(delta) < 0 else "above"
        lines.append(
            f"  Net revenue {_money(row.get('actual_net_revenue_usd'))} against an expected "
            f"{_money(row.get('expected_net_revenue_usd'))} - "
            f"{_money(abs(float(delta)))} {direction} baseline ({float(pct):+.1f}%)."
        )
    if row.get("average_order_value_usd") is not None:
        lines.append(
            f"  Average order value {_money(row['average_order_value_usd'])}, "
            f"refund rate {_percent(row.get('refund_rate'))}, "
            f"{row.get('orders_count')} orders."
        )
    if row.get("anomaly_score") is not None:
        lines.append(
            f"  Anomaly score {float(row['anomaly_score']):.2f} "
            f"({row.get('signal_count')} of 4 signals individually significant)."
        )
    if reason_codes:
        lines.append(f"  Reason codes: {', '.join(reason_codes)}.")
    lines.append("")

    lines.append(f"{HYPOTHESIS}:")
    if hypothesis is None:
        lines.append("  AI analysis unavailable for this anomaly. No explanation has")
        lines.append("  been generated; the observed evidence above stands on its own.")
    else:
        lines.append(f"  {hypothesis['summary']}")
        lines.append(f"  Most plausible explanation: {hypothesis['primary_hypothesis']}")
        lines.append(f"  Confidence in this explanation: {hypothesis['confidence']}.")
        checks = hypothesis.get("recommended_checks") or []
        if checks:
            lines.append("  Suggested checks:")
            for check in checks[:4]:
                lines.append(f"    - {check}")
    lines.append("")

    lines.append(f"{NOT_CONFIRMED}:")
    if hypothesis is None:
        lines.append("  No cause has been established.")
    else:
        lines.append("  The explanation above is a hypothesis, not a finding. It has not")
        lines.append("  been verified against any system outside this warehouse.")
        for gap in (hypothesis.get("missing_evidence") or [])[:4]:
            lines.append(f"    - {gap}")
    lines.append("")

    if row["routing"] == "human_review":
        lines.append("This anomaly requires human review before any action is taken.")
    else:
        lines.append("No action has been taken. This notification is informational.")

    return "\n".join(lines)


def _money(value) -> str:
    return "unavailable" if value is None else f"${float(value):,.2f}"


def _percent(value) -> str:
    return "unavailable" if value is None else f"{float(value) * 100:.2f}%"


def _optional_float(value) -> float | None:
    return None if value is None else float(value)
