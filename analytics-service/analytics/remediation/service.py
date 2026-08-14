"""Stage 9 orchestration: approve, authorise, execute - three separate acts.

The shape of the stage
----------------------
1. A human working the Stage 8 queue **approves a review** and names an action.
   That is the authorisation. It creates a remediation action in `proposed`.
2. Somebody **authorises the action** (`proposed -> approved`). Approving the
   review said "this anomaly is real and warrants a response"; authorising the
   action says "and this is the response". They are different judgements, and
   collapsing them would make it impossible to record a reviewer who confirmed
   an anomaly but rejected the action proposed for it.
3. Something **executes it** - the scheduled workflow, or an explicit call.
   Execution is mechanical: it acts only on what a human already authorised, and
   it selects work by reading `remediation_pending_execution`, never by grading
   anything itself.

What this module cannot do
--------------------------
It never writes to `anomaly_decisions`, `anomaly_hypotheses`, `notifications` or
any Stage 8 table other than the one review transition a human explicitly asked
for. It computes no severity: which actions are permitted at which severity is a
foreign key into reference data, so an ineligible request fails as an integrity
error rather than passing a check somebody forgot to write.

And it takes no business action, because there is nothing here that could. The
provider records a request for human work and contacts nothing.
"""

from __future__ import annotations

import json
import logging

import psycopg

from analytics import repository
from analytics.config import Settings
from analytics.remediation.models import (
    ActionType,
    ExecutionResult,
    RemediationRequest,
    RemediationRunSummary,
    build_request_payload,
)
from analytics.remediation.provider import RemediationProvider

logger = logging.getLogger(__name__)

#: The eligibility ruleset in force. Part of every action row, so a policy change
#: is a versioned, visible event rather than a silent re-interpretation of
#: history - the same discipline `decision_version` gives Stage 6.
POLICY_VERSION = "stage9-v1"

MAX_EXECUTION_ATTEMPTS = repository.MAX_EXECUTION_ATTEMPTS

#: The only two review resolutions that can carry an approval. You cannot
#: authorise action on something you have just called a false alarm, and
#: 'expected_business_variation' says the movement was normal. Mirrored by a
#: CHECK constraint; kept here so the API refuses it with a clear message
#: instead of surfacing a constraint name.
APPROVABLE_RESOLUTIONS = ("confirmed", "requires_follow_up")


class RemediationError(RuntimeError):
    """The request is well-formed but the object is not in a state that permits it."""


class NotAuthorized(RemediationError):
    """Execution was requested for something no human has authorised."""


# =============================================================================
# 1. Approval - the authorisation boundary
# =============================================================================


def approve_review_for_remediation(
    settings: Settings,
    review_id: int,
    actor: str,
    action_type: ActionType,
    resolution: str = "confirmed",
    notes: str | None = None,
) -> dict:
    """Approve a Stage 8 review and propose one remediation action.

    Idempotent in both halves. Approving a review that is already `approved`
    reuses the original approval - the same approver, the same timestamp - and
    proposing an action that already exists returns it untouched, including one
    that has already executed. So a caller may retry this freely without
    creating a second authorisation or disturbing a completed action.

    Approving with a *different* action type on an already-approved review adds
    that action. A critical anomaly may legitimately warrant both an
    investigation and an operations review, and the review transition has
    already happened once.
    """
    if resolution not in APPROVABLE_RESOLUTIONS:
        raise RemediationError(
            f"Resolution {resolution!r} cannot authorise remediation. "
            f"Approval requires one of: {', '.join(APPROVABLE_RESOLUTIONS)}. "
            "A false positive or an expected business variation needs no action, "
            "and 'resolve' is the operation that records that."
        )

    with repository.connect(settings.dsn) as connection:
        review = repository.load_review_for_remediation(connection, review_id)
        if review is None:
            raise LookupError(f"No review item {review_id}")

        review = _ensure_approved(connection, review, actor, resolution, notes)

        reason_codes = repository.load_decision_reason_codes(
            connection, review["decision_id"]
        )
        payload = build_request_payload(review, action_type, reason_codes)

        try:
            claimed = repository.create_remediation_action(connection, {
                "review_id": review["review_id"],
                "anomaly_id": review["anomaly_id"],
                "decision_id": review["decision_id"],
                "hypothesis_id": review["hypothesis_id"],
                "calendar_date": review["calendar_date"],
                "decision_version": review["decision_version"],
                "severity": review["severity"],
                "routing": review["routing"],
                "decision": review["decision"],
                "notification_allowed": review["notification_allowed"],
                "human_review_required": review["human_review_required"],
                "decision_reason_code": review["decision_reason_code"],
                "decision_reason_codes": list(reason_codes),
                "hypothesis_status": review["hypothesis_status"],
                "hypothesis_prompt_version": review.get("hypothesis_prompt_version"),
                "hypothesis_model_name": review.get("hypothesis_model_name"),
                "review_approved_by": review["approved_by"],
                "review_approved_at": review["approved_at"],
                "review_resolution": review["resolution"],
                "action_type": str(action_type),
                "policy_version": POLICY_VERSION,
                "request_payload": json.dumps(payload),
            })
        except psycopg.errors.ForeignKeyViolation as exc:
            # The eligibility FK. A severity/action pair with no row in
            # remediation_action_eligibility cannot produce an action - the
            # rejection comes from the database, not from a branch up here.
            connection.rollback()
            raise RemediationError(
                f"Action {action_type} is not permitted for a {review['severity']} "
                f"anomaly under policy {POLICY_VERSION}."
            ) from exc
        except psycopg.Error as exc:
            connection.rollback()
            raise RemediationError(_clean(exc)) from exc

        connection.commit()

    logger.info(
        "Review %s approved by %s; remediation %s (%s) %s",
        review_id, actor, claimed["remediation_id"], action_type,
        "proposed" if claimed["created"] else "already existed",
    )
    return {
        "review_id": review_id,
        "review_status": "approved",
        "approved_by": review["approved_by"],
        "remediation_id": claimed["remediation_id"],
        "action_type": str(action_type),
        "status": claimed["status"],
        "created": claimed["created"],
        "executed": False,
        "note": (
            "The action is proposed, not executed. Authorising it and executing "
            "it are separate operations."
        ),
    }


