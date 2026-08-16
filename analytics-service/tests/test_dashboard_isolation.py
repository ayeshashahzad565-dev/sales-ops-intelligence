"""Stage 11 must be able to see everything and change nothing.

Two halves.

**The role.** salesops_readonly is asked to write - to a fact table, to a
decision, to an approval, to the audit log - and PostgreSQL must refuse every
time. It is also asked to call the functions that recover, replay and purge,
because a VOLATILE function is a write path that a SELECT box can reach. These
tests connect as that role rather than reasoning about the grants, because a
grant that looks right and is not is exactly the failure worth catching.

**The data.** Every Stage 6-10 fingerprint is taken, every presentation query in
the catalogue is executed, and the fingerprints are taken again. Reading is
allowed to be slow, expensive or wrong. It is not allowed to leave a mark.
"""

from __future__ import annotations

import re

import psycopg
import pytest

from analytics import repository
from tests.operations_fixtures import all_fingerprints
from tests.presentation_fixtures import (
    CRITICAL_DATE,
    REPO_ROOT,
    dashboards_module,
    ensure_critical_incident,
    major_date,
    make_settings,
    query,
    readonly_settings,
)

#: One write per table that Section 5 names, plus the audit log. Each is the
#: cheapest statement that would do damage if it succeeded.
FORBIDDEN_WRITES = [
    ("fact_orders", "DELETE FROM salesops.fact_orders"),
    ("exchange_rates", "DELETE FROM salesops.exchange_rates"),
    ("kpi_daily", "UPDATE salesops.kpi_daily SET net_revenue_usd = 0"),
    ("anomaly_daily", "UPDATE salesops.anomaly_daily SET is_anomaly = FALSE"),
    ("anomaly_decisions", "UPDATE salesops.anomaly_decisions SET severity = 'none'"),
    ("anomaly_hypotheses", "DELETE FROM salesops.anomaly_hypotheses"),
    ("notifications", "UPDATE salesops.notifications SET status = 'sent'"),
    ("review_queue", "UPDATE salesops.review_queue SET approved_by = 'attacker'"),
    ("remediation_actions",
     "UPDATE salesops.remediation_actions SET status = 'approved'"),
    ("remediation_events", "DELETE FROM salesops.remediation_events"),
    ("review_events", "DELETE FROM salesops.review_events"),
    ("operational_events", "DELETE FROM salesops.operational_events"),
    ("operational_config",
     "UPDATE salesops.operational_config SET config_value = 1"),
    ("decision_thresholds",
     "UPDATE salesops.decision_thresholds SET threshold_value = 0"),
]

#: Every VOLATILE function in the schema is a write path.
FORBIDDEN_CALLS = [
    "SELECT salesops.purge_staging(TRUE, 'attacker')",
    "SELECT salesops.refresh_kpi_daily()",
    "SELECT salesops.decide_anomalies('stage6-v1')",
    "SELECT salesops.recover_stale_runs('attacker', TRUE)",
    "SELECT salesops.recover_stale_remediation('attacker', TRUE)",
]


@pytest.fixture(scope="module")
def owner_connection():
    try:
        conn = repository.connect(make_settings().dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")
    with conn:
        yield conn


@pytest.fixture(scope="module")
def reporting_connection():
    settings = readonly_settings()
    try:
        conn = repository.connect(settings.dsn)
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"salesops_readonly cannot connect ({exc}) - run metabase/provision.sh"
        )
    with conn:
        yield conn


@pytest.fixture(scope="module", autouse=True)
def critical_incident(owner_connection):
    """Same reason as the view suite: the tests below assert that an executed
    action has a named authoriser and a complete audit history, and both are
    vacuously true of an empty table."""
    return ensure_critical_incident(make_settings(), owner_connection)


def _expect_refused(connection, sql: str) -> str:
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
        with connection.cursor() as cursor:
            cursor.execute(sql)
    connection.rollback()
    return str(excinfo.value)


# =============================================================================
# The reporting role can read
# =============================================================================
def test_the_reporting_role_can_read_the_warehouse(reporting_connection):
    """The point of the role. A read-only account that cannot read is just a
    broken account."""
    row = query(reporting_connection,
                "SELECT count(*) AS n FROM salesops.fact_orders")[0]
    assert row["n"] > 0


def test_the_reporting_role_can_read_every_presentation_view(reporting_connection):
    views = query(reporting_connection, """
        SELECT table_name FROM information_schema.views
        WHERE table_schema = 'salesops' ORDER BY table_name
    """)
    assert views
    for view in views:
        name = view["table_name"]
        with reporting_connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM salesops.{name} LIMIT 1")
            cursor.fetchall()


