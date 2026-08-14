"""Behavioural tests for Stage 6: the deterministic decision layer.

Same harness as Stages 3 and 4. Every statement under test is either extracted
from the committed workflow JSON at run time, or is the function the workflow
calls - so the suite exercises the SQL production actually runs, and editing a
node's query runs the edited query here.

Everything happens inside one transaction that ROLLBACKs, so the suite is safe
against the populated live database and leaves nothing behind. Synthetic
fixtures live in March 2025, far from the ingested 2026 series, and detector
version 'v9.9.9' keeps them separable from real detections. The isolation is
asserted in section 0 rather than assumed.

The two fixture PAIRS are the point of the suite
------------------------------------------------
Most of these checks pass trivially if the engine merely runs. Two pairs do not:

    2025-03-07 vs 2025-03-10   identical anomaly score, identical robust-z,
                               identical percent deviation - different DOLLARS.
                               Must come out major and minor.

    2025-03-07 vs 2025-03-18   identical dollars, identical score - one has
                               corroborating operational damage.
                               Must come out major and critical.

Between them they pin down the two claims Stage 6 actually makes: that severity
is not a re-labelling of the z-score, and that critical means "money moved AND
something broke", not "the biggest number this week".

Usage (from the repo root, with the stack running):
    python n8n/tests/test_stage6_decisions.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DECISION_WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "deterministic-anomaly-decision.json"

# Detector version used only by fixtures. Real detections are v1.0.0.
FIXTURE_DETECTOR = "v9.9.9"

# March 2025: inside dim_date's range, nowhere near the 2026 ingested series.
F_NORMAL = "2025-03-03"      # scored, not flagged
F_INSUFF = "2025-03-04"      # insufficient_history
F_INCOMPLETE = "2025-03-05"  # incomplete_kpi
F_MINOR = "2025-03-06"       # flagged, money below material
F_MAJOR = "2025-03-07"       # flagged, material money, no operational damage
F_TINY = "2025-03-10"        # SAME statistics as F_MAJOR, trivial dollars
F_CRITICAL = "2025-03-11"    # severe money + refund spike
F_REFUND = "2025-03-12"      # refund spike, limited money
F_AOV = "2025-03-13"         # AOV collapse, limited money
F_ORDERS = "2025-03-14"      # order-volume collapse, limited money
F_MULTI = "2025-03-17"       # 4 significant signals, limited money
F_CORROB = "2025-03-18"      # SAME money as F_MAJOR + two operational failures
F_UNMEASURED = "2025-03-19"  # flagged, but no baseline revenue persisted
F_IMPROVED = "2025-03-20"    # every operational measure moved the GOOD way

FIXTURE_DATES = (
    F_NORMAL, F_INSUFF, F_INCOMPLETE, F_MINOR, F_MAJOR, F_TINY, F_CRITICAL,
    F_REFUND, F_AOV, F_ORDERS, F_MULTI, F_CORROB, F_UNMEASURED, F_IMPROVED,
)

# Live dates the specification names explicitly.
LIVE_CRITICAL = "2026-08-05"   # the injected event
LIVE_NORMAL_SUNDAY = "2026-08-02"


def node_sql(workflow: dict, node_name: str) -> str:
    for node in workflow["nodes"]:
        if node["name"] == node_name:
            return node["parameters"]["query"].rstrip().rstrip(";")
    raise SystemExit(f"Node not found: {node_name!r}")


def build_script() -> str:
    workflow = json.loads(DECISION_WORKFLOW.read_text(encoding="utf-8"))

    open_run = node_sql(workflow, "Open Decision Run").replace("$1", "'test-exec-stage6'")
    decide = node_sql(workflow, "Decide Anomalies").replace("$1", "'stage6-v1'")
    finalize = node_sql(workflow, "Finalize Decision Run")
    abort = node_sql(workflow, "Abort - Stage 5 Not Ready")

    def finalize_sql(batch_expr: str, received: str, accepted: str, version: str) -> str:
        return (finalize
                .replace("$1", batch_expr)
                .replace("$2", received)
                .replace("$3", accepted)
                .replace("$4", f"'{version}'"))

    abort_sql = (abort
                 .replace("$1", "(SELECT batch_id::text FROM abort_run)")
                 .replace("$2", "'0'")
                 .replace("$3", "'running'"))

    def capture(name: str, sql: str) -> str:
        """Capture an UPDATE ... RETURNING into a temp table.

        CREATE TABLE AS cannot wrap a data-modifying statement, so the statement
        goes inside a data-modifying CTE instead. The workflow's SQL is used
        verbatim either way - only where its result lands changes.
        """
        return f"WITH captured AS (\n{sql}\n)\nSELECT * INTO TEMP {name} FROM captured;"

    return f"""\
\\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE test_results (
    id SERIAL PRIMARY KEY, section TEXT, name TEXT, passed BOOLEAN, detail TEXT
) ON COMMIT DROP;

CREATE OR REPLACE FUNCTION pg_temp.check(
    p_section TEXT, p_name TEXT, p_passed BOOLEAN, p_detail TEXT DEFAULT ''
) RETURNS VOID LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO test_results (section, name, passed, detail)
    VALUES (p_section, p_name, COALESCE(p_passed, FALSE), p_detail);
END;
$fn$;

-- Asserts that a statement is REJECTED. Used for the constraints that make the
-- decision layer non-overridable: the interesting behaviour is the refusal.
CREATE OR REPLACE FUNCTION pg_temp.check_rejects(
    p_section TEXT, p_name TEXT, p_sql TEXT
) RETURNS VOID LANGUAGE plpgsql AS $fn$
BEGIN
    EXECUTE p_sql;
    PERFORM pg_temp.check(p_section, p_name, FALSE, 'statement was ACCEPTED');
EXCEPTION WHEN OTHERS THEN
    PERFORM pg_temp.check(p_section, p_name, TRUE, 'rejected: ' || SQLERRM);
END;
$fn$;


