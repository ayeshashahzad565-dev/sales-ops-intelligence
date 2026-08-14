-- =============================================================================
-- V008  Stage 6: the deterministic decision layer
--
--   Stage 4  what happened            (kpi_daily)
--   Stage 5  how unusual it was       (anomaly_daily)          <- statistics
--   Stage 6  whether it matters       (anomaly_decisions)      <- THIS FILE
--   Stage 7  why it might have happened                        <- LLM, later
--
-- Everything here is deterministic SQL. Given the same Stage 5 evidence, the
-- same KPI row and the same decision_version, this file produces exactly the
-- same severity, routing, flags and reason codes on every machine, forever.
--
-- The architectural point
-- -----------------------
-- Severity, routing, notification permission and the human-review requirement
-- are decided HERE, before any language model is involved. Stage 7 will receive
-- a decision that has already been made and add reasoning to it; it cannot
-- create, upgrade, downgrade or veto one. There is deliberately no column in
-- this schema an LLM is allowed to write.
--
-- That ordering is what makes "the LLM proposes, deterministic rules decide, a
-- human approves" true of the system rather than merely stated in a README.
--
-- Why SQL and not more Python
-- ---------------------------
-- Stage 5 is in Python because robust statistics need real numerics and a unit
-- test suite. These are business rules over columns that already exist in one
-- database: expressing them as SQL keeps them next to the data, keeps them
-- inspectable by anyone with psql, and avoids shipping a second service whose
-- only job is comparing numbers to constants.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Reason codes
--
-- A closed vocabulary, in a table rather than a CHECK constraint, so an operator
-- reading a decision can look up what a code means without reading this file -
-- and so a new code cannot be introduced by a typo in an INSERT.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.decision_reason_codes (
    reason_code   TEXT PRIMARY KEY,
    description   TEXT NOT NULL,

    -- TRUE when the code is evidence that something needs attention. Used by the
    -- audit view and by tests; the decision rules themselves never read it, so
    -- it cannot become a hidden second severity model.
    is_escalating BOOLEAN NOT NULL,

    CONSTRAINT decision_reason_codes_format CHECK (reason_code ~ '^[A-Z][A-Z_]+[A-Z]$')
);

COMMENT ON TABLE salesops.decision_reason_codes IS
    'Closed vocabulary of Stage 6 decision reasons. Structured codes, not free text: '
    'a decision must be explainable by machine, without an LLM.';

INSERT INTO salesops.decision_reason_codes (reason_code, description, is_escalating) VALUES
    ('STATISTICAL_ANOMALY',
     'Stage 5 flagged this date: its weighted robust-z score reached the detector threshold.',
     TRUE),
    ('HIGH_REVENUE_IMPACT',
     'Net USD revenue differed from its calendar-aware baseline by a material absolute AND relative amount.',
     TRUE),
    ('SEVERE_REFUND_SPIKE',
     'The refund rate rose above its baseline by at least the severe-spike threshold, in absolute rate points.',
     TRUE),
    ('SEVERE_AOV_DECLINE',
     'Average order value fell below its baseline by at least the severe-decline threshold.',
     TRUE),
    ('HIGH_ORDER_VOLUME_DECLINE',
     'Order count fell below its baseline by at least the severe-decline threshold.',
     TRUE),
    ('MULTI_SIGNAL_EVENT',
     'Three or more of the four Stage 5 signals independently cleared statistical significance.',
     TRUE),
    ('CRITICAL_COMBINED_IMPACT',
     'Severe or material revenue impact arriving together with corroborating operational damage.',
     TRUE),
    ('INSUFFICIENT_BUSINESS_IMPACT',
     'Statistically unusual, but the revenue difference is too small to act on. Severity held at minor.',
     FALSE),
    ('BUSINESS_IMPACT_UNAVAILABLE',
     'No baseline revenue was persisted for this date, so impact could not be measured. '
     'Escalated rather than assumed harmless.',
     TRUE),
    ('INSUFFICIENT_HISTORY',
     'Stage 5 had too few comparable prior observations to judge this date. Not an anomaly, and not normal either.',
     FALSE),
    ('INCOMPLETE_KPI',
     'The date''s KPI row lacked full FX coverage, so its money columns are understated and were not judged.',
     FALSE),
    ('NORMAL_VARIATION',
     'Scored against a real baseline and found ordinary. No operational action.',
     FALSE)
ON CONFLICT (reason_code) DO UPDATE
    SET description   = EXCLUDED.description,
        is_escalating = EXCLUDED.is_escalating;