def test_the_reporting_role_may_call_the_configuration_readers(reporting_connection):
    """Four views read a threshold through a STABLE function, and PostgreSQL
    checks EXECUTE against the CALLING role even inside a view. Denying these
    would show up as a blank panel rather than an error."""
    row = query(reporting_connection,
                "SELECT salesops.operational_setting('max_replay_attempts') AS v")[0]
    assert row["v"] is not None


# =============================================================================
# ...and cannot write
# =============================================================================
@pytest.mark.parametrize("table,sql", FORBIDDEN_WRITES, ids=[t for t, _ in FORBIDDEN_WRITES])
def test_the_reporting_role_cannot_write(reporting_connection, table, sql):
    message = _expect_refused(reporting_connection, sql)
    assert table in message


@pytest.mark.parametrize("sql", FORBIDDEN_CALLS)
def test_the_reporting_role_cannot_call_a_volatile_function(reporting_connection, sql):
    """A VOLATILE function is a write reachable from a SELECT box. PostgreSQL
    grants EXECUTE to PUBLIC by default, which is the default this stage exists
    to undo."""
    _expect_refused(reporting_connection, sql)


def test_the_reporting_role_cannot_create_anything(reporting_connection):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with reporting_connection.cursor() as cursor:
            cursor.execute("CREATE TABLE salesops.smuggled (id int)")
    reporting_connection.rollback()


def test_the_reporting_role_holds_no_write_privilege_at_all(reporting_connection):
    """Belt and braces: ask the catalogue rather than probing statement by
    statement, so a table added later is covered without a new test."""
    rows = query(reporting_connection, """
        SELECT table_name, privilege_type
        FROM information_schema.table_privileges
        WHERE grantee = 'salesops_readonly'
          AND privilege_type <> 'SELECT'
    """)
    assert rows == []


def test_the_reporting_role_is_not_privileged(reporting_connection):
    row = query(reporting_connection, """
        SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
        FROM pg_roles WHERE rolname = 'salesops_readonly'
    """)[0]
    assert not any(row.values())


def test_the_reporting_role_holds_execute_on_nothing_volatile(owner_connection):
    rows = query(owner_connection, """
        SELECT p.proname
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'salesops'
          AND p.provolatile = 'v'
          AND has_function_privilege('salesops_readonly', p.oid, 'EXECUTE')
    """)
    assert rows == []


# =============================================================================
# Reading changes nothing
# =============================================================================
def test_running_every_dashboard_query_changes_no_state(owner_connection,
                                                        reporting_connection):
    """The whole catalogue, executed, between two sets of fingerprints."""
    catalogue = dashboards_module()
    before = all_fingerprints(owner_connection)

    executed = 0
    for card in catalogue.CARDS:
        sql = card["sql"].replace("{{incident_date}}", f"'{CRITICAL_DATE}'")
        with reporting_connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.fetchall()
        executed += 1
    assert executed == len(catalogue.CARDS)

    assert all_fingerprints(owner_connection) == before


def test_the_stage_six_decisions_are_untouched(owner_connection):
    major = major_date(owner_connection)
    rows = query(owner_connection, """
        SELECT calendar_date, severity, routing, decision, decision_reason_code
        FROM salesops.anomaly_decisions
        WHERE calendar_date IN (%(critical)s, %(major)s)
        ORDER BY calendar_date
    """, {"critical": CRITICAL_DATE, "major": major})
    by_date = {str(r["calendar_date"]): r for r in rows}
    assert by_date[CRITICAL_DATE]["severity"] == "critical"
    assert by_date[CRITICAL_DATE]["decision_reason_code"] == "CRITICAL_COMBINED_IMPACT"
    assert by_date[major]["severity"] == "major"
    assert by_date[major]["decision_reason_code"] == "HIGH_REVENUE_IMPACT"


def test_stage_seven_has_no_write_path_to_stage_six(owner_connection):
    """The ordering that makes the whole architecture work, asserted structurally.

    This test used to compare timestamps - `generated_at` had to be later than
    `decided_at` - and that was the wrong evidence. `decide_anomalies()` upserts,
    refreshing `decided_at` on every run, so simply re-running Stage 6 moves the
    decision's timestamp past hypotheses that were legitimately generated after
    the original decision. The assertion failed while nothing was wrong, which is
    the definition of a test measuring the wrong thing.

    What actually guarantees the ordering is that Stage 7 has nowhere to write.
    It reads `anomaly_decisions` and writes `anomaly_hypotheses`, and there is no
    third option - so that is what gets asserted, from the source rather than
    from a clock.
    """
    llm_package = REPO_ROOT / "analytics-service" / "analytics" / "llm"
    sources = list(llm_package.glob("*.py"))
    assert sources, "the Stage 7 package is missing"

    writes = re.compile(
        r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+salesops\.anomaly_decisions",
        re.IGNORECASE,
    )
    for path in sources:
        assert not writes.search(path.read_text(encoding="utf-8")), path.name