-- =============================================================================
-- Fixture planting
--
-- One helper builds a consistent (kpi_daily, anomaly_daily) pair, so a fixture
-- differs from its neighbour only in the values under test. revenue_deviation_pct
-- is DERIVED from the planted revenue and median rather than passed in - a
-- fixture that could state a percentage inconsistent with its own dollars would
-- let a broken engine agree with a broken test.
-- =============================================================================
CREATE OR REPLACE FUNCTION pg_temp.plant(
    p_date            DATE,
    p_status          TEXT,
    p_is_anomaly      BOOLEAN,
    p_score           NUMERIC,
    p_net_revenue     NUMERIC,
    p_baseline_median NUMERIC,
    p_signal_count    INTEGER DEFAULT 0,
    p_revenue_z       NUMERIC DEFAULT NULL,
    p_refund_dev      NUMERIC DEFAULT 0,
    p_aov_dev         NUMERIC DEFAULT 0,
    p_orders_dev      NUMERIC DEFAULT 0
) RETURNS BIGINT LANGUAGE plpgsql AS $fn$
DECLARE
    v_key    INTEGER := EXTRACT(YEAR FROM p_date) * 10000
                      + EXTRACT(MONTH FROM p_date) * 100
                      + EXTRACT(DAY FROM p_date);
    v_orders INTEGER := 100;
    v_gross  NUMERIC := round(p_net_revenue * 1.05, 4);
    v_id     BIGINT;
BEGIN
    IF p_status = 'incomplete_kpi' THEN
        -- No order on this day carried an exchange rate, so every money column
        -- is genuinely unknown - not zero.
        INSERT INTO salesops.kpi_daily (
            date_key, calendar_date, orders_count, customers_count, new_customers,
            units_sold, gross_revenue_usd, refund_amount_usd, net_revenue_usd,
            average_order_value_usd, refund_rate, orders_pending_fx,
            fx_completeness_pct, is_complete
        ) VALUES (
            v_key, p_date, v_orders, 90, 10, 200,
            NULL, NULL, NULL, NULL, NULL, v_orders, 0, FALSE
        );
    ELSE
        INSERT INTO salesops.kpi_daily (
            date_key, calendar_date, orders_count, customers_count, new_customers,
            units_sold, gross_revenue_usd, refund_amount_usd, net_revenue_usd,
            average_order_value_usd, refund_rate, orders_pending_fx,
            fx_completeness_pct, is_complete
        ) VALUES (
            v_key, p_date, v_orders, 90, 10, 200,
            v_gross, round(v_gross - p_net_revenue, 4), p_net_revenue,
            round(p_net_revenue / v_orders, 4),
            round((v_gross - p_net_revenue) / NULLIF(v_gross, 0), 6),
            0, 100, TRUE
        );
    END IF;

    INSERT INTO salesops.anomaly_daily (
        calendar_date, detector_version, anomaly_score, is_anomaly,
        revenue_deviation_pct, revenue_robust_z,
        aov_deviation_pct, aov_robust_z,
        refund_rate_deviation, refund_robust_z,
        orders_deviation_pct, orders_robust_z,
        revenue_baseline_median,
        baseline_status, baseline_kind, baseline_size,
        dominant_signal, signal_count
    ) VALUES (
        p_date, '{FIXTURE_DETECTOR}', p_score, p_is_anomaly,
        CASE WHEN p_baseline_median IS NULL OR p_net_revenue IS NULL THEN NULL
             ELSE round(100.0 * (p_net_revenue - p_baseline_median)
                        / abs(p_baseline_median), 4) END,
        p_revenue_z,
        p_aov_dev,    NULL,
        p_refund_dev, NULL,
        p_orders_dev, NULL,
        p_baseline_median,
        p_status,
        CASE WHEN p_status = 'scored' THEN 'day_of_week' END,
        CASE WHEN p_status = 'scored' THEN 8 END,
        CASE WHEN p_is_anomaly THEN 'revenue' END,
        p_signal_count
    )
    RETURNING anomaly_id INTO v_id;

    RETURN v_id;
END;
$fn$;


-- =============================================================================
-- 0. Preconditions
-- =============================================================================
DO $do$
DECLARE existing INTEGER; thresholds INTEGER; live_rows INTEGER;
BEGIN
    SELECT count(*) INTO existing
    FROM salesops.kpi_daily
    WHERE calendar_date BETWEEN DATE '2025-03-01' AND DATE '2025-03-31';

    PERFORM pg_temp.check('preconditions', 'fixture window is empty of real data',
                          existing = 0, format('%s existing kpi row(s) in March 2025', existing));

    SELECT count(*) INTO thresholds
    FROM salesops.decision_thresholds WHERE decision_version = 'stage6-v1';
    PERFORM pg_temp.check('preconditions', 'stage6-v1 thresholds are configured',
                          thresholds = 9, format('%s threshold(s)', thresholds));

    SELECT count(*) INTO live_rows FROM salesops.anomaly_daily WHERE detector_version = 'v1.0.0';
    PERFORM pg_temp.check('preconditions', 'live Stage 5 evidence is present',
                          live_rows > 0, format('%s row(s)', live_rows));

    -- Every threshold the engine reads must exist, or it would silently compare
    -- against nothing. decision_threshold() raises; prove it.
    BEGIN
        PERFORM salesops.decision_threshold('stage6-v1', 'no_such_threshold');
        PERFORM pg_temp.check('preconditions', 'a missing threshold raises', FALSE, 'returned a value');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('preconditions', 'a missing threshold raises', TRUE, SQLERRM);
    END;
END;
$do$;


