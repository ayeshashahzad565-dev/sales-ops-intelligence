"""The authorisation boundary: who may create a remediation action, and from what.

This is the file that matters most in Stage 9. Everything downstream - the state
machine, the retry budget, the audit trail - is bookkeeping around a single
claim: **nothing can be remediated that a human did not approve.** These tests
try to break that claim from every direction a real system would eventually be
attacked from, deliberately or by accident:

* from a review nobody has looked at;
* from a review somebody dismissed;
* from a review somebody resolved without asking for anything to be done;
* with an action the severity does not permit;
* with a snapshot that claims a severity the review does not carry;
* by asking twice and hoping for two authorisations.

A test that only proved the happy path works would prove nothing worth knowing.
"""

from __future__ import annotations

import psycopg
import pytest

from analytics import repository
from analytics.notifications import service as notification_service
from analytics.remediation import service
from analytics.remediation.models import ActionType
from tests.remediation_fixtures import (
    APPROVER,
    LIVE_CRITICAL,
    LIVE_MAJOR,
    action_row,
    approve,
    claim,
    events_for,
    make_settings,
    populate,
    purge,
    query,
    review_id_for,
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
def clean_queue(settings, connection):
    purge(connection)
    populate(settings)
    yield
    purge(connection)


# =============================================================================
# The states that do not authorise anything
# =============================================================================


def test_a_pending_review_cannot_authorise_remediation(settings, connection):
    """Nobody has looked at it. There is no approval to act on."""
    review_id = review_id_for(connection, LIVE_CRITICAL)

    with pytest.raises(service.RemediationError) as exc:
        approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    assert "pending" in str(exc.value)
    assert _action_count(connection) == 0


def test_an_in_review_item_is_not_yet_an_approval(settings, connection):
    """Claiming is not approving.

    Exercised through the database rather than the service, because the service
    is the thing that would be bypassed if somebody wrote directly to the table.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    with pytest.raises(Exception) as exc:
        _insert_action_directly(connection, review_id, ActionType.CREATE_INVESTIGATION)
    connection.rollback()

    assert "approved review" in str(exc.value)
    assert _action_count(connection) == 0


def test_a_dismissed_review_cannot_authorise_remediation(settings, connection):
    """Dismissal is the opposite of approval, not a quieter form of it."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    notification_service.dismiss_review(settings, review_id, "false_positive", APPROVER)

    with pytest.raises(service.RemediationError) as exc:
        approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    assert "dismissed" in str(exc.value)
    assert _action_count(connection) == 0


def test_a_resolved_review_cannot_authorise_remediation(settings, connection):
    """'resolved' means reviewed and closed WITHOUT remediation.

    This is the distinction V011 exists to make. Before it, a single 'resolved'
    state had to stand for both "confirmed, do something" and "confirmed, do
    nothing", and reading either as consent would be guessing at what a person
    meant.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    notification_service.resolve_review(
        settings, review_id, "confirmed", APPROVER, "Confirmed. Nothing to do."
    )

    with pytest.raises(service.RemediationError) as exc:
        approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    assert "resolved" in str(exc.value)
    assert "not an approval" in str(exc.value)
    assert _action_count(connection) == 0


def test_a_missing_review_is_a_lookup_error(settings):
    with pytest.raises(LookupError):
        approve(settings, 99_999_999, ActionType.CREATE_INVESTIGATION)


# =============================================================================
# The resolution has to mean something
# =============================================================================


@pytest.mark.parametrize("resolution", ["false_positive", "expected_business_variation"])
def test_a_non_confirming_resolution_cannot_approve(settings, connection, resolution):
    """You cannot authorise action on something you have just called normal."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    with pytest.raises(service.RemediationError) as exc:
        approve(settings, review_id, ActionType.CREATE_INVESTIGATION, resolution=resolution)

    assert resolution in str(exc.value)
    assert _action_count(connection) == 0
    # And the review is untouched - a refused approval is not a partial one.
    assert _review_status(connection, review_id) == "in_review"


@pytest.mark.parametrize("resolution", ["confirmed", "requires_follow_up"])
def test_a_confirming_resolution_can_approve(settings, connection, resolution):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION, resolution=resolution)

    assert created["status"] == "proposed"
    assert _review_status(connection, review_id) == "approved"


# =============================================================================
# Eligibility
# =============================================================================


def test_refund_review_is_refused_for_a_major_anomaly(settings, connection):
    """Eligibility is a foreign key, so this fails in the database.

    Re-opening settled financial transactions is the most disruptive thing in
    the vocabulary. Stage 6 has already published which days it considers worth
    that, and Stage 9 reuses its answer rather than forming a second opinion.
    """
    review_id = review_id_for(connection, LIVE_MAJOR)
    claim(settings, review_id)

    with pytest.raises(service.RemediationError) as exc:
        approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    assert "not permitted" in str(exc.value)
    assert "major" in str(exc.value)
    assert _action_count(connection) == 0


