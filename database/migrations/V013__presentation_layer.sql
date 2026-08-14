-- =============================================================================
-- V013  Presentation layer
--
-- Stage 11. The last migration, and the only one that adds no behaviour.
--
-- Everything here is a VIEW over data the previous twelve migrations already
-- produced, plus one reference table that names the layers, plus a database
-- role that physically cannot write. Nothing in this file computes a KPI, a
-- z-score, a severity, a routing decision, an ageing bucket or a health status.
-- Where a number is shown it was already stored; where a label is shown it was
-- already chosen by the stage that owns it.
--
-- ---------------------------------------------------------------------------
-- THE ONE IDEA
--
-- A dashboard is where a measurement, a statistic, a rule, a language model, a
-- person and a machine all end up rendered in the same typeface. That is the
-- risk this file exists to manage.
--
-- So every presentation view carries the LAYER its content came from, drawn
-- from salesops.presentation_layers, and the model layer is the only one whose
-- is_model_generated is true. The distinction is a column and a foreign key,
-- not a colour scheme - a dashboard can still render it badly, but it cannot
-- render it badly by accident, and a test can prove the separation holds.
--
-- Concretely:
--
--   * no executive view exposes a single free-text column written by the model
--     (summary, primary_hypothesis, supporting_evidence, alternative_hypotheses,
--     recommended_checks). The executive dashboard shows WHETHER a hypothesis
--     exists, never what it says;
--   * the investigation view does show the hypothesis, in full, under column
--     names prefixed llm_ and beneath the deterministic evidence it is trying
--     to explain - because a hypothesis read before the decision is a hypothesis
--     that frames the decision;
--   * llm_verified is a column, and it is FALSE on every row, because nothing
--     in this system verifies a hypothesis. An unverifiable claim shown next to
--     an audited one has to say so.
--
-- ---------------------------------------------------------------------------
-- READ-ONLY IS A ROLE, NOT A PROMISE
--
-- Section 5 of the specification requires that no dashboard query can mutate
-- business data. Writing only SELECT statements would satisfy that until the
-- first person opens the SQL editor in Metabase.
--
-- So salesops_readonly is created here with USAGE on the schema, SELECT on
-- relations, and nothing else - no INSERT, no UPDATE, no DELETE, no TRUNCATE,
-- and deliberately no EXECUTE on the functions, because purge_staging() and
-- replay_failed_batch() are perfectly good ways to modify data from a SELECT
-- box. PostgreSQL grants EXECUTE to PUBLIC by default, which is exactly the
-- kind of default that turns a reporting login into a write path, so it is
-- revoked explicitly.
--
-- The role has no password. It cannot connect until one is set, which the
-- provisioning script does from the environment. A password in a migration is
-- a password in version control.
-- =============================================================================

BEGIN;


-- =============================================================================
-- 0. Re-runnability
--
-- CREATE OR REPLACE VIEW cannot add a column anywhere but the end, so a file
-- that is edited and re-applied fails on the first reordered column with a
-- message about renaming. Dropping first makes the migration behave the way
-- every other one in this project does: run it again and you get what the file
-- says, not a diff against what happened to be there.
--
-- Only Stage 11's own views are dropped, in reverse dependency order. CASCADE
-- is deliberately NOT used: if some later object comes to depend on one of
-- these, that should be a loud failure here rather than a silent deletion.
-- =============================================================================
DROP VIEW IF EXISTS salesops.incident_timeline;
DROP VIEW IF EXISTS salesops.anomaly_investigation_detail;
DROP VIEW IF EXISTS salesops.anomaly_investigation;
DROP VIEW IF EXISTS salesops.audit_event_stream;
DROP VIEW IF EXISTS salesops.ops_attention_items;
DROP VIEW IF EXISTS salesops.ops_pipeline_runs;
DROP VIEW IF EXISTS salesops.exec_pipeline_health;
DROP VIEW IF EXISTS salesops.exec_remediation_status;
DROP VIEW IF EXISTS salesops.exec_review_status;
DROP VIEW IF EXISTS salesops.exec_notification_status;
DROP VIEW IF EXISTS salesops.exec_anomaly_timeline;
DROP VIEW IF EXISTS salesops.exec_actionable_anomalies;
DROP VIEW IF EXISTS salesops.exec_anomaly_severity_summary;
DROP VIEW IF EXISTS salesops.exec_headline_kpis;
DROP VIEW IF EXISTS salesops.exec_kpi_daily;


-- =============================================================================
-- 1. The layer vocabulary
--
-- Seven layers plus the operational one. The ordering is the pipeline's own
-- ordering, and it is the order things must be READ in: what happened, then
-- what was unusual about it, then what the rules concluded, then - last, and
-- only last - what a language model guessed.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.presentation_layers (
    layer_key           TEXT PRIMARY KEY,
    layer_rank          INTEGER     NOT NULL UNIQUE,
    layer_label         TEXT        NOT NULL,
    evidence_kind       TEXT        NOT NULL,
    is_model_generated  BOOLEAN     NOT NULL,
    -- Which stage produced it. Kept as text: this is a label, not a join key.
    produced_by_stage   TEXT        NOT NULL,
    source_relations    TEXT[]      NOT NULL,
    description         TEXT        NOT NULL,

    CONSTRAINT presentation_layers_kind_chk CHECK (
        evidence_kind IN ('measured', 'statistical', 'deterministic',
                          'model_generated', 'human_judgement', 'system_action')
    ),

    -- The invariant the whole file rests on: exactly one kind of evidence is
    -- model-generated, and it is flagged. Neither half can drift from the other
    -- because neither half can be written without the other.
    CONSTRAINT presentation_layers_model_flag_chk CHECK (
        is_model_generated = (evidence_kind = 'model_generated')
    )
);

COMMENT ON TABLE salesops.presentation_layers IS
    'The eight layers a dashboard renders, in reading order. Every presentation '
    'view carries one of these keys so that a measurement, a rule and a language '
    'model cannot be displayed as the same kind of thing.';

COMMENT ON COLUMN salesops.presentation_layers.is_model_generated IS
    'True for exactly one layer. Enforced by CHECK against evidence_kind.';

INSERT INTO salesops.presentation_layers
    (layer_key, layer_rank, layer_label, evidence_kind, is_model_generated,
     produced_by_stage, source_relations, description)
VALUES
    ('observed_fact', 1, 'Observed fact', 'measured', FALSE,
     'Stage 3-4', ARRAY['fact_orders', 'kpi_daily', 'exchange_rates'],
     'What was actually ordered, refunded and converted. Measured, not inferred.'),

    ('statistical_signal', 2, 'Statistical signal', 'statistical', FALSE,
     'Stage 5', ARRAY['anomaly_daily'],
     'How far the day sits from its own baseline, by robust z-score. Says a day '
     'is unusual. Says nothing about whether it matters.'),

    ('deterministic_decision', 3, 'Business decision', 'deterministic', FALSE,
     'Stage 6', ARRAY['anomaly_decisions', 'anomaly_decision_reasons'],
     'Severity, routing and reason codes from fixed thresholds. Reproducible '
     'from the stored inputs by anyone with the threshold table.'),

    ('model_hypothesis', 4, 'LLM hypothesis (unverified)', 'model_generated', TRUE,
     'Stage 7', ARRAY['anomaly_hypotheses'],
     'A language model''s guess at the cause, generated AFTER the decision and '
     'incapable of changing it. Never verified by this system.'),

    ('human_review', 5, 'Human review', 'human_judgement', FALSE,
     'Stage 8', ARRAY['review_queue', 'review_events', 'notifications'],
     'A named person looked at it. Claiming, resolving, dismissing and approving '
     'are separate acts and are recorded separately.'),

    ('approved_remediation', 6, 'Approved remediation', 'human_judgement', FALSE,
     'Stage 9', ARRAY['remediation_actions'],
     'An action a named person authorised. Authorised is not executed.'),

    ('completed_remediation', 7, 'Completed remediation', 'system_action', FALSE,
     'Stage 9', ARRAY['remediation_actions', 'remediation_attempts'],
     'An authorised action that actually ran, once, with the attempt recorded.'),

    ('operational_event', 8, 'Operational event', 'system_action', FALSE,
     'Stage 10', ARRAY['operational_events', 'ingestion_runs', 'ingestion_replays'],
     'What the pipeline did to keep itself running: runs, recoveries, replays, '
     'retention. Nothing here is a business fact.')
