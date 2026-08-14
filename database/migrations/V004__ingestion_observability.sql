-- =============================================================================
-- V004  Ingestion observability: run ledger, error log, safe-cast helpers
--
-- Stage 2 built the analytics model. Stage 3 adds the pipeline that fills it,
-- and a pipeline you cannot see the state of is not production-shaped. These
-- two tables answer the two questions that matter when something goes wrong:
--
--   "did the last load actually finish?"        -> ingestion_runs
--   "what broke, when, and in which node?"      -> pipeline_errors
--
-- Idempotent and transactional, like every migration in this directory.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Safe casts
--
-- Validating untrusted payloads needs casts that FAIL SOFT. `'banana'::int`
-- raises and aborts the whole statement, which would take a dead-letter batch
-- down with the one bad row it was supposed to isolate. These return NULL
-- instead, so "not a number" becomes a validation result rather than an outage.
--
-- STABLE, not IMMUTABLE: text->date depends on the DateStyle setting.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION salesops.try_to_date(p_value TEXT)
RETURNS DATE
LANGUAGE plpgsql
STABLE
RETURNS NULL ON NULL INPUT
AS $$
BEGIN
    RETURN p_value::DATE;
EXCEPTION
    WHEN OTHERS THEN RETURN NULL;
END;
$$;

COMMENT ON FUNCTION salesops.try_to_date(TEXT) IS
    'Cast text to date, or NULL if it is not a valid date. Never raises.';


CREATE OR REPLACE FUNCTION salesops.try_to_numeric(p_value TEXT)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
RETURNS NULL ON NULL INPUT
AS $$
BEGIN
    RETURN p_value::NUMERIC;
EXCEPTION
    WHEN OTHERS THEN RETURN NULL;
END;
$$;

COMMENT ON FUNCTION salesops.try_to_numeric(TEXT) IS
    'Cast text to numeric, or NULL if it is not a number. Never raises.';


-- -----------------------------------------------------------------------------
-- ingestion_runs
--
-- One row per pipeline execution, written at the START with status 'running'
-- and finalised at the end. That ordering is the point: a run that crashes
-- half-way leaves a visible 'running' row rather than no evidence at all.
-- A missing row means the pipeline never started; a stale 'running' row means
-- it started and died.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.ingestion_runs (
    run_id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Joins this run to every row it landed in raw_orders_staging.
    batch_id          UUID        NOT NULL UNIQUE,

    source            TEXT        NOT NULL DEFAULT 'mock-sales-api',

    -- The window actually requested from the API, inclusive both ends.
    window_from       DATE        NOT NULL,
    window_to         DATE        NOT NULL,

    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,

    status            TEXT        NOT NULL DEFAULT 'running',

    -- received = accepted + rejected + duplicate, once a run completes.
    records_received  INTEGER     NOT NULL DEFAULT 0,
    records_accepted  INTEGER     NOT NULL DEFAULT 0,  -- new rows in fact_orders
    records_rejected  INTEGER     NOT NULL DEFAULT 0,  -- failed validation
    records_duplicate INTEGER     NOT NULL DEFAULT 0,  -- valid, already present

    error_message     TEXT,

    -- Ties a database row back to an n8n execution, so the error handler can
    -- find the run it belongs to without guessing.
    n8n_execution_id  TEXT,

    CONSTRAINT ingestion_runs_status_valid
        CHECK (status IN ('running', 'success', 'partial', 'failed')),

    -- 'partial' exists so a run that rejected rows is never reported as clean.
    CONSTRAINT ingestion_runs_failure_has_reason
        CHECK (status <> 'failed' OR error_message IS NOT NULL),

    CONSTRAINT ingestion_runs_window_ordered
        CHECK (window_from <= window_to),

    CONSTRAINT ingestion_runs_finished_after_start
        CHECK (finished_at IS NULL OR finished_at >= started_at),

    CONSTRAINT ingestion_runs_counts_non_negative
        CHECK (records_received  >= 0 AND records_accepted  >= 0
           AND records_rejected  >= 0 AND records_duplicate >= 0),

    -- A finished run must account for every record it received. Enforced only
    -- once the run completes, because counts are zero while it is in flight.
    CONSTRAINT ingestion_runs_counts_balance
        CHECK (
            status IN ('running', 'failed')
            OR records_received = records_accepted + records_rejected + records_duplicate
        )
);

COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per ingestion execution. Written as ''running'' up front so a crashed run is visible.';
COMMENT ON COLUMN salesops.ingestion_runs.status IS
    'running = in flight | success = all records accepted or already present | '
    'partial = completed but some records rejected | failed = the pipeline errored';
COMMENT ON COLUMN salesops.ingestion_runs.records_duplicate IS
    'Valid records already in fact_orders. On a re-run of the same window this equals records_received.';

-- "What is the newest window we have successfully loaded?" - the query the
-- pipeline runs on every execution to compute its next window.
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_success_window
    ON salesops.ingestion_runs (window_to DESC)
    WHERE status IN ('success', 'partial');

-- Finding runs that died in flight.
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_unfinished
    ON salesops.ingestion_runs (started_at)
    WHERE status = 'running';

-- The error handler looks runs up by n8n execution id.
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_execution
    ON salesops.ingestion_runs (n8n_execution_id)
    WHERE n8n_execution_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- pipeline_errors
--
-- Written by the n8n Error Trigger workflow. Stage 3 deliberately stops at
-- persisting the failure - Slack and escalation come later. Establishing the
-- record now means the notification layer has something to read when it arrives.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.pipeline_errors (
    error_id      BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    workflow_id   TEXT,
    workflow_name TEXT        NOT NULL,
    execution_id  TEXT,

    -- The node that actually threw, which is the first thing you want to know.
    node_name     TEXT,

    error_message TEXT        NOT NULL,
    error_stack   TEXT,

    -- Whatever else the Error Trigger provided. JSONB because the shape of an
    -- n8n error payload is not ours to control and will change between versions.
    context       JSONB
);

COMMENT ON TABLE salesops.pipeline_errors IS
    'Failures caught by the n8n Error Trigger workflow. Stage 10 adds alerting on top.';

CREATE INDEX IF NOT EXISTS idx_pipeline_errors_occurred
    ON salesops.pipeline_errors (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_errors_workflow
    ON salesops.pipeline_errors (workflow_name, occurred_at DESC);


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V004', 'Ingestion observability: ingestion_runs, pipeline_errors, safe-cast helpers')
ON CONFLICT (version) DO NOTHING;

COMMIT;