-- =============================================================================
-- 1. Plant the fixtures
-- =============================================================================
DO $do$
BEGIN
    -- scored, ordinary
    PERFORM pg_temp.plant(DATE '{F_NORMAL}', 'scored', FALSE, 0.5000, 10000, 9800, 0, 0.4);

    -- unscorable
    PERFORM pg_temp.plant(DATE '{F_INSUFF}', 'insufficient_history', FALSE, NULL, 10000, NULL, 0);
    PERFORM pg_temp.plant(DATE '{F_INCOMPLETE}', 'incomplete_kpi', FALSE, NULL, NULL, NULL, 0);

    -- flagged, +1,500 -> 'limited', below the 4,000 material threshold
    PERFORM pg_temp.plant(DATE '{F_MINOR}', 'scored', TRUE, 3.0000, 11500, 10000, 1, 3.6);

    -- PAIR A. Identical statistics, different dollars.
    --   +6,000 on a 10,000 baseline  -> material -> major
    PERFORM pg_temp.plant(DATE '{F_MAJOR}', 'scored', TRUE, 5.0000, 16000, 10000, 1, 4.2);
    --   +450 on a 750 baseline: same +60%, same score, same z -> trivial -> minor
    PERFORM pg_temp.plant(DATE '{F_TINY}',  'scored', TRUE, 5.0000, 1200, 750, 1, 4.2);

    -- severe money (-10,000, -83%) with a refund spike -> critical
    PERFORM pg_temp.plant(DATE '{F_CRITICAL}', 'scored', TRUE, 9.0000, 2000, 12000, 3, -5.1, 0.2500, -20, -10);

    -- operational damage on limited money: recognised, but not escalated
    PERFORM pg_temp.plant(DATE '{F_REFUND}', 'scored', TRUE, 3.2000, 11500, 10000, 1, 1.2, 0.1500, 0, 0);
    PERFORM pg_temp.plant(DATE '{F_AOV}',    'scored', TRUE, 3.2000, 11500, 10000, 1, 1.2, 0, -40, 0);
    PERFORM pg_temp.plant(DATE '{F_ORDERS}', 'scored', TRUE, 3.2000, 11500, 10000, 1, 1.2, 0, 0, -45);
    PERFORM pg_temp.plant(DATE '{F_MULTI}',  'scored', TRUE, 4.0000, 11500, 10000, 4, 2.0, 0, 0, 0);

    -- PAIR B. Same money and score as F_MAJOR, plus two operational failures.
    PERFORM pg_temp.plant(DATE '{F_CORROB}', 'scored', TRUE, 5.0000, 16000, 10000, 1, 4.2, 0.2000, -35, 0);

    -- flagged, but Stage 5 persisted no baseline revenue for it
    PERFORM pg_temp.plant(DATE '{F_UNMEASURED}', 'scored', TRUE, 6.0000, 11000, NULL, 2, 3.9);

    -- The mirror image of F_CORROB: the same magnitudes, every one of them
    -- pointing the RIGHT way. Refunds down 15 points, order value up 40%,
    -- volume up 50%. Nothing here is operational damage, and a detector that
    -- compared magnitudes instead of directions would escalate it anyway.
    PERFORM pg_temp.plant(DATE '{F_IMPROVED}', 'scored', TRUE, 4.5000, 11500, 10000, 2, 2.4,
                          -0.1500, 40, 50);
END;
$do$;


-- =============================================================================
-- 2. Run the engine (the function the workflow node calls)
-- =============================================================================
CREATE TEMP TABLE first_run AS SELECT * FROM salesops.decide_anomalies('stage6-v1');

DO $do$
DECLARE r RECORD; planted INTEGER;
BEGIN
    SELECT * INTO r FROM first_run;
    SELECT count(*) INTO planted FROM salesops.anomaly_daily;

    PERFORM pg_temp.check('engine', 'every evidence row is decided',
                          r.anomalies_evaluated = planted,
                          format('%s evaluated of %s evidence rows', r.anomalies_evaluated, planted));
    PERFORM pg_temp.check('engine', 'every evaluated row is written',
                          r.decisions_written = r.anomalies_evaluated,
                          format('%s written', r.decisions_written));
    PERFORM pg_temp.check('engine', 'reason codes were written',
                          r.reason_codes_written > r.decisions_written,
                          format('%s codes for %s decisions', r.reason_codes_written, r.decisions_written));
END;
$do$;

CREATE TEMP VIEW fx AS
SELECT d.*
FROM salesops.anomaly_decisions d
JOIN salesops.anomaly_daily a ON a.anomaly_id = d.anomaly_id
WHERE a.detector_version = '{FIXTURE_DETECTOR}' AND d.decision_version = 'stage6-v1';

CREATE OR REPLACE FUNCTION pg_temp.dec(p_date DATE)
RETURNS salesops.anomaly_decisions LANGUAGE sql STABLE AS $fn$
    SELECT d.* FROM salesops.anomaly_decisions d
    JOIN salesops.anomaly_daily a ON a.anomaly_id = d.anomaly_id
    WHERE a.detector_version = '{FIXTURE_DETECTOR}'
      AND d.decision_version = 'stage6-v1'
      AND d.calendar_date = p_date;
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.reasons(p_date DATE)
RETURNS TEXT LANGUAGE sql STABLE AS $fn$
    SELECT string_agg(r.reason_code, ',' ORDER BY r.reason_code)
    FROM salesops.anomaly_decision_reasons r
    WHERE r.decision_id = (pg_temp.dec(p_date)).decision_id;
$fn$;