ON CONFLICT (layer_key) DO NOTHING;


-- =============================================================================
-- 2. Executive: the daily numbers
--
-- Stage 4 already stores every KPI. This view adds no arithmetic to them
-- beyond the baseline comparison, which is itself read from the Stage 5 and
-- Stage 6 rows rather than recomputed - expected_net_revenue_usd is Stage 6's
-- number, and this is the same number.
--
-- ---------------------------------------------------------------------------
-- ONE DAY, SEVERAL VERSIONS
--
-- A calendar date does not have one row downstream. It has one per detector
-- version in anomaly_daily, one per decision version in anomaly_decisions, and
-- one per (prompt version, model) in anomaly_hypotheses - all three uniqueness
-- constraints say so, and keeping old generations is the point of storing a
-- version at all.
--
-- A plain equi-join to any of them therefore multiplies rows, and the failure
-- mode is not an error: it is a dashboard that quietly reports two of every
-- anomaly, or a revenue total counted twice, the first time anyone re-runs
-- Stage 7 with a new prompt. Every join below is a LATERAL that takes the
-- newest row, and the version it took is a column, so "which decision is this"
-- is answerable on the page rather than by reading this file.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.exec_kpi_daily AS
SELECT
    k.calendar_date,
    dd.day_name,
    dd.is_weekend,

    -- Layer 1. Measured.
    k.orders_count,
    k.customers_count,
    k.units_sold,
    k.gross_revenue_usd,
    k.refund_amount_usd,
    k.net_revenue_usd,
    k.average_order_value_usd,
    k.refund_rate,
    k.rolling_7d_net_revenue_usd,
    k.rolling_28d_net_revenue_usd,

    -- Completeness travels with the number it qualifies, as it has since V003.
    k.is_complete,
    k.orders_pending_fx,
    k.fx_completeness_pct,

    -- Layer 2. The baseline this day was judged against, as stored.
    a.revenue_baseline_median            AS baseline_net_revenue_usd,
    CASE
        WHEN a.revenue_baseline_median IS NULL
          OR a.revenue_baseline_median = 0 THEN NULL
        ELSE round(
            (k.net_revenue_usd - a.revenue_baseline_median)
            / a.revenue_baseline_median * 100, 2)
    END                                  AS revenue_vs_baseline_pct,
    a.revenue_robust_z,
    a.anomaly_score,
    a.is_anomaly,
    a.baseline_status,
    a.dominant_signal,
    a.signal_count,
    a.detector_version,

    -- Layer 3. Stage 6's words, unaltered. NULL means no decision row, which is
    -- not the same as a decision of 'none' and is not collapsed into one.
    d.decision_version,
    d.severity,
    d.routing,
    d.decision,
    d.decision_reason_code,
    d.business_impact_tier,
    d.revenue_delta_usd,
    d.human_review_required,
    d.notification_allowed,

    -- Layer 4 is represented by its EXISTENCE and nothing else. What the model
    -- said is not on the executive dashboard.
    (h.hypothesis_id IS NOT NULL)        AS hypothesis_available,

    'observed_fact'::text                AS evidence_layer
FROM salesops.kpi_daily k
JOIN salesops.dim_date dd
     ON dd.calendar_date = k.calendar_date
LEFT JOIN LATERAL (
    SELECT * FROM salesops.anomaly_daily a2
    WHERE a2.calendar_date = k.calendar_date
    ORDER BY a2.detected_at DESC, a2.anomaly_id DESC LIMIT 1
) a ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM salesops.anomaly_decisions d2
    WHERE d2.calendar_date = k.calendar_date
    ORDER BY d2.decided_at DESC, d2.decision_id DESC LIMIT 1
) d ON TRUE
LEFT JOIN LATERAL (
    SELECT h2.hypothesis_id FROM salesops.anomaly_hypotheses h2
    WHERE h2.decision_id = d.decision_id
    ORDER BY h2.generated_at DESC, h2.hypothesis_id DESC LIMIT 1
) h ON TRUE;

COMMENT ON VIEW salesops.exec_kpi_daily IS
    'Daily revenue, orders, AOV and refund rate with the baseline each was judged '
    'against and the deterministic verdict that followed. Carries no model text: '
    'hypothesis_available says a hypothesis exists, never what it claims.';


-- -----------------------------------------------------------------------------
-- exec_headline_kpis
-- Long form - one row per headline number - because a dashboard tile wants a
-- value and a label, and a long table survives adding a metric without anyone
-- editing a card.
--
-- "Latest complete day" means is_complete, not max(date): a day still missing
-- exchange rates has understated revenue, and a headline figure that quietly
-- understates revenue is worse than one that is a day old.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.exec_headline_kpis AS
WITH latest AS (
    SELECT * FROM salesops.kpi_daily
    WHERE is_complete
    ORDER BY calendar_date DESC
    LIMIT 1
),
prior AS (
    SELECT * FROM salesops.kpi_daily k
    WHERE k.is_complete
      AND k.calendar_date < (SELECT calendar_date FROM latest)
    ORDER BY k.calendar_date DESC
    LIMIT 1
),
baseline AS (
    SELECT a.revenue_baseline_median
    FROM salesops.anomaly_daily a
    WHERE a.calendar_date = (SELECT calendar_date FROM latest)
),
metrics(metric_key, metric_rank, metric_label, metric_value, comparison_value,
        comparison_label, unit) AS (
    -- The comparison is Stage 5's own DAY-OF-WEEK baseline, not last week's
    -- number and not a mean. Naming it precisely matters: delta_pct here
    -- reproduces anomaly_daily.revenue_deviation_pct exactly, and would stop
    -- doing so the moment this compared against something else.
    SELECT 'net_revenue_usd', 1, 'Net revenue',
           l.net_revenue_usd, b.revenue_baseline_median,
           'day-of-week baseline median', 'usd'
      FROM latest l LEFT JOIN baseline b ON TRUE
    UNION ALL
    SELECT 'orders_count', 2, 'Orders',
           l.orders_count::numeric, p.orders_count::numeric, 'previous day', 'count'
      FROM latest l LEFT JOIN prior p ON TRUE
    UNION ALL
    SELECT 'average_order_value_usd', 3, 'Average order value',
           l.average_order_value_usd, p.average_order_value_usd, 'previous day', 'usd'
      FROM latest l LEFT JOIN prior p ON TRUE
    UNION ALL
    SELECT 'refund_rate', 4, 'Refund rate',
           l.refund_rate, p.refund_rate, 'previous day', 'ratio'
      FROM latest l LEFT JOIN prior p ON TRUE
    UNION ALL
    -- Stage 4 stores these as AVERAGES over the window, not totals. The label
    -- says average because the number is one; "trailing 28 days" beside a
    -- figure smaller than the single day above it is how a dashboard teaches
    -- someone the wrong thing.
    SELECT 'rolling_28d_net_revenue_usd', 5,
           'Net revenue, trailing 28-day daily average',
           l.rolling_28d_net_revenue_usd, l.rolling_7d_net_revenue_usd,
           'trailing 7-day daily average', 'usd'
      FROM latest l
)
SELECT
    m.metric_key,
    m.metric_rank,
    m.metric_label,
    m.metric_value,
    m.unit,
    m.comparison_value,
    m.comparison_label,
    CASE
        WHEN m.comparison_value IS NULL OR m.comparison_value = 0 THEN NULL
        ELSE round((m.metric_value - m.comparison_value) / m.comparison_value * 100, 2)
    END                                        AS delta_pct,
    (SELECT calendar_date FROM latest)         AS as_of_date,
    (SELECT max(calendar_date) FROM salesops.kpi_daily)
                                               AS latest_loaded_date,
    'observed_fact'::text                      AS evidence_layer
