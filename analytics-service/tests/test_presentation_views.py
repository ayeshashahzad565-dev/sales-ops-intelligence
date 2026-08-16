"""Stage 11 views against the live warehouse.

These are integration tests by necessity. A presentation layer's whole job is to
report what is actually stored, so a test that mocked the warehouse would be
testing the mock's opinion of the pipeline rather than the pipeline.

Two anomalies are asserted here. The critical one is the collapse bootstrap.sh
injects, and it is named by date - asserting "the worst anomaly is critical"
would keep passing if a change silently moved which day that was. The major one
is discovered, because which ordinary days qualify is a property of the
generator's window rather than of the pipeline.
"""

from __future__ import annotations

import pytest

from analytics import repository
from tests.presentation_fixtures import (
    CRITICAL_DATE,
    dashboards_module,
    ensure_critical_incident,
    major_date,
    make_settings,
    query,
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


@pytest.fixture(scope="module", autouse=True)
def critical_incident(settings, connection):
    """Guarantee the 2026-08-05 chain is complete before anything asserts on it.

    Earlier suites empty the review queue and the action table on their own
    teardown, so by the time this module runs the incident may exist only as a
    Stage 6 decision. Populating is the alternative to skipping, and skipping
    would turn a dozen assertions about an end-to-end chain into a dozen silent
    passes.
    """
    return ensure_critical_incident(settings, connection)


# =============================================================================
# The layer vocabulary
# =============================================================================
def test_the_layers_are_seeded_in_pipeline_order(connection):
    rows = query(connection, """
        SELECT layer_rank, layer_key, evidence_kind, is_model_generated
        FROM salesops.presentation_layers ORDER BY layer_rank
    """)
    assert [r["layer_key"] for r in rows] == [
        "observed_fact", "statistical_signal", "deterministic_decision",
        "model_hypothesis", "human_review", "approved_remediation",
        "completed_remediation", "operational_event",
    ]
    assert [r["layer_rank"] for r in rows] == list(range(1, 9))


def test_exactly_one_layer_is_model_generated(connection):
    """The whole separation rests on this being one layer, not a property
    sprinkled across several."""
    rows = query(connection, """
        SELECT layer_key FROM salesops.presentation_layers WHERE is_model_generated
    """)
    assert [r["layer_key"] for r in rows] == ["model_hypothesis"]


def test_the_model_flag_cannot_disagree_with_the_evidence_kind(connection):
    """A CHECK constraint, not a convention: is_model_generated and
    evidence_kind = 'model_generated' cannot be written apart."""
    with pytest.raises(Exception) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO salesops.presentation_layers
                    (layer_key, layer_rank, layer_label, evidence_kind,
                     is_model_generated, produced_by_stage, source_relations,
                     description)
                VALUES ('sneaky', 99, 'Facts', 'measured', TRUE, 'x',
                        ARRAY['y'], 'z')
            """)
    connection.rollback()
    assert "presentation_layers_model_flag_chk" in str(excinfo.value)


def test_every_presentation_layer_key_used_by_a_view_exists(connection):
    """A view emitting an unknown layer would render with no label at all."""
    unknown = query(connection, """
        SELECT DISTINCT layer_key FROM salesops.incident_timeline
        WHERE layer_key NOT IN (SELECT layer_key FROM salesops.presentation_layers)
        UNION
        SELECT DISTINCT layer_key FROM salesops.anomaly_investigation_detail
        WHERE layer_key NOT IN (SELECT layer_key FROM salesops.presentation_layers)
    """)
    assert unknown == []


# =============================================================================
# Executive views return live data
# =============================================================================
def test_headline_kpis_are_present_and_complete(connection):
    rows = query(connection, "SELECT * FROM salesops.exec_headline_kpis")
    assert len(rows) == 5
    keys = {r["metric_key"] for r in rows}
    assert keys == {
        "net_revenue_usd", "orders_count", "average_order_value_usd",
        "refund_rate", "rolling_28d_net_revenue_usd",
    }
    for row in rows:
        assert row["metric_value"] is not None
        assert row["as_of_date"] is not None


def test_the_headline_day_is_a_complete_day(connection):
    """A day still missing exchange rates has understated revenue. The headline
    figure must never be one of those."""
    row = query(connection, """
        SELECT k.is_complete, k.orders_pending_fx
        FROM salesops.kpi_daily k
        WHERE k.calendar_date = (SELECT as_of_date FROM salesops.exec_headline_kpis LIMIT 1)
    """)[0]
    assert row["is_complete"] is True
    assert row["orders_pending_fx"] == 0


def test_revenue_against_baseline_reproduces_the_stored_deviation(connection):
    """exec_kpi_daily recomputes nothing: the percentage it shows must equal the
    percentage Stage 5 stored, to the rounding both use."""
    rows = query(connection, """
        SELECT e.calendar_date, e.revenue_vs_baseline_pct, a.revenue_deviation_pct
        FROM salesops.exec_kpi_daily e
        JOIN salesops.anomaly_daily a ON a.calendar_date = e.calendar_date
        WHERE e.revenue_vs_baseline_pct IS NOT NULL
          AND a.revenue_deviation_pct IS NOT NULL
    """)
    assert rows
    mismatched = [
        r for r in rows
        if abs(float(r["revenue_vs_baseline_pct"]) - float(r["revenue_deviation_pct"])) > 0.02
    ]
    assert not mismatched, f"{len(mismatched)} day(s) disagree with Stage 5"


def test_every_severity_appears_in_the_summary_even_at_zero(connection):
    rows = query(connection, "SELECT * FROM salesops.exec_anomaly_severity_summary")
    assert {r["severity"] for r in rows} == {"critical", "major", "minor", "none"}


def test_actionable_anomalies_are_stage_sixs_own_decision(connection):
    """'Actionable' is a stored column, not a severity filter applied here."""
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.exec_actionable_anomalies
        WHERE decision <> 'action_required'
    """)
    assert rows[0]["n"] == 0

    # count(DISTINCT anomaly_id), not count(*): re-deciding under a new version
    # keeps the old row, and the view deliberately shows the newest one only.
    counts = query(connection, """
        SELECT
          (SELECT count(*) FROM salesops.exec_actionable_anomalies) AS shown,
          (SELECT count(DISTINCT anomaly_id) FROM salesops.anomaly_decisions
            WHERE decision = 'action_required') AS stored
    """)[0]
    assert counts["shown"] == counts["stored"]


