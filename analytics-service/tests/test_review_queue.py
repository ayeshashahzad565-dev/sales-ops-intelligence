"""The human-review queue and its state machine.

Two things are being protected.

The **state machine** must admit only the transitions it declares. A queue where
any status can be written over any other is not a workflow, it is a text column
with opinions - and the audit trail it produces cannot be trusted, because
nothing stops a resolution being rewritten after the fact.

The **Stage 6 boundary** must survive contact with a reviewer. A person working
the queue can record what they concluded; they cannot re-grade the anomaly. That
distinction is the entire reason the deterministic layer exists, and a review UI
is exactly where it would quietly erode.
"""

from __future__ import annotations

import os
import pathlib
from datetime import date

import psycopg
import pytest

from analytics import repository
from analytics.config import Settings
from analytics.notifications import service
from analytics.notifications.provider import RecordingProvider
from tests.live_dates import INCIDENT_DATE as LIVE_CRITICAL
from tests.live_dates import MAJOR_DATE as LIVE_MAJOR

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


TEST_RECIPIENT = "stage8-review-tests@example.invalid"


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
def queue(settings, connection):
    """A freshly populated review queue for each test."""
    def purge():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM salesops.review_queue")
            cursor.execute(
                "DELETE FROM salesops.notifications WHERE recipient = %s", (TEST_RECIPIENT,)
            )
        connection.commit()

    purge()
    service.run_routing(
        settings=settings,
        provider=RecordingProvider(),
        recipients=[TEST_RECIPIENT],
    )
    yield
    purge()


