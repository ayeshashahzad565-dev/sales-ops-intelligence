"""Execution: the state machine, the exactly-once guarantee, and the retry budget.

Authorisation says an action *may* run. This file is about what happens when
something tries to run it, and the property it exists to protect is narrow and
absolute:

    **the provider is called at most once per logical action.**

Not "usually once". Not "once unless two runs overlap". The recording provider
counts its own calls, so every claim here is checked against what the provider
actually received rather than against what the database says happened.
"""

from __future__ import annotations

import pytest

from analytics import repository
from analytics.remediation import service
from analytics.remediation.models import ActionType, ExecutionOutcome
from analytics.remediation.provider import RecordingRemediationProvider
from tests.live_dates import INCIDENT_DATE as LIVE_CRITICAL
from tests.live_dates import MAJOR_DATE as LIVE_MAJOR
from tests.remediation_fixtures import (
    APPROVER,
    AUTHORIZER,
    action_row,
    approve,
    authorized_action,
    claim,
    events_for,
    make_settings,
    populate,
    purge,
    query,
    review_id_for,
)

EXECUTOR = "executor@example.invalid"


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


@pytest.fixture
def provider():
    return RecordingRemediationProvider()


# =============================================================================
# Authorisation gates execution
# =============================================================================


def test_a_proposed_action_will_not_execute(settings, connection, provider):
    """Approving the review is not authorising the action."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    with pytest.raises(service.NotAuthorized):
        service.execute_action(settings, provider, created["remediation_id"], EXECUTOR)

    assert provider.calls == 0
    assert action_row(connection, created["remediation_id"])["status"] == "proposed"


def test_the_batch_run_never_sees_an_unauthorised_action(settings, connection, provider):
    """The work set is `remediation_pending_execution`, which excludes 'proposed'.

    This is the one that matters for the scheduled workflow: even a run that
    executes everything it is given cannot execute something nobody authorised,
    because it is never given it.
    """
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    summary = service.execute_approved(settings, provider)

    assert summary.eligible == 0
    assert summary.executed == 0
    assert provider.calls == 0


def test_an_authorised_action_executes(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)

    result = service.execute_action(settings, provider, remediation_id, EXECUTOR)

    assert result["status"] == "executed"
    assert result["attempt"] == 1
    assert provider.calls == 1

    action = action_row(connection, remediation_id)
    assert action["status"] == "executed"
    assert action["executed_at"] is not None
    assert action["executed_by"] == EXECUTOR
    assert action["provider_reference"] == "local-record-1"


def test_authorisation_alone_executes_nothing(settings, connection, provider):
    """Between approval and execution the action sits at rest."""
    remediation_id = authorized_action(settings, connection)

    action = action_row(connection, remediation_id)
    assert action["status"] == "approved"
    assert action["executed_at"] is None
    assert action["attempt_count"] == 0
    assert provider.calls == 0


def test_the_executing_actor_is_never_the_approving_one(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    action = action_row(connection, remediation_id)
    assert action["review_approved_by"] == APPROVER
    assert action["authorized_by"] == AUTHORIZER
    assert action["executed_by"] == EXECUTOR


# =============================================================================
# Exactly once
# =============================================================================


def test_executing_twice_calls_the_provider_once(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)

    first = service.execute_action(settings, provider, remediation_id, EXECUTOR)
    second = service.execute_action(settings, provider, remediation_id, EXECUTOR)

    assert first["status"] == "executed"
    assert second["status"] == "executed"
    assert second["changed"] is False
    assert provider.calls == 1
    assert provider.requests_for == {remediation_id: 1}


def test_rerunning_the_batch_executes_nothing_new(settings, connection, provider):
    """The idempotency property the scheduled workflow depends on."""
    authorized_action(settings, connection, LIVE_CRITICAL)
    authorized_action(settings, connection, LIVE_MAJOR)

    first = service.execute_approved(settings, provider)
    second = service.execute_approved(settings, provider)
    third = service.execute_approved(settings, provider)

    assert first.executed == 2
    assert (second.eligible, second.executed) == (0, 0)
    assert (third.eligible, third.executed) == (0, 0)
    assert provider.calls == 2
    assert set(provider.requests_for.values()) == {1}


def test_an_executed_action_is_terminal_in_the_database(settings, connection, provider):
    """Not merely refused by the service - refused by the database.

    A caller writing straight to the table must fail too, or "executed once" is
    a property of one code path rather than of the system.
    """
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    for target in ("executing", "approved", "proposed", "failed", "cancelled"):
        with pytest.raises(Exception) as exc:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE salesops.remediation_actions SET status = %(s)s
                    WHERE remediation_id = %(id)s
                """, {"s": target, "id": remediation_id})
        connection.rollback()
        assert "Invalid remediation transition" in str(exc.value), target