-- =============================================================================
-- 2. Thresholds
--
-- Every number the decision rules compare against lives in this table, keyed by
-- decision_version. Not constants buried in a function body: an operator asking
-- "what counts as material?" runs one SELECT, and a threshold change is forced
-- to be a versioned, visible event.
--
-- Calibration basis (from the live 90-day series, documented so the numbers are
-- arguable rather than arbitrary):
--
--     net revenue per trading day     min 1,846   p25 7,520   median 11,146
--                                     p75 13,723  max 18,495
--     typical weekday                 ~11-13k
--     typical weekend day             ~5.7-6.4k
--     typical refund rate             ~0.02-0.05
--
-- The dollar thresholds are expressed as fractions of a typical trading day,
-- because that is the unit an operator actually reasons in: "we lost most of a
-- day" is a sentence; "the robust z was -3.78" is not.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.decision_thresholds (
    decision_version TEXT          NOT NULL,
    threshold_key    TEXT          NOT NULL,
    threshold_value  NUMERIC(18,6) NOT NULL,
    unit             TEXT          NOT NULL,
    description      TEXT          NOT NULL,

    PRIMARY KEY (decision_version, threshold_key),

    CONSTRAINT decision_thresholds_unit_valid
        CHECK (unit IN ('usd', 'percent', 'rate', 'count')),
    CONSTRAINT decision_thresholds_version_format
        CHECK (decision_version ~ '^stage[0-9]+-v[0-9]+$'),
    CONSTRAINT decision_thresholds_positive
        CHECK (threshold_value > 0)
);

COMMENT ON TABLE salesops.decision_thresholds IS
    'Every constant the Stage 6 rules compare against, keyed by decision_version. '
    'Immutable once decisions exist for that version - see the guard trigger below.';

INSERT INTO salesops.decision_thresholds
    (decision_version, threshold_key, threshold_value, unit, description) VALUES

    ('stage6-v1', 'trivial_revenue_delta_usd', 1000, 'usd',
     'Below this absolute revenue difference there is nothing an operator can act on. '
     'About 9% of a median trading day.'),

    ('stage6-v1', 'material_revenue_delta_usd', 4000, 'usd',
     'A material revenue difference: roughly a third of a typical weekday, or two thirds '
     'of an entire typical weekend day. Reaching this is what makes an anomaly major.'),

    ('stage6-v1', 'severe_revenue_delta_usd', 9000, 'usd',
     'Approaching a whole trading day of net revenue gained or lost (median day is 11,146).'),

    ('stage6-v1', 'material_revenue_delta_pct', 20, 'percent',
     'Relative gate on material impact. Both the absolute and the relative test must pass, so '
     'a large dollar move on an exceptionally large day is not automatically material.'),

    ('stage6-v1', 'severe_revenue_delta_pct', 40, 'percent',
     'Relative gate on severe impact, applied the same way.'),

    ('stage6-v1', 'refund_rate_spike_severe', 0.10, 'rate',
     'A rise of ten percentage points in the refund rate. Baseline rates sit near 0.02-0.05, '
     'so this is refunds at three to six times normal. One-sided on purpose: refunds FALLING '
     'is not an incident.'),

    ('stage6-v1', 'aov_decline_pct_severe', 30, 'percent',
     'A third off average order value. One-sided: rising order value is not operational damage.'),

    ('stage6-v1', 'orders_decline_pct_severe', 30, 'percent',
     'A third of the day''s order volume gone. One-sided, for the same reason.'),

    ('stage6-v1', 'multi_signal_min_count', 3, 'count',
     'Three of the four Stage 5 signals individually significant. Corroboration across '
     'independent measures, not one measure shouting louder.')
ON CONFLICT (decision_version, threshold_key) DO NOTHING;
-- DO NOTHING, not DO UPDATE. Re-applying this migration must never silently
-- re-point historical decisions at different numbers. Changing a threshold means
-- introducing a new decision_version, which is what the trigger below enforces.