def query(connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchall()


def review_id_for(connection, anomaly_date: date) -> int:
    rows = query(connection, """
        SELECT review_id FROM salesops.review_queue WHERE calendar_date = %(d)s
    """, {"d": anomaly_date})
    assert rows, f"no review item for {anomaly_date}"
    return rows[0]["review_id"]


def status_of(connection, review_id: int) -> dict:
    return query(connection, """
        SELECT status, resolution, assigned_to, claimed_at, reviewed_at
        FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]


def decisions_snapshot(connection) -> list[dict]:
    return query(connection, """
        SELECT decision_id, severity, routing, decision,
               notification_allowed, human_review_required
        FROM salesops.anomaly_decisions ORDER BY decision_id
    """)


# =============================================================================
# The queue itself
# =============================================================================


def test_the_critical_event_is_queued_with_its_stage6_verdict(settings, connection):
    review = query(connection, """
        SELECT * FROM salesops.review_queue_audit WHERE calendar_date = %(d)s
    """, {"d": LIVE_CRITICAL})[0]

    assert review["queued_severity"] == "critical"
    assert review["decision_routing"] == "human_review"
    assert review["decision_decision"] == "action_required"
    assert review["status"] == "pending"
    assert review["decision_current"] is True


def test_a_queue_item_carries_everything_needed_to_understand_the_escalation(
    settings, connection
):
    """Section 13: what happened, why it escalated, what Stage 7 thought."""
    review = query(connection, """
        SELECT * FROM salesops.review_queue_audit WHERE calendar_date = %(d)s
    """, {"d": LIVE_CRITICAL})[0]

    assert review["decision_reason_code"]
    assert review["anomaly_score"] is not None
    assert review["expected_net_revenue_usd"] is not None
    assert review["actual_net_revenue_usd"] is not None
    assert review["revenue_delta_usd"] is not None
    assert review["business_impact_tier"]

    if review["hypothesis_status"] == "available":
        assert review["hypothesis_summary"]
        assert review["primary_hypothesis"]
        assert review["hypothesis_confidence"] in {"low", "medium", "high"}
        # What the model could NOT show is part of the escalation, not a footnote.
        assert review["missing_evidence"] is not None


def test_a_new_item_starts_pending_and_unclaimed(settings, connection):
    row = status_of(connection, review_id_for(connection, LIVE_MAJOR))

    assert row["status"] == "pending"
    assert row["assigned_to"] is None
    assert row["claimed_at"] is None
    assert row["reviewed_at"] is None
    assert row["resolution"] is None


def test_creation_is_recorded_in_the_history(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    events = repository.review_events(connection, review_id)

    assert len(events) == 1
    assert events[0]["from_status"] is None
    assert events[0]["to_status"] == "pending"


# =============================================================================
# Valid transitions
# =============================================================================


def test_pending_to_in_review(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")

    row = status_of(connection, review_id)
    assert row["status"] == "in_review"
    assert row["assigned_to"] == "alex@example.invalid"
    assert row["claimed_at"] is not None
    assert row["reviewed_at"] is None


def test_in_review_to_resolved(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(
        settings, review_id, "confirmed", "alex@example.invalid",
        notes="Refund surge confirmed against the order detail.",
    )

    row = status_of(connection, review_id)
    assert row["status"] == "resolved"
    assert row["resolution"] == "confirmed"
    assert row["reviewed_at"] is not None


def test_in_review_to_dismissed(settings, connection):
    review_id = review_id_for(connection, LIVE_MAJOR)

    service.claim_review(settings, review_id, "sam@example.invalid")
    service.dismiss_review(settings, review_id, "expected_business_variation")

    row = status_of(connection, review_id)
    assert row["status"] == "dismissed"
    assert row["resolution"] == "expected_business_variation"
    assert row["reviewed_at"] is not None


def test_pending_to_dismissed_without_claiming(settings, connection):
    """Triaged away directly - a real workflow, not every item needs claiming."""
    review_id = review_id_for(connection, LIVE_MAJOR)

    service.dismiss_review(settings, review_id, "false_positive")

    assert status_of(connection, review_id)["status"] == "dismissed"


def test_in_review_to_pending_releases_the_claim(settings, connection):
    """Without this, an item claimed by someone unavailable is stuck forever."""
    review_id = review_id_for(connection, LIVE_MAJOR)

    service.claim_review(settings, review_id, "sam@example.invalid")
    service.release_review(settings, review_id)

    row = status_of(connection, review_id)
    assert row["status"] == "pending"
    assert row["assigned_to"] is None
    assert row["claimed_at"] is None


def test_every_transition_is_recorded_with_a_timestamp(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "requires_follow_up", "alex@example.invalid")

    events = repository.review_events(connection, review_id)
    path = [(e["from_status"], e["to_status"]) for e in events]

    assert path == [(None, "pending"), ("pending", "in_review"), ("in_review", "resolved")]
    assert all(event["occurred_at"] is not None for event in events)
    assert events[-1]["resolution"] == "requires_follow_up"
    assert events[1]["actor"] == "alex@example.invalid"


# =============================================================================
# Invalid transitions
# =============================================================================


@pytest.mark.parametrize("resolution", ["confirmed", "requires_follow_up"])
def test_pending_cannot_jump_straight_to_resolved(settings, connection, resolution):
    """An item nobody claimed cannot have been reviewed by anybody."""
    review_id = review_id_for(connection, LIVE_CRITICAL)

    with pytest.raises(service.ReviewTransitionError, match="Invalid review transition"):
        service.resolve_review(settings, review_id, resolution)

    assert status_of(connection, review_id)["status"] == "pending"


def test_a_resolved_item_cannot_be_reopened(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed")

    with pytest.raises(service.ReviewTransitionError):
        service.claim_review(settings, review_id, "someone-else@example.invalid")

    assert status_of(connection, review_id)["status"] == "resolved"


def test_a_dismissed_item_cannot_be_resolved(settings, connection):
    review_id = review_id_for(connection, LIVE_MAJOR)
    service.dismiss_review(settings, review_id, "false_positive")

    with pytest.raises(service.ReviewTransitionError):
        service.resolve_review(settings, review_id, "confirmed")


def test_a_resolution_cannot_be_rewritten_after_the_fact(settings, connection):
    """The one thing an audit trail exists to prevent."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed", notes="Original finding.")

    with pytest.raises(Exception, match="final"):
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET resolution = 'false_positive'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    connection.rollback()

    assert status_of(connection, review_id)["resolution"] == "confirmed"


def test_a_direct_update_to_an_invalid_status_is_refused(settings, connection):
    """The state machine is not a convention the API happens to follow."""
    review_id = review_id_for(connection, LIVE_CRITICAL)

    with pytest.raises(Exception, match="Invalid review transition"):
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET status = 'resolved', resolution = 'confirmed'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    connection.rollback()


def test_an_unknown_status_is_rejected(settings, connection):
    """And rejected by the TRANSITION GUARD, which reaches an unknown status
    before the CHECK does - asserting a bare Exception here would pass on a
    misspelled column name just as happily."""
    review_id = review_id_for(connection, LIVE_CRITICAL)

    with pytest.raises(psycopg.errors.DatabaseError) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET status = 'escalated_to_ceo'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    assert "Invalid review transition" in str(excinfo.value)
    connection.rollback()


def test_an_unknown_resolution_is_rejected(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")

    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue
                SET status = 'resolved', resolution = 'critical'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    # 'critical' is an anomaly severity, not a review resolution. The two
    # vocabularies must not be interchangeable even by accident.
    assert "review_queue_resolution_valid" in str(excinfo.value)
    connection.rollback()


def test_a_terminal_state_requires_a_resolution(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")

    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET status = 'resolved'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    assert "review_queue_terminal_has_resolution" in str(excinfo.value)
    connection.rollback()


def test_a_transition_on_a_missing_item_is_a_lookup_error(settings):
    with pytest.raises(LookupError):
        service.claim_review(settings, 999_999_999, "nobody@example.invalid")


# =============================================================================
# The Stage 6 boundary survives a reviewer
# =============================================================================


def test_reviewing_an_item_does_not_change_the_stage6_decision(settings, connection):
    """The whole reason the deterministic layer exists.

    A reviewer records what they concluded. They do not re-grade the anomaly, and
    a resolution of 'false_positive' does not make it a minor one.
    """
    before = decisions_snapshot(connection)
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "false_positive", notes="Not a real event.")

    assert decisions_snapshot(connection) == before

    decision = query(connection, """
        SELECT severity, routing, decision, human_review_required
        FROM salesops.anomaly_decisions
        WHERE calendar_date = %(d)s AND decision_version = 'stage6-v1'
    """, {"d": LIVE_CRITICAL})[0]

    assert decision["severity"] == "critical"
    assert decision["routing"] == "human_review"
    assert decision["human_review_required"] is True


def test_a_reviewer_cannot_edit_the_snapshot_on_the_queue_item(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)

    with pytest.raises(Exception, match="may not restate"):
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET severity = 'minor'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    connection.rollback()


def test_the_resolution_triggers_no_business_action(settings, connection):
    """Section 28: Stage 8 ends at the recorded outcome."""
    before = query(connection, """
        SELECT (SELECT count(*) FROM salesops.fact_orders)   AS orders,
               (SELECT count(*) FROM salesops.kpi_daily)     AS kpis,
               (SELECT count(*) FROM salesops.anomaly_daily) AS anomalies,
               (SELECT count(*) FROM salesops.notifications) AS notifications
    """)[0]

    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "requires_follow_up")

    after = query(connection, """
        SELECT (SELECT count(*) FROM salesops.fact_orders)   AS orders,
               (SELECT count(*) FROM salesops.kpi_daily)     AS kpis,
               (SELECT count(*) FROM salesops.anomaly_daily) AS anomalies,
               (SELECT count(*) FROM salesops.notifications) AS notifications
    """)[0]

    assert after == before


# =============================================================================
# Review notes are untrusted input
# =============================================================================


def test_review_notes_never_reach_a_notification(settings, connection):
    """Section 23. A note is written by a person and read by a person.

    Letting it flow into an outbound payload would make the review queue a relay
    for whatever anyone typed into it.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    marker = "REVIEW-NOTE-CANARY-8831"

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed", notes=f"{marker} internal only")

    payloads = query(connection, "SELECT payload::text AS body FROM salesops.notifications")
    assert all(marker not in row["body"] for row in payloads)

    # ...and a later routing run must not pick it up either.
    service.run_routing(
        settings=settings, provider=RecordingProvider(), recipients=[TEST_RECIPIENT]
    )
    payloads = query(connection, "SELECT payload::text AS body FROM salesops.notifications")
    assert all(marker not in row["body"] for row in payloads)


def test_a_note_containing_markup_or_sql_is_stored_verbatim_and_inertly(settings, connection):
    """Stored as data, not interpreted. It is neither escaped nor executed."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    hostile = "'; DROP TABLE salesops.notifications; -- <script>alert(1)</script>"

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed", notes=hostile)

    stored = query(connection, """
        SELECT review_notes FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]["review_notes"]

    assert stored == hostile
    # The table it named is still there.
    assert query(connection, "SELECT count(*) AS n FROM salesops.notifications")[0]["n"] >= 0


def test_over_long_notes_are_bounded(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed", notes="x" * 10_000)

    stored = query(connection, """
        SELECT length(review_notes) AS n FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]["n"]

    assert stored <= 4000


def test_the_history_excerpt_is_bounded_too(settings, connection):
    """The event log keeps an excerpt, not the whole note.

    A 4,000-character note copied into every transition row would make the
    history unreadable and duplicate untrusted content for no benefit.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)

    service.claim_review(settings, review_id, "alex@example.invalid")
    service.resolve_review(settings, review_id, "confirmed", notes="y" * 3000)

    excerpts = query(connection, """
        SELECT note_excerpt FROM salesops.review_events
        WHERE review_id = %(id)s AND note_excerpt IS NOT NULL
    """, {"id": review_id})

    assert excerpts, "the resolving transition should have recorded an excerpt"
    assert all(len(row["note_excerpt"]) <= 500 for row in excerpts)


# =============================================================================
# Reading the queue
# =============================================================================


def test_the_queue_is_ordered_by_severity(settings):
    reviews = service.fetch_reviews(settings)
    severities = [item["queued_severity"] for item in reviews]

    assert severities[0] == "critical"
    assert set(severities) <= {"critical", "major"}


def test_the_queue_can_be_filtered(settings):
    assert all(
        item["queued_severity"] == "major"
        for item in service.fetch_reviews(settings, severity="major")
    )
    assert all(
        item["status"] == "pending"
        for item in service.fetch_reviews(settings, status="pending")
    )


def test_fetching_one_item_includes_its_history(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    service.claim_review(settings, review_id, "alex@example.invalid")

    review = service.fetch_review(settings, review_id)

    assert review["review_id"] == review_id
    assert len(review["history"]) == 2


def test_fetching_a_missing_item_returns_nothing(settings):
    assert service.fetch_review(settings, 999_999_999) is None