def test_a_claimed_action_cannot_be_claimed_again(settings, connection, provider):
    """The claim is a conditional UPDATE, which is what makes the race safe."""
    remediation_id = authorized_action(settings, connection)

    first = repository.claim_remediation_for_execution(connection, remediation_id, EXECUTOR)
    connection.commit()
    second = repository.claim_remediation_for_execution(connection, remediation_id, EXECUTOR)
    connection.commit()

    assert first is not None
    assert second is None, "two callers both claimed the same action"


# =============================================================================
# Failure and retry
# =============================================================================


def test_a_failed_execution_is_recorded_and_retryable(settings, connection):
    failing = RecordingRemediationProvider(outcome=ExecutionOutcome.RETRYABLE_FAILURE)
    remediation_id = authorized_action(settings, connection)

    result = service.execute_action(settings, failing, remediation_id, EXECUTOR)

    assert result["status"] == "failed"
    assert result["will_retry"] is True

    action = action_row(connection, remediation_id)
    assert action["status"] == "failed"
    assert action["attempt_count"] == 1
    assert action["executed_at"] is None, "a failed action claimed an execution time"
    assert action["last_error"]


def test_a_failure_converges_on_retry(settings, connection):
    """Two failures then a success, across three runs - as the schedule would."""
    flaky = RecordingRemediationProvider(fail_first=2)
    remediation_id = authorized_action(settings, connection)

    first = service.execute_approved(settings, flaky)
    second = service.execute_approved(settings, flaky)
    third = service.execute_approved(settings, flaky)

    assert (first.failed, second.failed, third.executed) == (1, 1, 1)
    assert flaky.calls == 3
    assert action_row(connection, remediation_id)["status"] == "executed"

    outcomes = [row["outcome"] for row in repository.remediation_attempts(connection, remediation_id)]
    assert outcomes == ["retryable_failure", "retryable_failure", "success"]


def test_the_retry_budget_is_bounded(settings, connection):
    """Three attempts, then nothing further is tried automatically.

    Without this, a permanently broken action would be re-executed by every
    scheduled run for the rest of the system's life.
    """
    always_failing = RecordingRemediationProvider(outcome=ExecutionOutcome.RETRYABLE_FAILURE)
    remediation_id = authorized_action(settings, connection)

    for _ in range(6):
        service.execute_approved(settings, always_failing)

    assert always_failing.calls == service.MAX_EXECUTION_ATTEMPTS
    assert action_row(connection, remediation_id)["attempt_count"] == 3


def test_a_spent_action_leaves_the_work_set_quietly(settings, connection):
    always_failing = RecordingRemediationProvider(outcome=ExecutionOutcome.PERMANENT_FAILURE)
    remediation_id = authorized_action(settings, connection)

    for _ in range(3):
        service.execute_approved(settings, always_failing)

    pending = repository.list_executable_remediations(connection)
    assert remediation_id not in [row["remediation_id"] for row in pending]

    # And a later run is a clean success rather than a repeated failure.
    summary = service.execute_approved(settings, always_failing)
    assert (summary.eligible, summary.status) == (0, "success")


