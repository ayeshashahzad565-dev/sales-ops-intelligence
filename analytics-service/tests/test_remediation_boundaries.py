"""What Stage 9 must never touch, and what must never reach it.

Stage 9 is the first stage in this pipeline with the word "execute" in it, which
makes it the first one capable of doing damage. Three boundaries hold it in
place, and this file is where each is checked from the outside:

* **Upstream.** A full remediation lifecycle - approve, authorise, execute, fail,
  retry, reject, cancel - must leave Stage 6, Stage 7 and Stage 8 byte-identical.
  Not "mostly unchanged". Identical.

* **The warehouse.** Executing remediation must not change an order, a KPI, a
  detection or a customer. If it ever did, the system would have taken a
  business action, which is the one thing the whole architecture exists to
  prevent it doing on its own.

* **The model.** No Stage 7 output may reach an execution path, in any form, by
  any route. This is checked structurally as well as behaviourally, because a
  behavioural test only covers the paths somebody thought to write.

The workflow checks are here too: no credentials in the JSON, no severity logic
in a Code node, and a failure path that closes its own ledger entry.
"""

from __future__ import annotations

import json
import re

import pytest

from analytics import repository
from analytics.notifications import service as notification_service
from analytics.remediation import service
from analytics.remediation.models import ActionType, ExecutionOutcome
from analytics.remediation.provider import RecordingRemediationProvider
from tests.live_dates import INCIDENT_DATE as LIVE_CRITICAL
from tests.live_dates import MAJOR_DATE as LIVE_MAJOR
from tests.remediation_fixtures import (
    APPROVER,
    REPO_ROOT,
    action_row,
    approve,
    authorized_action,
    claim,
    make_settings,
    populate,
    purge,
    query,
    review_id_for,
    stage6_fingerprint,
    stage7_fingerprint,
    warehouse_fingerprint,
)

WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "remediation-execution.json"
REMEDIATION_PACKAGE = REPO_ROOT / "analytics-service" / "analytics" / "remediation"


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


@pytest.fixture
def queue(settings, connection):
    purge(connection)
    populate(settings)
    yield
    purge(connection)


# =============================================================================
# Upstream stages are untouched
# =============================================================================


def test_no_stage6_decision_changes_whatever_remediation_does(settings, connection, queue):
    """The load-bearing test of the stage.

    One anomaly is driven through every outcome Stage 9 can produce - executed,
    failed, retried to exhaustion, rejected, cancelled - and the entire Stage 6
    verdict set is compared before and after.
    """
    before = stage6_fingerprint(connection)

    # Executed.
    service.execute_action(
        settings, RecordingRemediationProvider(),
        authorized_action(settings, connection, LIVE_CRITICAL), "executor",
    )

    # Failed, retried, and exhausted.
    failing = RecordingRemediationProvider(outcome=ExecutionOutcome.RETRYABLE_FAILURE)
    major = authorized_action(settings, connection, LIVE_MAJOR)
    for _ in range(5):
        service.execute_approved(settings, failing)

    # Rejected, then cancelled - on a second action type from the same review.
    critical_review = review_id_for(connection, LIVE_CRITICAL)
    second = approve(settings, critical_review, ActionType.REQUEST_REFUND_REVIEW)
    service.reject_action(settings, second["remediation_id"], APPROVER, "Not needed.")

    service.cancel_action(settings, major, APPROVER, "Superseded by the investigation.")

    assert stage6_fingerprint(connection) == before


def test_no_stage7_hypothesis_changes(settings, connection, queue):
    before = stage7_fingerprint(connection)

    remediation_id = authorized_action(settings, connection, LIVE_CRITICAL)
    service.execute_action(settings, RecordingRemediationProvider(), remediation_id, "executor")

    assert stage7_fingerprint(connection) == before


def test_stage9_performs_no_business_action(settings, connection, queue):
    """Orders, KPIs, detections and customers, before and after.

    "Remediation" here means a request for a person to look at something. If any
    of these ever moved, the word would mean something else entirely.
    """
    before = warehouse_fingerprint(connection)

    remediation_id = authorized_action(settings, connection, LIVE_CRITICAL)
    service.execute_action(settings, RecordingRemediationProvider(), remediation_id, "executor")
    service.execute_approved(settings, RecordingRemediationProvider())

    assert warehouse_fingerprint(connection) == before


def test_stage8_notifications_are_untouched(settings, connection, queue):
    before = query(connection, """
        SELECT md5(COALESCE(string_agg(
            notification_id || '|' || status || '|' || recipient || '|' ||
            attempt_count, ',' ORDER BY notification_id), '')) AS f
        FROM salesops.notifications
    """)[0]["f"]

    remediation_id = authorized_action(settings, connection, LIVE_CRITICAL)
    service.execute_action(settings, RecordingRemediationProvider(), remediation_id, "executor")

    after = query(connection, """
        SELECT md5(COALESCE(string_agg(
            notification_id || '|' || status || '|' || recipient || '|' ||
            attempt_count, ',' ORDER BY notification_id), '')) AS f
        FROM salesops.notifications
    """)[0]["f"]
    assert after == before