FROM metrics m
ORDER BY m.metric_rank;

COMMENT ON VIEW salesops.exec_headline_kpis IS
    'The headline tiles, one row each. as_of_date is the latest COMPLETE day; '
    'latest_loaded_date is the latest day loaded at all, so a gap between them is '
    'visible rather than hidden behind an understated total.';


-- =============================================================================
-- 3. Executive: anomalies
-- =============================================================================
CREATE OR REPLACE VIEW salesops.exec_anomaly_severity_summary AS
WITH severities(severity, severity_rank) AS (
    VALUES ('critical', 1), ('major', 2), ('minor', 3), ('none', 4)
)
SELECT
    s.severity,
    s.severity_rank,
    count(d.decision_id)                                            AS anomaly_count,
    count(d.decision_id) FILTER (WHERE d.decision = 'action_required')
                                                                    AS actionable_count,
    count(d.decision_id) FILTER (WHERE d.human_review_required)     AS review_required_count,
    count(d.decision_id) FILTER (WHERE d.notification_allowed)      AS notifiable_count,
    count(d.decision_id) FILTER (WHERE d.calendar_date >= current_date - 30)
                                                                    AS last_30_days_count,
    -- Money is summed as an absolute delta: a shortfall and an unexplained
    -- surplus are both anomalies, and netting them off would hide both.
    round(COALESCE(sum(d.absolute_revenue_delta_usd), 0), 2)         AS absolute_revenue_delta_usd,
    'deterministic_decision'::text                                  AS evidence_layer
FROM severities s
LEFT JOIN salesops.anomaly_decisions d ON d.severity = s.severity
GROUP BY s.severity, s.severity_rank
ORDER BY s.severity_rank;

COMMENT ON VIEW salesops.exec_anomaly_severity_summary IS
    'Anomaly counts by Stage 6 severity, with every severity present even at zero '
    'so an empty bar is distinguishable from a missing one.';


-- -----------------------------------------------------------------------------
-- exec_actionable_anomalies
-- What is open right now. "Actionable" is Stage 6's decision column, not a
-- predicate invented here.
--
-- One row per ANOMALY, not per decision row: re-deciding under a new version
-- keeps the old row, and a dashboard that listed both would report the same
-- anomaly twice with two different severities beside it.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.exec_actionable_anomalies AS
SELECT DISTINCT ON (d.anomaly_id)
    d.decision_id,
    d.anomaly_id,
    d.calendar_date,
    d.severity,
    d.routing,
    d.decision,
    d.decision_reason_code,
    d.business_impact_tier,
    d.expected_net_revenue_usd,
    d.actual_net_revenue_usd,
    d.revenue_delta_usd,
    d.revenue_delta_pct,
    d.dominant_signal,
    d.signal_count,
    d.human_review_required,
    d.notification_allowed,
    d.decision_version,
    d.decided_at,

    -- Where it got to. Each of these is a state another stage owns; none is
    -- recomputed and none is defaulted when absent.
    (h.hypothesis_id IS NOT NULL)                      AS hypothesis_available,
    n.notification_status,
    r.review_status,
    r.assigned_to                                      AS review_assigned_to,
    r.approved_by                                      AS review_approved_by,
    m.remediation_status,
    m.action_type                                      AS remediation_action_type,

    -- The furthest layer this anomaly has actually reached, so a dashboard can
    -- sort by progress without a CASE expression per card.
    CASE
        WHEN m.remediation_status = 'executed'                  THEN 'completed_remediation'
        WHEN m.remediation_status IN ('approved', 'executing',
                                      'execution_unknown')      THEN 'approved_remediation'
        WHEN r.review_status IS NOT NULL
          OR n.notification_status IS NOT NULL                  THEN 'human_review'
        WHEN h.hypothesis_id IS NOT NULL                        THEN 'model_hypothesis'
        ELSE 'deterministic_decision'
    END                                                AS furthest_layer_reached,
    'deterministic_decision'::text                     AS evidence_layer
FROM salesops.anomaly_decisions d
LEFT JOIN LATERAL (
    SELECT h2.hypothesis_id FROM salesops.anomaly_hypotheses h2
    WHERE h2.decision_id = d.decision_id
    ORDER BY h2.generated_at DESC, h2.hypothesis_id DESC LIMIT 1
) h ON TRUE
LEFT JOIN LATERAL (
    SELECT n2.status AS notification_status
    FROM salesops.notifications n2
    WHERE n2.decision_id = d.decision_id
    ORDER BY n2.created_at DESC, n2.notification_id DESC
    LIMIT 1
) n ON TRUE
LEFT JOIN LATERAL (
    SELECT r2.status AS review_status, r2.assigned_to, r2.approved_by
    FROM salesops.review_queue r2
    WHERE r2.decision_id = d.decision_id
    ORDER BY r2.created_at DESC, r2.review_id DESC
    LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT a2.status AS remediation_status, a2.action_type
    FROM salesops.remediation_actions a2
    WHERE a2.decision_id = d.decision_id
    ORDER BY a2.created_at DESC, a2.remediation_id DESC
    LIMIT 1
) m ON TRUE
WHERE d.decision = 'action_required'
ORDER BY d.anomaly_id, d.decided_at DESC, d.decision_id DESC;

COMMENT ON VIEW salesops.exec_actionable_anomalies IS
    'Every anomaly Stage 6 marked action_required, and how far along the chain it '
    'has travelled. furthest_layer_reached is derived only from stored states.';


-- -----------------------------------------------------------------------------
-- exec_anomaly_timeline
-- The recent-anomaly strip. One row per anomalous day, newest first.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.exec_anomaly_timeline AS
SELECT DISTINCT ON (a.calendar_date)
    a.calendar_date,
    a.anomaly_id,
    a.detected_at,
    a.anomaly_score,
    a.dominant_signal,
    a.signal_count,
    a.baseline_status,
    k.net_revenue_usd,
    a.revenue_baseline_median                       AS baseline_net_revenue_usd,
    a.revenue_deviation_pct,
    d.severity,
    d.routing,
    d.decision,
    d.decision_reason_code,
    d.decided_at,
    (h.hypothesis_id IS NOT NULL)                   AS hypothesis_available,
    h.generated_at                                  AS hypothesis_generated_at,
    CASE
        WHEN d.decision_id IS NULL THEN 'statistical_signal'
        ELSE 'deterministic_decision'
    END                                             AS evidence_layer
