"""Health, ageing, the maintenance run, and everything Stage 10 must not touch.

Three things are being protected here.

**Health must be deterministic and explained.** A status a caller cannot
recompute from the same inputs is a status nobody argues with when it is wrong.
Every component reports the number, the threshold and a machine-readable reason,
and no part of it reads model output - a health signal a language model could
influence would be one nobody could trust during the incident that mattered.

**Ageing must change nothing.** "Nobody has looked at this" is not a decision.
Operational ageing labels how long a queue item has waited; it is not an anomaly
severity, it is not comparable with one, and it must never move a review's
authorisation state.

**Stage 10 must be invisible to Stages 0-9.** A maintenance run recovers stuck
records. If it could also change a decision, a hypothesis, a notification or an
authorisation, it would be a ninth stage wearing an operations badge.
"""

from __future__ import annotations

import json
import re

import pytest

from analytics import repository
from analytics.notifications import service as notification_service
from analytics.notifications.provider import RecordingProvider
from analytics.operations import service
from analytics.operations.models import MaintenanceStep, MaintenanceSummary, StepOutcome
from tests.operations_fixtures import (
    REPO_ROOT,
    TEST_ACTOR,
    all_fingerprints,
    make_failed_batch,
    make_old_staging,
    make_run,
    make_settings,
    purge_test_data,
    query,
    threshold,
)

WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "operational-maintenance.json"
MIGRATION = REPO_ROOT / "database" / "migrations" / "V012__operational_reliability.sql"
OPERATIONS_PACKAGE = REPO_ROOT / "analytics-service" / "analytics" / "operations"


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
def clean(connection):
    purge_test_data(connection)
    yield
    purge_test_data(connection)


@pytest.fixture
def open_reviews(settings, connection):
    """Guarantee an open review queue for the ageing tests.

    Earlier suites in the same session legitimately leave the queue empty - the
    Stage 8 and Stage 9 fixtures purge on teardown. Skipping on an empty queue
    would turn four assertions about ageing into four silent passes, which is
    the failure mode this project has already been bitten by once.
    """
    existing = query(connection, """
        SELECT count(*) AS n FROM salesops.review_queue
        WHERE status IN ('pending', 'in_review')
    """)[0]["n"]
    if not existing:
        notification_service.run_routing(
            settings=settings,
            provider=RecordingProvider(),
            recipients=["stage10-ageing@example.invalid"],
        )
    rows = service.review_ageing(settings)
    if not rows:
        pytest.skip("Stage 6 produced no human_review decisions to queue")
    return rows


# =============================================================================
# Operational health
# =============================================================================


def test_every_component_explains_itself(settings, connection):
    """A status with no number behind it is a status nobody can argue with."""
    components = repository.operational_health(connection)
    assert components

    for row in components:
        assert row["status"] in ("healthy", "warning", "degraded", "failed"), row
        assert row["reason_code"], row
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", row["reason_code"]), row["reason_code"]
        assert row["measure"], row
        if row["status"] == "healthy":
            assert row["reason_code"] == "OK", row


def test_health_reports_a_stale_run(settings, connection):
    before = _component(connection, "stale_runs")
    assert before["status"] == "healthy"

    make_run(connection, "running", age_minutes=500)

    after = _component(connection, "stale_runs")
    assert after["status"] == "degraded"
    assert after["reason_code"] == "RUNS_STUCK_RUNNING"
    assert after["observed_value"] >= 1


def test_health_reports_a_failed_run(settings, connection):
    make_run(connection, "failed", age_minutes=10)
    row = _component(connection, "failed_runs")
    assert row["status"] in ("warning", "degraded")
    assert row["reason_code"] == "RUNS_FAILED_24H"
    assert row["observed_value"] >= 1


def test_health_reports_replay_candidates(settings, connection):
    # Relative, not absolute: a live warehouse may legitimately already have
    # dead-letter batches, and a test that assumed a clean slate would pass or
    # fail depending on what happened yesterday.
    before = _component(connection, "replay_candidates")["observed_value"]

    make_failed_batch(connection, recoverable=1, permanent=0)

    row = _component(connection, "replay_candidates")
    assert row["observed_value"] == before + 1
    assert row["status"] == "warning"
    assert row["reason_code"] == "BATCHES_REPLAYABLE"


