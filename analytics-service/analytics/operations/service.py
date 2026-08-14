"""Stage 10 orchestration: recovery, replay, retention and health.

Almost every operation here is one call into a V012 function. That thinness is
deliberate - recovery has to be atomic and has to hold its lock while it
decides, and both of those are properties of a single SQL statement rather than
of a sequence of Python round trips. What this module adds is the run boundary,
the per-step isolation, and the honest derivation of a run's status.

Two rules hold throughout.

**Recovery never repeats work.** Closing a stale run does not re-run it. Moving
a crashed remediation to `execution_unknown` does not call a provider. Replay is
the only operation here that causes anything to happen twice, it is explicit, it
is bounded, and it is idempotent against `fact_orders`.

**One failed step never stops the others.** The maintenance operations are
unrelated to each other; a retention sweep failing has nothing to do with
whether a stale run gets closed. A reliability feature that abandons the rest of
its work on the first error is a reliability feature that reduces reliability.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from analytics import repository
from analytics.config import Settings
from analytics.notifications import service as notification_service
from analytics.notifications.provider import NotificationProvider
from analytics.operations.models import MaintenanceStep, MaintenanceSummary, StepOutcome

logger = logging.getLogger(__name__)

#: Every automated recovery is attributed to this, never to a person. An
#: operator reading the audit log must be able to tell at a glance what the
#: machine did to itself.
RECOVERY_ACTOR = "stage10-recovery"


class OperationsError(RuntimeError):
    """The request is well-formed but the operation cannot be performed."""


# =============================================================================
# Reads
# =============================================================================


def health(settings: Settings) -> dict:
    with repository.connect(settings.dsn) as connection:
        return {
            "summary": repository.operational_health_summary(connection),
            "components": repository.operational_health(connection),
        }


def configuration(settings: Settings) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.operational_config(connection)


def retry_queue(
    settings: Settings,
    entity_type: str | None = None,
    eligible_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.retry_queue(connection, entity_type, eligible_only, limit)


def replay_candidates(settings: Settings) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.replay_candidates(connection)


def review_ageing(settings: Settings, bucket: str | None = None) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.review_ageing(connection, bucket)


def retention_report(settings: Settings) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.retention_report(connection)


def events(
    settings: Settings,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with repository.connect(settings.dsn) as connection:
        return repository.operational_events(connection, entity_type, entity_id, limit)


# =============================================================================
# Recovery
# =============================================================================


def recover_stale_runs(
    settings: Settings, actor: str = RECOVERY_ACTOR, dry_run: bool = False
) -> list[dict]:
    """Close runs abandoned at 'running'.

    The work is NOT repeated. Whether it should be is a separate question with a
    different answer per pipeline: the ingestion window self-heals on the next
    run, Stages 5-8 are idempotent, and Stage 9 needs a human. Recovery only
    stops the ledger lying about what is currently in flight.
    """
    with repository.connect(settings.dsn) as connection:
        recovered = repository.recover_stale_runs(connection, actor, dry_run)
        connection.commit()
    return recovered


def recover_stale_remediation(
    settings: Settings, actor: str = RECOVERY_ACTOR, dry_run: bool = False
) -> list[dict]:
    """Move crashed executions into `execution_unknown`.

    Never calls a provider. The process died somewhere around a provider call
    and nothing in the database can know whether that call landed - so the
    uncertainty is recorded rather than resolved, and a human reconciles it.
    """
    with repository.connect(settings.dsn) as connection:
        recovered = repository.recover_stale_remediation(connection, actor, dry_run)
        connection.commit()
    return recovered


def reconcile_remediation(
    settings: Settings,
    remediation_id: int,
    outcome: str,
    actor: str,
    evidence: str,
) -> dict:
    """Record what actually happened to an action stranded in execution_unknown."""
    import psycopg

    with repository.connect(settings.dsn) as connection:
        try:
            result = repository.reconcile_remediation(
                connection, remediation_id, outcome, actor, evidence
            )
        except psycopg.Error as exc:
            connection.rollback()
            raise OperationsError(str(exc).strip().split("\n")[0]) from exc
        connection.commit()
    return result


# =============================================================================
# Replay
# =============================================================================


def replay_batch(settings: Settings, batch_id: str, actor: str = RECOVERY_ACTOR) -> dict:
    """Replay one failed staging batch.

    Takes a batch id and nothing else - no payload, no override, no correction.
    The payloads come from the database, so there is no path by which a caller
    can put an order into the warehouse through this endpoint.
    """
    import psycopg

    with repository.connect(settings.dsn) as connection:
        try:
            result = repository.replay_failed_batch(connection, batch_id, actor)
        except psycopg.Error as exc:
            connection.rollback()
            raise OperationsError(str(exc).strip().split("\n")[0]) from exc
        connection.commit()

    logger.info(
        "Replayed batch %s: %s staged, %s accepted, %s rejected, %s duplicate (%s)",
        batch_id, result.get("rows_staged"), result.get("records_accepted"),
        result.get("records_rejected"), result.get("records_duplicate"),
        result.get("run_status"),
    )
    return result


# =============================================================================
# Retention
# =============================================================================


def purge_staging(
    settings: Settings, dry_run: bool = True, actor: str = RECOVERY_ACTOR
) -> dict:
    """Delete settled staging rows past the retention period.

    Dry run by default, here as well as in SQL. `pending` and `failed` rows are
    never eligible - unfinished work and the dead-letter trail respectively -
    and neither is any row involved in a replay.
    """
    with repository.connect(settings.dsn) as connection:
        result = repository.purge_staging(connection, dry_run, actor)
        connection.commit()
    return result


# =============================================================================
# Notification retry
# =============================================================================


def retry_stale_notifications(
    settings: Settings,
    provider: NotificationProvider,
    recipients: list[str],
    actor: str = RECOVERY_ACTOR,
) -> dict:
    """Ask Stage 8 to route again, restricted to dates with a stale notification.

    Nothing here delivers anything. Stage 8 owns delivery, its idempotency key,
    its retry classification and its attempt accounting, and re-implementing any
    of that would create a second delivery path to disagree with the first.

    Restricting to the stale dates matters: calling Stage 8 unrestricted would
    also create notifications and review items for anything newly actionable,
    which is routing work masquerading as maintenance.

    A notification that is already `sent` cannot be touched by this - Stage 8
    refuses to resend without an explicit `resend` flag, which is never set here.
    """
    with repository.connect(settings.dsn) as connection:
        stale = repository.stale_notifications(connection)

    eligible = [row for row in stale if row["retry_eligible"]]
    if not eligible:
        return {"stale": len(stale), "eligible": 0, "retried": 0, "dates": []}

    dates: list[date] = sorted({row["calendar_date"] for row in eligible})

    summary = notification_service.run_routing(
        settings=settings,
        provider=provider,
        recipients=recipients,
        only_dates=dates,
        resend=False,
    )

    with repository.connect(settings.dsn) as connection:
        repository.record_maintenance_event(connection, {
            "entity_id": "notification-retry",
            "actor": actor,
            "reason_code": "NOTIFICATION_RETRY_REQUESTED",
            "detail": json.dumps({
                "stale": len(stale),
                "eligible": len(eligible),
                "dates": [d.isoformat() for d in dates],
                "sent": summary.notifications_sent,
                "failed": summary.notifications_failed,
                "resend_flag": False,
            }),
        })
        connection.commit()

    return {
        "stale": len(stale),
        "eligible": len(eligible),
        "retried": summary.notifications_sent + summary.notifications_failed,
        "sent": summary.notifications_sent,
        "failed": summary.notifications_failed,
        # Stage 8's own verdict, carried through rather than re-derived. Without
        # it a retry that attempted one delivery and failed reads identically to
        # one that attempted one and succeeded.
        "routing_status": summary.status,
        "failures": [f.get("reason") for f in summary.failures][:5],
        "dates": [d.isoformat() for d in dates],
    }


# =============================================================================
# The maintenance run
# =============================================================================


def run_maintenance(
    settings: Settings,
    provider: NotificationProvider | None = None,
    recipients: list[str] | None = None,
    actor: str = RECOVERY_ACTOR,
    purge: bool = False,
    replay: bool = False,
) -> MaintenanceSummary:
    """Every maintenance operation, each isolated from the others.

    Args:
        purge: Actually delete retention-eligible staging rows. Off by default:
            the reporting step runs either way, so a scheduled run says what it
            would delete before anybody authorises deleting it.
        replay: Replay eligible failed batches. Off by default for the same
            reason - replay is the one operation here that repeats work.
    """
    summary = MaintenanceSummary()

    summary.record(_step("recover_stale_runs", lambda: {
        "recovered": len(recover_stale_runs(settings, actor)),
    }))

    summary.record(_step("recover_stale_remediation", lambda: {
        "recovered": len(recover_stale_remediation(settings, actor)),
    }))

    summary.record(_step("review_ageing", lambda: _ageing_counts(settings)))

    summary.record(_step("staging_retention", lambda: _retention_step(settings, purge, actor)))

    summary.record(_step("replay_candidates", lambda: _replay_step(settings, replay, actor)))

    if provider is not None and recipients:
        summary.record(_step("notification_retry", lambda: _notification_step(
            settings, provider, recipients, actor)))
    else:
        summary.record(MaintenanceStep(
            name="notification_retry",
            outcome=StepOutcome.SKIPPED,
            detail={"reason": "no delivery channel configured"},
        ))

    summary.record(_step("health", lambda: _health_step(settings)))

    logger.info(
        "Stage 10 maintenance complete (%s): %d succeeded, %d skipped, %d failed, "
        "%d change(s) made",
        summary.status, summary.succeeded, summary.skipped, summary.failed,
        summary.changes_made,
    )
    return summary


def _step(name: str, operation) -> MaintenanceStep:
    """Run one maintenance operation, isolated.

    A step that raises is recorded and the run continues. This is the whole
    reason the steps are separate calls rather than one transaction: they are
    unrelated operations, and one being impossible says nothing about whether
    the others are.
    """
    try:
        detail = operation()
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        logger.warning("Maintenance step %s failed: %s", name, exc)
        return MaintenanceStep(
            name=name,
            outcome=StepOutcome.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:400],
        )

    changed = any(
        isinstance(value, (int, float)) and value
        for key, value in detail.items()
        if key.startswith(("recovered", "deleted", "staged", "retried", "closed"))
    )
    return MaintenanceStep(
        name=name,
        outcome=StepOutcome.SUCCEEDED if changed else StepOutcome.SKIPPED,
        detail=detail,
    )


def _ageing_counts(settings: Settings) -> dict:
    """Read-only. Ageing changes no review state, ever.

    "Nobody has looked at this" is not a decision, and a system that resolved,
    dismissed or approved an item because it got old would be making one.
    """
    rows = review_ageing(settings)
    buckets: dict[str, int] = {}
    for row in rows:
        buckets[row["ageing_bucket"]] = buckets.get(row["ageing_bucket"], 0) + 1
    return {
        "open_reviews": len(rows),
        "buckets": buckets,
        "escalation_eligible": sum(1 for row in rows if row["escalation_eligible"]),
        "state_changes": 0,
    }


def _retention_step(settings: Settings, purge: bool, actor: str) -> dict:
    report = purge_staging(settings, dry_run=not purge, actor=actor)
    return {
        "eligible": int(report.get("rows_eligible") or 0),
        "deleted": int(report.get("rows_deleted") or 0),
        "protected": int(report.get("rows_protected") or 0),
        "retention_days": int(report.get("retention_days") or 0),
        "dry_run": bool(report.get("dry_run")),
    }


def _replay_step(settings: Settings, replay: bool, actor: str) -> dict:
    candidates = replay_candidates(settings)
    eligible = [c for c in candidates if c["replay_eligible"]]

    if not replay or not eligible:
        return {
            "candidates": len(candidates),
            "eligible": len(eligible),
            "staged": 0,
            "replayed": False,
        }

    staged = 0
    accepted = 0
    for candidate in eligible:
        result = replay_batch(settings, str(candidate["original_batch_id"]), actor)
        staged += int(result.get("rows_staged") or 0)
        accepted += int(result.get("records_accepted") or 0)

    return {
        "candidates": len(candidates),
        "eligible": len(eligible),
        "staged": staged,
        "accepted": accepted,
        "replayed": True,
    }


def _notification_step(
    settings: Settings, provider: NotificationProvider, recipients: list[str], actor: str
) -> dict:
    result = retry_stale_notifications(settings, provider, recipients, actor)
    if result.get("routing_status") == "failed":
        # Stage 8 tried and every delivery failed. Reporting that as a
        # successful maintenance step would hide the one thing the step exists
        # to surface.
        raise OperationsError(
            f"Stage 8 routing failed for {result['eligible']} stale notification(s): "
            + "; ".join(str(f) for f in result.get("failures") or [])[:300]
        )
    return {
        "stale": result["stale"],
        "eligible": result["eligible"],
        "retried": result["retried"],
        "sent": result["sent"],
        "failed": result["failed"],
        "routing_status": result.get("routing_status"),
    }


def _health_step(settings: Settings) -> dict:
    report = health(settings)
    summary = report["summary"]
    return {
        "overall_status": summary.get("overall_status"),
        "unhealthy": list(summary.get("unhealthy") or []),
        "degraded": int(summary.get("degraded") or 0),
        "failed_components": int(summary.get("failed") or 0),
    }