def test_a_provider_that_raises_does_not_strand_the_action(settings, connection):
    """A provider is contracted never to raise. Contracts get broken.

    If one does, the action must not be left in 'executing' with nothing
    watching it - so the exception becomes a recorded permanent failure.
    """
    class ExplodingProvider:
        name = "exploding"

        def execute(self, request):
            raise RuntimeError("the ticket system fell over")

    remediation_id = authorized_action(settings, connection)
    result = service.execute_action(settings, ExplodingProvider(), remediation_id, EXECUTOR)

    assert result["status"] == "failed"
    action = action_row(connection, remediation_id)
    assert action["status"] == "failed"
    assert "the ticket system fell over" in action["last_error"]


# =============================================================================
# Refusal and cancellation
# =============================================================================


def test_a_rejected_action_never_executes(settings, connection, provider):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    service.reject_action(settings, created["remediation_id"], APPROVER, "Known cause.")

    with pytest.raises(service.RemediationError):
        service.execute_action(settings, provider, created["remediation_id"], EXECUTOR)
    assert provider.calls == 0
    assert action_row(connection, created["remediation_id"])["closed_reason"] == "Known cause."


def test_rejecting_leaves_the_review_approved(settings, connection):
    """The anomaly stays confirmed; only this response is refused."""
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)
    service.reject_action(settings, created["remediation_id"], APPROVER, "Known cause.")

    assert query(connection, """
        SELECT status FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]["status"] == "approved"


def test_an_authorised_action_can_be_cancelled(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)
    service.cancel_action(settings, remediation_id, APPROVER, "Superseded.")

    with pytest.raises(service.RemediationError):
        service.execute_action(settings, provider, remediation_id, EXECUTOR)
    assert provider.calls == 0


def test_an_executed_action_cannot_be_cancelled(settings, connection, provider):
    """Pretending otherwise would put a lie in the audit trail.

    An action already handed to a provider cannot be un-handed by changing a
    row, whatever the row says afterwards.
    """
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    with pytest.raises(service.RemediationError) as exc:
        service.cancel_action(settings, remediation_id, APPROVER, "Changed my mind.")
    assert "executed" in str(exc.value)


def test_closing_requires_a_reason(settings, connection):
    remediation_id = authorized_action(settings, connection)
    with pytest.raises(service.RemediationError):
        service.cancel_action(settings, remediation_id, APPROVER, "   ")


def test_rejecting_an_authorised_action_is_refused(settings, connection):
    """Reject is for a proposal. Once authorised, the operation is cancel."""
    remediation_id = authorized_action(settings, connection)
    with pytest.raises(service.RemediationError) as exc:
        service.reject_action(settings, remediation_id, APPROVER, "Too late.")
    assert "approved" in str(exc.value)


# =============================================================================
# The state machine, exhaustively
# =============================================================================


ILLEGAL_TRANSITIONS = [
    ("proposed", "executing"),
    ("proposed", "executed"),
    ("proposed", "failed"),
    ("approved", "executed"),
    ("approved", "failed"),
    ("approved", "rejected"),
    ("approved", "proposed"),
]


@pytest.mark.parametrize("from_status,to_status", ILLEGAL_TRANSITIONS)
def test_illegal_transitions_are_refused(settings, connection, from_status, to_status):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)
    remediation_id = created["remediation_id"]

    if from_status == "approved":
        service.authorize_action(settings, remediation_id, AUTHORIZER)

    with pytest.raises(Exception) as exc:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.remediation_actions
                SET status = %(s)s, executed_by = 'x', attempt_count = 1
                WHERE remediation_id = %(id)s
            """, {"s": to_status, "id": remediation_id})
    connection.rollback()
    assert "Invalid remediation transition" in str(exc.value)


