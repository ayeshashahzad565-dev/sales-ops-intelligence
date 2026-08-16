"""Integration tests against the live warehouse.

These complement the pure unit tests: those prove the mathematics, these prove
the mathematics reaches PostgreSQL intact and that the persisted result obeys
its contract.

Skipped automatically when the stack is not running, so the suite stays usable
without Docker.

Everything that writes runs inside a transaction that is rolled back. The one
exception is the detection run itself, which is idempotent by design - running
it is exactly what the scheduled workflow does.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

from analytics import repository
from analytics.config import Settings
from analytics.detector import ANOMALY_SCORE_THRESHOLD, DETECTOR_VERSION
from analytics.runner import RunMode, run_detection
from tests.live_dates import INCIDENT_DATE, NORMAL_DATE

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The reference case the Stage 5 specification requires be detected: the day
# bootstrap.sh injects a revenue drop and a refund spike into.
REFERENCE_ANOMALY = INCIDENT_DATE
# The reference case that must NOT be flagged: an ordinary Sunday that looks
# alarming against a blind moving average.
REFERENCE_NORMAL_SUNDAY = NORMAL_DATE


def _load_env_file() -> dict[str, str]:
    """Read the repo's .env, so host-run tests can reach the same database.

    A development convenience only - the container gets these from Compose. No
    dependency on python-dotenv for a five-line parser.
    """
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
    except Exception as exc:  # noqa: BLE001 - any connection failure means "no stack"
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")

    with conn:
        yield conn


def _query(connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchall()


def _one(connection, sql: str, params: dict | None = None) -> dict:
    rows = _query(connection, sql, params)
    assert rows, "expected exactly one row, got none"
    return rows[0]


# --- the detector reaches the database ---------------------------------------

def test_kpi_observations_load(connection) -> None:
    observations = repository.load_kpi_observations(connection)

    assert observations, "kpi_daily is empty - run the KPI refresh first"
    assert all(o.day_of_week in range(1, 8) for o in observations)
    # Calendar attributes come from dim_date, not from Python's date arithmetic.
    assert all(o.is_weekend == (o.day_of_week >= 6) for o in observations)


def test_results_are_stored_for_every_kpi_date(connection, settings) -> None:
    run_detection(settings, mode=RunMode.FULL)

    row = _one(connection, """
        SELECT (SELECT count(*) FROM salesops.kpi_daily)                        AS kpi_rows,
               (SELECT count(*) FROM salesops.anomaly_daily
                 WHERE detector_version = %(version)s)                          AS result_rows
    """, {"version": DETECTOR_VERSION})

    assert row["result_rows"] == row["kpi_rows"]


def test_every_result_references_an_existing_kpi_date(connection) -> None:
    """No verdict may exist about a day the warehouse has no KPIs for."""
    orphans = _query(connection, """
        SELECT a.calendar_date
        FROM salesops.anomaly_daily a
        LEFT JOIN salesops.kpi_daily k ON k.calendar_date = a.calendar_date
        WHERE k.calendar_date IS NULL
    """)

    assert orphans == []


def test_no_duplicate_results_per_date_and_version(connection) -> None:
    duplicates = _query(connection, """
        SELECT calendar_date, detector_version, count(*) AS n
        FROM salesops.anomaly_daily
        GROUP BY calendar_date, detector_version
        HAVING count(*) > 1
    """)

    assert duplicates == []


def test_detector_version_is_populated_and_well_formed(connection) -> None:
    rows = _query(connection, """
        SELECT DISTINCT detector_version FROM salesops.anomaly_daily
    """)

    assert rows
    for row in rows:
        assert re.fullmatch(r"v\d+\.\d+\.\d+", row["detector_version"]), row


# --- the persisted contract ---------------------------------------------------

def test_scored_rows_carry_a_baseline(connection) -> None:
    bad = _query(connection, """
        SELECT calendar_date FROM salesops.anomaly_daily
        WHERE baseline_status = 'scored'
          AND (anomaly_score IS NULL OR baseline_kind IS NULL OR baseline_size IS NULL)
    """)

    assert bad == []


def test_unscored_rows_are_never_anomalies(connection) -> None:
    """Absence of evidence must not be recorded as a finding."""
    bad = _query(connection, """
        SELECT calendar_date, baseline_status FROM salesops.anomaly_daily
        WHERE baseline_status <> 'scored' AND (is_anomaly OR anomaly_score IS NOT NULL)
    """)

    assert bad == []


def test_the_database_rejects_an_unscored_anomaly(connection) -> None:
    """The constraint, not just the code, forbids it.

    A future bug that forgot to check baseline_status must not be able to publish
    a verdict derived from no baseline at all.
    """
    import psycopg

    with connection.transaction() as transaction:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO salesops.anomaly_daily
                        (calendar_date, detector_version, baseline_status, is_anomaly)
                    VALUES (%(d)s, 'v9.9.9', 'insufficient_history', TRUE)
                """, {"d": REFERENCE_ANOMALY})
        transaction.force_rollback = True


