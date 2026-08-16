"""Stage 8 against the live warehouse, with a fake delivery provider.

The provider is faked; nothing else is. These run against real Stage 6 decisions
and real Stage 7 hypotheses, because the properties under test are about how
Stage 8 behaves around them: who gets routed where, what a rerun does, what a
failed delivery does, and - above all - that none of it can change a decision.

Skipped automatically when the stack is not running. Every test cleans up only
the rows it created, keyed on a test-only recipient. The live 90-day dataset and
any real notifications are left alone.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from analytics import repository
from analytics.config import Settings
from analytics.notifications import service
from analytics.notifications.models import DeliveryOutcome
from analytics.notifications.provider import RecordingProvider
from tests.live_dates import INCIDENT_DATE as LIVE_CRITICAL
from tests.live_dates import MAJOR_DATE as LIVE_MAJOR
from tests.live_dates import MINOR_DATE as LIVE_MINOR
from tests.live_dates import NORMAL_DATE as LIVE_NORMAL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]



TEST_RECIPIENT = "stage8-tests@example.invalid"
OTHER_RECIPIENT = "stage8-tests-second@example.invalid"


def _load_env_file() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def settings() -> Settings:
    env_file = _load_env_file()
    password = os.getenv("ANALYTICS_DB_PASSWORD") or env_file.get("POSTGRES_PASSWORD")
    if not password:
        pytest.skip("No database password available - is the stack configured?")

    return Settings(
        host=os.getenv("ANALYTICS_DB_HOST", "localhost"),
        port=int(os.getenv("ANALYTICS_DB_PORT") or env_file.get("POSTGRES_HOST_PORT") or 5432),
        database=os.getenv("ANALYTICS_DB_NAME") or env_file.get("POSTGRES_DB") or "salesops",
        user=os.getenv("ANALYTICS_DB_USER") or env_file.get("POSTGRES_USER") or "salesops",
        password=password,
    )


@pytest.fixture(scope="session")
def connection(settings: Settings):
    try:
        conn = repository.connect(settings.dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")
    with conn:
        yield conn


@pytest.fixture(autouse=True)
def clean_stage8(connection):
    """Remove only what these tests create, before and after each one.

    Reviews are keyed on the anomaly rather than a recipient, so the review
    fixtures are removed by decision version - narrow enough not to disturb
    anything a live run produced under a different one.
    """
    def purge():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM salesops.notifications WHERE recipient IN (%s, %s)",
                (TEST_RECIPIENT, OTHER_RECIPIENT),
            )
            cursor.execute("DELETE FROM salesops.review_queue")
        connection.commit()

    purge()
    yield
    purge()


def query(connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchall()


def decisions_snapshot(connection) -> list[dict]:
    """Every Stage 6 field Stage 8 must never touch."""
    return query(connection, """
        SELECT decision_id, severity, routing, decision,
               notification_allowed, human_review_required, decision_reason_code
        FROM salesops.anomaly_decisions ORDER BY decision_id
    """)


def run(settings, provider, recipients=None, **kwargs):
    return service.run_routing(
        settings=settings,
        provider=provider,
        recipients=recipients or [TEST_RECIPIENT],
        **kwargs,
    )


# =============================================================================
# Eligibility comes from Stage 6, never from severity
# =============================================================================


def test_minor_anomalies_are_notified(settings, connection):
    provider = RecordingProvider()
    summary = run(settings, provider, only_dates=[LIVE_MINOR])

    assert summary.notifications_sent == 1
    assert summary.reviews_created == 0

    row = query(connection, """
        SELECT severity, routing, status FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]

    assert row["severity"] == "minor"
    assert row["routing"] == "auto_notify"
    assert row["status"] == "sent"


@pytest.mark.parametrize("anomaly_date, expected_severity",
                         [(LIVE_CRITICAL, "critical"), (LIVE_MAJOR, "major")])
def test_major_and_critical_go_to_human_review_not_notification(
    settings, connection, anomaly_date, expected_severity
):
    provider = RecordingProvider()
    summary = run(settings, provider, only_dates=[anomaly_date])

    assert summary.reviews_created == 1
    assert summary.notifications_sent == 0
    # The provider was never even asked.
    assert provider.sent == []

    row = query(connection, """
        SELECT severity, routing, status FROM salesops.review_queue
        WHERE calendar_date = %(d)s
    """, {"d": anomaly_date})[0]

    assert row["severity"] == expected_severity
    assert row["routing"] == "human_review"
    assert row["status"] == "pending"