FROM salesops.anomaly_daily a
LEFT JOIN salesops.kpi_daily k ON k.calendar_date = a.calendar_date
LEFT JOIN LATERAL (
    SELECT * FROM salesops.anomaly_decisions d2
    WHERE d2.anomaly_id = a.anomaly_id
    ORDER BY d2.decided_at DESC, d2.decision_id DESC LIMIT 1
) d ON TRUE
LEFT JOIN LATERAL (
    SELECT h2.hypothesis_id, h2.generated_at FROM salesops.anomaly_hypotheses h2
    WHERE h2.decision_id = d.decision_id
    ORDER BY h2.generated_at DESC, h2.hypothesis_id DESC LIMIT 1
) h ON TRUE
WHERE a.is_anomaly
-- Newest detector version wins, one row per day. DISTINCT ON needs the
-- deduplicating key to lead the ORDER BY; the display sort is the view's
-- consumer's job, and every card that reads this states its own.
ORDER BY a.calendar_date DESC, a.detected_at DESC, a.anomaly_id DESC;

COMMENT ON VIEW salesops.exec_anomaly_timeline IS
    'Days Stage 5 flagged, newest first, with the Stage 6 verdict beside each. A '
    'row with a severity but no decision_reason_code is impossible; a row with a '
    'signal and no decision means Stage 6 has not run for that day yet.';


-- =============================================================================
-- 4. Executive: delivery, review and remediation status
--
-- Three small summary views rather than one wide one. They count different
-- populations - a notification is not a review is not an action - and joining
-- them into a single row would produce numbers that look comparable and are not.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.exec_notification_status AS
SELECT
    n.status                                            AS notification_status,
    n.severity,
    count(*)                                            AS notification_count,
    max(n.created_at)                                   AS newest_created_at,
    max(n.sent_at)                                      AS newest_sent_at,
    sum(n.attempt_count)                                AS total_attempts,
    count(*) FILTER (WHERE n.last_error IS NOT NULL)    AS with_error_count,
    'human_review'::text                                AS evidence_layer
FROM salesops.notifications n
GROUP BY n.status, n.severity;

COMMENT ON VIEW salesops.exec_notification_status IS
    'Delivery outcomes by status and severity. Counts notifications, not anomalies: '
    'one anomaly can produce several.';


CREATE OR REPLACE VIEW salesops.exec_review_status AS
SELECT
    r.status                                            AS review_status,
    r.severity                                          AS anomaly_severity,
    count(*)                                            AS review_count,
    count(*) FILTER (WHERE g.ageing_bucket = 'warning')          AS ageing_warning,
    count(*) FILTER (WHERE g.ageing_bucket = 'overdue')          AS ageing_overdue,
    count(*) FILTER (WHERE g.ageing_bucket = 'critical_overdue') AS ageing_critical_overdue,
    min(r.created_at)                                   AS oldest_created_at,
    max(r.reviewed_at)                                  AS newest_reviewed_at,
    count(*) FILTER (WHERE r.approved_by IS NOT NULL)   AS approved_count,
    'human_review'::text                                AS evidence_layer
FROM salesops.review_queue r
LEFT JOIN salesops.review_ageing g ON g.review_id = r.review_id
GROUP BY r.status, r.severity;

COMMENT ON VIEW salesops.exec_review_status IS
    'The review queue by state, with Stage 10 ageing buckets counted alongside. '
    'anomaly_severity and ageing_* are different vocabularies and are never added '
    'together.';


CREATE OR REPLACE VIEW salesops.exec_remediation_status AS
SELECT
    a.status                                            AS remediation_status,
    a.action_type,
    a.severity                                          AS anomaly_severity,
    count(*)                                            AS action_count,
    count(*) FILTER (WHERE a.authorized_by IS NOT NULL) AS authorized_count,
    count(*) FILTER (WHERE a.executed_at IS NOT NULL)   AS executed_count,
    sum(a.attempt_count)                                AS total_attempts,
    max(a.created_at)                                   AS newest_created_at,
    max(a.executed_at)                                  AS newest_executed_at,
    CASE WHEN a.status = 'executed' THEN 'completed_remediation'
         ELSE 'approved_remediation' END                AS evidence_layer
FROM salesops.remediation_actions a
GROUP BY a.status, a.action_type, a.severity;

COMMENT ON VIEW salesops.exec_remediation_status IS
    'Remediation by state and action type. authorized_count and executed_count are '
    'reported separately because authorised is not executed - that gap is the '
    'point of Stage 9.';


-- =============================================================================
-- 5. Operational health
--
-- Stage 10 already computes health. These views order it for display and add
-- the per-pipeline run detail section 3 asks for. The vocabulary is Stage 10's:
-- healthy | warning | degraded | failed, never an anomaly severity.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.ops_pipeline_runs AS
WITH sources AS (
    SELECT DISTINCT source FROM salesops.ingestion_runs
)
SELECT
    s.source                                            AS pipeline,
    last_run.run_id                                     AS latest_run_id,
    last_run.status                                     AS latest_run_status,
    last_run.started_at                                 AS latest_run_started_at,
    last_run.finished_at                                AS latest_run_finished_at,
    CASE
        WHEN last_run.finished_at IS NULL THEN NULL
        ELSE round(extract(epoch FROM (last_run.finished_at - last_run.started_at))::numeric, 1)
    END                                                 AS latest_run_seconds,
    round(extract(epoch FROM (now() - last_run.started_at)) / 3600.0, 1)
                                                        AS hours_since_latest_run,
    last_run.error_message                              AS latest_run_error,

    last_ok.run_id                                      AS latest_success_run_id,
    last_ok.started_at                                  AS latest_success_started_at,
    last_ok.finished_at                                 AS latest_success_finished_at,
    round(extract(epoch FROM (now() - last_ok.finished_at)) / 3600.0, 1)
                                                        AS hours_since_latest_success,

    counts.runs_24h,
    counts.failed_24h,
    counts.partial_24h,
    counts.running_now,
    counts.runs_7d,
    counts.failed_7d,
    -- Median rather than mean: one crashed run that hung for eight hours should
    -- not become the pipeline's reported duration.
    counts.median_seconds_7d,

    'operational_event'::text                           AS evidence_layer
FROM sources s
LEFT JOIN LATERAL (
    SELECT r.run_id, r.status, r.started_at, r.finished_at, r.error_message
    FROM salesops.ingestion_runs r
    WHERE r.source = s.source
    ORDER BY r.started_at DESC, r.run_id DESC
    LIMIT 1
) last_run ON TRUE
LEFT JOIN LATERAL (
    SELECT r.run_id, r.started_at, r.finished_at
    FROM salesops.ingestion_runs r
    WHERE r.source = s.source AND r.status = 'success'
    ORDER BY r.started_at DESC, r.run_id DESC
    LIMIT 1
) last_ok ON TRUE
LEFT JOIN LATERAL (
    SELECT
        count(*) FILTER (WHERE r.started_at >= now() - interval '24 hours') AS runs_24h,
        count(*) FILTER (WHERE r.started_at >= now() - interval '24 hours'
                           AND r.status = 'failed')                         AS failed_24h,
        count(*) FILTER (WHERE r.started_at >= now() - interval '24 hours'
                           AND r.status = 'partial')                        AS partial_24h,
        count(*) FILTER (WHERE r.status = 'running')                        AS running_now,
        count(*) FILTER (WHERE r.started_at >= now() - interval '7 days')   AS runs_7d,
        count(*) FILTER (WHERE r.started_at >= now() - interval '7 days'
                           AND r.status = 'failed')                         AS failed_7d,
        round(percentile_cont(0.5) WITHIN GROUP (
                  ORDER BY extract(epoch FROM (r.finished_at - r.started_at))
              ) FILTER (WHERE r.started_at >= now() - interval '7 days'
                          AND r.finished_at IS NOT NULL)::numeric, 1)       AS median_seconds_7d
    FROM salesops.ingestion_runs r
    WHERE r.source = s.source
) counts ON TRUE;