def test_a_refused_action_does_not_half_approve_the_review(settings, connection):
    """The approval and the action are one transaction.

    Without this, an ineligible request would leave the review terminally
    approved with no action to show for it - and because 'approved' is terminal,
    the reviewer could never approve anything else.
    """
    review_id = review_id_for(connection, LIVE_MAJOR)
    claim(settings, review_id)

    with pytest.raises(service.RemediationError):
        approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    assert _review_status(connection, review_id) == "in_review"
    assert query(connection, """
        SELECT approved_by, approved_at FROM salesops.review_queue
        WHERE review_id = %(id)s
    """, {"id": review_id})[0] == {"approved_by": None, "approved_at": None}


def test_refund_review_is_permitted_for_a_critical_anomaly(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    created = approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    assert created["action_type"] == "request_refund_review"
    assert action_row(connection, created["remediation_id"])["severity"] == "critical"


@pytest.mark.parametrize(
    "action_type",
    [ActionType.CREATE_INVESTIGATION, ActionType.REQUEST_OPERATIONS_REVIEW],
)
def test_review_actions_are_permitted_at_both_review_severities(
    settings, connection, action_type
):
    for anomaly_date in (LIVE_CRITICAL, LIVE_MAJOR):
        review_id = review_id_for(connection, anomaly_date)
        claim(settings, review_id)
        created = approve(settings, review_id, action_type)
        assert created["created"] is True


def test_an_unknown_action_type_cannot_be_named(settings, connection):
    """The vocabulary is closed at three separate layers.

    The enum here, a `Literal` on the HTTP model, and a foreign key in the
    database. No caller - and no language model, whose output never reaches this
    path at all - can name a fourth action.
    """
    with pytest.raises(ValueError):
        ActionType("delete_all_refunds")

    # The review is approved first, deliberately. An unapproved review is
    # refused by the AUTHORISATION guard, which would mask the thing under test
    # here - so the only remaining objection to 'issue_refund' is that no such
    # action exists. That objection is the eligibility FOREIGN KEY.
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    with pytest.raises(psycopg.errors.ForeignKeyViolation) as exc:
        _insert_action_directly(connection, review_id, "issue_refund")
    connection.rollback()
    assert "remediation_actions_eligible_fk" in str(exc.value)


def test_no_eligibility_row_permits_a_non_review_severity(connection):
    """A 'minor' row here would be a side door around human review.

    Stage 6 routes minor to auto_notify, so no review item is ever created for
    one, so there is nothing to approve. If a minor anomaly ever needs
    remediating the honest fix is to change Stage 6's routing - not to open a
    path into Stage 9 that never passes a person.
    """
    rows = query(connection, """
        SELECT severity FROM salesops.remediation_action_eligibility
        WHERE severity NOT IN ('major', 'critical')
    """)
    assert rows == []


# =============================================================================
# Snapshot integrity
# =============================================================================


def test_a_fabricated_severity_snapshot_is_refused(settings, connection):
    """The attack the eligibility foreign key alone would not stop.

    Claim a severity the review does not carry, and an ineligible action looks
    eligible. The guard trigger compares the snapshot against the live review
    before the foreign key is ever consulted.
    """
    review_id = review_id_for(connection, LIVE_MAJOR)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    with pytest.raises(Exception) as exc:
        _insert_action_directly(
            connection, review_id, ActionType.REQUEST_REFUND_REVIEW,
            severity_override="critical",
        )
    connection.rollback()

    assert "does not match review" in str(exc.value)


def test_the_snapshot_records_what_was_approved(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    action = action_row(connection, created["remediation_id"])
    review = query(connection, """
        SELECT * FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]

    for column in (
        "anomaly_id", "decision_id", "calendar_date", "decision_version",
        "severity", "routing", "decision", "notification_allowed",
        "human_review_required", "hypothesis_id", "hypothesis_status",
    ):
        assert action[column] == review[column], column

    assert action["review_approved_by"] == review["approved_by"] == APPROVER
    assert action["review_approved_at"] == review["approved_at"]
    assert action["review_resolution"] == review["resolution"]
    assert action["decision_reason_codes"], "reason codes were not snapshotted"


def test_the_snapshot_cannot_be_rewritten_afterwards(settings, connection):
    """A later Stage 6 re-decision must not rewrite what a human authorised."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    with pytest.raises(Exception) as exc:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.remediation_actions SET severity = 'major'
                WHERE remediation_id = %(id)s
            """, {"id": created["remediation_id"]})
    connection.rollback()

    assert "immutable" in str(exc.value)


def test_an_action_cannot_be_created_already_executed(settings, connection):
    """Authorisation and execution are separate, including at INSERT."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    with pytest.raises(Exception) as exc:
        _insert_action_directly(
            connection, review_id, ActionType.CREATE_INVESTIGATION, status="executed"
        )
    connection.rollback()

    assert "not an opening state" in str(exc.value)


# =============================================================================
# Idempotency of approval
# =============================================================================


def test_approving_twice_creates_one_action(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    first = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)
    second = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    assert first["remediation_id"] == second["remediation_id"]
    assert first["created"] is True
    assert second["created"] is False
    assert _action_count(connection) == 1


def test_a_repeated_approval_does_not_re_attribute_the_first(settings, connection):
    """The approver of record is whoever actually approved it."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    first = approve(settings, review_id, ActionType.CREATE_INVESTIGATION, actor=APPROVER)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION, actor="someone-else@example.invalid")

    assert action_row(connection, first["remediation_id"])["review_approved_by"] == APPROVER
    assert _review_row(connection, review_id)["approved_by"] == APPROVER


def test_a_second_action_type_from_the_same_review_is_allowed(settings, connection):
    """A critical anomaly may warrant both an investigation and a refund review.

    The review transition happens once; the idempotency key is per action type,
    so a second distinct action is a new row rather than a conflict.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)

    first = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)
    second = approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)

    assert first["remediation_id"] != second["remediation_id"]
    assert _action_count(connection) == 2
    # Both carry the same, single approval.
    assert (action_row(connection, first["remediation_id"])["review_approved_at"]
            == action_row(connection, second["remediation_id"])["review_approved_at"])