-- =============================================================================
-- 3. Decision correctness
-- =============================================================================
DO $do$
DECLARE d salesops.anomaly_decisions;
BEGIN
    -- ---- normal observation -------------------------------------------------
    d := pg_temp.dec(DATE '{F_NORMAL}');
    PERFORM pg_temp.check('decisions', 'normal observation -> no_action',
        d.severity = 'none' AND d.routing = 'no_action' AND d.decision = 'no_action'
        AND NOT d.notification_allowed AND NOT d.human_review_required,
        format('%s / %s', d.severity, d.routing));

    -- ---- insufficient history ----------------------------------------------
    d := pg_temp.dec(DATE '{F_INSUFF}');
    PERFORM pg_temp.check('decisions', 'insufficient history -> no_action',
        d.severity = 'none' AND d.routing = 'no_action'
        AND NOT d.notification_allowed AND NOT d.human_review_required,
        format('%s / %s', d.severity, d.routing));

    -- It must stay DISTINGUISHABLE from a normal day, not be quietly folded in.
    PERFORM pg_temp.check('decisions', 'insufficient history is not labelled normal',
        d.decision_reason_code = 'INSUFFICIENT_HISTORY'
        AND pg_temp.reasons(DATE '{F_INSUFF}') = 'INSUFFICIENT_HISTORY',
        pg_temp.reasons(DATE '{F_INSUFF}'));

    -- ...and carries no invented impact figures.
    PERFORM pg_temp.check('decisions', 'unscored dates carry no expected revenue',
        d.expected_net_revenue_usd IS NULL AND d.actual_net_revenue_usd IS NULL
        AND d.business_impact_tier IS NULL, 'all NULL');

    -- ---- incomplete KPI ------------------------------------------------------
    d := pg_temp.dec(DATE '{F_INCOMPLETE}');
    PERFORM pg_temp.check('decisions', 'incomplete KPI -> no_action, own reason code',
        d.severity = 'none' AND d.routing = 'no_action'
        AND d.decision_reason_code = 'INCOMPLETE_KPI',
        format('%s / %s', d.severity, d.decision_reason_code));

    -- ---- minor ---------------------------------------------------------------
    d := pg_temp.dec(DATE '{F_MINOR}');
    PERFORM pg_temp.check('decisions', 'minor anomaly -> auto_notify',
        d.severity = 'minor' AND d.routing = 'auto_notify' AND d.decision = 'action_required'
        AND d.notification_allowed AND NOT d.human_review_required,
        format('%s / %s', d.severity, d.routing));

    -- ---- major ---------------------------------------------------------------
    d := pg_temp.dec(DATE '{F_MAJOR}');
    PERFORM pg_temp.check('decisions', 'major anomaly -> human_review',
        d.severity = 'major' AND d.routing = 'human_review' AND d.decision = 'action_required'
        AND d.human_review_required AND NOT d.notification_allowed,
        format('%s / %s', d.severity, d.routing));

    -- ---- critical ------------------------------------------------------------
    d := pg_temp.dec(DATE '{F_CRITICAL}');
    PERFORM pg_temp.check('decisions', 'critical anomaly -> human_review',
        d.severity = 'critical' AND d.routing = 'human_review'
        AND d.human_review_required, format('%s / %s', d.severity, d.routing));

    PERFORM pg_temp.check('decisions', 'critical is never auto-notified',
        NOT d.notification_allowed, 'notification_allowed = false');

    -- ---- unmeasurable impact fails SAFE -------------------------------------
    d := pg_temp.dec(DATE '{F_UNMEASURED}');
    PERFORM pg_temp.check('decisions', 'unmeasurable impact escalates instead of defaulting to minor',
        d.severity = 'major' AND d.human_review_required
        AND d.business_impact_tier = 'unknown'
        AND d.decision_reason_code = 'BUSINESS_IMPACT_UNAVAILABLE',
        format('%s / %s / %s', d.severity, d.business_impact_tier, d.decision_reason_code));
END;
$do$;


-- =============================================================================
-- 4. Severity is not a re-labelling of the z-score
--
-- PAIR A. Same score, same robust-z, same percent deviation. Only the dollars
-- differ. If severity were driven by statistical unusualness these two would be
-- identical; the whole point of Stage 6 is that they are not.
-- =============================================================================
DO $do$
DECLARE big salesops.anomaly_decisions; small salesops.anomaly_decisions;
BEGIN
    big   := pg_temp.dec(DATE '{F_MAJOR}');
    small := pg_temp.dec(DATE '{F_TINY}');

    PERFORM pg_temp.check('impact', 'the pair really is statistically identical',
        big.anomaly_score = small.anomaly_score
        AND big.revenue_robust_z = small.revenue_robust_z
        AND big.revenue_deviation_pct = small.revenue_deviation_pct
        AND big.signal_count = small.signal_count,
        format('score %s, z %s, pct %s', big.anomaly_score, big.revenue_robust_z,
               big.revenue_deviation_pct));

    PERFORM pg_temp.check('impact', 'financial impact changes severity',
        big.severity = 'major' AND small.severity = 'minor',
        format('%s (%s USD) vs %s (%s USD)',
               big.severity, big.absolute_revenue_delta_usd,
               small.severity, small.absolute_revenue_delta_usd));

    PERFORM pg_temp.check('impact', 'severity is not driven by the score alone',
        big.severity <> small.severity AND big.anomaly_score = small.anomaly_score,
        'equal scores, different severities');

    PERFORM pg_temp.check('impact', 'a statistically extreme but tiny event is not escalated',
        NOT small.human_review_required AND small.routing = 'auto_notify',
        format('routing = %s', small.routing));

    PERFORM pg_temp.check('impact', 'the small event still cites its limited impact',
        pg_temp.reasons(DATE '{F_TINY}') LIKE '%INSUFFICIENT_BUSINESS_IMPACT%',
        pg_temp.reasons(DATE '{F_TINY}'));

    -- Impact arithmetic is reported, not just used.
    PERFORM pg_temp.check('impact', 'expected, actual and delta are all persisted',
        big.expected_net_revenue_usd = 10000 AND big.actual_net_revenue_usd = 16000
        AND big.revenue_delta_usd = 6000 AND big.absolute_revenue_delta_usd = 6000
        AND big.revenue_delta_pct = 60,
        format('%s -> %s (%s, %s%%)', big.expected_net_revenue_usd,
               big.actual_net_revenue_usd, big.revenue_delta_usd, big.revenue_delta_pct));

    PERFORM pg_temp.check('impact', 'the signed delta preserves direction',
        (pg_temp.dec(DATE '{F_CRITICAL}')).revenue_delta_usd < 0
        AND big.revenue_delta_usd > 0,
        'collapse negative, spike positive');
END;
$do$;


-- =============================================================================
-- 5. Corroborating signals
--
-- PAIR B. Same dollars and the same score as F_MAJOR, plus a refund spike and an
-- AOV collapse. Critical must require the corroboration, not the money alone.
-- =============================================================================
DO $do$
DECLARE plain salesops.anomaly_decisions; corrob salesops.anomaly_decisions;
        d salesops.anomaly_decisions;