COMMENT ON VIEW salesops.ops_pipeline_runs IS
    'Per pipeline: the latest run, the latest SUCCESSFUL run, and how they differ. '
    'A pipeline whose latest run failed still shows when it last worked, which is '
    'the question an operator actually asks.';


-- -----------------------------------------------------------------------------
-- exec_pipeline_health
-- The Stage 10 health view, ordered for a dashboard and given a display rank.
-- No status is recomputed: this is a projection.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.exec_pipeline_health AS
SELECT
    h.component,
    h.component_kind,
    h.status                                            AS health_status,
    h.reason_code,
    h.observed_value,
    h.threshold_value,
    h.measure,
    h.last_status                                       AS last_run_status,
    h.last_run_at,
    h.detail,
    CASE h.status
        WHEN 'failed'   THEN 1
        WHEN 'degraded' THEN 2
        WHEN 'warning'  THEN 3
        WHEN 'healthy'  THEN 4
    END                                                 AS status_rank,
    'operational_event'::text                           AS evidence_layer
FROM salesops.operational_health h;

COMMENT ON VIEW salesops.exec_pipeline_health IS
    'Stage 10 health, ordered worst-first for display. The vocabulary is '
    'healthy | warning | degraded | failed and is deliberately not an anomaly '
    'severity: a pipeline is not a business event.';


-- -----------------------------------------------------------------------------
-- ops_attention_items
-- Everything currently asking for a person, in one shape, from the three places
-- that know: the Stage 10 retry queue, review ageing, and unknown executions.
--
-- Used by both the executive and the operational dashboard rather than written
-- twice - a second definition of "stale" is a second answer to "how many".
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.ops_attention_items AS
SELECT
    q.entity_type,
    q.entity_id::text                                   AS entity_id,
    -- The retry queue is about pipeline objects, several of which - a staging
    -- batch, a run - belong to no single business day. NULL says so.
    NULL::date                                          AS calendar_date,
    q.disposition,
    q.failure_reason,
    q.attempt_count::integer                            AS attempt_count,
    q.max_attempts,
    q.retry_eligible,
    q.latest_failure_at                                 AS last_activity_at,
    round(extract(epoch FROM (now() - q.latest_failure_at)) / 3600.0, 1)
                                                        AS hours_since_activity,
    NULL::text                                          AS ageing_bucket,
    'operational_event'::text                           AS evidence_layer
FROM salesops.operational_retry_queue q

UNION ALL

SELECT
    'review'                                            AS entity_type,
    g.review_id::text                                   AS entity_id,
    g.calendar_date,
    'AWAITING_REVIEWER'                                 AS disposition,
    format('open %s hours, %s', g.age_hours, g.review_status)
                                                        AS failure_reason,
    NULL::integer                                       AS attempt_count,
    NULL::integer                                       AS max_attempts,
    FALSE                                               AS retry_eligible,
    COALESCE(g.last_event_at, g.created_at)             AS last_activity_at,
    g.age_hours                                         AS hours_since_activity,
    g.ageing_bucket,
    'human_review'::text                                AS evidence_layer
FROM salesops.review_ageing g
WHERE g.ageing_bucket <> 'fresh';

COMMENT ON VIEW salesops.ops_attention_items IS
    'One shape for everything waiting: failed runs, undelivered notifications, '
    'unknown executions, replayable batches and reviews nobody has picked up. '
    'ageing_bucket is populated only for reviews, because only reviews have one.';


-- =============================================================================
-- 6. The investigation view
--
-- Section 2 of the specification asks for an ordered drill-down. Order is the
-- whole of it: KPI facts, then the statistics, then the deterministic decision
-- and its reason codes, then - only then - the model's hypothesis, then what is
-- missing, then the human and machine states that followed.
--
-- Two views, because a dashboard needs both shapes. The wide one drives filters
-- and headline fields; the long one IS the ordered narrative, one line at a
-- time, each line stamped with its layer.
--
-- Grain: one row per CALENDAR DATE. anomaly_investigation_detail and
-- incident_timeline are both keyed by date and both build on this, so a second
-- decision version here would silently double the length of a narrative that is
-- supposed to be read once.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.anomaly_investigation AS
SELECT DISTINCT ON (d.calendar_date)
    d.decision_id,
    d.anomaly_id,
    d.calendar_date,
    dd.day_name,

    -- 1. KPI facts (layer 1)
    k.orders_count                                      AS fact_orders_count,
    k.customers_count                                   AS fact_customers_count,
    k.units_sold                                        AS fact_units_sold,
    k.gross_revenue_usd                                 AS fact_gross_revenue_usd,
    k.refund_amount_usd                                 AS fact_refund_amount_usd,
    k.net_revenue_usd                                   AS fact_net_revenue_usd,
    k.average_order_value_usd                           AS fact_average_order_value_usd,
    k.refund_rate                                       AS fact_refund_rate,
    k.is_complete                                       AS fact_is_complete,
    k.orders_pending_fx                                 AS fact_orders_pending_fx,

    -- 2. Statistical evidence (layer 2)
    a.detector_version                                  AS signal_detector_version,
    a.anomaly_score                                     AS signal_anomaly_score,
    a.is_anomaly                                        AS signal_is_anomaly,
    a.dominant_signal                                   AS signal_dominant,
    a.signal_count                                      AS signal_count,
    a.baseline_status                                   AS signal_baseline_status,
    a.baseline_kind                                     AS signal_baseline_kind,
    a.baseline_size                                     AS signal_baseline_size,
    a.revenue_baseline_median                           AS signal_baseline_median_usd,
    a.revenue_robust_z                                  AS signal_revenue_robust_z,
    a.aov_robust_z                                      AS signal_aov_robust_z,
    a.refund_robust_z                                   AS signal_refund_robust_z,
    a.orders_robust_z                                   AS signal_orders_robust_z,
    a.detected_at                                       AS signal_detected_at,

    -- 3. Deterministic decision (layer 3)
    d.decision_version,
    d.severity                                          AS decision_severity,
    d.routing                                           AS decision_routing,
    d.decision                                          AS decision_outcome,
    d.business_impact_tier                              AS decision_impact_tier,
    d.expected_net_revenue_usd                          AS decision_expected_revenue_usd,
    d.actual_net_revenue_usd                            AS decision_actual_revenue_usd,
    d.revenue_delta_usd                                 AS decision_revenue_delta_usd,
    d.revenue_delta_pct                                 AS decision_revenue_delta_pct,
    d.human_review_required                             AS decision_human_review_required,
    d.notification_allowed                              AS decision_notification_allowed,
    d.decision_reason_code                              AS decision_primary_reason,
    (SELECT string_agg(rc.reason_code, ', ' ORDER BY rc.reason_code)
     FROM salesops.anomaly_decision_reasons rc
     WHERE rc.decision_id = d.decision_id)              AS decision_all_reasons,
    d.decided_at,

    -- 4. Model hypothesis (layer 4). Every column below is llm_-prefixed, and
    --    llm_verified is FALSE on every row that has one - not because
    --    verification failed, but because nothing here verifies.
    h.hypothesis_id                                     AS llm_hypothesis_id,
    h.summary                                           AS llm_summary,
    h.primary_hypothesis                                AS llm_primary_hypothesis,
    h.alternative_hypotheses                            AS llm_alternative_hypotheses,
    h.supporting_evidence                               AS llm_supporting_evidence,
    h.recommended_checks                                AS llm_recommended_checks,
    h.confidence                                        AS llm_confidence,
    h.model_provider                                    AS llm_model_provider,
    h.model_name                                        AS llm_model_name,
    h.prompt_version                                    AS llm_prompt_version,
    h.evidence_digest                                   AS llm_evidence_digest,
    h.generated_at                                      AS llm_generated_at,
    (h.hypothesis_id IS NOT NULL)                       AS llm_is_model_generated,
    FALSE                                               AS llm_verified,

    -- 5. Missing evidence - the model's own account of what it could not see.
    h.missing_evidence                                  AS llm_missing_evidence,

    -- 6. Notification and review (layer 5)
    n.notification_id,
    n.status                                            AS notification_status,
    n.channel                                           AS notification_channel,
    n.attempt_count                                     AS notification_attempts,
    n.sent_at                                           AS notification_sent_at,
    n.last_error                                        AS notification_last_error,
    r.review_id,
    r.status                                            AS review_status,
    r.assigned_to                                       AS review_assigned_to,
    r.resolution                                        AS review_resolution,
    r.approved_by                                       AS review_approved_by,
    r.approved_at                                       AS review_approved_at,
    r.created_at                                        AS review_created_at,
    g.ageing_bucket                                     AS review_ageing_bucket,

    -- 7. Remediation (layers 6 and 7)
    m.remediation_id,
    m.action_type                                       AS remediation_action_type,
    m.status                                            AS remediation_status,
    m.policy_version                                    AS remediation_policy_version,
    m.authorized_by                                     AS remediation_authorized_by,
    m.authorized_at                                     AS remediation_authorized_at,
    m.executed_by                                       AS remediation_executed_by,
    m.executed_at                                       AS remediation_executed_at,
    m.attempt_count                                     AS remediation_attempts,
    m.provider_reference                                AS remediation_provider_reference,
    m.last_error                                        AS remediation_last_error,

    -- 8. Operational history, counted here and enumerated in audit_event_stream.
    (SELECT count(*) FROM salesops.operational_events oe
      WHERE oe.entity_type = 'remediation_action'
        AND oe.entity_id = m.remediation_id::text)      AS operational_event_count