def test_the_timeline_shows_only_days_stage_five_flagged(connection):
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.exec_anomaly_timeline t
        JOIN salesops.anomaly_daily a ON a.anomaly_id = t.anomaly_id
        WHERE NOT a.is_anomaly
    """)
    assert rows[0]["n"] == 0


# =============================================================================
# The 2026-08-05 incident, end to end
# =============================================================================
def test_the_injected_incident_is_still_critical(connection):
    row = query(connection, """
        SELECT decision_severity, decision_routing, decision_outcome,
               decision_primary_reason, decision_human_review_required
        FROM salesops.anomaly_investigation WHERE calendar_date = %(d)s
    """, {"d": CRITICAL_DATE})
    assert len(row) == 1, f"no investigation row for {CRITICAL_DATE}"
    assert row[0]["decision_severity"] == "critical"
    assert row[0]["decision_routing"] == "human_review"
    assert row[0]["decision_outcome"] == "action_required"
    assert row[0]["decision_human_review_required"] is True


def test_the_major_anomaly_is_still_major(connection):
    """A major is routed to a human too, but is not the injected critical."""
    major = major_date(connection)
    row = query(connection, """
        SELECT decision_severity, decision_routing
        FROM salesops.anomaly_investigation WHERE calendar_date = %(d)s
    """, {"d": major})
    assert len(row) == 1, f"no investigation row for {major}"
    assert row[0]["decision_severity"] == "major"
    assert row[0]["decision_routing"] == "human_review"
    assert major != CRITICAL_DATE


def test_the_incident_reads_in_layer_order(connection):
    """The drill-down's order is its argument. Facts before statistics before
    the decision before the model."""
    rows = query(connection, """
        SELECT layer_rank, layer_key, line_rank
        FROM salesops.anomaly_investigation_detail
        WHERE calendar_date = %(d)s
        ORDER BY layer_rank, line_rank
    """, {"d": CRITICAL_DATE})
    assert rows

    ranks = [r["layer_rank"] for r in rows]
    assert ranks == sorted(ranks)

    first_seen = {}
    for row in rows:
        first_seen.setdefault(row["layer_key"], row["layer_rank"])
    assert first_seen["observed_fact"] < first_seen["statistical_signal"]
    assert first_seen["statistical_signal"] < first_seen["deterministic_decision"]
    assert first_seen["deterministic_decision"] < first_seen["model_hypothesis"]


def test_every_model_line_is_flagged_as_model_generated(connection):
    """And every line that is not from the model is not flagged."""
    rows = query(connection, """
        SELECT layer_key, is_model_generated
        FROM salesops.anomaly_investigation_detail
        WHERE calendar_date = %(d)s
    """, {"d": CRITICAL_DATE})
    assert rows
    for row in rows:
        assert row["is_model_generated"] == (row["layer_key"] == "model_hypothesis")


def test_the_hypothesis_is_never_reported_as_verified(connection):
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.anomaly_investigation
        WHERE llm_verified IS DISTINCT FROM FALSE
    """)
    assert rows[0]["n"] == 0


