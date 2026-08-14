"""Stage 8 orchestration: who gets told, who gets queued, and what happened.

The shape of a run
------------------
1. Ask Stage 6 which decisions are actionable, and which way each one routes.
2. For an `auto_notify` decision: claim a notification row, render the body,
   attempt delivery, record the attempt and the outcome.
3. For a `human_review` decision: create a queue item.
4. Count what happened and report it honestly.

Steps 2 and 3 are mutually exclusive because Stage 6's routing is. Nothing here
chooses between them; `needs_notification` and `needs_review` arrive from the
decision row already answered.

What this module cannot do
--------------------------
It never writes to `anomaly_decisions`. Not on success, not on failure, not on
a retry that exhausts its budget. A delivery failure leaves the anomaly exactly
as serious as it was - unnotified is a recoverable state, and quietly downgrading
something because the webhook was down would be the worst possible response to a
transport error.

It also performs no business action. The last thing that happens to an anomaly
here is a row saying someone was told, or a row saying someone must look.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import psycopg

from analytics import repository
from analytics.config import Settings
from analytics.notifications.models import (
    DeliveryResult,
    HypothesisStatus,
    Notification,
    RoutingRunSummary,
    build_payload,
    build_subject,
)
from analytics.notifications.provider import NotificationProvider

logger = logging.getLogger(__name__)

DEFAULT_DECISION_VERSION = "stage6-v1"

# Bounded, and small. Three attempts across three scheduled runs is a day and a
# half of trying; a webhook that has not accepted a POST in that time is not
# going to be fixed by a fourth attempt from us.
MAX_DELIVERY_ATTEMPTS = 3


def run_routing(
    settings: Settings,
    provider: NotificationProvider,
    recipients: list[str],
    decision_version: str = DEFAULT_DECISION_VERSION,
    only_dates: list[date] | None = None,
    resend: bool = False,
) -> RoutingRunSummary:
    """Deliver notifications and queue reviews for actionable Stage 6 decisions.

    Args:
        resend: Re-attempt deliveries that already succeeded. Off by default and
            never set by the schedule - the whole point of the idempotency key is
            that a rerun does not tell somebody the same thing twice.
    """
    summary = RoutingRunSummary()
    seen_anomalies: set[int] = set()

    with repository.connect(settings.dsn) as connection:
        for recipient in recipients:
            rows = repository.load_routable_decisions(
                connection,
                decision_version=decision_version,
                channel=provider.channel,
                recipient=recipient,
            )

            if only_dates:
                wanted = set(only_dates)
                rows = [row for row in rows if row["calendar_date"] in wanted]

            for row in rows:
                # Eligibility is counted once per anomaly even when several
                # recipients are configured, so `eligible` stays a count of
                # anomalies rather than of messages.
                if row["anomaly_id"] not in seen_anomalies:
                    seen_anomalies.add(row["anomaly_id"])
                    summary.eligible += 1

                try:
                    _route_one(
                        connection=connection,
                        provider=provider,
                        row=row,
                        recipient=recipient,
                        resend=resend,
                        summary=summary,
                    )
                except Exception as exc:
                    connection.rollback()
                    summary.notifications_failed += 1
                    summary.failures.append({
                        "calendar_date": row["calendar_date"].isoformat(),
                        "anomaly_id": row["anomaly_id"],
                        "reason": f"{type(exc).__name__}: {exc}"[:400],
                    })
                    logger.warning(
                        "Routing failed for %s: %s", row["calendar_date"], exc
                    )

    logger.info(
        "Stage 8 complete (%s): %d eligible, %d processed, %d notified, %d failed, "
        "%d queued for review, %d skipped",
        summary.status, summary.eligible, summary.processed,
        summary.notifications_sent, summary.notifications_failed,
        summary.reviews_created, summary.skipped,
    )
    return summary


def _route_one(
    connection: psycopg.Connection,
    provider: NotificationProvider,
    row: dict,
    recipient: str,
    resend: bool,
    summary: RoutingRunSummary,
) -> None:
    # Stage 6 answered both of these. Stage 8 only reads them.
    if row["needs_review"]:
        _queue_review(connection, row, summary)
        return

    if row["needs_notification"]:
        _deliver(connection, provider, row, recipient, resend, summary)
        return

    # Actionable but neither notifiable nor reviewable should be unreachable -
    # Stage 6's routing is total. Counted rather than ignored, so if the decision
    # layer ever grows a fourth routing value this shows up as a number instead
    # of as silence.
    summary.skipped += 1
    logger.warning(
        "Decision %s (%s) is actionable but routes to %r - nothing to do",
        row["decision_id"], row["calendar_date"], row["routing"],
    )


# =============================================================================
# Human review
# =============================================================================


def _queue_review(connection: psycopg.Connection, row: dict, summary: RoutingRunSummary) -> None:
    if row["review_id"] is not None:
        summary.skipped += 1
        return

    summary.processed += 1

    # A missing Stage 7 analysis does not block the queue. The reviewer gets the
    # deterministic evidence - which is what actually justified the escalation -
    # and the item says plainly that no explanation was generated.
    hypothesis_status = (
        HypothesisStatus.AVAILABLE if row["hypothesis_id"] else HypothesisStatus.UNAVAILABLE
    )

    review_id = repository.create_review(connection, {
        "anomaly_id": row["anomaly_id"],
        "decision_id": row["decision_id"],
        "hypothesis_id": row["hypothesis_id"],
        "calendar_date": row["calendar_date"],
        "decision_version": row["decision_version"],
        "severity": row["severity"],
        "routing": row["routing"],
        "decision": row["decision"],
        "notification_allowed": row["notification_allowed"],
        "human_review_required": row["human_review_required"],
        "hypothesis_status": str(hypothesis_status),
    })
    connection.commit()

    if review_id is None:
        # Another run created it between our read and this write.
        summary.processed -= 1
        summary.skipped += 1
        return

    summary.reviews_created += 1
    summary.queued_dates.append(row["calendar_date"].isoformat())


# =============================================================================
# Notification
# =============================================================================


def _deliver(
    connection: psycopg.Connection,
    provider: NotificationProvider,
    row: dict,
    recipient: str,
    resend: bool,
    summary: RoutingRunSummary,
) -> None:
    hypothesis = _hypothesis_from(row)
    reason_codes = _reason_codes(connection, row["decision_id"])

    notification = Notification(
        anomaly_id=row["anomaly_id"],
        decision_id=row["decision_id"],
        hypothesis_id=row["hypothesis_id"],
        calendar_date=row["calendar_date"],
        decision_version=row["decision_version"],
        severity=row["severity"],
        routing=row["routing"],
        decision=row["decision"],
        notification_allowed=row["notification_allowed"],
        human_review_required=row["human_review_required"],
        channel=provider.channel,
        recipient=recipient,
        subject=build_subject(row),
        payload=build_payload(row, hypothesis, reason_codes),
        hypothesis_status=(
            HypothesisStatus.AVAILABLE if hypothesis else HypothesisStatus.UNAVAILABLE
        ),
    )

    claimed = repository.claim_notification(connection, {
        "anomaly_id": notification.anomaly_id,
        "decision_id": notification.decision_id,
        "hypothesis_id": notification.hypothesis_id,
        "calendar_date": notification.calendar_date,
        "decision_version": notification.decision_version,
        "severity": notification.severity,
        "routing": notification.routing,
        "decision": notification.decision,
        "notification_allowed": notification.notification_allowed,
        "human_review_required": notification.human_review_required,
        "channel": notification.channel,
        "recipient": notification.recipient,
        "subject": notification.subject,
        "payload": json.dumps(notification.payload),
        "hypothesis_status": str(notification.hypothesis_status),
    })
    connection.commit()

    if not _should_attempt(claimed, resend):
        summary.skipped += 1
        return

    summary.processed += 1
    attempt_number = int(claimed["attempt_count"]) + 1
    result = provider.send(notification)

    # The recorded number, not the requested one. They differ only if the
    # counter has drifted from the attempt history, and taking the recorded one
    # brings the counter back into step instead of leaving it permanently behind.
    attempt_number = repository.record_attempt(connection, {
        "notification_id": claimed["notification_id"],
        "attempt_number": attempt_number,
        "outcome": str(result.outcome),
        "provider": result.provider,
        "provider_message_id": result.provider_message_id,
        "status_code": result.status_code,
        "error_message": result.error_message,
        "latency_ms": result.latency_ms,
    })

    status = _status_after(result, attempt_number)
    repository.update_notification_result(connection, {
        "notification_id": claimed["notification_id"],
        "status": status,
        "attempt_count": attempt_number,
        "provider": result.provider,
        "provider_message_id": result.provider_message_id,
        "last_error": result.error_message,
    })
    connection.commit()

    if result.succeeded:
        summary.notifications_sent += 1
        summary.notified_dates.append(row["calendar_date"].isoformat())
        return

    summary.notifications_failed += 1
    summary.failures.append({
        "calendar_date": row["calendar_date"].isoformat(),
        "anomaly_id": row["anomaly_id"],
        "reason": (result.error_message or "delivery failed")[:400],
        "outcome": str(result.outcome),
        "attempt": attempt_number,
        "will_retry": status == "failed",
    })


def _should_attempt(claimed: dict, resend: bool) -> bool:
    """Whether this run should hand the notification to the provider.

    The four states, and why each behaves as it does:

      pending    never attempted - attempt it
      failed     retryable, and the budget is not spent - attempt it again
      abandoned  permanently failed or out of attempts - leave it alone
      sent       already delivered - do NOT resend. This is the whole point of
                 the idempotency key: a rerun must not tell somebody the same
                 thing twice.
    """
    status = claimed["status"]
    attempts = int(claimed["attempt_count"])

    if status == "sent":
        return resend
    if status == "abandoned":
        return resend
    if status == "failed":
        return attempts < MAX_DELIVERY_ATTEMPTS
    return True


def _status_after(result: DeliveryResult, attempt_number: int) -> str:
    if result.succeeded:
        return "sent"
    if result.may_retry and attempt_number < MAX_DELIVERY_ATTEMPTS:
        return "failed"
    # Permanent, or the retry budget is spent. Either way nothing further will
    # be tried automatically - which is a state someone should notice, so it
    # gets its own name rather than sitting in 'failed' forever.
    return "abandoned"


def _hypothesis_from(row: dict) -> dict | None:
    """The Stage 7 analysis attached to this decision, if there is one."""
    if not row.get("hypothesis_id"):
        return None
    return {
        "summary": row["hypothesis_summary"],
        "primary_hypothesis": row["primary_hypothesis"],
        "confidence": row["hypothesis_confidence"],
        "supporting_evidence": row.get("supporting_evidence") or [],
        "missing_evidence": row.get("missing_evidence") or [],
        "recommended_checks": row.get("recommended_checks") or [],
        "model_name": row.get("model_name"),
    }


def _reason_codes(connection: psycopg.Connection, decision_id: int) -> tuple[str, ...]:
    return repository.load_decision_reason_codes(connection, decision_id)


# =============================================================================
# Review transitions
#
# Thin on purpose. The state machine is a database trigger, so these functions
# name the operation and let the write be refused if it is not allowed. Putting
# the transition table here as well would create a second copy to disagree with.
# =============================================================================


class ReviewTransitionError(RuntimeError):
    """The requested transition is not permitted from the current state."""


def claim_review(settings: Settings, review_id: int, actor: str) -> dict:
    """pending -> in_review."""
    return _transition(settings, review_id, "in_review", assigned_to=actor)


def release_review(settings: Settings, review_id: int) -> dict:
    """in_review -> pending. Frees an item claimed by someone unavailable."""
    return _transition(settings, review_id, "pending")


def resolve_review(
    settings: Settings,
    review_id: int,
    resolution: str,
    actor: str | None = None,
    notes: str | None = None,
) -> dict:
    """in_review -> resolved.

    The resolution records what a human concluded. It is not a new severity, and
    nothing downstream fires because of it: Stage 8 ends here.
    """
    return _transition(
        settings, review_id, "resolved",
        assigned_to=actor, resolution=resolution, review_notes=notes,
    )


def dismiss_review(
    settings: Settings,
    review_id: int,
    resolution: str,
    actor: str | None = None,
    notes: str | None = None,
) -> dict:
    """pending | in_review -> dismissed."""
    return _transition(
        settings, review_id, "dismissed",
        assigned_to=actor, resolution=resolution, review_notes=notes,
    )


def _transition(
    settings: Settings,
    review_id: int,
    status: str,
    assigned_to: str | None = None,
    resolution: str | None = None,
    review_notes: str | None = None,
) -> dict:
    with repository.connect(settings.dsn) as connection:
        current = repository.get_review_row(connection, review_id)
        if current is None:
            raise LookupError(f"No review item {review_id}")

        try:
            updated = repository.update_review_status(connection, {
                "review_id": review_id,
                "status": status,
                "assigned_to": assigned_to,
                "resolution": resolution,
                # Untrusted human input. Bounded here as well as by the column
                # CHECK, and it never leaves this table - no notification payload
                # is ever built from a review note.
                "review_notes": review_notes[:4000] if review_notes else None,
            })
        except psycopg.errors.RaiseException as exc:
            connection.rollback()
            raise ReviewTransitionError(str(exc).strip()) from exc
        except psycopg.Error as exc:
            connection.rollback()
            raise ReviewTransitionError(str(exc).strip()) from exc

        connection.commit()
        return dict(updated) if updated else {}


def fetch_reviews(
    settings: Settings,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.list_reviews(connection, status, severity, limit)


def fetch_review(settings: Settings, review_id: int) -> dict | None:
    with repository.connect(settings.dsn) as connection:
        review = repository.get_review(connection, review_id)
        if review is None:
            return None
        return {**review, "history": repository.review_events(connection, review_id)}