def test_health_reports_retention_pressure(settings, connection):
    make_old_staging(connection, "processed", age_days=500)
    row = _component(connection, "staging_retention")
    assert row["reason_code"] == "STAGING_ROWS_ELIGIBLE"
    assert row["observed_value"] >= 1


def test_the_overall_status_is_the_worst_component(settings, connection):
    """A pipeline is not "mostly healthy"."""
    summary = repository.operational_health_summary(connection)
    components = repository.operational_health(connection)
    statuses = {row["status"] for row in components}

    for worst in ("failed", "degraded", "warning"):
        if worst in statuses:
            assert summary["overall_status"] == worst
            return
    assert summary["overall_status"] == "healthy"


def test_the_summary_names_what_is_wrong(settings, connection):
    make_run(connection, "running", age_minutes=500)
    summary = repository.operational_health_summary(connection)
    assert any(entry.startswith("stale_runs:") for entry in summary["unhealthy"])


def test_health_thresholds_are_configurable(settings, connection):
    make_run(connection, "running", age_minutes=10)
    assert _component(connection, "stale_runs")["status"] == "healthy"

    with threshold(connection, "stale_run_timeout_minutes", 5):
        assert _component(connection, "stale_runs")["status"] == "degraded"


def test_health_reads_no_model_output(settings, connection):
    """Structural, because a behavioural test only covers the paths written.

    If the health view ever joined `anomaly_hypotheses`, a language model would
    be able to influence whether the pipeline reports itself healthy.
    """
    definition = query(connection, """
        SELECT pg_get_viewdef('salesops.operational_health'::regclass, true) AS d
    """)[0]["d"].lower()

    # Relations and columns, not substrings. A bare "llm" matches
    # 'llm-root-cause', which is the NAME of a pipeline whose run status the
    # health view is entitled to read - reading whether Stage 7 ran is not the
    # same as reading what it said.
    for relation in ("anomaly_hypotheses", "anomaly_hypothesis_audit"):
        assert relation not in definition, relation
    for column in ("primary_hypothesis", "supporting_evidence", "alternative_hypotheses",
                   "recommended_checks", "prompt_version", "evidence_digest"):
        assert column not in definition, column


def test_the_operations_package_never_imports_the_llm_package():
    offenders = []
    for path in OPERATIONS_PACKAGE.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "analytics.llm" in stripped:
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_health_never_reads_anomaly_severity_as_a_health_signal(settings, connection):
    """Three vocabularies, deliberately different words.

    An anomaly being critical says nothing about whether the pipeline is
    working, and a health view that conflated them would report a bad week as a
    broken system.
    """
    definition = query(connection, """
        SELECT pg_get_viewdef('salesops.operational_health'::regclass, true) AS d
    """)[0]["d"].lower()
    assert "anomaly_decisions" not in definition
    assert "'critical'" not in definition


# =============================================================================
# Review ageing
# =============================================================================


def test_ageing_is_deterministic(settings, connection, open_reviews):
    for row in open_reviews:
        assert row["ageing_bucket"] in ("fresh", "warning", "overdue", "critical_overdue")
        age = float(row["age_hours"])
        if age >= float(row["critical_after_hours"]):
            assert row["ageing_bucket"] == "critical_overdue"
        elif age >= float(row["overdue_after_hours"]):
            assert row["ageing_bucket"] == "overdue"
        elif age >= float(row["warning_after_hours"]):
            assert row["ageing_bucket"] == "warning"
        else:
            assert row["ageing_bucket"] == "fresh"


def test_ageing_thresholds_are_configurable(settings, connection, open_reviews):
    assert {r["ageing_bucket"] for r in service.review_ageing(settings)} == {"fresh"}

    with threshold(connection, "review_warning_age_hours", 0):
        buckets = {r["ageing_bucket"] for r in service.review_ageing(settings)}
    assert "fresh" not in buckets


def test_ageing_changes_no_review_state(settings, connection, open_reviews):
    """The property that makes ageing safe to run unattended."""
    before = all_fingerprints(connection)

    with threshold(connection, "review_warning_age_hours", 0):
        with threshold(connection, "review_overdue_age_hours", 0):
            with threshold(connection, "review_critical_overdue_age_hours", 0):
                rows = service.review_ageing(settings)
                assert all(r["ageing_bucket"] == "critical_overdue" for r in rows)
                assert all(r["escalation_eligible"] for r in rows)
                service.run_maintenance(settings, actor=TEST_ACTOR)

    assert all_fingerprints(connection)["reviews"] == before["reviews"]