def test_the_incident_chain_runs_orders_to_operational_outcome(connection):
    rows = query(connection, """
        SELECT step_rank, step, layer_key, reached
        FROM salesops.incident_timeline
        WHERE calendar_date = %(d)s ORDER BY step_rank
    """, {"d": CRITICAL_DATE})
    assert [r["step"] for r in rows] == [
        "orders", "kpi", "anomaly", "decision", "hypothesis",
        "notification", "review", "remediation", "execution",
        "operational_outcome",
    ]


def test_the_incident_reached_execution(connection):
    """Detection through remediation, as the specification asks. Each of these
    is a state a different stage owns."""
    by_step = {
        r["step"]: r for r in query(connection, """
            SELECT step, reached, actor, summary FROM salesops.incident_timeline
            WHERE calendar_date = %(d)s
        """, {"d": CRITICAL_DATE})
    }
    for step in ("orders", "kpi", "anomaly", "decision", "hypothesis",
                 "review", "remediation", "execution"):
        assert by_step[step]["reached"] is True, f"{step} was never reached"

    assert by_step["review"]["actor"], "the review has no named human"
    assert by_step["execution"]["actor"], "the execution has no named actor"


def test_a_step_that_did_not_happen_says_why(connection):
    """A chain that stopped must look like a decision, not like missing data."""
    rows = query(connection, """
        SELECT step, reached, summary FROM salesops.incident_timeline
        WHERE NOT reached AND (summary IS NULL OR summary = '')
    """)
    assert rows == []


def test_the_critical_incident_was_never_auto_notified(connection):
    """Stage 6 routed it to a human. A notification would mean the routing
    column was ignored somewhere."""
    row = query(connection, """
        SELECT reached, summary FROM salesops.incident_timeline
        WHERE calendar_date = %(d)s AND step = 'notification'
    """, {"d": CRITICAL_DATE})[0]
    assert row["reached"] is False
    assert "human_review" in row["summary"]


# =============================================================================
# Operational views
# =============================================================================
def test_health_keeps_stage_tens_vocabulary(connection):
    rows = query(connection, """
        SELECT DISTINCT health_status FROM salesops.exec_pipeline_health
    """)
    assert {r["health_status"] for r in rows} <= {
        "healthy", "warning", "degraded", "failed"
    }


def test_pipeline_health_never_borrows_an_anomaly_severity(connection):
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.exec_pipeline_health
        WHERE health_status IN ('none', 'minor', 'major', 'critical')
    """)
    assert rows[0]["n"] == 0


def test_the_worst_component_sorts_first(connection):
    rows = query(connection, """
        SELECT health_status, status_rank FROM salesops.exec_pipeline_health
    """)
    expected = {"failed": 1, "degraded": 2, "warning": 3, "healthy": 4}
    for row in rows:
        assert row["status_rank"] == expected[row["health_status"]]


def test_every_pipeline_reports_its_last_success_separately(connection):
    """The question an operator asks about a failing pipeline is when it last
    worked, which is not the same row as its latest run."""
    rows = query(connection, "SELECT * FROM salesops.ops_pipeline_runs")
    assert rows
    for row in rows:
        assert row["pipeline"]
        assert "latest_success_finished_at" in row
        assert "latest_run_status" in row


def test_attention_items_have_one_shape(connection):
    rows = query(connection, "SELECT * FROM salesops.ops_attention_items")
    for row in rows:
        assert row["entity_type"]
        assert row["disposition"]
        assert row["failure_reason"]


def test_only_reviews_carry_an_ageing_bucket(connection):
    """Ageing is a property of a queue item waiting for a person. A failed run
    does not have one, and inventing one would put a review vocabulary on a
    pipeline object."""
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.ops_attention_items
        WHERE ageing_bucket IS NOT NULL AND entity_type <> 'review'
    """)
    assert rows[0]["n"] == 0


