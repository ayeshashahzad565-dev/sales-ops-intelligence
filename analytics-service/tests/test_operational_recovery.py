"""Recovery: closing what is stuck, without repeating what it was doing.

The distinction this file exists to protect:

    RECOVERY      moves a stuck record into an honest, final-or-actionable
                  state. It never repeats work.
    RE-EXECUTION  repeats work. It is always somebody's explicit decision.

The sharpest case is a remediation action stranded in `executing`. The process
died around a provider call and nothing in the database can know whether that
call landed. Re-executing might do the thing twice; failing it might claim
something did not happen when it did. Both automatic answers are wrong, so
recovery produces `execution_unknown` and a human reconciles it - and several
tests here exist purely to prove no code path skips that.
"""

from __future__ import annotations

import pytest

from analytics import repository
from analytics.notifications import service as notification_service
from analytics.notifications.provider import RecordingProvider
from analytics.operations import service
from analytics.remediation import service as remediation_service
from analytics.remediation.models import ActionType
from analytics.remediation.provider import RecordingRemediationProvider
from tests.operations_fixtures import (
    TEST_ACTOR,
    TEST_SOURCE,
    all_fingerprints,
    execute,
    make_run,
    make_settings,
    purge_test_data,
    query,
    threshold,
)


@pytest.fixture(scope="session")
def settings():
    return make_settings()