-- =============================================================================
-- 3. Decisions
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.anomaly_decisions (
    decision_id             BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The Stage 5 record this decision was made about.
    anomaly_id              BIGINT       NOT NULL,
    calendar_date           DATE         NOT NULL,

    -- Which ruleset produced it. Two versions coexist on the same anomaly so a
    -- threshold change can be diffed against its predecessor instead of erasing it.
    decision_version        TEXT         NOT NULL,

    -- --- statistical evidence, as it stood when the decision was made ---------
    -- A snapshot, not a join. anomaly_daily is upserted in place by the detector,
    -- so a later Stage 5 re-run would otherwise rewrite the evidence under a
    -- decision that was made from different numbers. Re-running Stage 6 refreshes
    -- the snapshot and the verdict together, in one statement, so the two can
    -- never disagree.
    detector_version        TEXT         NOT NULL,
    baseline_status         TEXT         NOT NULL,
    anomaly_score           NUMERIC(12,4),
    is_anomaly              BOOLEAN      NOT NULL,
    signal_count            INTEGER      NOT NULL,
    dominant_signal         TEXT,

    revenue_robust_z        NUMERIC(14,4),
    aov_robust_z            NUMERIC(14,4),
    refund_robust_z         NUMERIC(14,4),
    orders_robust_z         NUMERIC(14,4),

    revenue_deviation_pct   NUMERIC(14,4),
    aov_deviation_pct       NUMERIC(14,4),
    refund_rate_deviation   NUMERIC(14,6),
    orders_deviation_pct    NUMERIC(14,4),

    -- --- business impact ------------------------------------------------------
    -- Expected revenue is Stage 5's own baseline median (V007), not a second
    -- estimate. Populated exactly when the date was scored.
    expected_net_revenue_usd   NUMERIC(18,4),
    actual_net_revenue_usd     NUMERIC(18,4),
    revenue_delta_usd          NUMERIC(18,4),   -- signed: actual - expected
    absolute_revenue_delta_usd NUMERIC(18,4),   -- magnitude, what thresholds compare
    revenue_delta_pct          NUMERIC(14,4),
    business_impact_tier       TEXT,

    -- --- the decision ---------------------------------------------------------
    severity                TEXT         NOT NULL,
    routing                 TEXT         NOT NULL,
    decision                TEXT         NOT NULL,
    notification_allowed    BOOLEAN      NOT NULL,
    human_review_required   BOOLEAN      NOT NULL,

    -- The single code that best explains the severity. The full set lives in
    -- anomaly_decision_reasons; this one exists so a list of decisions is
    -- readable without a join.
    decision_reason_code    TEXT         NOT NULL,

    decided_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Idempotency: re-running the engine updates in place, never duplicates.
    CONSTRAINT anomaly_decisions_unique_per_version
        UNIQUE (anomaly_id, decision_version),

    -- References the Stage 5 evidence. No ON DELETE CASCADE: an audit record
    -- must not vanish because someone pruned a detector version. Deleting
    -- evidence that decisions were made from has to be a deliberate, two-step act.
    CONSTRAINT anomaly_decisions_anomaly_fk
        FOREIGN KEY (anomaly_id) REFERENCES salesops.anomaly_daily (anomaly_id),

    -- dim_date, NOT kpi_daily. kpi_daily is rebuilt wholesale (DELETE + INSERT)
    -- by refresh_kpi_daily(), so a foreign key onto it would either block every
    -- rebuild or cascade-delete the decision history.
    CONSTRAINT anomaly_decisions_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    CONSTRAINT anomaly_decisions_reason_fk
        FOREIGN KEY (decision_reason_code)
        REFERENCES salesops.decision_reason_codes (reason_code),

    CONSTRAINT anomaly_decisions_version_format
        CHECK (decision_version ~ '^stage[0-9]+-v[0-9]+$'),

    CONSTRAINT anomaly_decisions_severity_valid
        CHECK (severity IN ('none', 'minor', 'major', 'critical')),
    CONSTRAINT anomaly_decisions_routing_valid
        CHECK (routing IN ('no_action', 'auto_notify', 'human_review')),
    CONSTRAINT anomaly_decisions_decision_valid
        CHECK (decision IN ('no_action', 'action_required')),
    CONSTRAINT anomaly_decisions_impact_tier_valid
        CHECK (business_impact_tier IS NULL
               OR business_impact_tier IN ('unknown', 'trivial', 'limited', 'material', 'severe')),

    -- ---- the invariants that make this layer trustworthy ---------------------
    -- Routing is a total function of severity. Not a convention - a constraint.
    -- A future writer cannot produce a critical anomaly that quietly routes to
    -- no_action, whatever code path it came from.
    CONSTRAINT anomaly_decisions_routing_follows_severity
        CHECK ((severity = 'none'                 AND routing = 'no_action')
            OR (severity = 'minor'                AND routing = 'auto_notify')
            OR (severity IN ('major', 'critical') AND routing = 'human_review')),

    -- The two published flags ARE the routing, restated. They cannot drift from
    -- it, so a downstream consumer may trust whichever it reads.
    CONSTRAINT anomaly_decisions_notification_follows_routing
        CHECK (notification_allowed = (routing = 'auto_notify')),
    CONSTRAINT anomaly_decisions_review_follows_routing
        CHECK (human_review_required = (routing = 'human_review')),
    CONSTRAINT anomaly_decisions_decision_follows_routing
        CHECK (decision = CASE WHEN routing = 'no_action'
                               THEN 'no_action' ELSE 'action_required' END),

    -- Nothing that was not scored, and nothing Stage 5 did not flag, may carry
    -- severity. This is the structural reason an insufficient-history date can
    -- never become an alert.
    CONSTRAINT anomaly_decisions_severity_needs_anomaly
        CHECK (is_anomaly OR severity = 'none'),
    CONSTRAINT anomaly_decisions_severity_needs_scoring
        CHECK (baseline_status = 'scored' OR severity = 'none'),

    -- No automated notification without a real, scored, flagged anomaly.
    CONSTRAINT anomaly_decisions_no_blind_notification
        CHECK (NOT notification_allowed OR (is_anomaly AND baseline_status = 'scored')),

    CONSTRAINT anomaly_decisions_status_valid
        CHECK (baseline_status IN ('scored', 'insufficient_history', 'incomplete_kpi')),
    CONSTRAINT anomaly_decisions_signal_count_range
        CHECK (signal_count BETWEEN 0 AND 4),

    -- ---- impact arithmetic ---------------------------------------------------
    -- The delta exists exactly when both of its inputs do.
    CONSTRAINT anomaly_decisions_delta_needs_inputs
        CHECK ((revenue_delta_usd IS NOT NULL)
               = (expected_net_revenue_usd IS NOT NULL AND actual_net_revenue_usd IS NOT NULL)),
    CONSTRAINT anomaly_decisions_absolute_delta_agrees
        CHECK (absolute_revenue_delta_usd IS NOT DISTINCT FROM abs(revenue_delta_usd)),

    -- The impact block belongs to scored rows only. An unscored date has no
    -- baseline, so reporting an expected revenue for it would be an invention.
    CONSTRAINT anomaly_decisions_impact_needs_scoring
        CHECK (baseline_status = 'scored'
               OR (expected_net_revenue_usd IS NULL
                   AND actual_net_revenue_usd IS NULL
                   AND business_impact_tier IS NULL)),

    -- Severity above minor must rest on a measured, or explicitly unmeasurable,
    -- impact - never on a silent NULL.
    CONSTRAINT anomaly_decisions_escalation_needs_impact
        CHECK (severity IN ('none', 'minor') OR business_impact_tier IS NOT NULL)
);