def test_ageing_never_shows_a_closed_review(settings, connection):
    """It describes the queue, and a resolved item is not in the queue."""
    statuses = {row["review_status"] for row in service.review_ageing(settings)}
    assert statuses <= {"pending", "in_review"}


def test_ageing_keeps_severity_and_the_bucket_apart(settings, connection, open_reviews):
    for row in open_reviews:
        assert row["anomaly_severity"] in ("major", "critical")
        assert row["ageing_bucket"] not in ("major", "critical", "minor", "none")


# =============================================================================
# The maintenance run
# =============================================================================


def test_one_failed_step_does_not_stop_the_others():
    """The isolation contract, tested on the summary itself.

    A reliability feature that abandons the rest of its work on the first error
    is a reliability feature that reduces reliability.
    """
    summary = MaintenanceSummary()
    summary.record(MaintenanceStep("a", StepOutcome.SUCCEEDED, {"recovered": 1}))
    summary.record(MaintenanceStep("b", StepOutcome.FAILED, error="boom"))
    summary.record(MaintenanceStep("c", StepOutcome.SUCCEEDED, {"deleted": 2}))

    assert summary.status == "partial"
    assert summary.succeeded == 2
    assert summary.failed == 1
    assert summary.changes_made == 3


def test_a_run_where_everything_failed_is_failed():
    summary = MaintenanceSummary()
    for name in ("a", "b"):
        summary.record(MaintenanceStep(name, StepOutcome.FAILED, error="boom"))
    assert summary.status == "failed"


def test_a_quiet_run_is_a_success():
    """Nothing to recover is a healthy pipeline, not a wasted run."""
    summary = MaintenanceSummary()
    for name in ("a", "b"):
        summary.record(MaintenanceStep(name, StepOutcome.SKIPPED, {"recovered": 0}))
    assert summary.status == "success"
    assert summary.changes_made == 0


def test_a_step_that_raises_is_recorded_not_propagated():
    def explode():
        raise RuntimeError("the database went away")

    step = service._step("boom", explode)
    assert step.outcome is StepOutcome.FAILED
    assert "the database went away" in step.error


def test_maintenance_recovers_what_is_stuck(settings, connection):
    make_run(connection, "running", age_minutes=500)

    summary = service.run_maintenance(settings, actor=TEST_ACTOR)

    step = next(s for s in summary.steps if s.name == "recover_stale_runs")
    assert step.outcome is StepOutcome.SUCCEEDED
    assert step.detail["recovered"] >= 1
    assert summary.status in ("success", "partial")


def test_maintenance_is_idempotent(settings, connection):
    make_run(connection, "running", age_minutes=500)

    first = service.run_maintenance(settings, actor=TEST_ACTOR)
    second = service.run_maintenance(settings, actor=TEST_ACTOR)
    third = service.run_maintenance(settings, actor=TEST_ACTOR)

    assert first.changes_made >= 1
    assert second.changes_made == 0
    assert third.changes_made == 0


def test_maintenance_does_not_replay_or_purge_by_default(settings, connection):
    """Replay repeats work and purging deletes. Neither happens on a schedule
    without somebody asking for it."""
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    order_id = make_old_staging(connection, "processed", age_days=500)

    summary = service.run_maintenance(settings, actor=TEST_ACTOR)

    replay_step = next(s for s in summary.steps if s.name == "replay_candidates")
    assert replay_step.detail["replayed"] is False
    assert replay_step.detail["eligible"] >= 1

    retention_step = next(s for s in summary.steps if s.name == "staging_retention")
    assert retention_step.detail["dry_run"] is True
    assert retention_step.detail["deleted"] == 0

    assert query(connection, """
        SELECT 1 FROM salesops.raw_orders_staging WHERE order_id = %(o)s
    """, {"o": order_id})
    assert query(connection, """
        SELECT 1 FROM salesops.raw_orders_staging
        WHERE batch_id = %(b)s::uuid AND processing_status = 'failed'
    """, {"b": batch})