@pytest.fixture(scope="session")
def connection(settings):
    try:
        conn = repository.connect(settings.dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")
    with conn:
        yield conn


@pytest.fixture(autouse=True)
def clean(settings, connection):
    """A fresh review queue and no Stage 10 test debris, before every test.

    The queue is rebuilt rather than rewound. Approving a review makes it
    terminal by design, so a suite that consumes one cannot put it back - and
    without a rebuild the first few tests would silently starve the rest, which
    is how a suite ends up reporting 13 skips as a pass.
    """
    _reset_queue(settings, connection)
    purge_test_data(connection)
    yield
    purge_test_data(connection)
    _reset_queue(settings, connection)


def _reset_queue(settings, connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM salesops.remediation_actions")
        cursor.execute("DELETE FROM salesops.review_queue")
    connection.commit()
    notification_service.run_routing(
        settings=settings,
        provider=RecordingProvider(),
        recipients=["stage10-tests@example.invalid"],
    )


# =============================================================================
# Stale runs
# =============================================================================


def test_a_stale_running_run_is_recovered(settings, connection):
    run_id = make_run(connection, "running", age_minutes=500)

    recovered = service.recover_stale_runs(settings)

    assert run_id in [row["run_id"] for row in recovered]
    row = _run(connection, run_id)
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["error_message"].startswith("STALE_RUN_TIMEOUT:")


def test_a_fresh_running_run_is_left_alone(settings, connection):
    """The whole point of a timeout. Recovering every open run would close the
    run that is currently doing the work."""
    run_id = make_run(connection, "running", age_minutes=1)

    service.recover_stale_runs(settings)

    assert _run(connection, run_id)["status"] == "running"


def test_a_completed_run_is_never_touched(settings, connection):
    for status in ("success", "partial", "failed"):
        run_id = make_run(connection, status, age_minutes=5000)
        before = _run(connection, run_id)

        service.recover_stale_runs(settings)

        after = _run(connection, run_id)
        assert after["status"] == status
        assert after["error_message"] == before["error_message"]
        assert after["finished_at"] == before["finished_at"]


def test_recovery_is_idempotent(settings, connection):
    run_id = make_run(connection, "running", age_minutes=500)

    first = service.recover_stale_runs(settings)
    second = service.recover_stale_runs(settings)
    third = service.recover_stale_runs(settings)

    assert run_id in [r["run_id"] for r in first]
    assert run_id not in [r["run_id"] for r in second]
    assert second == [] or run_id not in [r["run_id"] for r in second]
    assert third == [] or run_id not in [r["run_id"] for r in third]

    events = _events(connection, "ingestion_run", str(run_id))
    assert len(events) == 1, "a second recovery wrote a second event"


def test_a_recovered_run_keeps_its_history(settings, connection):
    """Recovery closes the record. It does not delete it, and it does not
    rewrite when it started or what it was."""
    run_id = make_run(connection, "running", age_minutes=500)
    before = _run(connection, run_id)

    service.recover_stale_runs(settings)

    after = _run(connection, run_id)
    assert after["started_at"] == before["started_at"]
    assert after["source"] == before["source"]
    assert after["batch_id"] == before["batch_id"]


def test_recovery_records_a_machine_readable_reason(settings, connection):
    run_id = make_run(connection, "running", age_minutes=500)
    service.recover_stale_runs(settings, actor="stage10-recovery")

    event = _events(connection, "ingestion_run", str(run_id))[0]
    assert event["reason_code"] == "STALE_RUN_TIMEOUT"
    assert event["from_state"] == "running"
    assert event["to_state"] == "failed"
    assert event["actor"] == "stage10-recovery"
    assert event["detail"]["work_repeated"] is False
    assert event["detail"]["source"] == TEST_SOURCE


def test_recovery_repeats_no_work(settings, connection):
    """Recovery is not re-execution. Nothing downstream of the run moves."""
    before = all_fingerprints(connection)
    make_run(connection, "running", age_minutes=500)

    service.recover_stale_runs(settings)

    assert all_fingerprints(connection) == before


def test_a_dry_run_changes_nothing(settings, connection):
    run_id = make_run(connection, "running", age_minutes=500)

    found = service.recover_stale_runs(settings, dry_run=True)

    assert run_id in [row["run_id"] for row in found]
    assert all(row["recovered"] is False for row in found)
    assert _run(connection, run_id)["status"] == "running"
    assert _events(connection, "ingestion_run", str(run_id)) == []


def test_the_timeout_is_configurable(settings, connection):
    run_id = make_run(connection, "running", age_minutes=10)

    service.recover_stale_runs(settings)
    assert _run(connection, run_id)["status"] == "running"

    with threshold(connection, "stale_run_timeout_minutes", 5):
        service.recover_stale_runs(settings)

    assert _run(connection, run_id)["status"] == "failed"


# =============================================================================
# Stale remediation execution
# =============================================================================


@pytest.fixture
def executing_action(settings, connection):
    """A remediation action stranded mid-execution, as a crashed process leaves it."""
    review = query(connection, """
        SELECT review_id FROM salesops.review_queue
        WHERE status = 'pending' ORDER BY review_id LIMIT 1
    """)
    if not review:
        pytest.skip("no pending review to build a remediation action from")
    review_id = review[0]["review_id"]

    notification_service.claim_review(settings, review_id, TEST_ACTOR)
    created = remediation_service.approve_review_for_remediation(
        settings, review_id, TEST_ACTOR, ActionType.CREATE_INVESTIGATION
    )
    remediation_id = created["remediation_id"]
    remediation_service.authorize_action(settings, remediation_id, TEST_ACTOR)

    execute(connection, """
        UPDATE salesops.remediation_actions SET status = 'executing'
        WHERE remediation_id = %(id)s
    """, {"id": remediation_id})

    yield remediation_id

    execute(connection, """
        DELETE FROM salesops.remediation_actions WHERE remediation_id = %(id)s
    """, {"id": remediation_id})


def test_a_fresh_execution_is_not_recovered(settings, connection, executing_action):
    """A slow provider call is not a crashed one."""
    recovered = service.recover_stale_remediation(settings)

    assert executing_action not in [row["remediation_id"] for row in recovered]
    assert _action(connection, executing_action)["status"] == "executing"


def test_a_stale_execution_becomes_execution_unknown(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        recovered = service.recover_stale_remediation(settings)

    assert executing_action in [row["remediation_id"] for row in recovered]
    action = _action(connection, executing_action)
    assert action["status"] == "execution_unknown"
    assert action["executed_by"] is None
    assert action["executed_at"] is None
    assert action["last_error"].startswith("EXECUTION_UNKNOWN:")


def test_recovery_never_calls_the_provider(settings, connection, executing_action):
    """The single most important property in this file.

    A crashed process may have completed the external call before dying.
    Re-executing on recovery would be the system doing the thing twice on its
    own initiative, which is precisely what every stage since 6 has been built
    to prevent.
    """
    provider = RecordingRemediationProvider()

    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    assert provider.calls == 0

    # ...and the Stage 9 batch executor cannot pick it up either. Scoped to this
    # action rather than to the provider's total, because the executor rightly
    # runs whatever else a human has authorised.
    summary = remediation_service.execute_approved(settings, provider)
    assert executing_action not in provider.requests_for
    assert executing_action not in summary.executed_ids


def test_execution_unknown_is_absent_from_the_work_set(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    pending = repository.list_executable_remediations(connection)
    assert executing_action not in [row["remediation_id"] for row in pending]


def test_the_unknown_attempt_is_recorded_as_unknown(settings, connection, executing_action):
    """An attempt was made. What it achieved is not known.

    Recording it as a failure would be guessing; recording nothing would lose
    the single most important fact for whoever reconciles it.
    """
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    attempts = repository.remediation_attempts(connection, executing_action)
    assert attempts
    assert attempts[-1]["outcome"] == "unknown"
    assert attempts[-1]["external_side_effect"] is False
    assert "EXECUTION_UNKNOWN" in attempts[-1]["error_message"]


def test_stale_remediation_recovery_is_idempotent(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        first = service.recover_stale_remediation(settings)
        second = service.recover_stale_remediation(settings)

    assert executing_action in [r["remediation_id"] for r in first]
    assert executing_action not in [r["remediation_id"] for r in second]
    assert len(_events(connection, "remediation_action", str(executing_action))) == 1


# =============================================================================
# Reconciliation
# =============================================================================


def test_reconciliation_is_required_before_any_retry(settings, connection, executing_action):
    """There is no transition from execution_unknown back to executing."""
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    with pytest.raises(Exception) as exc:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.remediation_actions SET status = 'executing'
                WHERE remediation_id = %(id)s
            """, {"id": executing_action})
    connection.rollback()
    assert "Invalid remediation transition" in str(exc.value)


def test_reconciling_as_executed_closes_it(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    service.reconcile_remediation(
        settings, executing_action, "confirmed_executed", TEST_ACTOR,
        "Provider record shows the request was received.",
    )

    action = _action(connection, executing_action)
    assert action["status"] == "executed"
    assert action["executed_by"] == TEST_ACTOR
    assert action["executed_at"] is not None
    assert action["attempt_count"] >= 1


def test_reconciling_as_not_executed_returns_it_to_the_retry_path(
    settings, connection, executing_action
):
    """`failed`, not `approved`.

    So a retry is still bounded by the ordinary attempt budget, and is still
    something a person chose rather than something recovery did.
    """
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    service.reconcile_remediation(
        settings, executing_action, "confirmed_not_executed", TEST_ACTOR,
        "Provider record shows no request was received.",
    )

    action = _action(connection, executing_action)
    assert action["status"] == "failed"
    assert action["executed_at"] is None
    assert action["last_error"].startswith("RECONCILED_NOT_EXECUTED:")


def test_reconciliation_requires_an_actor_and_evidence(settings, connection, executing_action):
    """Unattributed or unexplained, a reconciliation is a guess with a timestamp."""
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    for actor, evidence in ((" ", "some evidence"), (TEST_ACTOR, "  ")):
        with pytest.raises(service.OperationsError):
            service.reconcile_remediation(
                settings, executing_action, "confirmed_executed", actor, evidence)


def test_only_an_unknown_execution_can_be_reconciled(settings, connection, executing_action):
    with pytest.raises(service.OperationsError) as exc:
        service.reconcile_remediation(
            settings, executing_action, "confirmed_executed", TEST_ACTOR, "evidence")
    assert "executing" in str(exc.value)


def test_reconciling_twice_is_refused(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)

    service.reconcile_remediation(
        settings, executing_action, "confirmed_executed", TEST_ACTOR, "evidence")

    with pytest.raises(service.OperationsError):
        service.reconcile_remediation(
            settings, executing_action, "confirmed_not_executed", TEST_ACTOR, "changed my mind")


def test_reconciliation_is_audited(settings, connection, executing_action):
    with threshold(connection, "stale_remediation_timeout_minutes", 0):
        service.recover_stale_remediation(settings)
    service.reconcile_remediation(
        settings, executing_action, "confirmed_executed", TEST_ACTOR, "checked the log")

    events = _events(connection, "remediation_action", str(executing_action))
    reasons = [e["reason_code"] for e in events]
    assert "EXECUTION_UNKNOWN" in reasons
    assert "RECONCILED_EXECUTED" in reasons
    assert any(e["detail"].get("evidence") == "checked the log" for e in events)


# =============================================================================
# Notifications
# =============================================================================


def test_a_delivered_notification_is_never_stale(settings, connection):
    """`sent` is terminal for staleness. There is nothing to recover."""
    with threshold(connection, "stale_notification_timeout_minutes", 0):
        stale = repository.stale_notifications(connection)

    statuses = {row["status"] for row in stale}
    assert "sent" not in statuses


def test_a_failed_notification_is_detected_and_retryable(settings, connection):
    notification_id = _break_a_notification(connection)
    try:
        with threshold(connection, "stale_notification_timeout_minutes", 0):
            stale = repository.stale_notifications(connection)

        row = next(r for r in stale if r["notification_id"] == notification_id)
        assert row["retry_eligible"] is True
        assert row["terminal"] is False
    finally:
        _restore_notification(connection, notification_id)


def test_notification_retry_is_bounded(settings, connection):
    notification_id = _break_a_notification(connection, attempts=3)
    try:
        with threshold(connection, "stale_notification_timeout_minutes", 0):
            stale = repository.stale_notifications(connection)

        row = next(r for r in stale if r["notification_id"] == notification_id)
        assert row["retry_eligible"] is False
        assert row["terminal"] is True
    finally:
        _restore_notification(connection, notification_id)


def test_an_abandoned_notification_is_visible_in_the_retry_queue(settings, connection):
    notification_id = _break_a_notification(connection, attempts=3, status="abandoned")
    try:
        rows = repository.retry_queue(connection, entity_type="notification")
        row = next(r for r in rows if r["entity_id"] == str(notification_id))
        assert row["disposition"] == "ABANDONED"
        assert row["terminal"] is True
        assert row["retry_eligible"] is False
    finally:
        _restore_notification(connection, notification_id)


def test_an_attempt_number_collision_cannot_strand_a_notification(settings, connection):
    """The bug this suite found.

    `attempt_count` and `max(attempt_number)` cannot drift through the ordinary
    path, but they can through a manual repair or a restore. When they did, the
    next attempt violated the unique constraint, the violation was counted as a
    *delivery* failure, and the notification sat at `failed` forever looking
    like a broken webhook. The attempt number is now taken from the history.
    """
    notification_id = _break_a_notification(connection, attempts=1)
    try:
        # Force the drift: three attempts on record, counter says one.
        highest = query(connection, """
            SELECT COALESCE(max(attempt_number), 0) AS n
            FROM salesops.notification_attempts WHERE notification_id = %(id)s
        """, {"id": notification_id})[0]["n"]

        recorded = repository.record_attempt(connection, {
            "notification_id": notification_id,
            "attempt_number": 1,
            "outcome": "retryable_failure",
            "provider": "test",
            "provider_message_id": None,
            "status_code": 503,
            "error_message": "drift fixture",
            "latency_ms": 1,
        })
        connection.commit()

        assert recorded == highest + 1, "the attempt number ignored the history"
    finally:
        _restore_notification(connection, notification_id)


def test_retrying_stale_notifications_never_resends_a_delivered_one(settings, connection):
    """Stage 8 owns delivery, and `resend` is never set here."""
    before = query(connection, """
        SELECT notification_id, attempt_count FROM salesops.notifications
        WHERE status = 'sent' ORDER BY notification_id
    """)

    with threshold(connection, "stale_notification_timeout_minutes", 0):
        result = service.retry_stale_notifications(
            settings, RecordingProvider(), ["stage10-tests@example.invalid"])

    after = query(connection, """
        SELECT notification_id, attempt_count FROM salesops.notifications
        WHERE notification_id = ANY(%(ids)s) ORDER BY notification_id
    """, {"ids": [row["notification_id"] for row in before]})

    assert after == before, "a delivered notification was attempted again"
    assert isinstance(result["retried"], int)


# =============================================================================
# Helpers
# =============================================================================


def _run(connection, run_id: int) -> dict:
    return query(connection, """
        SELECT * FROM salesops.ingestion_runs WHERE run_id = %(id)s
    """, {"id": run_id})[0]


def _action(connection, remediation_id: int) -> dict:
    return query(connection, """
        SELECT * FROM salesops.remediation_actions WHERE remediation_id = %(id)s
    """, {"id": remediation_id})[0]


def _events(connection, entity_type: str, entity_id: str) -> list[dict]:
    return query(connection, """
        SELECT * FROM salesops.operational_events
        WHERE entity_type = %(t)s AND entity_id = %(i)s
        ORDER BY event_id
    """, {"t": entity_type, "i": entity_id})


def _break_a_notification(connection, attempts: int = 1, status: str = "failed") -> int:
    rows = query(connection, "SELECT notification_id, status, attempt_count, sent_at "
                             "FROM salesops.notifications ORDER BY notification_id LIMIT 1")
    if not rows:
        pytest.skip("no notification to break")
    notification_id = rows[0]["notification_id"]
    execute(connection, """
        UPDATE salesops.notifications
        SET status = %(s)s, sent_at = NULL, attempt_count = %(a)s,
            last_error = 'stage10 test fixture'
        WHERE notification_id = %(id)s
    """, {"id": notification_id, "s": status, "a": attempts})
    return notification_id


def _restore_notification(connection, notification_id: int) -> None:
    """Put it back exactly as Stage 8 left it: delivered, with a delivery time."""
    execute(connection, """
        UPDATE salesops.notifications
        SET status = 'sent', last_error = NULL,
            attempt_count = GREATEST(attempt_count, 1),
            sent_at = COALESCE(
                (SELECT max(attempted_at) FROM salesops.notification_attempts a
                  WHERE a.notification_id = salesops.notifications.notification_id
                    AND a.outcome = 'success'), now())
        WHERE notification_id = %(id)s
    """, {"id": notification_id})