def _ensure_approved(
    connection: psycopg.Connection,
    review: dict,
    actor: str,
    resolution: str,
    notes: str | None,
) -> dict:
    """Move the review to `approved`, or confirm it already is.

    'resolved' is refused explicitly rather than treated as approval. It means
    a human reviewed the anomaly and closed it WITHOUT remediation, and reading
    it as consent would be inventing an authorisation nobody gave.
    """
    if review["status"] == "approved":
        return review

    if review["status"] != "in_review":
        detail = (
            " 'resolved' means reviewed and closed WITHOUT remediation - it is not "
            "an approval."
            if review["status"] == "resolved"
            else " Claim it first."
            if review["status"] == "pending"
            else ""
        )
        raise RemediationError(
            f"Review {review['review_id']} is {review['status']}; remediation can "
            f"only be approved from 'in_review'.{detail}"
        )

    try:
        updated = repository.approve_review(connection, {
            "review_id": review["review_id"],
            "resolution": resolution,
            "actor": actor,
            # Untrusted human input. Bounded here as well as by the column CHECK,
            # and it never leaves the review table - no remediation payload is
            # built from a review note, and nothing executes one.
            "review_notes": notes[:4000] if notes else None,
        })
    except psycopg.Error as exc:
        connection.rollback()
        raise RemediationError(_clean(exc)) from exc

    if updated is None:
        # Lost a race with another approver. Re-read and use theirs.
        connection.rollback()
        current = repository.load_review_for_remediation(connection, review["review_id"])
        if current is None or current["status"] != "approved":
            raise RemediationError(
                f"Review {review['review_id']} could not be approved; it is "
                f"{current['status'] if current else 'gone'}."
            )
        return current

    return {**review, **updated, "status": "approved"}


# =============================================================================
# 2. Authorisation and refusal
# =============================================================================


def authorize_action(settings: Settings, remediation_id: int, actor: str) -> dict:
    """proposed -> approved. This is what makes an action executable."""
    with repository.connect(settings.dsn) as connection:
        current = _require_action(connection, remediation_id)

        if current["status"] != "proposed":
            if current["status"] == "approved":
                # Already authorised. Idempotent, and not a conflict: the caller
                # wanted it authorised and it is.
                return {
                    "remediation_id": remediation_id,
                    "status": "approved",
                    "authorized_by": current["authorized_by"],
                    "changed": False,
                }
            raise RemediationError(
                f"Remediation {remediation_id} is {current['status']}; only a "
                "'proposed' action can be authorised."
            )

        try:
            updated = repository.authorize_remediation(connection, remediation_id, actor)
        except psycopg.Error as exc:
            connection.rollback()
            raise RemediationError(_clean(exc)) from exc

        connection.commit()

    return {**dict(updated or {}), "changed": True, "executed": False}


def reject_action(settings: Settings, remediation_id: int, actor: str, reason: str) -> dict:
    """proposed -> rejected. The anomaly stays confirmed; this response does not."""
    return _close(settings, remediation_id, "rejected", actor, reason, ("proposed",))