def test_every_hypothesis_is_anchored_to_the_decision_it_explains(owner_connection):
    """And anchored by identity, not by narrative.

    A hypothesis carries a snapshot of the verdict it was given. That snapshot
    may become historical - Stage 6 can be re-run under a new version - but it
    can never point at a different anomaly or a different decision version than
    the decision row it references.
    """
    rows = query(owner_connection, """
        SELECT count(*) AS n
        FROM salesops.anomaly_hypotheses h
        JOIN salesops.anomaly_decisions d ON d.decision_id = h.decision_id
        WHERE h.anomaly_id <> d.anomaly_id
           OR h.decision_version <> d.decision_version
           OR h.calendar_date <> d.calendar_date
    """)
    assert rows[0]["n"] == 0

    orphans = query(owner_connection, """
        SELECT count(*) AS n FROM salesops.anomaly_hypotheses h
        WHERE NOT EXISTS (SELECT 1 FROM salesops.anomaly_decisions d
                          WHERE d.decision_id = h.decision_id)
    """)
    assert orphans[0]["n"] == 0


def test_stage_nine_states_remain_a_closed_vocabulary(owner_connection):
    rows = query(owner_connection, """
        SELECT DISTINCT status FROM salesops.remediation_actions
    """)
    assert {r["status"] for r in rows} <= {
        "proposed", "approved", "executing", "executed", "failed",
        "rejected", "cancelled", "execution_unknown",
    }


def test_nothing_executed_without_a_named_authoriser(owner_connection):
    """Stage 9's core promise, re-asserted from the presentation layer's own
    view of the world."""
    rows = query(owner_connection, """
        SELECT count(*) AS n FROM salesops.exec_actionable_anomalies
        WHERE remediation_status = 'executed'
          AND review_approved_by IS NULL
    """)
    assert rows[0]["n"] == 0


def test_the_audit_history_is_still_complete(owner_connection):
    """Every remediation that reached a state has an event recording the
    transition into it. An audit trail with a hole is worse than none."""
    rows = query(owner_connection, """
        SELECT a.remediation_id, a.status
        FROM salesops.remediation_actions a
        WHERE NOT EXISTS (
            SELECT 1 FROM salesops.remediation_events e
            WHERE e.remediation_id = a.remediation_id AND e.to_status = a.status
        )
    """)
    assert rows == []


def test_the_operational_log_still_refuses_to_be_rewritten(owner_connection):
    """Stage 10's append-only guard, re-checked because Stage 11 exposes the log
    to a wider audience than Stage 10 did.

    Structural rather than behavioural, and deliberately so. A row trigger
    cannot fire on an `UPDATE` that matches nothing, so the behavioural version
    of this test passed only on a warehouse that had already recorded an
    operational event - and it reported "did not raise", which reads as a
    missing guard rather than as an empty table. Manufacturing a row to attack
    is not an option either: the log is append-only, so the fixture could create
    it and never clean it up.

    So the guard is asserted where it actually lives, and the refusal is
    exercised against whatever history the warehouse genuinely has.
    """
    triggers = query(owner_connection, """
        SELECT pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'salesops'
          AND c.relname = 'operational_events'
          AND NOT t.tgisinternal
    """)
    assert triggers, "operational_events has no trigger guarding it at all"
    guard = " ".join(t["definition"].upper() for t in triggers)
    assert "UPDATE" in guard, "nothing stops an operational event being rewritten"
    assert "DELETE" in guard, "nothing stops an operational event being erased"

    recorded = query(owner_connection, """
        SELECT count(*) AS n FROM salesops.operational_events
    """)[0]["n"]
    if not recorded:
        return

    # The guard raises with an integrity-constraint SQLSTATE rather than the
    # bare P0001 a plain RAISE would give, so the specific class matters: it is
    # the difference between "the trigger refused this" and "some function
    # somewhere raised".
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as excinfo:
        with owner_connection.cursor() as cursor:
            cursor.execute("""
                UPDATE salesops.operational_events SET actor = 'rewritten'
                WHERE event_id = (SELECT min(event_id) FROM salesops.operational_events)
            """)
    owner_connection.rollback()
    assert "append-only" in str(excinfo.value).lower()