def test_the_database_rejects_a_malformed_detector_version(connection) -> None:
    import psycopg

    with connection.transaction() as transaction:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO salesops.anomaly_daily
                        (calendar_date, detector_version, baseline_status)
                    VALUES (%(d)s, 'not-a-version', 'insufficient_history')
                """, {"d": REFERENCE_ANOMALY})
        transaction.force_rollback = True


def test_incomplete_kpi_dates_are_recorded_not_dropped(connection) -> None:
    """Whatever the count, an FX gap must appear as a status, never as silence."""
    row = _one(connection, """
        SELECT count(*) FILTER (WHERE NOT is_complete)                      AS incomplete_kpi_rows,
               (SELECT count(*) FROM salesops.anomaly_daily
                 WHERE baseline_status = 'incomplete_kpi')                  AS marked_results
        FROM salesops.kpi_daily
    """)

    assert row["marked_results"] == row["incomplete_kpi_rows"]


# --- idempotency --------------------------------------------------------------

def test_repeated_detection_is_idempotent(connection, settings) -> None:
    """Same inputs, same version, same rows - byte for byte."""
    checksum_sql = """
        SELECT count(*) AS rows,
               md5(string_agg(
                   calendar_date::text
                   || coalesce(anomaly_score::text, '-')
                   || is_anomaly::text
                   || coalesce(revenue_robust_z::text, '-')
                   || coalesce(dominant_signal, '-')
                   || baseline_status,
                   '|' ORDER BY calendar_date)) AS digest
        FROM salesops.anomaly_daily WHERE detector_version = %(version)s
    """

    run_detection(settings, mode=RunMode.FULL)
    before = _one(connection, checksum_sql, {"version": DETECTOR_VERSION})

    run_detection(settings, mode=RunMode.FULL)
    after = _one(connection, checksum_sql, {"version": DETECTOR_VERSION})

    assert before["rows"] == after["rows"]
    assert before["digest"] == after["digest"]


def test_incremental_mode_writes_nothing_when_everything_is_current(settings) -> None:
    run_detection(settings, mode=RunMode.FULL)

    summary = run_detection(settings, mode=RunMode.INCREMENTAL)

    assert summary.dates_evaluated == 0
    assert summary.dates_written == 0


# --- behaviour on real data ---------------------------------------------------
#
# Stated as properties over the whole series rather than as expectations about
# particular dates, so they keep their meaning as the dataset moves.

def test_a_day_with_collapsed_revenue_and_spiking_refunds_is_flagged(connection, settings) -> None:
    """The signature the detector exists to catch, wherever it occurs."""
    run_detection(settings, mode=RunMode.FULL)

    severe = _query(connection, """
        SELECT calendar_date, is_anomaly, anomaly_score
        FROM salesops.anomaly_daily
        WHERE baseline_status = 'scored'
          AND revenue_deviation_pct < -50
          AND refund_rate_deviation > 0.1
    """)

    if not severe:
        pytest.skip("No revenue-collapse-with-refund-spike day in the current dataset")

    for row in severe:
        assert row["is_anomaly"], f"{row['calendar_date']} should have been flagged"
        assert float(row["anomaly_score"]) >= ANOMALY_SCORE_THRESHOLD


def test_a_day_far_below_its_moving_average_but_normal_for_its_weekday_is_not_flagged(
    connection, settings
) -> None:
    """The exact false positive that calendar awareness exists to prevent.

    Every weekend sits far below the trailing 7-day mean, because that mean is
    dominated by weekdays worth roughly double. A detector comparing days against
    an undifferentiated moving average would flag one every week.

    So: any day under 60% of its own 7-day moving average, yet at or above the
    median for its own weekday, must be normal. This is the 2026-08-02 situation
    stated as a property - it holds for whichever dates happen to satisfy it.
    """
    run_detection(settings, mode=RunMode.FULL)

    misleading_days = _query(connection, """
        SELECT a.calendar_date, a.is_anomaly, a.anomaly_score,
               a.revenue_deviation_pct, d.day_name,
               round(100.0 * k.net_revenue_usd / k.rolling_7d_net_revenue_usd, 1) AS pct_of_ma7
        FROM salesops.anomaly_daily a
        JOIN salesops.dim_date  d ON d.calendar_date = a.calendar_date
        JOIN salesops.kpi_daily k ON k.calendar_date = a.calendar_date
        WHERE a.baseline_status = 'scored'
          AND k.rolling_7d_net_revenue_usd > 0
          -- Would look alarming against a blind moving average...
          AND k.net_revenue_usd < 0.60 * k.rolling_7d_net_revenue_usd
          -- ...but is at or above par for its own weekday.
          AND a.revenue_deviation_pct >= 0
    """)

    assert misleading_days, (
        "expected at least one day that looks alarming against its moving average "
        "but is ordinary for its weekday - otherwise this test proves nothing"
    )

    flagged = [
        (r["calendar_date"], r["day_name"], float(r["pct_of_ma7"]))
        for r in misleading_days if r["is_anomaly"]
    ]
    assert not flagged, f"seasonality was mistaken for an anomaly: {flagged}"


def test_weekends_are_not_flagged_merely_for_being_weekends(connection, settings) -> None:
    """Every flagged weekend day must be extreme FOR A WEEKEND.

    The weekend flag rate is legitimately higher than the weekday rate in this
    dataset - weekend revenue is far more heavy-tailed (1.8k to 14.7k across
    Sundays). What must never happen is a weekend flagged on ordinary
    weekend-scale numbers, so every weekend flag is required to carry at least
    one signal that genuinely stands out against its own weekday.
    """
    run_detection(settings, mode=RunMode.FULL)

    flagged_weekends = _query(connection, """
        SELECT a.calendar_date, d.day_name, a.dominant_signal, a.anomaly_score,
               a.revenue_robust_z, a.aov_robust_z, a.refund_robust_z, a.orders_robust_z
        FROM salesops.anomaly_daily a
        JOIN salesops.dim_date d ON d.calendar_date = a.calendar_date
        WHERE d.is_weekend AND a.is_anomaly
    """)

    for row in flagged_weekends:
        strongest = max(
            abs(float(row[column] or 0.0))
            for column in ("revenue_robust_z", "aov_robust_z",
                           "refund_robust_z", "orders_robust_z")
        )
        assert strongest > 3.0, (
            f"{row['calendar_date']} ({row['day_name']}) was flagged without any "
            f"signal standing out against its own weekday (strongest |z| = {strongest:.2f})"
        )


def test_the_reference_anomaly_is_detected(connection, settings) -> None:
    """The specific case Stage 5 is required to surface.

    2026-08-05 carries three anomalies injected through the Mock API in Stage 1
    (revenue drop, refund spike, regional drop). It is asserted by date because
    it is a known, documented event in this dataset - a regression guard, not a
    tuned expectation.
    """
    run_detection(settings, mode=RunMode.FULL)

    rows = _query(connection, """
        SELECT * FROM salesops.anomaly_daily
        WHERE calendar_date = %(d)s AND detector_version = %(version)s
    """, {"d": REFERENCE_ANOMALY, "version": DETECTOR_VERSION})

    if not rows:
        pytest.skip(f"{REFERENCE_ANOMALY} is not in the current dataset")

    result = rows[0]
    assert result["baseline_status"] == "scored"
    assert result["is_anomaly"], "the injected anomaly was not detected"
    assert float(result["anomaly_score"]) >= ANOMALY_SCORE_THRESHOLD

    # Multiple independent signals, which is why it clears the bar.
    assert float(result["revenue_deviation_pct"]) < -50
    assert float(result["aov_deviation_pct"]) < -50
    assert float(result["refund_rate_deviation"]) > 0.1
    # Order volume held steady - prices fell, demand did not.
    assert abs(float(result["orders_robust_z"])) < 1.5


def test_the_reference_sunday_is_not_flagged(connection, settings) -> None:
    """The discrimination case.

    2026-08-02 sits at ~45% of the trailing 7-day mean - alarming to a detector
    without calendar awareness - but is ABOVE the median for a Sunday.
    """
    run_detection(settings, mode=RunMode.FULL)

    rows = _query(connection, """
        SELECT a.*, d.day_name
        FROM salesops.anomaly_daily a
        JOIN salesops.dim_date d ON d.calendar_date = a.calendar_date
        WHERE a.calendar_date = %(d)s AND a.detector_version = %(version)s
    """, {"d": REFERENCE_NORMAL_SUNDAY, "version": DETECTOR_VERSION})

    if not rows:
        pytest.skip(f"{REFERENCE_NORMAL_SUNDAY} is not in the current dataset")

    result = rows[0]
    assert result["baseline_status"] == "scored"
    assert result["baseline_kind"] == "day_of_week"
    assert not result["is_anomaly"], "an ordinary Sunday was flagged as anomalous"
    # Compared against its own weekday it is above median, not below.
    assert float(result["revenue_deviation_pct"]) > 0