BEGIN
    plain  := pg_temp.dec(DATE '{F_MAJOR}');
    corrob := pg_temp.dec(DATE '{F_CORROB}');

    PERFORM pg_temp.check('corroboration', 'the pair really has identical money',
        plain.absolute_revenue_delta_usd = corrob.absolute_revenue_delta_usd
        AND plain.business_impact_tier = corrob.business_impact_tier
        AND plain.anomaly_score = corrob.anomaly_score,
        format('%s USD, tier %s', plain.absolute_revenue_delta_usd, plain.business_impact_tier));

    PERFORM pg_temp.check('corroboration', 'corroborating signals raise severity',
        plain.severity = 'major' AND corrob.severity = 'critical',
        format('%s vs %s', plain.severity, corrob.severity));

    PERFORM pg_temp.check('corroboration', 'critical cites the combination',
        corrob.decision_reason_code = 'CRITICAL_COMBINED_IMPACT'
        AND pg_temp.reasons(DATE '{F_CORROB}') LIKE '%CRITICAL_COMBINED_IMPACT%',
        pg_temp.reasons(DATE '{F_CORROB}'));

    -- ---- each operational signal is recognised on its own --------------------
    PERFORM pg_temp.check('corroboration', 'refund spikes are recognised',
        pg_temp.reasons(DATE '{F_REFUND}') LIKE '%SEVERE_REFUND_SPIKE%',
        pg_temp.reasons(DATE '{F_REFUND}'));

    PERFORM pg_temp.check('corroboration', 'AOV collapse is recognised',
        pg_temp.reasons(DATE '{F_AOV}') LIKE '%SEVERE_AOV_DECLINE%',
        pg_temp.reasons(DATE '{F_AOV}'));

    PERFORM pg_temp.check('corroboration', 'order-volume collapse is recognised',
        pg_temp.reasons(DATE '{F_ORDERS}') LIKE '%HIGH_ORDER_VOLUME_DECLINE%',
        pg_temp.reasons(DATE '{F_ORDERS}'));

    PERFORM pg_temp.check('corroboration', 'multi-signal events are recognised',
        pg_temp.reasons(DATE '{F_MULTI}') LIKE '%MULTI_SIGNAL_EVENT%',
        pg_temp.reasons(DATE '{F_MULTI}'));

    -- ...but recognition is not escalation. Without material money they stay minor.
    PERFORM pg_temp.check('corroboration', 'operational damage alone does not escalate',
        (pg_temp.dec(DATE '{F_REFUND}')).severity = 'minor'
        AND (pg_temp.dec(DATE '{F_AOV}')).severity = 'minor'
        AND (pg_temp.dec(DATE '{F_ORDERS}')).severity = 'minor'
        AND (pg_temp.dec(DATE '{F_MULTI}')).severity = 'minor',
        'all four remain minor on limited money');

    -- ---- the one-sided tests: improvement is not damage ---------------------
    -- F_IMPROVED moves all three operational measures by MORE than the severe
    -- thresholds, in the favourable direction. A magnitude-based rule would
    -- read three severe failures here and escalate; a direction-aware one
    -- reads none.
    d := pg_temp.dec(DATE '{F_IMPROVED}');
    PERFORM pg_temp.check('corroboration', 'favourable moves are not operational damage',
        pg_temp.reasons(DATE '{F_IMPROVED}') NOT LIKE '%SEVERE%'
        AND pg_temp.reasons(DATE '{F_IMPROVED}') NOT LIKE '%DECLINE%',
        pg_temp.reasons(DATE '{F_IMPROVED}'));

    PERFORM pg_temp.check('corroboration', 'an all-improving day is not escalated',
        d.severity = 'minor' AND NOT d.human_review_required,
        format('%s / %s', d.severity, d.routing));

    -- The comparison that proves it is direction and not magnitude: the same
    -- three measures, same sizes, opposite signs, opposite outcome.
    PERFORM pg_temp.check('corroboration', 'sign, not size, decides operational damage',
        d.severity = 'minor' AND corrob.severity = 'critical'
        AND abs(d.refund_rate_deviation) > 0.1 AND abs(corrob.refund_rate_deviation) > 0.1,
        format('refund %s -> %s, %s -> %s',
               d.refund_rate_deviation, d.severity,
               corrob.refund_rate_deviation, corrob.severity));
END;
$do$;


-- =============================================================================
-- 6. The decision cannot be overridden
--
-- Section 8 of the specification: notification permission and the human-review
-- requirement are deterministic and not writable by anything downstream. These
-- checks prove the database refuses, rather than trusting callers to behave.
-- =============================================================================
DO $do$
DECLARE v_id BIGINT; v_none BIGINT;
BEGIN
    SELECT decision_id INTO v_id  FROM fx WHERE severity = 'critical' LIMIT 1;
    SELECT decision_id INTO v_none FROM fx WHERE baseline_status = 'insufficient_history' LIMIT 1;

    PERFORM pg_temp.check_rejects('overrides',
        'human review cannot be switched off under human_review routing',
        format('UPDATE salesops.anomaly_decisions SET human_review_required = FALSE WHERE decision_id = %s', v_id));

    PERFORM pg_temp.check_rejects('overrides',
        'a critical decision cannot be re-routed to no_action',
        format('UPDATE salesops.anomaly_decisions SET routing = ''no_action'' WHERE decision_id = %s', v_id));

    PERFORM pg_temp.check_rejects('overrides',
        'notification cannot be enabled on a critical decision',
        format('UPDATE salesops.anomaly_decisions SET notification_allowed = TRUE WHERE decision_id = %s', v_id));

    PERFORM pg_temp.check_rejects('overrides',
        'an unscored observation cannot be given a severity',
        format('UPDATE salesops.anomaly_decisions SET severity = ''major'' WHERE decision_id = %s', v_none));

    PERFORM pg_temp.check_rejects('overrides',
        'an unscored observation cannot be notified',
        format('UPDATE salesops.anomaly_decisions SET notification_allowed = TRUE WHERE decision_id = %s', v_none));

    PERFORM pg_temp.check_rejects('overrides',
        'a reason code outside the vocabulary is rejected',
        format('INSERT INTO salesops.anomaly_decision_reasons VALUES (%s, ''LLM_SAYS_ITS_FINE'')', v_id));

    PERFORM pg_temp.check_rejects('overrides',
        'severity outside the enum is rejected',
        format('UPDATE salesops.anomaly_decisions SET severity = ''catastrophic'' WHERE decision_id = %s', v_id));
END;
$do$;

-- Structural: there is nowhere for free text or model output to be written.
DO $do$
DECLARE leaky TEXT;
BEGIN
    SELECT string_agg(column_name, ', ') INTO leaky
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND table_name IN ('anomaly_decisions', 'anomaly_decision_reasons')
      AND (column_name ~ '(llm|prompt|explanation|narrative|hypothesis|reasoning|comment|summary)');

    PERFORM pg_temp.check('overrides', 'the decision tables have no free-text or model column',
                          leaky IS NULL, COALESCE(leaky, 'none'));
END;
$do$;