def cancel_action(settings: Settings, remediation_id: int, actor: str, reason: str) -> dict:
    """-> cancelled, from any state that has not run.

    Deliberately not permitted from `executing` or `executed`. An action already
    handed to a provider cannot be un-handed by changing a row, and pretending
    otherwise would put a lie in the audit trail.
    """
    return _close(
        settings, remediation_id, "cancelled", actor, reason,
        ("proposed", "approved", "failed"),
    )


def _close(
    settings: Settings,
    remediation_id: int,
    status: str,
    actor: str,
    reason: str,
    from_statuses: tuple[str, ...],
) -> dict:
    if not reason or not reason.strip():
        raise RemediationError("A reason is required to close a remediation action.")

    with repository.connect(settings.dsn) as connection:
        current = _require_action(connection, remediation_id)

        try:
            updated = repository.close_remediation(
                connection, remediation_id, status, actor,
                reason.strip()[:2000], from_statuses,
            )
        except psycopg.Error as exc:
            connection.rollback()
            raise RemediationError(_clean(exc)) from exc

        if updated is None:
            connection.rollback()
            raise RemediationError(
                f"Remediation {remediation_id} is {current['status']}; it cannot be "
                f"{status}. Permitted from: {', '.join(from_statuses)}."
            )

        connection.commit()
        return dict(updated)


# =============================================================================
# 3. Execution
# =============================================================================


def execute_action(
    settings: Settings,
    provider: RemediationProvider,
    remediation_id: int,
    actor: str,
) -> dict:
    """Execute one authorised action. Refuses everything else.

    The claim into `executing` is a conditional UPDATE, so this function calls
    the provider at most once per logical action no matter how many callers
    invoke it concurrently or how many times a scheduled run repeats.
    """
    with repository.connect(settings.dsn) as connection:
        current = _require_action(connection, remediation_id)

        if current["status"] == "proposed":
            raise NotAuthorized(
                f"Remediation {remediation_id} has not been authorised. Approving the "
                "review confirmed the anomaly; authorising the action is a separate "
                "step, and execution requires it."
            )
        if current["status"] == "executed":
            # Terminal, and the point of the whole stage. Reported rather than
            # raised: the caller asked for it to be executed and it is.
            return _already(current, "already executed")
        if current["status"] in ("rejected", "cancelled"):
            raise RemediationError(
                f"Remediation {remediation_id} is {current['status']}; it will not execute."
            )

        result = _execute_claimed(connection, provider, remediation_id, actor)
        if result is None:
            return _already(
                _require_action(connection, remediation_id),
                "not claimable - already executing, or out of retry budget",
            )
        return result


def execute_approved(
    settings: Settings,
    provider: RemediationProvider,
    actor: str = "stage9-workflow",
    limit: int = 100,
) -> RemediationRunSummary:
    """Execute every authorised action waiting to run.

    This is what the scheduled workflow calls. It reads its work from
    `remediation_pending_execution`, which contains only actions a human
    authorised and excludes anything out of retry budget - so the workflow
    selects nothing, grades nothing and authorises nothing.
    """
    summary = RemediationRunSummary()

    with repository.connect(settings.dsn) as connection:
        pending = repository.list_executable_remediations(connection, limit=limit)
        summary.eligible = len(pending)

        for row in pending:
            remediation_id = row["remediation_id"]
            try:
                outcome = _execute_claimed(connection, provider, remediation_id, actor)
            except Exception as exc:  # noqa: BLE001 - one action must not stop the run
                connection.rollback()
                summary.failed += 1
                summary.failures.append({
                    "remediation_id": remediation_id,
                    "reason": f"{type(exc).__name__}: {exc}"[:400],
                })
                logger.warning("Remediation %s failed: %s", remediation_id, exc)
                continue

            if outcome is None:
                # Claimed by someone else between the read and the write.
                summary.skipped += 1
                continue

            summary.processed += 1
            if outcome["status"] == "executed":
                summary.executed += 1
                summary.executed_ids.append(remediation_id)
            else:
                summary.failed += 1
                summary.failures.append({
                    "remediation_id": remediation_id,
                    "reason": (outcome.get("error") or "execution failed")[:400],
                    "attempt": outcome.get("attempt"),
                    "will_retry": outcome.get("will_retry", False),
                })

    logger.info(
        "Stage 9 complete (%s): %d eligible, %d processed, %d executed, %d failed, "
        "%d skipped",
        summary.status, summary.eligible, summary.processed,
        summary.executed, summary.failed, summary.skipped,
    )
    return summary