def test_stage8_review_transitions_still_work(settings, connection, queue):
    """V011 widened the review state machine. The Stage 8 paths must be intact.

    A regression here would be invisible in the Stage 9 tests and fatal to
    Stage 8: every reviewer who wanted to close something without remediating it
    would find they no longer could.
    """
    for anomaly_date, close in (
        (LIVE_CRITICAL, lambda rid: notification_service.resolve_review(
            settings, rid, "confirmed", APPROVER, "Closed without action.")),
        (LIVE_MAJOR, lambda rid: notification_service.dismiss_review(
            settings, rid, "false_positive", APPROVER)),
    ):
        review_id = review_id_for(connection, anomaly_date)
        notification_service.claim_review(settings, review_id, APPROVER)
        notification_service.release_review(settings, review_id)
        notification_service.claim_review(settings, review_id, APPROVER)
        close(review_id)

    statuses = {
        row["calendar_date"]: row["status"]
        for row in query(connection, """
            SELECT calendar_date, status FROM salesops.review_queue
        """)
    }
    assert statuses[LIVE_CRITICAL] == "resolved"
    assert statuses[LIVE_MAJOR] == "dismissed"


def test_stage8_routing_is_unaffected_by_an_approval(settings, connection, queue):
    """A Stage 8 rerun after an approval must still be a no-op.

    Approval is terminal for a review, and Stage 8's idempotency key is
    (anomaly, decision_version) - so a rerun finds the item already queued
    whatever state a human has since moved it to.
    """
    authorized_action(settings, connection, LIVE_CRITICAL)
    reviews_before = query(connection, "SELECT count(*) AS n FROM salesops.review_queue")[0]["n"]

    populate(settings)

    assert query(connection, "SELECT count(*) AS n FROM salesops.review_queue")[0]["n"] == reviews_before


# =============================================================================
# The model authorises nothing
# =============================================================================


def test_the_remediation_package_never_imports_the_llm_package():
    """Structural, because a behavioural test only covers the paths written.

    If this ever fails, some execution path has been given access to model
    output, and no amount of careful prompting downstream would fix it.
    """
    offenders = []
    for path in REMEDIATION_PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "analytics.llm" in stripped:
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_no_llm_field_can_reach_an_action_type(settings, connection, queue):
    """The vocabulary is closed and the enum is the only way in."""
    for invented in ("issue_refund", "cancel_orders", "suspend_account", "escalate", ""):
        with pytest.raises(ValueError):
            ActionType(invented)


def test_a_hypothesis_cannot_change_what_is_permitted(settings, connection, queue):
    """Eligibility reads severity, and nothing else.

    The Stage 7 columns are on the row for provenance; this proves they are not
    consulted, by checking that an action carrying a hypothesis is permitted or
    refused on exactly the same terms as one without.
    """
    review_id = review_id_for(connection, LIVE_MAJOR)
    claim(settings, review_id)

    # This review has a Stage 7 analysis attached...
    assert query(connection, """
        SELECT hypothesis_status FROM salesops.review_queue WHERE review_id = %(id)s
    """, {"id": review_id})[0]["hypothesis_status"] in ("available", "unavailable")

    # ...and refund review is still refused, because it is major.
    with pytest.raises(service.RemediationError):
        approve(settings, review_id, ActionType.REQUEST_REFUND_REVIEW)


def test_the_hypothesis_is_recorded_but_not_acted_on(settings, connection, queue):
    remediation_id = authorized_action(settings, connection, LIVE_CRITICAL)
    action = action_row(connection, remediation_id)

    # Provenance is kept...
    assert action["hypothesis_status"] in ("available", "unavailable")
    if action["hypothesis_status"] == "available":
        assert action["hypothesis_id"] is not None
        assert action["hypothesis_model_name"]

    # ...and nothing about it appears in what was requested.
    payload = json.dumps(action["request_payload"]).lower()
    for leak in ("hypothesis", "confidence", "supporting_evidence", "model"):
        assert leak not in payload