-- =============================================================================
-- 7. The live dataset
-- =============================================================================
DO $do$
DECLARE d salesops.anomaly_decisions; top_score RECORD;
BEGIN
    SELECT * INTO d FROM salesops.anomaly_decisions
    WHERE calendar_date = DATE '{LIVE_CRITICAL}' AND decision_version = 'stage6-v1';

    PERFORM pg_temp.check('live', '{LIVE_CRITICAL} produces an actionable decision',
        d.decision = 'action_required' AND d.human_review_required
        AND d.severity IN ('major', 'critical'),
        format('%s / %s', d.severity, d.routing));

    PERFORM pg_temp.check('live', '{LIVE_CRITICAL} is backed by measured business impact',
        d.expected_net_revenue_usd IS NOT NULL
        AND d.revenue_delta_usd < 0
        AND d.business_impact_tier IN ('material', 'severe'),
        format('%s expected, delta %s, tier %s', round(d.expected_net_revenue_usd),
               round(d.revenue_delta_usd), d.business_impact_tier));

    SELECT * INTO d FROM salesops.anomaly_decisions
    WHERE calendar_date = DATE '{LIVE_NORMAL_SUNDAY}' AND decision_version = 'stage6-v1';

    PERFORM pg_temp.check('live', '{LIVE_NORMAL_SUNDAY} is not an operational alert',
        d.severity = 'none' AND d.routing = 'no_action'
        AND NOT d.notification_allowed AND NOT d.human_review_required,
        format('%s / %s', d.severity, d.routing));

    PERFORM pg_temp.check('live', '{LIVE_NORMAL_SUNDAY} is recorded as ordinary variation',
        d.decision_reason_code = 'NORMAL_VARIATION', d.decision_reason_code);

    -- The claim that critical is not "the biggest number", checked against the
    -- real series rather than a fixture: the highest-scoring live day must not
    -- be the critical one.
    SELECT calendar_date, severity, anomaly_score INTO top_score
    FROM salesops.anomaly_decisions
    WHERE decision_version = 'stage6-v1' AND is_anomaly
      AND calendar_date >= DATE '2026-01-01'
    ORDER BY anomaly_score DESC LIMIT 1;

    PERFORM pg_temp.check('live', 'the highest-scoring live day is not automatically critical',
        top_score.severity <> 'critical',
        format('%s scored %s -> %s', top_score.calendar_date, top_score.anomaly_score,
               top_score.severity));

    -- Nothing unscorable anywhere in the live set may be actionable.
    PERFORM pg_temp.check('live', 'no unscorable date is actionable',
        NOT EXISTS (SELECT 1 FROM salesops.anomaly_decisions
                    WHERE baseline_status <> 'scored'
                      AND (decision <> 'no_action' OR notification_allowed
                           OR human_review_required)),
        'none');
END;
$do$;


-- =============================================================================
-- 8. Idempotency
-- =============================================================================
CREATE TEMP TABLE snapshot_1 AS
SELECT decision_id, anomaly_id, decision_version, severity, routing, decision,
       notification_allowed, human_review_required, decision_reason_code,
       business_impact_tier, expected_net_revenue_usd, actual_net_revenue_usd,
       revenue_delta_usd, absolute_revenue_delta_usd, revenue_delta_pct,
       (SELECT string_agg(r.reason_code, ',' ORDER BY r.reason_code)
        FROM salesops.anomaly_decision_reasons r WHERE r.decision_id = d.decision_id) AS reasons
FROM salesops.anomaly_decisions d;

DO $do$
BEGIN
    PERFORM * FROM salesops.decide_anomalies('stage6-v1');
    PERFORM * FROM salesops.decide_anomalies('stage6-v1');
END;
$do$;

DO $do$
DECLARE differing INTEGER; before_count INTEGER; after_count INTEGER;
        reason_dupes INTEGER;
BEGIN
    SELECT count(*) INTO before_count FROM snapshot_1;
    SELECT count(*) INTO after_count  FROM salesops.anomaly_decisions;

    PERFORM pg_temp.check('idempotency', 'two further runs create no duplicate decisions',
        before_count = after_count, format('%s -> %s rows', before_count, after_count));

    SELECT count(*) INTO differing FROM (
        SELECT decision_id, severity, routing, decision, notification_allowed,
               human_review_required, decision_reason_code, business_impact_tier,
               expected_net_revenue_usd, actual_net_revenue_usd, revenue_delta_usd,
               absolute_revenue_delta_usd, revenue_delta_pct, reasons
        FROM snapshot_1
        EXCEPT
        SELECT d.decision_id, d.severity, d.routing, d.decision, d.notification_allowed,
               d.human_review_required, d.decision_reason_code, d.business_impact_tier,
               d.expected_net_revenue_usd, d.actual_net_revenue_usd, d.revenue_delta_usd,
               d.absolute_revenue_delta_usd, d.revenue_delta_pct,
               (SELECT string_agg(r.reason_code, ',' ORDER BY r.reason_code)
                FROM salesops.anomaly_decision_reasons r WHERE r.decision_id = d.decision_id)
        FROM salesops.anomaly_decisions d
    ) diff;

    PERFORM pg_temp.check('idempotency', 'every decision value is unchanged',
        differing = 0, format('%s differing row(s)', differing));

    -- decision_id stability matters: a downstream stage will reference it.
    PERFORM pg_temp.check('idempotency', 'decision ids are stable across runs',
        NOT EXISTS (SELECT 1 FROM snapshot_1 s
                    LEFT JOIN salesops.anomaly_decisions d USING (decision_id)
                    WHERE d.decision_id IS NULL),
        'all ids survive');

    SELECT count(*) INTO reason_dupes FROM (
        SELECT decision_id, reason_code FROM salesops.anomaly_decision_reasons
        GROUP BY 1, 2 HAVING count(*) > 1
    ) x;
    PERFORM pg_temp.check('idempotency', 'reason codes are not duplicated',
        reason_dupes = 0, format('%s duplicate(s)', reason_dupes));

    PERFORM pg_temp.check('idempotency', 'one decision per (anomaly, version)',
        NOT EXISTS (SELECT 1 FROM salesops.anomaly_decisions
                    GROUP BY anomaly_id, decision_version HAVING count(*) > 1),
        'unique');
END;
$do$;