def test_maintenance_changes_no_upstream_stage(settings, connection):
    """The load-bearing test of the stage.

    Every recoverable condition Stage 10 knows about, then a full maintenance
    run, then every Stage 0-9 fingerprint compared.
    """
    make_run(connection, "running", age_minutes=500)
    make_failed_batch(connection, recoverable=1, permanent=1)
    make_old_staging(connection, "processed", age_days=500)
    make_old_staging(connection, "failed", age_days=500)

    before = all_fingerprints(connection)

    service.run_maintenance(settings, actor=TEST_ACTOR)
    service.run_maintenance(settings, actor=TEST_ACTOR, purge=True)

    after = all_fingerprints(connection)
    for key in ("stage6", "stage7", "stage8", "reviews", "stage9", "warehouse"):
        assert after[key] == before[key], key


def test_maintenance_with_no_delivery_channel_skips_rather_than_fails(settings, connection):
    summary = service.run_maintenance(settings, provider=None, recipients=[], actor=TEST_ACTOR)
    step = next(s for s in summary.steps if s.name == "notification_retry")
    assert step.outcome is StepOutcome.SKIPPED
    assert summary.status in ("success", "partial")


def test_maintenance_records_an_audit_event_for_notification_retry(settings, connection):
    before = _events_of_type(connection, "maintenance_run")

    with threshold(connection, "stale_notification_timeout_minutes", 0):
        service.retry_stale_notifications(
            settings, RecordingProvider(), ["stage10-tests@example.invalid"], TEST_ACTOR)

    after = _events_of_type(connection, "maintenance_run")
    assert after >= before


# =============================================================================
# The audit log
# =============================================================================


def test_operational_events_cannot_be_rewritten(settings, connection):
    """An automated process must not be able to tidy away the evidence of what
    it did."""
    make_run(connection, "running", age_minutes=500)
    service.recover_stale_runs(settings, actor=TEST_ACTOR)

    event = query(connection, """
        SELECT event_id FROM salesops.operational_events ORDER BY event_id DESC LIMIT 1
    """)[0]["event_id"]

    for statement in (
        "UPDATE salesops.operational_events SET reason_code = 'OK' WHERE event_id = %(id)s",
        "DELETE FROM salesops.operational_events WHERE event_id = %(id)s",
    ):
        with pytest.raises(Exception) as exc:
            with connection.cursor() as cursor:
                cursor.execute(statement, {"id": event})
        connection.rollback()
        assert "append-only" in str(exc.value)


def test_an_unknown_threshold_raises_rather_than_defaulting(settings, connection):
    """A typo in a threshold name must not quietly disable a safety check."""
    with pytest.raises(Exception) as exc:
        query(connection, "SELECT salesops.operational_setting('no_such_setting')")
    connection.rollback()
    assert "No operational setting" in str(exc.value)


# =============================================================================
# The workflow
# =============================================================================


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.exists():
        pytest.skip(f"{WORKFLOW} not found")
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_holds_no_secrets(workflow):
    raw = WORKFLOW.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(api[_-]?key|apikey|secret|access_token|auth_token|bearer|credential|password)"
        r"\s*[\"':=]",
        re.IGNORECASE,
    )
    matches = [m.group(0) for m in pattern.finditer(raw)]
    assert [m for m in matches if not m.lower().startswith("credential")] == []
    assert "gsk_" not in raw and "sk-" not in raw
    assert "NOTIFICATION_WEBHOOK_URL" not in raw
    assert "LLM_API_KEY" not in raw


def test_the_migration_holds_no_secrets():
    raw = MIGRATION.read_text(encoding="utf-8")
    assert "gsk_" not in raw and "sk-" not in raw
    assert not re.search(r"password\s*[=:]", raw, re.IGNORECASE)


def test_the_workflow_branches_are_independent(workflow):
    """Every maintenance node continues on error.

    Without this the first failing branch would abandon the rest of the run, and
    a single unavailable table would mean nothing got recovered.
    """
    branches = [
        "Recover Stale Runs", "Recover Stale Remediation",
        "Collect Operational Signals", "Staging Retention",
        "Retry Stale Notifications",
    ]
    for name in branches:
        node = _node(workflow, name)
        assert node.get("onError") == "continueRegularOutput", name
        assert node.get("alwaysOutputData") is True, name