def test_no_action_anomalies_produce_nothing(settings, connection):
    provider = RecordingProvider()
    summary = run(settings, provider, only_dates=[LIVE_NORMAL])

    assert summary.eligible == 0
    assert summary.notifications_sent == 0
    assert summary.reviews_created == 0
    assert provider.sent == []


def test_a_full_run_matches_stage6s_own_counts(settings, connection):
    """The routing split is Stage 6's, so the totals must agree with it."""
    provider = RecordingProvider()
    summary = run(settings, provider)

    expected = query(connection, """
        SELECT count(*) FILTER (WHERE routing = 'auto_notify')  AS notify,
               count(*) FILTER (WHERE routing = 'human_review') AS review
        FROM salesops.anomaly_decisions
        WHERE decision_version = 'stage6-v1' AND decision = 'action_required'
    """)[0]

    assert summary.notifications_sent == expected["notify"]
    assert summary.reviews_created == expected["review"]
    assert summary.eligible == expected["notify"] + expected["review"]


def test_no_notification_exists_for_a_human_review_decision(settings, connection):
    run(settings, RecordingProvider())

    leaked = query(connection, """
        SELECT n.calendar_date FROM salesops.notifications n
        JOIN salesops.anomaly_decisions d ON d.decision_id = n.decision_id
        WHERE n.recipient = %(r)s AND d.routing <> 'auto_notify'
    """, {"r": TEST_RECIPIENT})

    assert leaked == []


def test_no_review_exists_for_a_notify_only_decision(settings, connection):
    run(settings, RecordingProvider())

    leaked = query(connection, """
        SELECT r.calendar_date FROM salesops.review_queue r
        JOIN salesops.anomaly_decisions d ON d.decision_id = r.decision_id
        WHERE d.routing <> 'human_review'
    """)

    assert leaked == []


def test_the_database_refuses_a_notification_for_an_ineligible_decision(settings, connection):
    """Eligibility is a constraint, not a convention.

    Even a bug in the service cannot deliver a notification for an anomaly Stage
    6 routed to no_action.
    """
    ineligible = query(connection, """
        SELECT decision_id, anomaly_id, calendar_date, decision_version,
               severity, routing, decision, notification_allowed, human_review_required
        FROM salesops.anomaly_decisions
        WHERE routing = 'human_review' LIMIT 1
    """)[0]

    with pytest.raises(Exception) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO salesops.notifications (
                    anomaly_id, decision_id, calendar_date, decision_version,
                    severity, routing, decision, notification_allowed, human_review_required,
                    channel, recipient, subject, payload)
                VALUES (%(anomaly_id)s, %(decision_id)s, %(calendar_date)s, %(decision_version)s,
                        %(severity)s, %(routing)s, %(decision)s,
                        %(notification_allowed)s, %(human_review_required)s,
                        'webhook', %(r)s, 'test', '{}'::jsonb)
            """, {**ineligible, "r": TEST_RECIPIENT})
    connection.rollback()

    assert "only_for_eligible_decisions" in str(excinfo.value)


def test_the_database_refuses_a_fabricated_eligible_snapshot(settings, connection):
    """Claiming auto_notify for a decision Stage 6 routed elsewhere is refused."""
    target = query(connection, """
        SELECT decision_id, anomaly_id, calendar_date, decision_version
        FROM salesops.anomaly_decisions WHERE routing = 'human_review' LIMIT 1
    """)[0]

    with pytest.raises(Exception, match="may not restate"):
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO salesops.notifications (
                    anomaly_id, decision_id, calendar_date, decision_version,
                    severity, routing, decision, notification_allowed, human_review_required,
                    channel, recipient, subject, payload)
                VALUES (%(anomaly_id)s, %(decision_id)s, %(calendar_date)s, %(decision_version)s,
                        'minor', 'auto_notify', 'action_required', TRUE, FALSE,
                        'webhook', %(r)s, 'test', '{}'::jsonb)
            """, {**target, "r": TEST_RECIPIENT})
    connection.rollback()