FROM salesops.anomaly_decisions d
JOIN salesops.dim_date dd               ON dd.calendar_date = d.calendar_date
LEFT JOIN salesops.kpi_daily k          ON k.calendar_date  = d.calendar_date
LEFT JOIN salesops.anomaly_daily a      ON a.anomaly_id     = d.anomaly_id
LEFT JOIN LATERAL (
    SELECT * FROM salesops.anomaly_hypotheses h2
    WHERE h2.decision_id = d.decision_id
    ORDER BY h2.generated_at DESC, h2.hypothesis_id DESC LIMIT 1
) h ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM salesops.notifications n2
    WHERE n2.decision_id = d.decision_id
    ORDER BY n2.created_at DESC, n2.notification_id DESC LIMIT 1
) n ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM salesops.review_queue r2
    WHERE r2.decision_id = d.decision_id
    ORDER BY r2.created_at DESC, r2.review_id DESC LIMIT 1
) r ON TRUE
LEFT JOIN salesops.review_ageing g      ON g.review_id      = r.review_id
LEFT JOIN LATERAL (
    SELECT * FROM salesops.remediation_actions m2
    WHERE m2.decision_id = d.decision_id
    ORDER BY m2.created_at DESC, m2.remediation_id DESC LIMIT 1
) m ON TRUE
ORDER BY d.calendar_date, d.decided_at DESC, d.decision_id DESC;

COMMENT ON VIEW salesops.anomaly_investigation IS
    'One anomaly, every layer, in one row. Model output is confined to the llm_ '
    'prefix and carries llm_verified = false: this system never verifies a '
    'hypothesis, and a column that says so is harder to ignore than a footnote.';


-- -----------------------------------------------------------------------------
-- anomaly_investigation_detail
-- The same evidence as an ordered narrative. line_rank is stable, so the story
-- reads the same way every time it is opened.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.anomaly_investigation_detail AS
WITH base AS (
    SELECT * FROM salesops.anomaly_investigation
),
lines AS (
    -- 1. KPI facts
    SELECT b.calendar_date, b.decision_id, 'observed_fact' AS layer_key, 10 AS line_rank,
           'Net revenue' AS label, to_char(b.fact_net_revenue_usd, 'FM999999990.00') AS value,
           'usd' AS unit FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'observed_fact', 11,
           'Orders', b.fact_orders_count::text, 'count' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'observed_fact', 12,
           'Average order value', to_char(b.fact_average_order_value_usd, 'FM999999990.00'), 'usd' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'observed_fact', 13,
           'Refund rate', to_char(b.fact_refund_rate, 'FM0.0000'), 'ratio' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'observed_fact', 14,
           'Revenue figures complete', b.fact_is_complete::text, 'boolean' FROM base b

    -- 2. Statistical evidence
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 20,
           'Baseline median revenue', to_char(b.signal_baseline_median_usd, 'FM999999990.00'), 'usd' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 21,
           'Revenue robust z-score', to_char(b.signal_revenue_robust_z, 'FM990.000'), 'z' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 22,
           'Refund robust z-score', to_char(b.signal_refund_robust_z, 'FM990.000'), 'z' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 23,
           'Dominant signal', b.signal_dominant, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 24,
           'Signals breaching threshold', b.signal_count::text, 'count' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 25,
           'Baseline status', b.signal_baseline_status, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'statistical_signal', 26,
           'Detector version', b.signal_detector_version, 'version' FROM base b

    -- 3. Deterministic decision
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 30,
           'Severity', b.decision_severity, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 31,
           'Routing', b.decision_routing, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 32,
           'Decision', b.decision_outcome, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 33,
           'Primary reason code', b.decision_primary_reason, 'code' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 34,
           'All reason codes', b.decision_all_reasons, 'code' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 35,
           'Revenue delta', to_char(b.decision_revenue_delta_usd, 'FM999999990.00'), 'usd' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 36,
           'Business impact tier', b.decision_impact_tier, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'deterministic_decision', 37,
           'Decision version', b.decision_version, 'version' FROM base b

    -- 4. Model hypothesis
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 40,
           'Summary', b.llm_summary, 'model_text' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 41,
           'Primary hypothesis', b.llm_primary_hypothesis, 'model_text' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 42,
           'Stated confidence', b.llm_confidence, 'model_label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 43,
           'Model', b.llm_model_name, 'model_label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 44,
           'Prompt version', b.llm_prompt_version, 'version' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 45,
           'Verified by this system', 'no', 'model_label' FROM base b

    -- 5. Missing evidence
    UNION ALL SELECT b.calendar_date, b.decision_id, 'model_hypothesis', 50,
           'Evidence the model reported it lacked',
           NULLIF(jsonb_array_length(COALESCE(b.llm_missing_evidence, '[]'::jsonb)), 0)::text,
           'count' FROM base b

    -- 6. Notification and review
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 60,
           'Notification status', b.notification_status, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 61,
           'Review status', b.review_status, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 62,
           'Assigned to', b.review_assigned_to, 'actor' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 63,
           'Resolution', b.review_resolution, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 64,
           'Approved by', b.review_approved_by, 'actor' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'human_review', 65,
           'Review ageing', b.review_ageing_bucket, 'label' FROM base b

    -- 7. Remediation
    UNION ALL SELECT b.calendar_date, b.decision_id, 'approved_remediation', 70,
           'Action type', b.remediation_action_type, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'approved_remediation', 71,
           'Authorised by', b.remediation_authorized_by, 'actor' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'approved_remediation', 72,
           'Policy version', b.remediation_policy_version, 'version' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'completed_remediation', 73,
           'Remediation status', b.remediation_status, 'label' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'completed_remediation', 74,
           'Executed by', b.remediation_executed_by, 'actor' FROM base b
    UNION ALL SELECT b.calendar_date, b.decision_id, 'completed_remediation', 75,
           'Provider reference', b.remediation_provider_reference, 'reference' FROM base b

    -- 8. Operational history
    UNION ALL SELECT b.calendar_date, b.decision_id, 'operational_event', 80,
           'Operational events recorded', b.operational_event_count::text, 'count' FROM base b
)
SELECT
    l.calendar_date,
    l.decision_id,
    p.layer_rank,
    l.layer_key,
    p.layer_label,
    p.evidence_kind,
    p.is_model_generated,
    p.produced_by_stage,
    l.line_rank,
    l.label,
    l.value,
    l.unit