def test_the_idempotency_key_is_derived_not_supplied(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    action = action_row(connection, created["remediation_id"])
    assert action["idempotency_key"] == (
        f"{review_id}:create_investigation:{action['decision_version']}"
    )


# =============================================================================
# The approval is recorded as an event, not only as a state
# =============================================================================


def test_approval_appends_to_the_review_history(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    transitions = [
        (row["from_status"], row["to_status"])
        for row in query(connection, """
            SELECT from_status, to_status FROM salesops.review_events
            WHERE review_id = %(id)s ORDER BY occurred_at, event_id
        """, {"id": review_id})
    ]
    assert transitions == [(None, "pending"), ("pending", "in_review"), ("in_review", "approved")]


def test_creating_an_action_records_an_opening_event(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    events = events_for(connection, created["remediation_id"])
    assert len(events) == 1
    assert events[0]["from_status"] is None
    assert events[0]["to_status"] == "proposed"
    assert events[0]["actor"] == APPROVER
    assert str(review_id) in events[0]["reason"]


def test_an_approved_review_is_final(settings, connection):
    """Its resolution, notes and approver cannot be edited afterwards.

    Remediation has been authorised against exactly this text; letting it be
    rewritten would let the record of why something was done be changed after
    it was done.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    with pytest.raises(Exception) as exc:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET resolution = 'false_positive'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    connection.rollback()
    assert "final" in str(exc.value)

    with pytest.raises(Exception) as exc:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.review_queue SET approved_by = 'somebody-else'
                WHERE review_id = %(id)s
            """, {"id": review_id})
    connection.rollback()
    assert "final" in str(exc.value)


def test_an_approved_review_cannot_be_reopened(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    for target in ("pending", "in_review", "resolved", "dismissed"):
        with pytest.raises(Exception) as exc:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE salesops.review_queue SET status = %(s)s
                    WHERE review_id = %(id)s
                """, {"s": target, "id": review_id})
        connection.rollback()
        assert "Invalid review transition" in str(exc.value), target


# =============================================================================
# Helpers
# =============================================================================


def _action_count(connection) -> int:
    return query(connection, "SELECT count(*) AS n FROM salesops.remediation_actions")[0]["n"]


def _review_row(connection, review_id: int) -> dict:
    return query(connection, """
        SELECT * FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]


def _review_status(connection, review_id: int) -> str:
    return _review_row(connection, review_id)["status"]


def _insert_action_directly(
    connection,
    review_id: int,
    action_type,
    severity_override: str | None = None,
    status: str = "proposed",
) -> None:
    """Write straight to the table, bypassing the service entirely.

    This is how a determined caller with database access would try to reach
    Stage 9, so it is how the guards have to be tested. Nothing here goes
    through a code path the service controls.
    """
    review = _review_row(connection, review_id)
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO salesops.remediation_actions (
                review_id, anomaly_id, decision_id, calendar_date, decision_version,
                severity, routing, decision, notification_allowed, human_review_required,
                decision_reason_code, review_approved_by, review_approved_at,
                review_resolution, action_type, request_payload, status)
            VALUES (
                %(review_id)s, %(anomaly_id)s, %(decision_id)s, %(calendar_date)s,
                %(decision_version)s, %(severity)s, %(routing)s, %(decision)s,
                %(notification_allowed)s, %(human_review_required)s,
                'STATISTICAL_ANOMALY', %(approved_by)s, %(approved_at)s,
                %(resolution)s, %(action_type)s, '{}'::jsonb, %(status)s)
        """, {
            "review_id": review_id,
            "anomaly_id": review["anomaly_id"],
            "decision_id": review["decision_id"],
            "calendar_date": review["calendar_date"],
            "decision_version": review["decision_version"],
            "severity": severity_override or review["severity"],
            "routing": review["routing"],
            "decision": review["decision"],
            "notification_allowed": review["notification_allowed"],
            "human_review_required": review["human_review_required"],
            "approved_by": review["approved_by"] or "direct-insert",
            "approved_at": review["approved_at"] or "2026-01-01",
            "resolution": review["resolution"] or "confirmed",
            "action_type": str(action_type),
            "status": status,
        })