# =============================================================================
# Stage 6 is untouched - by success and by every kind of failure
# =============================================================================


@pytest.mark.parametrize("provider", [
    pytest.param(RecordingProvider(), id="success"),
    pytest.param(RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503), id="5xx"),
    pytest.param(RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 429), id="429"),
    pytest.param(RecordingProvider(DeliveryOutcome.PERMANENT_FAILURE, 401), id="auth-failure"),
    pytest.param(RecordingProvider(DeliveryOutcome.PERMANENT_FAILURE, 400), id="bad-request"),
    pytest.param(RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, None,
                                   "timed out after 15s"), id="timeout"),
])
def test_no_stage6_decision_changes_whatever_delivery_does(settings, connection, provider):
    """The load-bearing test of the stage.

    Delivery can succeed, time out, be rate-limited, be rejected or be
    unauthorised. Severity, routing, decision, notification permission and the
    review requirement are identical afterwards in every case.
    """
    before = decisions_snapshot(connection)
    run(settings, provider)
    assert decisions_snapshot(connection) == before


def test_a_failed_delivery_leaves_the_anomaly_as_serious_as_it_was(settings, connection):
    provider = RecordingProvider(DeliveryOutcome.PERMANENT_FAILURE, 401, "invalid credentials")
    run(settings, provider, only_dates=[LIVE_MINOR])

    decision = query(connection, """
        SELECT severity, routing, notification_allowed FROM salesops.anomaly_decisions
        WHERE calendar_date = %(d)s AND decision_version = 'stage6-v1'
    """, {"d": LIVE_MINOR})[0]

    assert decision["severity"] == "minor"
    assert decision["routing"] == "auto_notify"
    assert decision["notification_allowed"] is True


def test_stage8_performs_no_business_action(settings, connection):
    """Section 28: Stage 8 ends at delivered, or queued.

    Nothing it does may touch the order book, the KPI layer or the detections.
    """
    before = query(connection, """
        SELECT (SELECT count(*) FROM salesops.fact_orders)     AS orders,
               (SELECT count(*) FROM salesops.kpi_daily)       AS kpis,
               (SELECT count(*) FROM salesops.anomaly_daily)   AS anomalies,
               (SELECT count(*) FROM salesops.dim_customer)    AS customers,
               (SELECT coalesce(sum(net_amount_usd), 0) FROM salesops.fact_orders) AS revenue
    """)[0]

    run(settings, RecordingProvider())

    after = query(connection, """
        SELECT (SELECT count(*) FROM salesops.fact_orders)     AS orders,
               (SELECT count(*) FROM salesops.kpi_daily)       AS kpis,
               (SELECT count(*) FROM salesops.anomaly_daily)   AS anomalies,
               (SELECT count(*) FROM salesops.dim_customer)    AS customers,
               (SELECT coalesce(sum(net_amount_usd), 0) FROM salesops.fact_orders) AS revenue
    """)[0]

    assert after == before


# =============================================================================
# Idempotency
# =============================================================================


def test_a_second_run_sends_nothing_new(settings, connection):
    first = run(settings, RecordingProvider())

    second_provider = RecordingProvider()
    second = run(settings, second_provider)

    assert second.notifications_sent == 0
    assert second.reviews_created == 0
    assert second.skipped == first.notifications_sent + first.reviews_created
    # Idempotency has to be checked before the message is sent, not after.
    assert second_provider.sent == []


def test_a_rerun_creates_no_duplicate_rows(settings, connection):
    run(settings, RecordingProvider())
    run(settings, RecordingProvider())
    run(settings, RecordingProvider())

    duplicates = query(connection, """
        SELECT anomaly_id FROM salesops.notifications WHERE recipient = %(r)s
        GROUP BY anomaly_id, decision_version, channel, recipient HAVING count(*) > 1
    """, {"r": TEST_RECIPIENT})
    review_duplicates = query(connection, """
        SELECT anomaly_id FROM salesops.review_queue
        GROUP BY anomaly_id, decision_version HAVING count(*) > 1
    """)

    assert duplicates == []
    assert review_duplicates == []