# =============================================================================
# Audit
# =============================================================================
#: Every stream the unified audit view is meant to carry. Five of them have rows
#: on any warehouse the pipeline has run against. The sixth does not: an
#: operational event is only ever written when something went wrong - a run
#: abandoned mid-flight, an execution stranded, a batch replayed - so an empty
#: operational log is what a healthy warehouse looks like.
AUDIT_STREAMS = frozenset({
    "decision", "hypothesis", "notification", "review", "remediation",
    "operational",
})
ALWAYS_POPULATED_STREAMS = AUDIT_STREAMS - {"operational"}


def test_the_audit_view_carries_all_six_streams(connection):
    """Asserted against the view's definition, not its current contents.

    Requiring a row in every stream would make this pass only on a warehouse
    that had already suffered an incident, and the obvious way to satisfy it -
    manufacturing an operational event - is not available: the log is
    append-only by design, so a fixture could create the row and never remove
    it, and it would then show up on the operational dashboard as an incident
    that never happened.
    """
    definition = query(connection, """
        SELECT pg_get_viewdef('salesops.audit_event_stream'::regclass) AS sql
    """)[0]["sql"]
    missing = [s for s in AUDIT_STREAMS if f"'{s}'" not in definition]
    assert not missing, f"audit_event_stream has no branch for: {missing}"


def test_every_audit_row_belongs_to_a_known_stream(connection):
    """The other half: nothing appears that the view was not built to carry."""
    rows = query(connection, "SELECT DISTINCT stream FROM salesops.audit_event_stream")
    present = {r["stream"] for r in rows}
    assert present <= AUDIT_STREAMS, f"unknown stream(s): {present - AUDIT_STREAMS}"
    assert present >= ALWAYS_POPULATED_STREAMS, (
        f"the pipeline has run, so these should have history: "
        f"{ALWAYS_POPULATED_STREAMS - present}"
    )


def test_every_audit_row_has_an_actor_and_a_time(connection):
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.audit_event_stream
        WHERE actor IS NULL OR actor = '' OR occurred_at IS NULL
    """)
    assert rows[0]["n"] == 0


def test_every_transition_records_where_it_ended_up(connection):
    """Every event that IS a transition names the state it reached.

    Not every operational event is a transition - a retention purge and a
    maintenance summary are occurrences, and they carry neither end. Those are
    allowed to have no to_state precisely because they have no from_state
    either; a row with one and not the other would be a transition with a
    missing half, which is what this actually guards against.
    """
    rows = query(connection, """
        SELECT stream, event_type, from_state, to_state
        FROM salesops.audit_event_stream
        WHERE to_state IS NULL
    """)
    for row in rows:
        assert row["stream"] == "operational", row
        assert row["from_state"] is None, row


def test_the_audit_stream_loses_no_transition(connection):
    """The union must be complete. A stream that quietly dropped rows would make
    the audit trail look tidier than the system actually is."""
    counts = query(connection, """
        SELECT
          (SELECT count(*) FROM salesops.review_events)      AS review_events,
          (SELECT count(*) FROM salesops.remediation_events) AS remediation_events,
          (SELECT count(*) FROM salesops.operational_events) AS operational_events,
          (SELECT count(*) FROM salesops.notification_attempts) AS attempts,
          (SELECT count(*) FROM salesops.anomaly_decisions)  AS decisions,
          (SELECT count(*) FROM salesops.anomaly_hypotheses) AS hypotheses,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='review') AS s_review,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='remediation') AS s_rem,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='operational') AS s_ops,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='notification') AS s_notif,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='decision') AS s_dec,
          (SELECT count(*) FROM salesops.audit_event_stream WHERE stream='hypothesis') AS s_hyp
    """)[0]
    assert counts["s_review"] == counts["review_events"]
    assert counts["s_rem"] == counts["remediation_events"]
    assert counts["s_ops"] == counts["operational_events"]
    assert counts["s_notif"] == counts["attempts"]
    assert counts["s_dec"] == counts["decisions"]
    assert counts["s_hyp"] == counts["hypotheses"]


def test_machine_events_are_not_attributed_to_a_person(connection):
    """Stage 6 and Stage 7 are machines. Naming a person as the actor of a
    threshold comparison would be a fabricated attribution."""
    rows = query(connection, """
        SELECT DISTINCT actor FROM salesops.audit_event_stream
        WHERE stream IN ('decision', 'hypothesis')
    """)
    actors = {r["actor"] for r in rows}
    assert actors
    assert all("@" not in actor for actor in actors), actors


def test_human_transitions_keep_the_name_that_made_them(connection):
    rows = query(connection, """
        SELECT to_state, actor FROM salesops.audit_event_stream
        WHERE stream = 'remediation' AND to_state IN ('approved', 'executed')
    """)
    assert rows, "no authorised or executed remediation to check"
    for row in rows:
        assert row["actor"] not in (None, "", "unattributed")


def test_the_audit_stream_preserves_version_information(connection):
    """Section 4: version/snapshot information already stored must survive."""
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.audit_event_stream
        WHERE stream IN ('decision', 'review', 'remediation')
          AND (version_info IS NULL OR version_info = '')
    """)
    assert rows[0]["n"] == 0