FROM lines l
JOIN salesops.presentation_layers p ON p.layer_key = l.layer_key
WHERE l.value IS NOT NULL
ORDER BY l.calendar_date DESC, p.layer_rank, l.line_rank;

COMMENT ON VIEW salesops.anomaly_investigation_detail IS
    'The ordered drill-down: facts, then statistics, then the deterministic '
    'decision, then the model, then what followed. layer_rank IS the reading '
    'order, and is_model_generated travels with every line so a renderer cannot '
    'lose it. Rows with no value are omitted rather than shown as blanks.';


-- =============================================================================
-- 7. Auditability
--
-- Six streams, one shape. Everything below already existed as an event row or a
-- timestamped column; this view does no more than put them in one order.
--
-- Every row carries an actor, an occurrence time, and where a transition
-- exists, both of its ends. Where the system did not record an actor - Stage 6
-- and Stage 7 are machines, and pretending otherwise would be a fabrication -
-- the actor is the pipeline component's own name.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.audit_event_stream AS

-- Stage 6: decisions. One event per decision, actor = the deterministic engine.
SELECT
    'decision'::text                            AS stream,
    d.decision_id::text                         AS entity_id,
    d.calendar_date,
    d.decided_at                                AS occurred_at,
    'decided'::text                             AS event_type,
    NULL::text                                  AS from_state,
    d.decision                                  AS to_state,
    'stage6-decision-engine'::text              AS actor,
    d.decision_reason_code                      AS reason,
    d.decision_version                          AS version_info,
    format('severity=%s routing=%s impact=%s', d.severity, d.routing,
           d.business_impact_tier)              AS detail,
    'deterministic_decision'::text              AS layer_key
FROM salesops.anomaly_decisions d

UNION ALL

-- Stage 7: hypotheses. The model is named as the actor, which is the honest
-- attribution: no person wrote this.
-- to_state is 'generated', not the confidence. A hypothesis has exactly one
-- state, and putting 'medium' in a column whose other values are lifecycle
-- states would read as though the model had moved something into it. The
-- confidence is what the model SAID, so it goes in the reason, labelled.
SELECT
    'hypothesis', h.hypothesis_id::text, h.calendar_date, h.generated_at,
    'generated', NULL, 'generated',
    COALESCE(h.model_provider || '/' || h.model_name, 'llm'),
    'stated confidence: ' || COALESCE(h.confidence, 'none'), h.prompt_version,
    format('digest=%s tokens=%s latency_ms=%s',
           left(COALESCE(h.evidence_digest, ''), 12),
           COALESCE(h.prompt_tokens, 0) + COALESCE(h.completion_tokens, 0),
           COALESCE(h.latency_ms, 0)),
    'model_hypothesis'
FROM salesops.anomaly_hypotheses h

UNION ALL

-- Stage 8: delivery attempts, not just the final status.
SELECT
    'notification', n.notification_id::text, n.calendar_date, t.attempted_at,
    'delivery_attempt', NULL, t.outcome,
    COALESCE(t.provider, n.provider, 'notification-router'),
    COALESCE(t.error_message, 'ok'), n.decision_version,
    format('attempt=%s channel=%s status_code=%s', t.attempt_number, n.channel,
           COALESCE(t.status_code::text, '-')),
    'human_review'
FROM salesops.notification_attempts t
JOIN salesops.notifications n ON n.notification_id = t.notification_id

UNION ALL

-- Stage 8/9: review transitions. Actors here are people.
SELECT
    'review', e.review_id::text, r.calendar_date, e.occurred_at,
    'review_transition', e.from_status, e.to_status,
    COALESCE(e.actor, 'unattributed'),
    COALESCE(e.resolution, e.note_excerpt), r.decision_version,
    format('severity=%s', r.severity),
    'human_review'
FROM salesops.review_events e
JOIN salesops.review_queue r ON r.review_id = e.review_id

UNION ALL

-- Stage 9: remediation transitions.
SELECT
    'remediation', e.remediation_id::text, a.calendar_date, e.occurred_at,
    'remediation_transition', e.from_status, e.to_status,
    COALESCE(e.actor, 'unattributed'),
    e.reason, a.policy_version,
    format('action=%s severity=%s', a.action_type, a.severity),
    CASE WHEN e.to_status = 'executed' THEN 'completed_remediation'
         ELSE 'approved_remediation' END
FROM salesops.remediation_events e
JOIN salesops.remediation_actions a ON a.remediation_id = e.remediation_id

UNION ALL

-- Stage 10: operational events. Append-only at the table, so append-only here.
-- version_info is NULL here, and stays NULL. A recovery happens under no
-- policy version and no decision version; borrowing another column to fill the
-- space would put a value in the audit trail that nothing recorded.
SELECT
    'operational', o.entity_id, NULL::date, o.occurred_at,
    o.event_type, o.from_state, o.to_state,
    COALESCE(o.actor, 'unattributed'),
    o.reason_code, NULL,
    format('entity=%s %s', o.entity_type, COALESCE(o.detail::text, '')),
    'operational_event'
FROM salesops.operational_events o;

COMMENT ON VIEW salesops.audit_event_stream IS
    'Six audit streams in one shape: who, when, from what state to what state, '
    'and under which version. Machine-produced events name the component that '
    'produced them rather than borrowing a person''s name.';