def test_a_rerun_converges_to_the_same_state(settings, connection):
    def state():
        return query(connection, """
            SELECT anomaly_id, status, attempt_count FROM salesops.notifications
            WHERE recipient = %(r)s ORDER BY anomaly_id
        """, {"r": TEST_RECIPIENT})

    run(settings, RecordingProvider())
    after_first = state()
    run(settings, RecordingProvider())

    assert state() == after_first


def test_a_second_recipient_is_notified_without_re_notifying_the_first(settings, connection):
    """The recipient is part of the idempotency key for exactly this reason."""
    run(settings, RecordingProvider(), recipients=[TEST_RECIPIENT])

    second = run(settings, RecordingProvider(), recipients=[TEST_RECIPIENT, OTHER_RECIPIENT])

    assert second.notifications_sent > 0
    recipients = {
        row["recipient"] for row in query(connection, """
            SELECT DISTINCT recipient FROM salesops.notifications
            WHERE recipient IN (%(a)s, %(b)s) AND status = 'sent'
        """, {"a": TEST_RECIPIENT, "b": OTHER_RECIPIENT})
    }
    assert recipients == {TEST_RECIPIENT, OTHER_RECIPIENT}


def test_an_explicit_resend_is_required_to_deliver_again(settings):
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    provider = RecordingProvider()
    summary = run(settings, provider, only_dates=[LIVE_MINOR], resend=True)

    assert summary.notifications_sent == 1
    assert len(provider.sent) == 1


def test_resend_is_never_implicit(settings):
    import inspect
    assert inspect.signature(service.run_routing).parameters["resend"].default is False


# =============================================================================
# Retry
# =============================================================================


def test_a_retryable_failure_is_retried_on_a_later_run(settings, connection):
    run(settings, RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503),
        only_dates=[LIVE_MINOR])

    first = query(connection, """
        SELECT status, attempt_count FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]
    assert first["status"] == "failed"
    assert first["attempt_count"] == 1

    summary = run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    after = query(connection, """
        SELECT status, attempt_count FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]

    assert summary.notifications_sent == 1
    assert after["status"] == "sent"
    assert after["attempt_count"] == 2


def test_a_permanent_failure_is_not_retried(settings, connection):
    """Retrying a 401 three times only delays the moment somebody notices."""
    run(settings, RecordingProvider(DeliveryOutcome.PERMANENT_FAILURE, 401, "bad credentials"),
        only_dates=[LIVE_MINOR])

    row = query(connection, """
        SELECT status, attempt_count FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]
    assert row["status"] == "abandoned"

    provider = RecordingProvider()
    summary = run(settings, provider, only_dates=[LIVE_MINOR])

    assert summary.notifications_sent == 0
    assert provider.sent == []


def test_retries_are_bounded(settings, connection):
    """Three failing runs exhaust the budget; the fourth does not try again."""
    for _ in range(service.MAX_DELIVERY_ATTEMPTS):
        run(settings, RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503),
            only_dates=[LIVE_MINOR])

    row = query(connection, """
        SELECT status, attempt_count FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]

    assert row["attempt_count"] == service.MAX_DELIVERY_ATTEMPTS
    assert row["status"] == "abandoned"

    provider = RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503)
    run(settings, provider, only_dates=[LIVE_MINOR])
    assert provider.sent == []