def test_operational_events_claim_no_version_they_do_not_have(connection):
    """A recovery happens under no policy version. Filling the column would put
    a value in the audit trail that nothing recorded."""
    rows = query(connection, """
        SELECT count(*) AS n FROM salesops.audit_event_stream
        WHERE stream = 'operational' AND version_info IS NOT NULL
    """)
    assert rows[0]["n"] == 0


# =============================================================================
# Model output is confined
# =============================================================================
def test_no_executive_view_exposes_model_text(connection):
    """The rule with one definition: the columns named in the dashboard
    catalogue may not appear in any exec_ view, under any alias.

    Checked against information_schema rather than by reading the SQL, so an
    aliased column is caught as readily as a copied one.
    """
    catalogue = dashboards_module()
    rows = query(connection, """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'salesops' AND left(table_name, 5) = 'exec_'
    """)
    assert rows
    leaked = [
        f"{r['table_name']}.{r['column_name']}" for r in rows
        if r["column_name"] in catalogue.LLM_TEXT_COLUMNS
    ]
    assert not leaked, leaked


def test_no_executive_view_reads_the_hypothesis_table_for_its_text(connection):
    """exec_kpi_daily and exec_actionable_anomalies join anomaly_hypotheses to
    answer 'does one exist'. They must select nothing else from it."""
    for view in ("exec_kpi_daily", "exec_actionable_anomalies",
                 "exec_anomaly_timeline"):
        definition = query(connection, """
            SELECT pg_get_viewdef(%(v)s::regclass, TRUE) AS d
        """, {"v": f"salesops.{view}"})[0]["d"]
        for column in ("summary", "primary_hypothesis", "supporting_evidence",
                       "alternative_hypotheses", "recommended_checks"):
            assert f"h.{column}" not in definition, f"{view} selects h.{column}"


def test_the_investigation_view_labels_every_model_column(connection):
    """Model output is allowed there, but only under the llm_ prefix - the
    prefix is what a renderer keys off."""
    rows = query(connection, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='salesops' AND table_name='anomaly_investigation'
    """)
    names = {r["column_name"] for r in rows}
    catalogue = dashboards_module()
    for column in catalogue.LLM_TEXT_COLUMNS:
        assert column in names, f"{column} is missing from anomaly_investigation"
        assert column.startswith("llm_")


def test_health_never_reads_model_output(connection):
    """Carried forward from Stage 10 and re-asserted for the projection: a
    language model must not be able to influence whether the pipeline reports
    itself healthy."""
    definition = query(connection, """
        SELECT pg_get_viewdef('salesops.exec_pipeline_health'::regclass, TRUE) AS d
    """)[0]["d"]
    assert "anomaly_hypotheses" not in definition