-- =============================================================================
-- 9. Versioning
--
-- A new version must be able to reach a DIFFERENT conclusion about the same
-- evidence without disturbing the decision already recorded under the old one.
-- stage6-v2 raises the material threshold far enough to demote F_MAJOR.
-- =============================================================================
INSERT INTO salesops.decision_thresholds (decision_version, threshold_key, threshold_value, unit, description)
SELECT 'stage6-v2', threshold_key,
       CASE threshold_key WHEN 'material_revenue_delta_usd' THEN 20000
                          WHEN 'severe_revenue_delta_usd'   THEN 40000
                          ELSE threshold_value END,
       unit, description
FROM salesops.decision_thresholds WHERE decision_version = 'stage6-v1';

DO $do$
BEGIN
    PERFORM * FROM salesops.decide_anomalies('stage6-v2');
END;
$do$;

DO $do$
DECLARE v1_severity TEXT; v2_severity TEXT; v1_rows INTEGER; v2_rows INTEGER;
        v_anomaly BIGINT;
BEGIN
    SELECT a.anomaly_id INTO v_anomaly FROM salesops.anomaly_daily a
    WHERE a.detector_version = '{FIXTURE_DETECTOR}' AND a.calendar_date = DATE '{F_MAJOR}';

    SELECT severity INTO v1_severity FROM salesops.anomaly_decisions
    WHERE anomaly_id = v_anomaly AND decision_version = 'stage6-v1';
    SELECT severity INTO v2_severity FROM salesops.anomaly_decisions
    WHERE anomaly_id = v_anomaly AND decision_version = 'stage6-v2';

    PERFORM pg_temp.check('versioning', 'a new version can reach a different conclusion',
        v1_severity = 'major' AND v2_severity = 'minor',
        format('v1 %s, v2 %s', v1_severity, v2_severity));

    SELECT count(*) INTO v1_rows FROM salesops.anomaly_decisions WHERE decision_version = 'stage6-v1';
    SELECT count(*) INTO v2_rows FROM salesops.anomaly_decisions WHERE decision_version = 'stage6-v2';

    PERFORM pg_temp.check('versioning', 'both versions coexist over the same evidence',
        v1_rows = v2_rows AND v1_rows > 0, format('%s and %s rows', v1_rows, v2_rows));

    -- The historical decision must be untouched, not merely present.
    PERFORM pg_temp.check('versioning', 'the earlier version is not rewritten',
        NOT EXISTS (
            SELECT 1 FROM snapshot_1 s
            JOIN salesops.anomaly_decisions d USING (decision_id)
            WHERE d.decision_version = 'stage6-v1'
              AND (d.severity, d.routing, d.decision_reason_code)
               IS DISTINCT FROM (s.severity, s.routing, s.decision_reason_code)),
        'stage6-v1 decisions unchanged');

    -- Reason codes must follow the version too.
    PERFORM pg_temp.check('versioning', 'each version carries its own reason codes',
        (SELECT count(*) FROM salesops.anomaly_decision_reasons r
         JOIN salesops.anomaly_decisions d USING (decision_id)
         WHERE d.decision_version = 'stage6-v2') > 0,
        'v2 reasons present');
END;
$do$;

-- Thresholds become immutable once decisions reference them.
DO $do$
BEGIN
    PERFORM pg_temp.check_rejects('versioning',
        'a threshold cannot be changed under existing decisions',
        'UPDATE salesops.decision_thresholds SET threshold_value = 1 '
        'WHERE decision_version = ''stage6-v1'' AND threshold_key = ''material_revenue_delta_usd''');

    PERFORM pg_temp.check_rejects('versioning',
        'a threshold cannot be deleted under existing decisions',
        'DELETE FROM salesops.decision_thresholds '
        'WHERE decision_version = ''stage6-v1'' AND threshold_key = ''severe_revenue_delta_usd''');
END;
$do$;


-- =============================================================================
-- 10. The committed workflow SQL
--
-- Everything above tests the function. This runs the queries out of
-- deterministic-anomaly-decision.json, so the workflow cannot drift from it.
-- =============================================================================
{open_run};

DO $do$
DECLARE r RECORD;
BEGIN
    SELECT * INTO r FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec-stage6';

    PERFORM pg_temp.check('workflow', 'the run opens as running, under its own source',
        r.status = 'running' AND r.source = 'anomaly-decision' AND r.finished_at IS NULL,
        format('%s / %s', r.source, r.status));

    PERFORM pg_temp.check('workflow', 'the window spans the evidence it will decide on',
        r.window_from = (SELECT min(calendar_date) FROM salesops.anomaly_daily)
        AND r.window_to = (SELECT max(calendar_date) FROM salesops.anomaly_daily),
        format('%s .. %s', r.window_from, r.window_to));

    -- Stage 5 has evidence and its last recorded run did not fail: ready.
    PERFORM pg_temp.check('workflow', 'the readiness gate passes on a healthy Stage 5',
        (SELECT count(*) FROM salesops.anomaly_daily) > 0, 'evidence present');
END;
$do$;

-- The decision node itself.
CREATE TEMP TABLE wf_decide AS {decide};

-- The finalize node, with the decision node's own output bound to it.
{capture("wf_final",
         finalize_sql("(SELECT batch_id::text FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec-stage6')",
                      "(SELECT anomalies_evaluated FROM wf_decide)",
                      "(SELECT decisions_written FROM wf_decide)",
                      "stage6-v1"))}

DO $do$
DECLARE f RECORD;
BEGIN
    SELECT * INTO f FROM wf_final;

    -- F_UNMEASURED is planted, so at least one decision rests on unmeasured
    -- impact and the run must say so rather than reporting a clean success.
    PERFORM pg_temp.check('workflow', 'unmeasured impact makes the run partial, not successful',
        f.status = 'partial' AND f.impact_unmeasured > 0,
        format('status %s, %s unmeasured', f.status, f.impact_unmeasured));

    PERFORM pg_temp.check('workflow', 'the run reports counts by severity',
        f.minor > 0 AND f.major > 0 AND f.critical > 0,
        format('minor %s, major %s, critical %s', f.minor, f.major, f.critical));

    PERFORM pg_temp.check('workflow', 'the run reports counts by routing',
        f.routing_no_action > 0 AND f.routing_auto_notify > 0 AND f.routing_human_review > 0,
        format('no_action %s, auto_notify %s, human_review %s',
               f.routing_no_action, f.routing_auto_notify, f.routing_human_review));

    PERFORM pg_temp.check('workflow', 'routing counts reconcile with severity counts',
        f.routing_human_review = f.major + f.critical
        AND f.routing_auto_notify = f.minor,
        format('%s = %s + %s', f.routing_human_review, f.major, f.critical));

    PERFORM pg_temp.check('workflow', 'the run accounts for every evidence row',
        f.anomalies_evaluated = f.decisions_written
        AND f.anomalies_evaluated = (SELECT count(*) FROM salesops.anomaly_daily),
        format('%s evaluated, %s written', f.anomalies_evaluated, f.decisions_written));

    PERFORM pg_temp.check('workflow', 'unscorable dates are reported, not hidden',
        f.unscorable >= 2, format('%s unscorable', f.unscorable));