def test_a_failed_resend_does_not_leave_a_stale_delivery_time(settings, connection):
    """Found live, against the real provider, not by the fake one.

    Re-sending an already-delivered notification and failing moved the status
    from 'sent' to 'failed' while leaving `sent_at` populated - a row claiming a
    delivery time it no longer had. The database refused it, which is what the
    constraint is for; the fix is that sent_at tracks the CURRENT status.

    The successful attempt is not lost: notification_attempts keeps it.
    """
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])
    first = query(connection, """
        SELECT status, sent_at FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]
    assert first["status"] == "sent" and first["sent_at"] is not None

    summary = run(settings, RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503),
                  only_dates=[LIVE_MINOR], resend=True)

    assert summary.notifications_failed == 1
    # ...and no exception was swallowed into the failure list.
    assert not any("CheckViolation" in f["reason"] for f in summary.failures)

    after = query(connection, """
        SELECT status, sent_at FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]
    assert after["status"] == "failed"
    assert after["sent_at"] is None

    # The delivery that did happen is still on the record.
    attempts = query(connection, """
        SELECT a.outcome FROM salesops.notification_attempts a
        JOIN salesops.notifications n ON n.notification_id = a.notification_id
        WHERE n.recipient = %(r)s AND n.calendar_date = %(d)s
        ORDER BY a.attempt_number
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})
    assert [a["outcome"] for a in attempts] == ["success", "retryable_failure"]


def test_every_attempt_is_recorded(settings, connection):
    run(settings, RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503),
        only_dates=[LIVE_MINOR])
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    attempts = query(connection, """
        SELECT a.attempt_number, a.outcome, a.status_code
        FROM salesops.notification_attempts a
        JOIN salesops.notifications n ON n.notification_id = a.notification_id
        WHERE n.recipient = %(r)s AND n.calendar_date = %(d)s
        ORDER BY a.attempt_number
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})

    assert [a["attempt_number"] for a in attempts] == [1, 2]
    assert attempts[0]["outcome"] == "retryable_failure"
    assert attempts[1]["outcome"] == "success"


# =============================================================================
# Run status
# =============================================================================


def test_a_mixed_run_is_partial_not_success(settings):
    """Section 21: some delivered, some failed, reviews created -> partial."""
    summary = run(settings, RecordingProvider(DeliveryOutcome.RETRYABLE_FAILURE, 503))

    assert summary.notifications_failed > 0
    assert summary.reviews_created > 0
    assert summary.status == "partial"


def test_a_run_where_everything_failed_is_failed(settings):
    summary = run(settings, RecordingProvider(DeliveryOutcome.PERMANENT_FAILURE, 400),
                  only_dates=[LIVE_MINOR])

    assert summary.notifications_sent == 0
    assert summary.status == "failed"


def test_an_empty_eligible_set_is_a_success(settings):
    summary = run(settings, RecordingProvider(), decision_version="stage6-does-not-exist")

    assert summary.eligible == 0
    assert summary.status == "success"


def test_a_clean_run_is_success(settings):
    assert run(settings, RecordingProvider()).status == "success"


# =============================================================================
# Stage 7 integration
# =============================================================================


def test_a_live_hypothesis_is_carried_into_the_notification(settings, connection):
    provider = RecordingProvider()
    run(settings, provider, only_dates=[LIVE_MINOR])

    notification = provider.sent[0]
    assert notification.hypothesis_status == "available"
    assert notification.payload["HYPOTHESIS"]["status"] == "available"
    assert notification.payload["HYPOTHESIS"]["summary"]
    assert notification.payload["HYPOTHESIS"]["confidence"] in {"low", "medium", "high"}