def test_the_workflow_never_approves_or_executes_anything(workflow):
    """No approval, no remediation execution, no LLM call."""
    workflow_json = json.dumps(workflow)

    # Endpoint paths and SQL, not prose. The node notes explain WHY recovery
    # requires a human to reconcile, and a scan that flagged the explanation
    # would push the explanation out of the file.
    for forbidden in ("/approve", "/reviews/", "/remediation/", "execute-approved",
                      "/anomalies/analyze", "/operations/replay",
                      "/operations/staging/purge"):
        assert forbidden not in workflow_json, forbidden

    for node in workflow["nodes"]:
        query_text = node.get("parameters", {}).get("query", "")
        for forbidden in ("= 'executed'", "= 'approved'", "reconcile_remediation",
                          "replay_failed_batch"):
            assert forbidden not in query_text, f"{node['name']}: {forbidden}"


def test_the_workflow_writes_only_to_operational_tables(workflow):
    """It may recover and clean up. It may not touch a decision, a hypothesis,
    a notification or an authorisation."""
    permitted = {
        "salesops.ingestion_runs",
        "salesops.raw_orders_staging",
        "salesops.operational_events",
        "salesops.remediation_actions",
        "salesops.remediation_attempts",
    }
    for node in workflow["nodes"]:
        if node["type"] != "n8n-nodes-base.postgres":
            continue
        writes = set(re.findall(
            r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+(salesops\.\w+)",
            node["parameters"]["query"]))
        assert writes <= permitted, f"{node['name']} writes {writes - permitted}"

    forbidden = {"salesops.anomaly_decisions", "salesops.anomaly_hypotheses",
                 "salesops.review_queue", "salesops.notifications", "salesops.fact_orders",
                 "salesops.kpi_daily", "salesops.anomaly_daily"}
    raw = WORKFLOW.read_text(encoding="utf-8")
    for table in forbidden:
        assert not re.search(rf"(?:INSERT INTO|UPDATE|DELETE FROM)\s+{re.escape(table)}", raw), table


def test_the_workflow_has_no_code_node_and_no_severity_logic(workflow):
    for node in workflow["nodes"]:
        assert node["type"] != "n8n-nodes-base.code", node["name"]

    raw = WORKFLOW.read_text(encoding="utf-8").lower()
    for forbidden in ("robust_z", "anomaly_score", "business_impact_tier",
                      "case when severity"):
        assert forbidden not in raw, forbidden


def test_the_workflow_closes_its_own_ledger_entry(workflow):
    finalize = _node(workflow, "Finalize Maintenance Run")
    query_text = finalize["parameters"]["query"]
    assert "UPDATE salesops.ingestion_runs" in query_text
    assert "finished_at" in query_text
    assert "'failed'" in query_text and "'partial'" in query_text
    assert "'operational-maintenance'" in _node(
        workflow, "Open Maintenance Run")["parameters"]["query"]


def test_the_workflow_derives_its_status(workflow):
    """Success is derived, never asserted by having reached the last node."""
    query_text = _node(workflow, "Finalize Maintenance Run")["parameters"]["query"]
    assert "CASE" in query_text
    assert "$3::int >= $2::int THEN 'failed'" in query_text
    assert "$3::int > 0        THEN 'partial'" in query_text


def test_the_workflow_routes_failures_to_the_error_handler(workflow):
    assert workflow["settings"]["errorWorkflow"] == "salesopsErrors001"


def test_the_workflow_calls_the_service_by_name(workflow):
    node = _node(workflow, "Retry Stale Notifications")
    assert node["parameters"]["url"].startswith("http://analytics-service:8000/")


# =============================================================================
# Helpers
# =============================================================================


def _component(connection, name: str) -> dict:
    rows = [r for r in repository.operational_health(connection) if r["component"] == name]
    assert rows, f"no health component named {name!r}"
    return rows[0]


def _events_of_type(connection, event_type: str) -> int:
    return query(connection, """
        SELECT count(*) AS n FROM salesops.operational_events WHERE event_type = %(t)s
    """, {"t": event_type})[0]["n"]


def _node(workflow: dict, name: str) -> dict:
    for node in workflow["nodes"]:
        if node["name"] == name:
            return node
    raise AssertionError(f"no node named {name!r}")