def _execute_claimed(
    connection: psycopg.Connection,
    provider: RemediationProvider,
    remediation_id: int,
    actor: str,
) -> dict | None:
    """Claim, execute, record. Returns None if the claim was lost."""
    claimed = repository.claim_remediation_for_execution(connection, remediation_id, actor)
    connection.commit()

    if claimed is None:
        return None

    attempt_number = int(claimed["attempt_count"]) + 1
    request = RemediationRequest(
        remediation_id=remediation_id,
        review_id=claimed["review_id"],
        anomaly_id=claimed["anomaly_id"],
        calendar_date=claimed["calendar_date"],
        severity=claimed["severity"],
        action_type=ActionType(claimed["action_type"]),
        payload=claimed["request_payload"],
        approved_by=claimed["review_approved_by"],
        authorized_by=claimed["authorized_by"],
    )

    try:
        result = provider.execute(request)
    except Exception as exc:  # noqa: BLE001
        # A provider is contracted never to raise. If one does, the action must
        # not be left stranded in 'executing' - so the exception becomes a
        # recorded permanent failure rather than a hung row.
        from analytics.remediation.models import ExecutionOutcome

        logger.exception("Provider raised for remediation %s", remediation_id)
        result = ExecutionResult(
            outcome=ExecutionOutcome.PERMANENT_FAILURE,
            provider=getattr(provider, "name", "unknown"),
            error_message=f"provider raised {type(exc).__name__}: {exc}"[:400],
            external_side_effect=False,
        )

    repository.record_remediation_attempt(connection, {
        "remediation_id": remediation_id,
        "attempt_number": attempt_number,
        "outcome": str(result.outcome),
        "provider": result.provider,
        "provider_reference": result.provider_reference,
        "error_message": result.error_message,
        "latency_ms": result.latency_ms,
        "external_side_effect": result.external_side_effect,
    })

    status = "executed" if result.succeeded else "failed"
    repository.finish_remediation_execution(connection, {
        "remediation_id": remediation_id,
        "status": status,
        "attempt_count": attempt_number,
        "provider": result.provider,
        "provider_reference": result.provider_reference,
        "last_error": result.error_message,
        "actor": actor,
    })
    connection.commit()

    return {
        "remediation_id": remediation_id,
        "status": status,
        "attempt": attempt_number,
        "provider": result.provider,
        "provider_reference": result.provider_reference,
        "error": result.error_message,
        "will_retry": (
            not result.succeeded
            and result.may_retry
            and attempt_number < MAX_EXECUTION_ATTEMPTS
        ),
        "external_side_effect": result.external_side_effect,
    }


def _already(current: dict, why: str) -> dict:
    return {
        "remediation_id": current["remediation_id"],
        "status": current["status"],
        "attempt": current["attempt_count"],
        "changed": False,
        "note": why,
        "external_side_effect": False,
    }


# =============================================================================
# Reads
# =============================================================================


def fetch_actions(
    settings: Settings,
    status: str | None = None,
    severity: str | None = None,
    action_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.list_remediations(connection, status, severity, action_type, limit)


def fetch_action(settings: Settings, remediation_id: int) -> dict | None:
    with repository.connect(settings.dsn) as connection:
        action = repository.get_remediation(connection, remediation_id)
        if action is None:
            return None
        return {
            **action,
            "attempts": repository.remediation_attempts(connection, remediation_id),
            "history": repository.remediation_events(connection, remediation_id),
        }


def fetch_events(settings: Settings, remediation_id: int) -> list[dict] | None:
    with repository.connect(settings.dsn) as connection:
        if repository.get_remediation_row(connection, remediation_id) is None:
            return None
        return repository.remediation_events(connection, remediation_id)


def fetch_vocabulary(settings: Settings) -> dict:
    """The action vocabulary and the eligibility policy, read from the database.

    Read rather than hardcoded so the endpoint cannot describe a policy the
    database does not enforce.
    """
    with repository.connect(settings.dsn) as connection:
        return {
            "policy_version": POLICY_VERSION,
            "max_execution_attempts": MAX_EXECUTION_ATTEMPTS,
            "actions": repository.load_action_vocabulary(connection, POLICY_VERSION),
            "eligibility": repository.load_action_eligibility(connection, POLICY_VERSION),
        }


def _require_action(connection: psycopg.Connection, remediation_id: int) -> dict:
    current = repository.get_remediation_row(connection, remediation_id)
    if current is None:
        raise LookupError(f"No remediation action {remediation_id}")
    return current


def _clean(exc: Exception) -> str:
    """A database refusal, without the stack of context lines around it."""
    return str(exc).strip().split("\n")[0]