COMMENT ON TABLE salesops.anomaly_decisions IS
    'Stage 6 deterministic decisions: business severity, routing, notification permission '
    'and human-review requirement for each Stage 5 result. Produced entirely by SQL rules. '
    'No column here is writable by a language model.';

COMMENT ON COLUMN salesops.anomaly_decisions.decision_version IS
    'The ruleset and threshold set used. Thresholds become immutable once decisions '
    'reference them; changing one means a new version.';
COMMENT ON COLUMN salesops.anomaly_decisions.expected_net_revenue_usd IS
    'Stage 5''s own baseline median (anomaly_daily.revenue_baseline_median). Reused rather '
    'than recomputed, so Stage 6 cannot develop a second, contradictory idea of "expected".';
COMMENT ON COLUMN salesops.anomaly_decisions.business_impact_tier IS
    'trivial | limited | material | severe, from the absolute AND relative revenue difference. '
    '''unknown'' means impact could not be measured, which escalates rather than excuses.';
COMMENT ON COLUMN salesops.anomaly_decisions.notification_allowed IS
    'TRUE only where the deterministic rules permit an AUTOMATED notification - i.e. minor '
    'severity. Major and critical go to a person, who decides what gets communicated. '
    'This never means "a model thought it was important".';
COMMENT ON COLUMN salesops.anomaly_decisions.decided_at IS
    'When the engine last evaluated this decision. Changes on every run by design; '
    'idempotency is asserted over the decision columns, not this one.';

-- "What needs a human, most recent first" - the operator's query.
CREATE INDEX IF NOT EXISTS idx_anomaly_decisions_review
    ON salesops.anomaly_decisions (calendar_date DESC)
    WHERE human_review_required;

CREATE INDEX IF NOT EXISTS idx_anomaly_decisions_version_date
    ON salesops.anomaly_decisions (decision_version, calendar_date);


-- =============================================================================
-- 4. Reason codes per decision
--
-- A child table rather than a delimited string: reason codes are queried
-- ("how often does a refund spike drive an escalation?"), and a comma-separated
-- column answers that question only with LIKE and a prayer.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.anomaly_decision_reasons (
    decision_id BIGINT NOT NULL,
    reason_code TEXT   NOT NULL,

    PRIMARY KEY (decision_id, reason_code),

    -- Reasons are part of the decision, not an annotation on it: deleting a
    -- decision must take its reasons with it.
    CONSTRAINT anomaly_decision_reasons_decision_fk
        FOREIGN KEY (decision_id) REFERENCES salesops.anomaly_decisions (decision_id)
        ON DELETE CASCADE,

    CONSTRAINT anomaly_decision_reasons_code_fk
        FOREIGN KEY (reason_code) REFERENCES salesops.decision_reason_codes (reason_code)
);

COMMENT ON TABLE salesops.anomaly_decision_reasons IS
    'Every reason code that applied to a decision. Structured, closed-vocabulary '
    'explainability - the audit trail that makes Stage 7 optional rather than load-bearing.';

CREATE INDEX IF NOT EXISTS idx_anomaly_decision_reasons_code
    ON salesops.anomaly_decision_reasons (reason_code);


-- =============================================================================
-- 5. Threshold immutability guard
--
-- Section 14 of the specification says thresholds must never change silently
-- under existing decisions. A comment saying so is a hope; this is the rule.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_decision_thresholds()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_version TEXT := OLD.decision_version;
    v_count   INTEGER;
BEGIN
    SELECT count(*) INTO v_count
    FROM salesops.anomaly_decisions
    WHERE decision_version = v_version;

    IF v_count > 0 THEN
        RAISE EXCEPTION
            'decision_version % already has % decision(s); its thresholds are immutable. '
            'Introduce a new decision_version instead.', v_version, v_count
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

COMMENT ON FUNCTION salesops.guard_decision_thresholds() IS
    'Refuses to alter or remove a threshold once decisions have been made with it, '
    'so historical decisions stay reproducible from the stored configuration.';

DROP TRIGGER IF EXISTS trg_guard_decision_thresholds ON salesops.decision_thresholds;
CREATE TRIGGER trg_guard_decision_thresholds
    BEFORE UPDATE OR DELETE ON salesops.decision_thresholds
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_decision_thresholds();


-- =============================================================================
-- 6. Threshold lookup
--
-- Fails loudly on a missing key. A decision engine that silently defaulted a
-- threshold to zero would classify everything as severe and be believed.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.decision_threshold(p_version TEXT, p_key TEXT)
RETURNS NUMERIC
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_value NUMERIC;
BEGIN
    SELECT threshold_value INTO v_value
    FROM salesops.decision_thresholds
    WHERE decision_version = p_version AND threshold_key = p_key;

    IF v_value IS NULL THEN
        RAISE EXCEPTION 'No threshold %.% is configured', p_version, p_key
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN v_value;
END;
$$;


-- =============================================================================
-- 7. decide_anomalies()
--
-- The decision engine. One function, one transaction, fully idempotent.
--
-- THE SEVERITY MODEL
-- ==================
-- Two independent axes, deliberately kept apart:
--
--   MONEY          how far net revenue moved from what this weekday normally
--                  earns, in dollars AND in percent - both gates must pass.
--                    trivial  < 1,000
--                    limited  1,000 .. material
--                    material >= 4,000  and >= 20%
--                    severe   >= 9,000  and >= 40%
--
--   CORROBORATION  independent operational damage:
--                    refund rate up   >= 10 points
--                    AOV       down   >= 30%
--                    order volume down>= 30%
--                  plus the multi-signal case: 3 of Stage 5's 4 signals
--                  independently significant.
--
-- and the ladder:
--
--   critical   severe money WITH corroboration
--              OR (severe or material money) WITH two operational failures
--   major      severe or material money
--   minor      flagged by Stage 5, but the money did not reach material
--   none       not flagged, or not scorable
--
-- Why it is built this way
-- ------------------------
-- Money gates every escalation, so a statistically spectacular move on a small
-- day stays minor - which is the single most common way an anomaly detector
-- destroys its own credibility. Corroboration gates CRITICAL specifically, so
-- critical is never a synonym for "biggest z-score": it means the revenue moved
-- AND something operational broke at the same time.
--
-- The live data demonstrates the distinction rather than merely claiming it.
-- 2026-08-09 has the highest anomaly score in the series (12.94, a Sunday at
-- 3.3x its baseline) and comes out MAJOR - one measure, moving upward, with no
-- operational damage behind it. 2026-08-05 scores lower (8.93) and comes out
-- CRITICAL, because its revenue shortfall arrives with the refund rate up 33
-- points and average order value down 64%. Ranking those two by score alone
-- would put the wrong one at the top of the queue.
--
-- Direction
-- ---------
-- Revenue impact is direction-neutral: a 3x day is as much worth a human's
-- attention as a collapse, and is very often a data problem. The signed delta is
-- stored so direction is never lost. The three operational tests, by contrast,
-- are one-sided - refunds falling and order value rising are not incidents.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.decide_anomalies(
    p_decision_version TEXT DEFAULT 'stage6-v1'
)
RETURNS TABLE (
    anomalies_evaluated         INTEGER,
    decisions_written           INTEGER,
    severity_none               INTEGER,
    severity_minor              INTEGER,
    severity_major              INTEGER,
    severity_critical           INTEGER,
    routing_no_action           INTEGER,
    routing_auto_notify         INTEGER,
    routing_human_review        INTEGER,
    notification_allowed_count  INTEGER,
    human_review_required_count INTEGER,
    unscorable_count            INTEGER,
    reason_codes_written        INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_written INTEGER;
    v_reasons INTEGER;
BEGIN
    -- Materialised because three separate statements need the same computed
    -- classification, and recomputing it risks the three disagreeing.
    -- Dropped explicitly so the function can be called twice in one transaction,
    -- which is exactly what the idempotency test does.
    DROP TABLE IF EXISTS pg_temp_decision_input;
    CREATE TEMP TABLE pg_temp_decision_input ON COMMIT DROP AS
    WITH t AS (
        SELECT
            salesops.decision_threshold(p_decision_version, 'trivial_revenue_delta_usd')  AS trivial_usd,
            salesops.decision_threshold(p_decision_version, 'material_revenue_delta_usd') AS material_usd,
            salesops.decision_threshold(p_decision_version, 'severe_revenue_delta_usd')   AS severe_usd,
            salesops.decision_threshold(p_decision_version, 'material_revenue_delta_pct') AS material_pct,
            salesops.decision_threshold(p_decision_version, 'severe_revenue_delta_pct')   AS severe_pct,
            salesops.decision_threshold(p_decision_version, 'refund_rate_spike_severe')   AS refund_spike,
            salesops.decision_threshold(p_decision_version, 'aov_decline_pct_severe')     AS aov_decline,
            salesops.decision_threshold(p_decision_version, 'orders_decline_pct_severe')  AS orders_decline,
            salesops.decision_threshold(p_decision_version, 'multi_signal_min_count')     AS multi_signal_min
    ),
    -- Step 1: evidence. Stage 5's row, plus the day's actual revenue.
    evidence AS (
        SELECT
            a.anomaly_id,
            a.calendar_date,
            a.detector_version,
            a.baseline_status,
            a.anomaly_score,
            a.is_anomaly,
            a.signal_count,
            a.dominant_signal,
            a.revenue_robust_z, a.aov_robust_z, a.refund_robust_z, a.orders_robust_z,
            a.revenue_deviation_pct, a.aov_deviation_pct,
            a.refund_rate_deviation, a.orders_deviation_pct,
            -- The impact block is populated for scored rows only. An unscored
            -- date has no baseline; reporting an expected revenue for it would
            -- be an invention, and NULL is the honest answer.
            CASE WHEN a.baseline_status = 'scored'
                 THEN a.revenue_baseline_median END AS expected_net_revenue_usd,
            CASE WHEN a.baseline_status = 'scored'
                 THEN k.net_revenue_usd     END AS actual_net_revenue_usd
        FROM salesops.anomaly_daily a
        LEFT JOIN salesops.kpi_daily k ON k.calendar_date = a.calendar_date
    ),
    -- Step 2: business impact, in dollars and in percent.
    impact AS (
        SELECT
            e.*,
            e.actual_net_revenue_usd - e.expected_net_revenue_usd AS revenue_delta_usd,
            abs(e.actual_net_revenue_usd - e.expected_net_revenue_usd) AS abs_delta,
            round(100.0 * (e.actual_net_revenue_usd - e.expected_net_revenue_usd)
                  / NULLIF(abs(e.expected_net_revenue_usd), 0), 4) AS revenue_delta_pct
        FROM evidence e
    ),
    -- Step 3: classify money, and test for operational damage.
    classified AS (
        SELECT
            i.*,
            CASE
                WHEN i.baseline_status <> 'scored'                 THEN NULL
                WHEN i.abs_delta IS NULL OR i.revenue_delta_pct IS NULL
                                                                   THEN 'unknown'
                WHEN i.abs_delta >= t.severe_usd
                     AND abs(i.revenue_delta_pct) >= t.severe_pct   THEN 'severe'
                WHEN i.abs_delta >= t.material_usd
                     AND abs(i.revenue_delta_pct) >= t.material_pct THEN 'material'
                WHEN i.abs_delta >= t.trivial_usd                   THEN 'limited'
                ELSE 'trivial'
            END AS business_impact_tier,

            -- One-sided on purpose: only deterioration counts as damage.
            COALESCE(i.refund_rate_deviation >=  t.refund_spike,   FALSE) AS refund_spike,
            COALESCE(i.aov_deviation_pct     <= -t.aov_decline,    FALSE) AS aov_collapse,
            COALESCE(i.orders_deviation_pct  <= -t.orders_decline, FALSE) AS orders_collapse,
            (i.signal_count >= t.multi_signal_min)                        AS multi_signal
        FROM impact i CROSS JOIN t
    ),
    counted AS (
        SELECT c.*,
               (c.refund_spike::INT + c.aov_collapse::INT + c.orders_collapse::INT) AS severe_ops
        FROM classified c
    ),
    -- Step 4: severity, then everything that follows from it.
    graded AS (
        SELECT c.*,
            CASE
                WHEN c.baseline_status <> 'scored' THEN 'none'
                WHEN NOT c.is_anomaly              THEN 'none'
                -- Impact could not be measured. Escalate: an unmeasured impact
                -- is not a small one, and defaulting to minor would let a
                -- missing baseline silence a real event.
                WHEN c.business_impact_tier = 'unknown' THEN 'major'
                WHEN c.business_impact_tier = 'severe'
                     AND (c.severe_ops >= 1 OR c.multi_signal)         THEN 'critical'
                WHEN c.business_impact_tier IN ('severe', 'material')
                     AND c.severe_ops >= 2                             THEN 'critical'
                WHEN c.business_impact_tier IN ('severe', 'material')  THEN 'major'
                ELSE 'minor'
            END AS severity
        FROM counted c
    )
    SELECT
        g.*,
        r.routing,
        CASE WHEN r.routing = 'no_action' THEN 'no_action' ELSE 'action_required' END AS decision,
        (r.routing = 'auto_notify')  AS notification_allowed,
        (r.routing = 'human_review') AS human_review_required,
        -- The one code that best explains the severity. Ordered most specific
        -- first, so the headline reason is the rule that actually decided it.
        CASE
            WHEN g.severity = 'critical'                    THEN 'CRITICAL_COMBINED_IMPACT'
            WHEN g.business_impact_tier = 'unknown'
                 AND g.is_anomaly                           THEN 'BUSINESS_IMPACT_UNAVAILABLE'
            WHEN g.severity = 'major'                       THEN 'HIGH_REVENUE_IMPACT'
            WHEN g.severity = 'minor' AND g.refund_spike    THEN 'SEVERE_REFUND_SPIKE'
            WHEN g.severity = 'minor' AND g.aov_collapse    THEN 'SEVERE_AOV_DECLINE'
            WHEN g.severity = 'minor' AND g.orders_collapse THEN 'HIGH_ORDER_VOLUME_DECLINE'
            WHEN g.severity = 'minor' AND g.multi_signal    THEN 'MULTI_SIGNAL_EVENT'
            WHEN g.severity = 'minor'                       THEN 'INSUFFICIENT_BUSINESS_IMPACT'
            WHEN g.baseline_status = 'insufficient_history' THEN 'INSUFFICIENT_HISTORY'
            WHEN g.baseline_status = 'incomplete_kpi'       THEN 'INCOMPLETE_KPI'
            ELSE 'NORMAL_VARIATION'
        END AS decision_reason_code
    FROM graded g
    CROSS JOIN LATERAL (
        SELECT CASE g.severity
                   WHEN 'none'  THEN 'no_action'
                   WHEN 'minor' THEN 'auto_notify'
                   ELSE              'human_review'
               END AS routing
    ) r;

    -- ---- persist the decisions ---------------------------------------------
    -- DO UPDATE, mirroring Stage 5: a decision is a derived judgement about
    -- evidence, so when the evidence changes the judgement must change with it.
    -- Freezing it would leave a verdict describing numbers that no longer exist.
    INSERT INTO salesops.anomaly_decisions (
        anomaly_id, calendar_date, decision_version,
        detector_version, baseline_status, anomaly_score, is_anomaly,
        signal_count, dominant_signal,
        revenue_robust_z, aov_robust_z, refund_robust_z, orders_robust_z,
        revenue_deviation_pct, aov_deviation_pct, refund_rate_deviation, orders_deviation_pct,
        expected_net_revenue_usd, actual_net_revenue_usd,
        revenue_delta_usd, absolute_revenue_delta_usd, revenue_delta_pct, business_impact_tier,
        severity, routing, decision, notification_allowed, human_review_required,
        decision_reason_code, decided_at
    )
    SELECT
        i.anomaly_id, i.calendar_date, p_decision_version,
        i.detector_version, i.baseline_status, i.anomaly_score, i.is_anomaly,
        i.signal_count, i.dominant_signal,
        i.revenue_robust_z, i.aov_robust_z, i.refund_robust_z, i.orders_robust_z,
        i.revenue_deviation_pct, i.aov_deviation_pct, i.refund_rate_deviation, i.orders_deviation_pct,
        i.expected_net_revenue_usd, i.actual_net_revenue_usd,
        i.revenue_delta_usd, i.abs_delta, i.revenue_delta_pct, i.business_impact_tier,
        i.severity, i.routing, i.decision, i.notification_allowed, i.human_review_required,
        i.decision_reason_code, now()
    FROM pg_temp_decision_input i
    ON CONFLICT (anomaly_id, decision_version) DO UPDATE SET
        calendar_date              = EXCLUDED.calendar_date,
        detector_version           = EXCLUDED.detector_version,
        baseline_status            = EXCLUDED.baseline_status,
        anomaly_score              = EXCLUDED.anomaly_score,
        is_anomaly                 = EXCLUDED.is_anomaly,
        signal_count               = EXCLUDED.signal_count,
        dominant_signal            = EXCLUDED.dominant_signal,
        revenue_robust_z           = EXCLUDED.revenue_robust_z,
        aov_robust_z               = EXCLUDED.aov_robust_z,
        refund_robust_z            = EXCLUDED.refund_robust_z,
        orders_robust_z            = EXCLUDED.orders_robust_z,
        revenue_deviation_pct      = EXCLUDED.revenue_deviation_pct,
        aov_deviation_pct          = EXCLUDED.aov_deviation_pct,
        refund_rate_deviation      = EXCLUDED.refund_rate_deviation,
        orders_deviation_pct       = EXCLUDED.orders_deviation_pct,
        expected_net_revenue_usd   = EXCLUDED.expected_net_revenue_usd,
        actual_net_revenue_usd     = EXCLUDED.actual_net_revenue_usd,
        revenue_delta_usd          = EXCLUDED.revenue_delta_usd,
        absolute_revenue_delta_usd = EXCLUDED.absolute_revenue_delta_usd,
        revenue_delta_pct          = EXCLUDED.revenue_delta_pct,
        business_impact_tier       = EXCLUDED.business_impact_tier,
        severity                   = EXCLUDED.severity,
        routing                    = EXCLUDED.routing,
        decision                   = EXCLUDED.decision,
        notification_allowed       = EXCLUDED.notification_allowed,
        human_review_required      = EXCLUDED.human_review_required,
        decision_reason_code       = EXCLUDED.decision_reason_code,
        decided_at                 = EXCLUDED.decided_at;

    GET DIAGNOSTICS v_written = ROW_COUNT;

    -- ---- persist the reason codes ------------------------------------------
    -- Replaced wholesale for the decisions just written. A rule that stops
    -- applying must stop being cited, and an incremental merge would leave the
    -- stale code behind - which is worse than no explanation at all.
    DELETE FROM salesops.anomaly_decision_reasons r
    USING salesops.anomaly_decisions d
    WHERE r.decision_id = d.decision_id
      AND d.decision_version = p_decision_version
      AND d.anomaly_id IN (SELECT anomaly_id FROM pg_temp_decision_input);

    INSERT INTO salesops.anomaly_decision_reasons (decision_id, reason_code)
    SELECT d.decision_id, codes.reason_code
    FROM pg_temp_decision_input i
    JOIN salesops.anomaly_decisions d
      ON d.anomaly_id = i.anomaly_id
     AND d.decision_version = p_decision_version
    CROSS JOIN LATERAL (VALUES
        -- Declarative on purpose: each row is one rule and the condition that
        -- fires it, readable top to bottom without following control flow.
        ('INSUFFICIENT_HISTORY',         i.baseline_status = 'insufficient_history'),
        ('INCOMPLETE_KPI',               i.baseline_status = 'incomplete_kpi'),
        ('NORMAL_VARIATION',             i.baseline_status = 'scored' AND NOT i.is_anomaly),
        ('STATISTICAL_ANOMALY',          i.is_anomaly),
        ('HIGH_REVENUE_IMPACT',          i.is_anomaly
                                         AND i.business_impact_tier IN ('material', 'severe')),
        ('INSUFFICIENT_BUSINESS_IMPACT', i.is_anomaly
                                         AND i.business_impact_tier IN ('trivial', 'limited')),
        ('BUSINESS_IMPACT_UNAVAILABLE',  i.is_anomaly AND i.business_impact_tier = 'unknown'),
        ('SEVERE_REFUND_SPIKE',          i.is_anomaly AND i.refund_spike),
        ('SEVERE_AOV_DECLINE',           i.is_anomaly AND i.aov_collapse),
        ('HIGH_ORDER_VOLUME_DECLINE',    i.is_anomaly AND i.orders_collapse),
        ('MULTI_SIGNAL_EVENT',           i.is_anomaly AND i.multi_signal),
        ('CRITICAL_COMBINED_IMPACT',     i.severity = 'critical')
    ) AS codes(reason_code, applies)
    WHERE codes.applies;

    GET DIAGNOSTICS v_reasons = ROW_COUNT;

    -- ---- report -------------------------------------------------------------
    -- Counted from what is actually in the table for this version, not from the
    -- temp table, so the numbers describe persisted state rather than intent.
    RETURN QUERY
    SELECT
        (SELECT count(*) FROM pg_temp_decision_input)::INTEGER,
        v_written,
        count(*) FILTER (WHERE d.severity = 'none')::INTEGER,
        count(*) FILTER (WHERE d.severity = 'minor')::INTEGER,
        count(*) FILTER (WHERE d.severity = 'major')::INTEGER,
        count(*) FILTER (WHERE d.severity = 'critical')::INTEGER,
        count(*) FILTER (WHERE d.routing = 'no_action')::INTEGER,
        count(*) FILTER (WHERE d.routing = 'auto_notify')::INTEGER,
        count(*) FILTER (WHERE d.routing = 'human_review')::INTEGER,
        count(*) FILTER (WHERE d.notification_allowed)::INTEGER,
        count(*) FILTER (WHERE d.human_review_required)::INTEGER,
        count(*) FILTER (WHERE d.baseline_status <> 'scored')::INTEGER,
        v_reasons
    FROM salesops.anomaly_decisions d
    WHERE d.decision_version = p_decision_version;
END;
$$;

COMMENT ON FUNCTION salesops.decide_anomalies(TEXT) IS
    'Stage 6 decision engine. Deterministic and idempotent: same evidence and same '
    'decision_version produce identical severity, routing, flags and reason codes. '
    'Upserts salesops.anomaly_decisions and rebuilds its reason codes.';


-- =============================================================================
-- 8. Audit view
--
-- Section 20: an operator must be able to answer "why was this classified this
-- way?" without asking a language model. This is that answer, in one row.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.anomaly_decision_audit AS
SELECT
    d.decision_id,
    d.calendar_date,
    dd.day_name,
    d.decision_version,
    d.detector_version,

    d.severity,
    d.routing,
    d.decision,
    d.notification_allowed,
    d.human_review_required,

    d.anomaly_score,
    d.signal_count,
    d.dominant_signal,
    d.baseline_status,

    d.expected_net_revenue_usd,
    d.actual_net_revenue_usd,
    d.revenue_delta_usd,
    d.revenue_delta_pct,
    d.business_impact_tier,

    d.revenue_robust_z,
    d.aov_deviation_pct,
    d.refund_rate_deviation,
    d.orders_deviation_pct,

    d.decision_reason_code AS primary_reason,
    -- Sorted so the same decision always renders the same string.
    (SELECT string_agg(r.reason_code, ', ' ORDER BY r.reason_code)
     FROM salesops.anomaly_decision_reasons r
     WHERE r.decision_id = d.decision_id) AS all_reasons,

    d.decided_at
FROM salesops.anomaly_decisions d
JOIN salesops.dim_date dd ON dd.calendar_date = d.calendar_date;

COMMENT ON VIEW salesops.anomaly_decision_audit IS
    'One readable row per decision: the evidence, the money, the verdict and every '
    'reason code behind it. Answers "why is this critical?" with SQL alone.';


-- -----------------------------------------------------------------------------
-- ingestion_runs gains a fifth pipeline. Same shared ledger, told apart by
-- `source` - see the V005 note. Queries deriving a window from max(window_to)
-- must still scope to their own source.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild), ''anomaly-detector'' (Stage 5), '
    '''anomaly-decision'' (Stage 6). Always filter by source when reading windows.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V008', 'Stage 6 deterministic decision layer: severity, routing, reason codes, decide_anomalies()')
ON CONFLICT (version) DO NOTHING;

COMMIT;