# =============================================================================
# The workflow
# =============================================================================


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.exists():
        pytest.skip(f"{WORKFLOW} not found")
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_holds_no_secrets(workflow):
    """Scanned as raw text, so a value nested anywhere is still caught.

    `token` on its own is excluded deliberately: it matches `prompt_tokens` and
    `completion_tokens`, which are usage counts and appear legitimately in this
    project. Narrowing it keeps the check honest rather than noisy.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(api[_-]?key|apikey|secret|access_token|auth_token|bearer|credential|password)"
        r"\s*[\"':=]",
        re.IGNORECASE,
    )
    matches = [m.group(0) for m in pattern.finditer(raw)]
    # "credentials" blocks reference a stored credential BY ID; the value lives
    # in n8n's encrypted store, never here.
    assert [m for m in matches if not m.lower().startswith("credential")] == []

    assert "gsk_" not in raw and "sk-" not in raw
    assert "NOTIFICATION_WEBHOOK_URL" not in raw
    assert "LLM_API_KEY" not in raw


def test_the_workflow_contains_no_severity_or_action_logic(workflow):
    """No Code node, and nothing that could grade an anomaly or pick an action.

    Stage 6 owns severity, and a human owns the choice of action. A workflow
    that could compute either would be a second opinion nobody asked for.
    """
    for node in workflow["nodes"]:
        assert node["type"] != "n8n-nodes-base.code", f"{node['name']} is a Code node"

    raw = WORKFLOW.read_text(encoding="utf-8").lower()
    for forbidden in ("robust_z", "anomaly_score >", "case when severity",
                      "business_impact_tier", "threshold"):
        assert forbidden not in raw, forbidden


def test_the_workflow_never_authorises_anything(workflow):
    """It executes; it does not approve.

    Checked against the SQL and the request body, because an execution workflow
    that could also authorise would collapse the two gates the stage is built
    on into one automated step.
    """
    raw = WORKFLOW.read_text(encoding="utf-8")

    # It calls no approval endpoint...
    for endpoint in ("/approve", "/reviews/"):
        assert endpoint not in raw, endpoint

    # ...and writes to no Stage 8 or Stage 9 table at all. Every Postgres node
    # here touches ingestion_runs and nothing else, so the workflow physically
    # cannot advance an action's state - it can only ask the service to.
    for node in workflow["nodes"]:
        if node["type"] != "n8n-nodes-base.postgres":
            continue
        writes = re.findall(
            r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+(salesops\.\w+)",
            node["parameters"]["query"],
        )
        assert set(writes) <= {"salesops.ingestion_runs"}, f"{node['name']} writes {writes}"

    execute = _node(workflow, "Execute Approved Actions")
    body = json.loads(
        execute["parameters"]["jsonBody"].split("JSON.stringify(", 1)[1].rsplit(")", 1)[0]
    )
    assert body["actor"] == "stage9-workflow"


def test_the_workflow_reads_only_authorised_work(workflow):
    """Its work set is the view that excludes unauthorised actions."""
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "remediation_pending_execution" in raw
    assert "/remediation/execute-approved" in raw


def test_the_workflow_closes_its_own_ledger_entry_on_failure(workflow):
    """The error handler does not fire in every execution mode.

    A run left at 'running' forever is indistinguishable from one still in
    flight, so the workflow routes its own HTTP failure to a node that closes
    the entry.
    """
    execute = _node(workflow, "Execute Approved Actions")
    assert execute["onError"] == "continueErrorOutput"

    outputs = workflow["connections"]["Execute Approved Actions"]["main"]
    assert len(outputs) == 2, "no error output is wired"
    assert outputs[1][0]["node"] == "Fail Remediation Run"

    fail = _node(workflow, "Fail Remediation Run")
    query_text = fail["parameters"]["query"]
    assert "salesops.ingestion_runs" in query_text
    assert "'failed'" in query_text
    assert "finished_at   = now()" in query_text


def test_every_path_out_of_the_workflow_closes_the_run(workflow):
    """Three terminal nodes, three closed ledger entries."""
    for name in ("Finalize Remediation Run", "Fail Remediation Run",
                 "Close - Nothing Authorized"):
        query_text = _node(workflow, name)["parameters"]["query"]
        assert "UPDATE salesops.ingestion_runs" in query_text, name
        assert "finished_at" in query_text, name


def test_the_workflow_uses_the_shared_run_ledger(workflow):
    open_node = _node(workflow, "Open Remediation Run")
    assert "'remediation-executor'" in open_node["parameters"]["query"]
    assert "'running'" in open_node["parameters"]["query"]


def test_the_workflow_routes_failures_to_the_error_handler(workflow):
    assert workflow["settings"]["errorWorkflow"] == "salesopsErrors001"


def test_the_workflow_calls_the_service_by_name_not_localhost(workflow):
    execute = _node(workflow, "Execute Approved Actions")
    assert execute["parameters"]["url"].startswith("http://analytics-service:8000/")


def test_the_migration_holds_no_secrets():
    migration = REPO_ROOT / "database" / "migrations" / "V011__remediation.sql"
    raw = migration.read_text(encoding="utf-8")
    assert "gsk_" not in raw and "sk-" not in raw
    assert not re.search(r"password\s*[=:]", raw, re.IGNORECASE)


def test_no_secret_shaped_column_exists_in_the_remediation_tables(connection):
    """No Stage 9 column is shaped like somewhere a credential would be kept.

    The pattern is the project's narrow one, and it is narrow on purpose. A bare
    `token` matches `prompt_tokens`; a bare `authorization` matches
    `authorization_current`, which is the audit view's honest answer to "does
    this action's snapshot still match the live decision?". A scan that flags
    those trains people to ignore it, which is worse than not scanning.
    """
    offenders = query(connection, """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'salesops'
          AND starts_with(table_name, 'remediation')
          AND column_name ~* '(api_?key|secret|access_token|auth_token|bearer|credential|password|authorization_header)'
    """)
    assert offenders == []


def _node(workflow: dict, name: str) -> dict:
    for node in workflow["nodes"]:
        if node["name"] == name:
            return node
    raise AssertionError(f"no node named {name!r}")