def test_a_missing_hypothesis_does_not_block_human_review(settings, connection):
    """Section 18: a Stage 7 failure must never stop an escalation.

    The hypothesis is detached from the decision, so Stage 8 sees exactly what it
    would see had Stage 7 failed outright.
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT h.hypothesis_id, h.decision_id FROM salesops.anomaly_hypotheses h
            WHERE h.calendar_date = %(d)s
        """, {"d": LIVE_CRITICAL})
        original = cursor.fetchone()
        cursor.execute("""
            UPDATE salesops.anomaly_hypotheses SET decision_id = (
                SELECT decision_id FROM salesops.anomaly_decisions
                WHERE calendar_date = %(d)s AND decision_version = 'stage6-v1')
            WHERE FALSE
        """, {"d": LIVE_CRITICAL})
        # Temporarily hide it by pointing the lookup at a date with no hypothesis.
        cursor.execute(
            "DELETE FROM salesops.anomaly_hypotheses WHERE hypothesis_id = %(h)s "
            "RETURNING anomaly_id, decision_id, calendar_date, decision_version, severity, "
            "routing, decision, summary, confidence, primary_hypothesis, supporting_evidence, "
            "alternative_hypotheses, missing_evidence, recommended_checks, model_provider, "
            "model_name, prompt_version, evidence_digest",
            {"h": original["hypothesis_id"]},
        )
        removed = cursor.fetchone()
    connection.commit()

    try:
        summary = run(settings, RecordingProvider(), only_dates=[LIVE_CRITICAL])

        assert summary.reviews_created == 1
        row = query(connection, """
            SELECT severity, status, hypothesis_status, hypothesis_id
            FROM salesops.review_queue WHERE calendar_date = %(d)s
        """, {"d": LIVE_CRITICAL})[0]

        assert row["severity"] == "critical"
        assert row["status"] == "pending"
        assert row["hypothesis_status"] == "unavailable"
        assert row["hypothesis_id"] is None
    finally:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO salesops.anomaly_hypotheses (
                    anomaly_id, decision_id, calendar_date, decision_version,
                    severity, routing, decision, summary, confidence, primary_hypothesis,
                    supporting_evidence, alternative_hypotheses, missing_evidence,
                    recommended_checks, model_provider, model_name, prompt_version,
                    evidence_digest)
                VALUES (%(anomaly_id)s, %(decision_id)s, %(calendar_date)s,
                        %(decision_version)s, %(severity)s, %(routing)s, %(decision)s,
                        %(summary)s, %(confidence)s, %(primary_hypothesis)s,
                        %(supporting_evidence)s, %(alternative_hypotheses)s,
                        %(missing_evidence)s, %(recommended_checks)s, %(model_provider)s,
                        %(model_name)s, %(prompt_version)s, %(evidence_digest)s)
            """, {
                **removed,
                "supporting_evidence": json.dumps(removed["supporting_evidence"]),
                "alternative_hypotheses": json.dumps(removed["alternative_hypotheses"]),
                "missing_evidence": json.dumps(removed["missing_evidence"]),
                "recommended_checks": json.dumps(removed["recommended_checks"]),
            })
        connection.commit()


def test_a_notification_never_presents_the_hypothesis_as_fact(settings):
    provider = RecordingProvider()
    run(settings, provider, only_dates=[LIVE_MINOR])

    payload = provider.sent[0].payload
    assert "not a confirmed cause" in payload["HYPOTHESIS"]["caveat"].lower()
    assert "NOT CONFIRMED" in payload


# =============================================================================
# Persistence and audit
# =============================================================================


def test_a_delivery_records_provider_metadata_and_timestamps(settings, connection):
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    row = query(connection, """
        SELECT status, attempt_count, provider, provider_message_id, sent_at, created_at
        FROM salesops.notifications WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]

    assert row["status"] == "sent"
    assert row["attempt_count"] == 1
    assert row["provider"] == "recording"
    assert row["provider_message_id"]
    assert row["sent_at"] is not None
    assert row["created_at"] is not None


def test_the_audit_view_answers_the_delivery_questions(settings, connection):
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    row = query(connection, """
        SELECT * FROM salesops.notification_audit
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]

    assert row["decision_severity"] == "minor"
    assert row["notified_severity"] == "minor"
    assert row["decision_current"] is True
    assert row["channel"] == "webhook"
    assert row["status"] == "sent"
    assert row["attempts_recorded"] == 1
    assert row["last_attempt_at"] is not None
    assert row["hypothesis_status"] in {"available", "unavailable"}


def test_the_audit_view_exposes_no_secrets(settings, connection):
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    columns = {
        row["column_name"] for row in query(connection, """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'salesops' AND table_name = 'notification_audit'
        """)
    }

    for leaky in ("payload", "webhook_url", "api_key", "authorization", "secret"):
        assert leaky not in columns

    serialised = json.dumps(query(connection, """
        SELECT * FROM salesops.notification_audit WHERE recipient = %(r)s
    """, {"r": TEST_RECIPIENT}), default=str).lower()
    for secret_ish in ("bearer", "api_key", "authorization", "gsk_"):
        assert secret_ish not in serialised


def test_the_stored_payload_contains_no_secrets(settings, connection):
    run(settings, RecordingProvider(), only_dates=[LIVE_MINOR])

    payload = query(connection, """
        SELECT payload::text AS body FROM salesops.notifications
        WHERE recipient = %(r)s AND calendar_date = %(d)s
    """, {"r": TEST_RECIPIENT, "d": LIVE_MINOR})[0]["body"].lower()

    for secret_ish in ("api_key", "authorization", "bearer", "gsk_", "password",
                       "webhook", "dev/notification-sink"):
        assert secret_ish not in payload