END;
$do$;

-- With the unmeasurable fixture gone, the same node must report success.
DELETE FROM salesops.anomaly_decision_reasons
WHERE decision_id IN (SELECT decision_id FROM salesops.anomaly_decisions
                      WHERE calendar_date = DATE '{F_UNMEASURED}');
DELETE FROM salesops.anomaly_decisions WHERE calendar_date = DATE '{F_UNMEASURED}';

{capture("wf_final_clean",
         finalize_sql("(SELECT batch_id::text FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec-stage6')",
                      "(SELECT anomalies_evaluated FROM wf_decide)",
                      "(SELECT decisions_written FROM wf_decide)",
                      "stage6-v1"))}

-- A run that wrote nothing over a populated evidence table is a failure.
{capture("wf_final_empty",
         finalize_sql("(SELECT batch_id::text FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec-stage6')",
                      "'90'", "'0'", "stage6-v1"))}

DO $do$
DECLARE clean RECORD; empty RECORD;
BEGIN
    SELECT * INTO clean FROM wf_final_clean;
    PERFORM pg_temp.check('workflow', 'a fully measured run reports success',
        clean.status = 'success' AND clean.impact_unmeasured = 0,
        format('status %s', clean.status));

    SELECT * INTO empty FROM wf_final_empty;
    PERFORM pg_temp.check('workflow', 'writing no decisions is recorded as failed, not success',
        empty.status = 'failed', format('status %s', empty.status));

    PERFORM pg_temp.check('workflow', 'a failed run carries a reason',
        (SELECT error_message FROM salesops.ingestion_runs
         WHERE n8n_execution_id = 'test-exec-stage6') IS NOT NULL,
        'error_message present');
END;
$do$;

-- The abort branch: Stage 5 not ready.
CREATE TEMP TABLE abort_run AS
SELECT batch_id FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec-stage6';

{capture("wf_abort", abort_sql)}

DO $do$
DECLARE a RECORD; decisions_now INTEGER;
BEGIN
    SELECT * INTO a FROM wf_abort;
    SELECT count(*) INTO decisions_now FROM salesops.anomaly_decisions
    WHERE decision_version = 'stage6-v1';

    PERFORM pg_temp.check('workflow', 'the abort branch closes the run as failed',
        a.status = 'failed' AND a.error_message IS NOT NULL, a.error_message);

    PERFORM pg_temp.check('workflow', 'the abort branch decides nothing',
        decisions_now > 0, 'existing decisions untouched');
END;
$do$;


-- =============================================================================
-- 11. Audit trail
--
-- Section 20: an operator must be able to answer "why is this critical?" from
-- the database alone.
-- =============================================================================
DO $do$
DECLARE v RECORD; orphan INTEGER;
BEGIN
    SELECT * INTO v FROM salesops.anomaly_decision_audit
    WHERE calendar_date = DATE '{LIVE_CRITICAL}' AND decision_version = 'stage6-v1';

    PERFORM pg_temp.check('audit', 'the audit view exposes evidence, money, verdict and reasons',
        v.anomaly_score IS NOT NULL AND v.expected_net_revenue_usd IS NOT NULL
        AND v.severity IS NOT NULL AND v.routing IS NOT NULL
        AND v.all_reasons IS NOT NULL AND v.decided_at IS NOT NULL
        AND v.decision_version IS NOT NULL,
        format('%s: %s', v.severity, v.all_reasons));

    PERFORM pg_temp.check('audit', 'the primary reason is among the full reason set',
        position(v.primary_reason IN v.all_reasons) > 0,
        format('%s in [%s]', v.primary_reason, v.all_reasons));

    -- Every decision must be explainable. A decision with no reason code is an
    -- opaque classification, which is exactly what section 20 forbids.
    SELECT count(*) INTO orphan
    FROM salesops.anomaly_decisions d
    WHERE NOT EXISTS (SELECT 1 FROM salesops.anomaly_decision_reasons r
                      WHERE r.decision_id = d.decision_id);
    PERFORM pg_temp.check('audit', 'no decision exists without a reason code',
        orphan = 0, format('%s unexplained decision(s)', orphan));

    PERFORM pg_temp.check('audit', 'every reason code is in the published vocabulary',
        NOT EXISTS (SELECT 1 FROM salesops.anomaly_decision_reasons r
                    LEFT JOIN salesops.decision_reason_codes c USING (reason_code)
                    WHERE c.reason_code IS NULL),
        'all codes documented');

    PERFORM pg_temp.check('audit', 'every threshold is documented with a unit',
        NOT EXISTS (SELECT 1 FROM salesops.decision_thresholds
                    WHERE description = '' OR unit IS NULL),
        'all documented');
END;
$do$;


-- =============================================================================
-- Report
-- =============================================================================
\\echo ''
\\echo '=============== STAGE 6 DECISION TEST RESULTS ==============='

SELECT CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END AS result,
       section, name, NULLIF(detail, '') AS detail
FROM test_results ORDER BY id;

SELECT count(*) AS total,
       count(*) FILTER (WHERE passed) AS passed,
       count(*) FILTER (WHERE NOT passed) AS failed
FROM test_results;

DO $do$
DECLARE failed INTEGER; names TEXT;
BEGIN
    SELECT count(*), string_agg(name, '; ') INTO failed, names
    FROM test_results WHERE NOT passed;
    IF failed > 0 THEN
        RAISE EXCEPTION 'STAGE 6 TESTS FAILED: % check(s) -> %', failed, names;
    END IF;
    RAISE NOTICE 'All Stage 6 decision checks passed.';
END;
$do$;

ROLLBACK;
"""


def main() -> int:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "salesops", "-d", "salesops", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        cwd=REPO_ROOT,
        input=build_script(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
