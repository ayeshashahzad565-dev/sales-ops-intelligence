-- =============================================================================
-- V006  Statistical anomaly detection results
--
-- Stage 4 = deterministic KPI generation
-- Stage 5 = statistical anomaly detection      <- this table
-- Stage 7 = LLM root-cause reasoning
--
-- This table holds STATISTICAL EVIDENCE ONLY. It says how unusual a day was and
-- which signals said so. It does not say how serious that is, what caused it,
-- or what anyone should do about it - those are later stages, and keeping them
-- out is what lets the deterministic layer be trusted to grade the model's work
-- rather than the other way round.
--
-- Consequently there is deliberately no severity column, no natural-language
-- column, and nowhere for an LLM to write.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS salesops.anomaly_daily (
    anomaly_id            BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    calendar_date         DATE         NOT NULL,

    -- Bump this when the algorithm changes. Results from two versions coexist
    -- on the same dates, so a change can be diffed against its predecessor
    -- instead of silently replacing it.
    detector_version      TEXT         NOT NULL,

    -- --- verdict -------------------------------------------------------------
    anomaly_score         NUMERIC(12,4),
    is_anomaly            BOOLEAN      NOT NULL DEFAULT FALSE,

    -- --- per-signal evidence -------------------------------------------------
    -- Percent deviations are relative to the baseline median. The refund signal
    -- is an ABSOLUTE difference in rate, not a percentage: baseline refund rates
    -- sit near 0.02, so a percentage change against them is numerically unstable
    -- and reads as thousands of percent for an ordinary move.
    revenue_deviation_pct NUMERIC(14,4),
    revenue_robust_z      NUMERIC(14,4),
    aov_deviation_pct     NUMERIC(14,4),
    aov_robust_z          NUMERIC(14,4),
    refund_rate_deviation NUMERIC(14,6),
    refund_robust_z       NUMERIC(14,4),
    orders_deviation_pct  NUMERIC(14,4),
    orders_robust_z       NUMERIC(14,4),

    -- --- how the comparison was made ----------------------------------------
    baseline_status       TEXT         NOT NULL,
    baseline_kind         TEXT,
    baseline_size         INTEGER,

    -- --- explainability ------------------------------------------------------
    dominant_signal       TEXT,
    signal_count          INTEGER      NOT NULL DEFAULT 0,

    detected_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Re-running the detector must update a date in place, never duplicate it.
    CONSTRAINT anomaly_daily_unique_per_version
        UNIQUE (calendar_date, detector_version),

    -- Referencing dim_date, NOT kpi_daily, on purpose. kpi_daily is rebuilt
    -- wholesale (DELETE + INSERT) by refresh_kpi_daily(); a foreign key onto it
    -- would either block every rebuild or cascade-delete detection history.
    -- The kpi_daily relationship is asserted by the Stage 5 test suite instead.
    CONSTRAINT anomaly_daily_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    CONSTRAINT anomaly_daily_status_valid
        CHECK (baseline_status IN ('scored', 'insufficient_history', 'incomplete_kpi')),

    CONSTRAINT anomaly_daily_kind_valid
        CHECK (baseline_kind IS NULL OR baseline_kind IN ('day_of_week', 'day_type')),

    -- An observation that could not be scored can never be an anomaly. Without
    -- this, a bug that forgot to check baseline_status could publish a verdict
    -- derived from no baseline at all.
    CONSTRAINT anomaly_daily_unscored_is_not_anomaly
        CHECK (baseline_status = 'scored' OR NOT is_anomaly),

    CONSTRAINT anomaly_daily_scored_has_score
        CHECK (baseline_status <> 'scored' OR anomaly_score IS NOT NULL),

    CONSTRAINT anomaly_daily_scored_has_baseline
        CHECK (baseline_status <> 'scored'
               OR (baseline_kind IS NOT NULL AND baseline_size > 0)),

    CONSTRAINT anomaly_daily_score_non_negative
        CHECK (anomaly_score IS NULL OR anomaly_score >= 0),

    -- Four signals: revenue, aov, refund, orders.
    CONSTRAINT anomaly_daily_signal_count_range
        CHECK (signal_count BETWEEN 0 AND 4),

    CONSTRAINT anomaly_daily_dominant_signal_valid
        CHECK (dominant_signal IS NULL
               OR dominant_signal IN ('revenue', 'aov', 'refund', 'orders')),

    CONSTRAINT anomaly_daily_version_format
        CHECK (detector_version ~ '^v[0-9]+\.[0-9]+\.[0-9]+$')
);

COMMENT ON TABLE salesops.anomaly_daily IS
    'Stage 5 statistical detection results. Evidence and scores only - no severity, '
    'no natural language, no LLM output. One row per (calendar_date, detector_version).';

COMMENT ON COLUMN salesops.anomaly_daily.detector_version IS
    'Algorithm version. Results from different versions coexist so a change can be diffed.';
COMMENT ON COLUMN salesops.anomaly_daily.baseline_status IS
    'scored = compared against a real baseline | '
    'insufficient_history = too few prior comparable observations to judge | '
    'incomplete_kpi = the day''s KPI row was missing FX coverage, so its money columns '
    'are understated and scoring it would measure an FX gap as a revenue event.';
COMMENT ON COLUMN salesops.anomaly_daily.baseline_kind IS
    'day_of_week = compared against prior same-weekday observations (preferred) | '
    'day_type = fallback, compared against prior weekday-or-weekend observations.';
COMMENT ON COLUMN salesops.anomaly_daily.refund_rate_deviation IS
    'ABSOLUTE difference from the baseline median refund rate, in rate units, not percent.';
COMMENT ON COLUMN salesops.anomaly_daily.signal_count IS
    'How many of the four signals individually cleared the robust-z significance threshold.';
COMMENT ON COLUMN salesops.anomaly_daily.dominant_signal IS
    'The signal contributing most to anomaly_score. Explainability only - not a cause.';


-- The dashboard and Stage 6 both ask "what is anomalous, most recent first".
CREATE INDEX IF NOT EXISTS idx_anomaly_daily_flagged
    ON salesops.anomaly_daily (calendar_date DESC)
    WHERE is_anomaly;

-- Reading one detector version's full series.
CREATE INDEX IF NOT EXISTS idx_anomaly_daily_version_date
    ON salesops.anomaly_daily (detector_version, calendar_date);


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V006', 'Statistical anomaly detection results: anomaly_daily')
ON CONFLICT (version) DO NOTHING;

COMMIT;