def test_every_transition_produces_an_event(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    assert [(e["from_status"], e["to_status"]) for e in events_for(connection, remediation_id)] == [
        (None, "proposed"),
        ("proposed", "approved"),
        ("approved", "executing"),
        ("executing", "executed"),
    ]


def test_every_transition_names_the_actor_who_made_it(settings, connection, provider):
    """Three different people, three correctly attributed events.

    Attributing a scheduled execution to the manager who approved the action
    three days earlier would be a small lie, and an audit trail made of small
    lies is not one.
    """
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    assert [(e["to_status"], e["actor"]) for e in events_for(connection, remediation_id)] == [
        ("proposed", APPROVER),
        ("approved", AUTHORIZER),
        ("executing", EXECUTOR),
        ("executed", EXECUTOR),
    ]


def test_a_failed_action_names_nobody_as_having_executed_it(settings, connection):
    """`executed_by` is stamped at the claim so the transition is attributable,
    then cleared if the action did not in fact execute. Who tried is still in
    the attempt row."""
    failing = RecordingRemediationProvider(outcome=ExecutionOutcome.PERMANENT_FAILURE)
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, failing, remediation_id, EXECUTOR)

    action = action_row(connection, remediation_id)
    assert action["status"] == "failed"
    assert action["executed_by"] is None
    assert action["executed_at"] is None

    events = events_for(connection, remediation_id)
    assert ("executing", EXECUTOR) in [(e["to_status"], e["actor"]) for e in events]


def test_authorising_twice_is_idempotent(settings, connection):
    review_id = review_id_for(connection, LIVE_CRITICAL)
    claim(settings, review_id)
    created = approve(settings, review_id, ActionType.CREATE_INVESTIGATION)

    first = service.authorize_action(settings, created["remediation_id"], AUTHORIZER)
    second = service.authorize_action(settings, created["remediation_id"], "somebody-else")

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["authorized_by"] == AUTHORIZER
    # ...and only one transition was recorded.
    approvals = [e for e in events_for(connection, created["remediation_id"])
                 if e["to_status"] == "approved"]
    assert len(approvals) == 1


def test_authorisation_records_who_and_when(settings, connection):
    remediation_id = authorized_action(settings, connection)
    action = action_row(connection, remediation_id)
    assert action["authorized_by"] == AUTHORIZER
    assert action["authorized_at"] is not None


# =============================================================================
# The provider claims nothing it did not do
# =============================================================================


def test_the_provider_never_claims_an_external_side_effect(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    attempts = repository.remediation_attempts(connection, remediation_id)
    assert attempts
    assert all(attempt["external_side_effect"] is False for attempt in attempts)

    audit = query(connection, """
        SELECT had_external_side_effect FROM salesops.remediation_audit
        WHERE remediation_id = %(id)s
    """, {"id": remediation_id})[0]
    assert audit["had_external_side_effect"] is False


def test_the_provider_reference_does_not_impersonate_a_ticket(settings, connection, provider):
    """"TICKET-4821" in an audit trail invites somebody to go looking for it."""
    remediation_id = authorized_action(settings, connection)
    result = service.execute_action(settings, provider, remediation_id, EXECUTOR)

    reference = result["provider_reference"].lower()
    assert reference.startswith("local-record-")
    for impersonation in ("ticket", "case", "inc", "jira", "sn", "crm"):
        assert not reference.startswith(impersonation)


def test_the_payload_says_plainly_that_nothing_external_happens(settings, connection, provider):
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    payload = provider.received[0].payload
    note = payload["action"]["note"].lower()
    assert "request for human" in note
    assert "issues no refund" in note

    # ...and the authorisation travels with it, so a downstream reader can never
    # mistake this for something a machine decided on its own.
    assert payload["authorization"]["approved_by"] == APPROVER
    assert payload["authorization"]["review_id"]


def test_no_stage_7_content_reaches_the_provider(settings, connection, provider):
    """A hypothesis is a guess. Putting one in front of the person asked to
    investigate would anchor the investigation on it."""
    remediation_id = authorized_action(settings, connection)
    service.execute_action(settings, provider, remediation_id, EXECUTOR)

    serialised = str(provider.received[0].payload).lower()
    for leak in ("hypothesis", "confidence", "primary_hypothesis", "llama", "model"):
        assert leak not in serialised, f"{leak} reached the provider"