-- =============================================================================
-- 8. End-to-end traceability
--
-- orders -> KPI -> anomaly -> decision -> hypothesis -> notification/review ->
-- remediation -> operational outcome, for one calendar date, in that order.
--
-- Every step is present for every anomaly date whether or not it happened, with
-- reached = false where it did not. A chain that stops at Stage 7 should LOOK
-- like a chain that stopped, not like a chain with a missing row.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.incident_timeline AS
WITH incidents AS (
    SELECT * FROM salesops.anomaly_investigation
),
steps AS (
    SELECT i.calendar_date, i.decision_id, 1 AS step_rank, 'orders' AS step,
           'observed_fact' AS layer_key,
           (i.fact_orders_count IS NOT NULL) AS reached,
           NULL::timestamptz AS occurred_at,
           NULL::text AS actor,
           format('%s orders, %s units', i.fact_orders_count, i.fact_units_sold) AS summary,
           i.fact_orders_count::text AS reference
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 2, 'kpi', 'observed_fact',
           (i.fact_net_revenue_usd IS NOT NULL), NULL, 'kpi-refresh',
           format('net revenue %s USD, AOV %s USD',
                  to_char(i.fact_net_revenue_usd, 'FM999999990.00'),
                  to_char(i.fact_average_order_value_usd, 'FM999999990.00')),
           NULL
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 3, 'anomaly', 'statistical_signal',
           COALESCE(i.signal_is_anomaly, FALSE), i.signal_detected_at, 'anomaly-detector',
           format('score %s, dominant %s, %s signal(s)',
                  to_char(i.signal_anomaly_score, 'FM990.000'),
                  COALESCE(i.signal_dominant, '-'), i.signal_count),
           i.anomaly_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 4, 'decision', 'deterministic_decision',
           TRUE, i.decided_at, 'stage6-decision-engine',
           format('%s / %s / %s', i.decision_severity, i.decision_routing,
                  i.decision_primary_reason),
           i.decision_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 5, 'hypothesis', 'model_hypothesis',
           (i.llm_hypothesis_id IS NOT NULL), i.llm_generated_at,
           COALESCE(i.llm_model_name, 'llm'),
           CASE WHEN i.llm_hypothesis_id IS NULL THEN 'no hypothesis generated'
                ELSE format('unverified hypothesis, stated confidence %s',
                            COALESCE(i.llm_confidence, '-')) END,
           i.llm_hypothesis_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 6, 'notification', 'human_review',
           (i.notification_id IS NOT NULL), i.notification_sent_at,
           'notification-router',
           CASE WHEN i.notification_id IS NULL
                THEN 'not notified (routing = ' || i.decision_routing || ')'
                ELSE format('%s via %s', i.notification_status, i.notification_channel) END,
           i.notification_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 7, 'review', 'human_review',
           (i.review_id IS NOT NULL), i.review_created_at,
           COALESCE(i.review_approved_by, i.review_assigned_to),
           CASE WHEN i.review_id IS NULL THEN 'no review required'
                ELSE format('%s%s', i.review_status,
                            CASE WHEN i.review_ageing_bucket IS NULL THEN ''
                                 ELSE ', ageing ' || i.review_ageing_bucket END) END,
           i.review_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 8, 'remediation', 'approved_remediation',
           (i.remediation_id IS NOT NULL), i.remediation_authorized_at,
           i.remediation_authorized_by,
           CASE WHEN i.remediation_id IS NULL THEN 'no action proposed'
                ELSE format('%s (%s)', i.remediation_action_type, i.remediation_status) END,
           i.remediation_id::text
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 9, 'execution', 'completed_remediation',
           (i.remediation_executed_at IS NOT NULL), i.remediation_executed_at,
           i.remediation_executed_by,
           CASE WHEN i.remediation_executed_at IS NOT NULL
                THEN format('executed, provider reference %s',
                            COALESCE(i.remediation_provider_reference, '-'))
                WHEN i.remediation_id IS NULL THEN 'nothing to execute'
                ELSE 'authorised work has not run' END,
           i.remediation_provider_reference
      FROM incidents i
    UNION ALL
    SELECT i.calendar_date, i.decision_id, 10, 'operational_outcome', 'operational_event',
           (i.operational_event_count > 0), NULL, 'stage10-maintenance',
           format('%s operational event(s) recorded against this action',
                  i.operational_event_count),
           NULL
      FROM incidents i
)
SELECT
    s.calendar_date,
    s.decision_id,
    s.step_rank,
    s.step,
    s.layer_key,
    p.layer_label,
    p.evidence_kind,
    p.is_model_generated,
    p.produced_by_stage,
    s.reached,
    s.occurred_at,
    s.actor,
    s.summary,
    s.reference
FROM steps s
JOIN salesops.presentation_layers p ON p.layer_key = s.layer_key
ORDER BY s.calendar_date DESC, s.step_rank;

COMMENT ON VIEW salesops.incident_timeline IS
    'One incident, ten steps, in pipeline order. Steps that did not happen are '
    'present with reached = false and a reason, so a chain that stopped early is '
    'legible as a decision rather than as missing data.';


-- =============================================================================
-- 9. The read-only reporting role
--
-- Created with NOLOGIN and no password. The provisioning script grants LOGIN
-- and sets a password from the environment; until then the role exists and can
-- do nothing, which is the correct state for a role defined in a file that is
-- committed to version control.
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salesops_readonly') THEN
        CREATE ROLE salesops_readonly NOLOGIN;
    END IF;
END;
$$;

COMMENT ON ROLE salesops_readonly IS
    'Stage 11 reporting login. SELECT on salesops only: no write privilege and no '
    'EXECUTE, so the SQL editor in a BI tool is not a write path.';

-- Connect and read. Nothing else.
GRANT CONNECT ON DATABASE salesops TO salesops_readonly;
GRANT USAGE  ON SCHEMA salesops    TO salesops_readonly;
GRANT SELECT ON ALL TABLES    IN SCHEMA salesops TO salesops_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA salesops TO salesops_readonly;

-- Relations added by a later migration inherit the same grant, so a new table is
-- readable without anyone remembering to say so.
ALTER DEFAULT PRIVILEGES IN SCHEMA salesops
    GRANT SELECT ON TABLES TO salesops_readonly;

-- The important half. PostgreSQL grants EXECUTE on every function to PUBLIC by
-- default; salesops.purge_staging() and salesops.replay_failed_batch() are
-- write operations reachable from a SELECT, so PUBLIC loses EXECUTE here and
-- the owner keeps it explicitly.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA salesops FROM PUBLIC;
REVOKE ALL     ON SCHEMA salesops FROM PUBLIC;
GRANT  USAGE   ON SCHEMA salesops TO salesops_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA salesops
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- ...but four views read a threshold through a function, and PostgreSQL checks
-- EXECUTE against the CALLING role even inside a view whose table access is
-- checked against its owner. Revoking everything therefore breaks
-- operational_health, staging_retention_report and their neighbours for the
-- reporting role - which would be discovered as a blank dashboard panel rather
-- than as an error, so it is dealt with here.
--
-- The grant is by VOLATILITY rather than by name. A function marked STABLE or
-- IMMUTABLE is one the author declared cannot change the database, and those
-- are exactly the four configuration readers; every operation that recovers,
-- replays, purges, decides or refreshes is VOLATILE and stays denied. Writing
-- the rule this way means a helper added by a later migration is covered
-- correctly without anyone remembering to come back here.
DO $$
DECLARE
    fn RECORD;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS signature
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'salesops'
          AND p.prokind = 'f'
          AND p.provolatile <> 'v'          -- 's' stable, 'i' immutable
          AND p.prorettype <> 'trigger'::regtype
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO salesops_readonly',
                       fn.signature);
    END LOOP;
END;
$$;

-- Writing to the analytics schema stays with the owner. Spelled out rather than
-- left implicit, because "nobody else was granted it" is a weaker guarantee than
-- "it was revoked".
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON ALL TABLES IN SCHEMA salesops FROM salesops_readonly;


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V013', 'Presentation layer: layer vocabulary, executive/investigation/'
                'operational/audit views, read-only reporting role')
ON CONFLICT (version) DO NOTHING;

COMMIT;
