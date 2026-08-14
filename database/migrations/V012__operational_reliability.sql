-- =============================================================================
-- V012  Stage 10: operational reliability, recovery and lifecycle management
--
--   Stages 3-9 built the machine. This file is about what happens when it
--   stops half-way through something.
--
-- Every stage so far has been careful about the state it writes and careless
-- about the state it might be interrupted in. A run opens as 'running' before
-- any data moves - deliberately, so a crash is visible - but nothing ever
-- closes it. A notification claims its row before the provider is called. A
-- remediation action enters 'executing' before a network call that may or may
-- not have landed. Each of those is the right design, and each leaves a state
-- that can stay stuck forever.
--
-- Stage 10 does not add a stage to the pipeline. It adds the parts that let the
-- pipeline run unattended: bounded recovery, explicit replay, retention, and a
-- health view that says WHY something is unhealthy rather than just that it is.
--
-- The distinction this file is built on
-- ------------------------------------
--     RECOVERY      moves a stuck record into an honest, final-or-actionable
--                   state. It never repeats work.
--     RE-EXECUTION  repeats work. It happens only where repeating is provably
--                   safe, and never as a side effect of recovery.
--
-- The sharpest case is a remediation action stranded in 'executing'. The
-- process died somewhere around a provider call, and nothing in this database
-- can know whether that call landed. Automatically re-executing would risk
-- doing the thing twice; automatically failing it would risk claiming something
-- did not happen when it did. So recovery moves it to 'execution_unknown' -
-- a state that is honest about the uncertainty - and a human reconciles it.
--
-- Three vocabularies that must not be confused
-- --------------------------------------------
--     anomaly severity    none | minor | major | critical      (Stage 6)
--     operational health  healthy | warning | degraded | failed
--     review ageing       fresh | warning | overdue | critical_overdue
--
-- They are different questions about different things. A critical anomaly whose
-- review is fresh is a healthy pipeline. A minor anomaly whose review has been
-- unclaimed for a week is not. Nothing in this file reads Stage 6 severity to
-- decide operational health, and nothing here changes a review's authorisation
-- state on the basis of its age.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Operational configuration
--
-- Every threshold this stage compares against lives in one table, keyed by
-- name, exactly as V008 put the decision thresholds in `decision_thresholds`.
-- The reasoning is the same: an operator asking "how old is stale?" runs one
-- SELECT, and a change is a visible row rather than an edited constant.
--
-- These are OPERATIONAL defaults, not business requirements. Nothing here was
-- handed down by a retention policy or an SLA; they are starting points chosen
-- to be safe, and every one is documented as configurable.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.operational_config (
    config_key   TEXT PRIMARY KEY,
    config_value NUMERIC     NOT NULL,
    unit         TEXT        NOT NULL,
    description  TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT operational_config_key_format
        CHECK (config_key ~ '^[a-z][a-z0-9_]+[a-z0-9]$'),
    CONSTRAINT operational_config_value_non_negative
        CHECK (config_value >= 0),
    CONSTRAINT operational_config_unit_valid
        CHECK (unit IN ('minutes', 'hours', 'days', 'count'))
);

COMMENT ON TABLE salesops.operational_config IS
    'Stage 10 operational thresholds. Operational defaults, not business requirements - '
    'every value is configurable and none was handed down by a retention policy or an SLA.';

INSERT INTO salesops.operational_config (config_key, config_value, unit, description) VALUES
    ('staging_retention_days', 90, 'days',
     'How long a settled raw_orders_staging row is kept. Only ''processed'' and '
     '''skipped'' rows are ever eligible; ''pending'' and ''failed'' are never deleted.'),

    ('stale_run_timeout_minutes', 120, 'minutes',
     'How long an ingestion_runs row may sit at ''running'' before it is treated as '
     'abandoned. Comfortably longer than the slowest pipeline (Stage 7, bounded by the '
     'LLM timeout multiplied by the anomaly count).'),

    ('stale_notification_timeout_minutes', 180, 'minutes',
     'How long a notification may sit undelivered before it is reported as stale. '
     'Longer than the gap between two scheduled routing runs would be, so a notification '
     'is only stale once the schedule has visibly failed to pick it up.'),

    ('stale_remediation_timeout_minutes', 60, 'minutes',
     'How long a remediation action may sit at ''executing'' before it is treated as '
     'crashed mid-call. Deliberately short: the provider call is bounded by an HTTP '
     'timeout, so anything beyond an hour is a dead process, not a slow one.'),

    ('review_warning_age_hours', 24, 'hours',
     'Age at which an open review item is labelled ''warning''. Operational ageing only '
     '- it changes no review state and no authorisation.'),

    ('review_overdue_age_hours', 72, 'hours',
     'Age at which an open review item is labelled ''overdue''.'),

    ('review_critical_overdue_age_hours', 168, 'hours',
     'Age at which an open review item is labelled ''critical_overdue''. One week - by '
     'which point nobody is going to look at it without being asked.'),

    ('max_replay_attempts', 3, 'count',
     'How many times a failed staging row may be replayed. Bounded because a row that '
     'has failed validation three times is failing for a reason replay cannot fix.'),

    ('retry_backoff_minutes', 30, 'minutes',
     'Minimum gap between a failure and the next retry becoming eligible. Flat rather '
     'than exponential: the failures this covers are environmental, and an operator '
     'watching a queue drain deserves a predictable interval.'),

    ('failed_run_warning_count', 1, 'count',
     'How many recent failed runs of one pipeline are tolerated before the health view '
     'reports ''degraded'' rather than ''warning''.')
ON CONFLICT (config_key) DO UPDATE
    SET unit        = EXCLUDED.unit,
        description = EXCLUDED.description;


CREATE OR REPLACE FUNCTION salesops.operational_setting(p_key TEXT)
RETURNS NUMERIC
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v NUMERIC;
BEGIN
    SELECT config_value INTO v
    FROM salesops.operational_config WHERE config_key = p_key;

    IF v IS NULL THEN
        -- Loudly, not with a plausible default. A silent fallback here would
        -- mean a typo in a threshold name quietly disables a safety check.
        RAISE EXCEPTION 'No operational setting named %', p_key
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN v;
END;
$$;

COMMENT ON FUNCTION salesops.operational_setting(TEXT) IS
    'One operational threshold. Raises on an unknown key rather than returning a '
    'plausible default - a typo must not quietly disable a safety check.';


-- =============================================================================
-- 2. The operational audit log
--
-- Every recovery, replay, purge and reconciliation appends here. It is the
-- answer to "who or what changed this record, and why" for changes that were
-- made BY the machine rather than by the pipeline doing its job.
--
-- Append-only, enforced. A recovery log that can be edited afterwards is not a
-- recovery log; and the specific failure mode it guards against is an automated
-- process tidying away the evidence of what it did.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.operational_events (
    event_id     BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    event_type   TEXT        NOT NULL,
    entity_type  TEXT        NOT NULL,
    entity_id    TEXT        NOT NULL,

    from_state   TEXT,
    to_state     TEXT,

    -- 'stage10-recovery' for anything the maintenance run did on its own.
    actor        TEXT        NOT NULL,

    -- Machine-readable. The health view and the retry queue both read this
    -- rather than parsing a sentence.
    reason_code  TEXT        NOT NULL,
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT operational_events_type_valid
        CHECK (event_type IN (
            'stale_run_recovered', 'stale_remediation_recovered',
            'remediation_reconciled', 'staging_replayed', 'staging_purged',
            'notification_retry_requested', 'maintenance_run')),
    CONSTRAINT operational_events_entity_valid
        CHECK (entity_type IN (
            'ingestion_run', 'notification', 'remediation_action',
            'staging_batch', 'maintenance')),
    CONSTRAINT operational_events_reason_format
        CHECK (reason_code ~ '^[A-Z][A-Z0-9_]+[A-Z0-9]$'),
    CONSTRAINT operational_events_actor_present
        CHECK (length(btrim(actor)) > 0)
);

COMMENT ON TABLE salesops.operational_events IS
    'Append-only log of everything Stage 10 did TO the pipeline: recoveries, replays, '
    'purges, reconciliations. UPDATE and DELETE are refused by trigger - an automated '
    'process must not be able to tidy away the evidence of what it did.';
COMMENT ON COLUMN salesops.operational_events.actor IS
    '''stage10-recovery'' for automated maintenance; a named person for anything done by '
    'hand. Asserted, not authenticated - there is no authentication in this project.';

CREATE INDEX IF NOT EXISTS idx_operational_events_entity
    ON salesops.operational_events (entity_type, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_events_recent
    ON salesops.operational_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_operational_events_type
    ON salesops.operational_events (event_type, occurred_at DESC);


CREATE OR REPLACE FUNCTION salesops.guard_operational_events_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'salesops.operational_events is append-only; % is not permitted.', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_operational_events_append_only ON salesops.operational_events;
CREATE TRIGGER trg_operational_events_append_only
    BEFORE UPDATE OR DELETE ON salesops.operational_events
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_operational_events_append_only();


-- =============================================================================
-- 3. Stale run recovery
--
-- A run opens as 'running' before any data moves, so a crash leaves a visible
-- row rather than silence. That was always the right design; what was missing
-- is anything that ever closes it. Until now a run that died in 2026 would
-- still read 'running' in 2030, and "is the pipeline currently working?" had no
-- answer that could be trusted.
--
-- What this does NOT do: re-run anything. A recovered run is a closed record of
-- something that did not finish. Whether the work should be repeated is a
-- separate question with a separate answer per pipeline - the ingestion window
-- self-heals on the next run, Stage 5-8 are idempotent, and Stage 9 needs a
-- human. Recovery is not re-execution.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.recover_stale_runs(
    p_actor TEXT DEFAULT 'stage10-recovery',
    p_dry_run BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    run_id         BIGINT,
    source         TEXT,
    started_at     TIMESTAMPTZ,
    stale_minutes  NUMERIC,
    recovered      BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_timeout INTERVAL := make_interval(
        mins => salesops.operational_setting('stale_run_timeout_minutes')::int);
BEGIN
    IF p_dry_run THEN
        RETURN QUERY
        SELECT r.run_id, r.source, r.started_at,
               round(extract(epoch FROM (now() - r.started_at)) / 60.0, 1),
               FALSE
        FROM salesops.ingestion_runs r
        WHERE r.status = 'running'
          AND r.started_at < now() - v_timeout
        ORDER BY r.started_at;
        RETURN;
    END IF;

    RETURN QUERY
    WITH stale AS (
        SELECT r.run_id, r.source, r.started_at,
               round(extract(epoch FROM (now() - r.started_at)) / 60.0, 1) AS mins
        FROM salesops.ingestion_runs r
        WHERE r.status = 'running'
          AND r.started_at < now() - v_timeout
        -- FOR UPDATE SKIP LOCKED: two maintenance runs overlapping must not
        -- both recover the same row and write two events for one recovery.
        FOR UPDATE SKIP LOCKED
    ),
    recovered AS (
        UPDATE salesops.ingestion_runs r
        SET status        = 'failed',
            finished_at   = now(),
            error_message = format(
                'STALE_RUN_TIMEOUT: no completion recorded within %s minutes '
                '(open for %s). Recovered by %s. The work was NOT repeated.',
                salesops.operational_setting('stale_run_timeout_minutes')::int,
                s.mins, p_actor)
        FROM stale s
        WHERE r.run_id = s.run_id
        RETURNING r.run_id, r.source, r.started_at, s.mins
    ),
    logged AS (
        INSERT INTO salesops.operational_events
            (event_type, entity_type, entity_id, from_state, to_state, actor,
             reason_code, detail)
        SELECT 'stale_run_recovered', 'ingestion_run', rec.run_id::text,
               'running', 'failed', p_actor, 'STALE_RUN_TIMEOUT',
               jsonb_build_object(
                   'source', rec.source,
                   'started_at', rec.started_at,
                   'stale_minutes', rec.mins,
                   'timeout_minutes',
                       salesops.operational_setting('stale_run_timeout_minutes'),
                   'work_repeated', FALSE)
        FROM recovered rec
        RETURNING 1
    )
    SELECT rec.run_id, rec.source, rec.started_at, rec.mins, TRUE FROM recovered rec;
END;
$$;

COMMENT ON FUNCTION salesops.recover_stale_runs(TEXT, BOOLEAN) IS
    'Closes ingestion_runs abandoned at ''running'' past the configured timeout, with a '
    'machine-readable reason and an audit event. Repeats no work. Idempotent: a run it '
    'has already closed is no longer ''running'' and is not found again.';


-- =============================================================================
-- 4. Ingestion replay
--
-- The rule that shapes everything below: a replay must not make a failure look
-- like it never happened.
--
-- So a replay never touches the original staging rows. It copies their payloads
-- into a NEW batch under a NEW ingestion run, and records the mapping. The
-- original rows keep their 'failed' status and their error message forever, and
-- the two questions
--
--     did the original attempt fail?        -> raw_orders_staging.processing_status
--     did a replay of it succeed?           -> ingestion_replays.outcome
--
-- have separate answers in separate places, which is the only way both can stay
-- true at once.
--
-- The replay run uses source = 'ingestion-replay', NOT 'mock-sales-api'. Stage
-- 3 computes its next window from the newest successful 'mock-sales-api' run;
-- a replay landing in that source would move the window and silently skip a day
-- of real orders.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.ingestion_replays (
    replay_id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Provenance: exactly which row this is a replay of.
    original_ingestion_id BIGINT      NOT NULL,
    original_batch_id     UUID        NOT NULL,
    original_error        TEXT        NOT NULL,

    -- ...and where the replay landed.
    replay_ingestion_id   BIGINT      NOT NULL,
    replay_batch_id       UUID        NOT NULL,
    attempt_number        INTEGER     NOT NULL,

    outcome               TEXT        NOT NULL DEFAULT 'pending',
    outcome_detail        TEXT,

    actor                 TEXT        NOT NULL,
    replayed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One replay row per (original row, attempt). Re-running a replay of the
    -- same original at the same attempt number is refused rather than silently
    -- creating a second copy of the payload.
    CONSTRAINT ingestion_replays_unique_attempt
        UNIQUE (original_ingestion_id, attempt_number),

    -- One replay row per replayed staging row: a replayed row has exactly one
    -- origin, so provenance can never be ambiguous.
    CONSTRAINT ingestion_replays_unique_target
        UNIQUE (replay_ingestion_id),

    CONSTRAINT ingestion_replays_original_fk
        FOREIGN KEY (original_ingestion_id)
        REFERENCES salesops.raw_orders_staging (ingestion_id),
    CONSTRAINT ingestion_replays_replay_fk
        FOREIGN KEY (replay_ingestion_id)
        REFERENCES salesops.raw_orders_staging (ingestion_id),

    CONSTRAINT ingestion_replays_outcome_valid
        CHECK (outcome IN ('pending', 'succeeded', 'failed_again', 'duplicate')),
    CONSTRAINT ingestion_replays_attempt_positive
        CHECK (attempt_number > 0),
    CONSTRAINT ingestion_replays_not_self
        CHECK (replay_ingestion_id <> original_ingestion_id)
);

COMMENT ON TABLE salesops.ingestion_replays IS
    'Provenance for every replayed staging row: which original row it came from, which '
    'new row carries it, and what happened. The original row is never modified - '
    '"the first attempt failed" and "a replay succeeded" are both true, and both '
    'recorded, in different places.';
COMMENT ON COLUMN salesops.ingestion_replays.outcome IS
    'pending = staged, not yet loaded | succeeded = the replay produced a fact row | '
    'failed_again = it failed validation again, deterministically | '
    'duplicate = the order was already in fact_orders';

CREATE INDEX IF NOT EXISTS idx_ingestion_replays_original
    ON salesops.ingestion_replays (original_batch_id, original_ingestion_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_replays_replay_batch
    ON salesops.ingestion_replays (replay_batch_id);


-- What is worth replaying, and what has run out of attempts.
CREATE OR REPLACE VIEW salesops.ingestion_replay_candidates AS
SELECT
    s.batch_id                                    AS original_batch_id,
    count(*)                                      AS failed_rows,
    count(*) FILTER (WHERE s.order_id IS NOT NULL) AS rows_with_order_id,
    min(s.received_at)                            AS first_failure_at,
    max(s.received_at)                            AS latest_failure_at,
    max(COALESCE(a.attempts, 0))                  AS max_attempts_used,
    salesops.operational_setting('max_replay_attempts')::int AS max_attempts,
    count(*) FILTER (
        WHERE COALESCE(a.attempts, 0)
              < salesops.operational_setting('max_replay_attempts')::int
    )                                             AS rows_eligible,
    -- A batch is replayable while any of its failed rows has attempts left.
    bool_or(COALESCE(a.attempts, 0)
            < salesops.operational_setting('max_replay_attempts')::int) AS replay_eligible,
    (array_agg(DISTINCT left(s.error_message, 120)))[1:3]  AS sample_errors
FROM salesops.raw_orders_staging s
LEFT JOIN LATERAL (
    SELECT count(*) AS attempts
    FROM salesops.ingestion_replays r
    WHERE r.original_ingestion_id = s.ingestion_id
) a ON TRUE
WHERE s.processing_status = 'failed'
GROUP BY s.batch_id;

COMMENT ON VIEW salesops.ingestion_replay_candidates IS
    'Failed staging batches, with how many replay attempts each has used. A batch stops '
    'being eligible once every failed row has spent its attempts - a row that has failed '
    'validation three times is failing for a reason replay cannot fix.';


-- -----------------------------------------------------------------------------
-- Staging a replay
--
-- Copies the payloads and records the provenance. Deliberately does NOT load
-- them: staging and loading are separate so that a replay which stages
-- successfully and then fails to load leaves a batch an operator can inspect,
-- rather than an all-or-nothing operation with nothing to look at.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION salesops.stage_replay_batch(
    p_original_batch_id UUID,
    p_actor TEXT DEFAULT 'stage10-recovery'
)
RETURNS TABLE (
    replay_batch_id UUID,
    replay_run_id   BIGINT,
    rows_staged     INTEGER,
    rows_skipped    INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_batch    UUID := gen_random_uuid();
    v_run      BIGINT;
    v_max      INTEGER := salesops.operational_setting('max_replay_attempts')::int;
    v_staged   INTEGER := 0;
    v_skipped  INTEGER := 0;
    v_from     DATE;
    v_to       DATE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM salesops.raw_orders_staging
                    WHERE batch_id = p_original_batch_id) THEN
        RAISE EXCEPTION 'No staging batch %', p_original_batch_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Rows that have spent their attempts are counted, not carried forward.
    SELECT count(*) FILTER (WHERE used >= v_max)
      INTO v_skipped
    FROM (
        SELECT (SELECT count(*) FROM salesops.ingestion_replays r
                 WHERE r.original_ingestion_id = s.ingestion_id) AS used
        FROM salesops.raw_orders_staging s
        WHERE s.batch_id = p_original_batch_id AND s.processing_status = 'failed'
    ) t;

    -- The window is derived from the payloads themselves, so the ledger entry
    -- describes the data rather than the day the replay happened to run.
    SELECT min(salesops.try_to_date(s.source_payload ->> 'order_date')),
           max(salesops.try_to_date(s.source_payload ->> 'order_date'))
      INTO v_from, v_to
    FROM salesops.raw_orders_staging s
    WHERE s.batch_id = p_original_batch_id AND s.processing_status = 'failed';

    INSERT INTO salesops.ingestion_runs
        (batch_id, source, window_from, window_to, status, n8n_execution_id)
    VALUES (v_batch, 'ingestion-replay',
            COALESCE(v_from, CURRENT_DATE), COALESCE(v_to, CURRENT_DATE),
            'running', NULL)
    RETURNING run_id INTO v_run;

    WITH eligible AS (
        SELECT s.ingestion_id, s.batch_id, s.order_id, s.source_payload,
               s.error_message,
               (SELECT count(*) FROM salesops.ingestion_replays r
                 WHERE r.original_ingestion_id = s.ingestion_id)::int AS used
        FROM salesops.raw_orders_staging s
        WHERE s.batch_id = p_original_batch_id
          AND s.processing_status = 'failed'
    ),
    -- The payload is copied verbatim. Nothing is repaired, corrected or
    -- defaulted on the way through: a replay that quietly fixed its input would
    -- be a different record of what the source actually sent.
    copied AS (
        INSERT INTO salesops.raw_orders_staging
            (batch_id, order_id, source_payload, processing_status)
        SELECT v_batch, e.order_id, e.source_payload, 'pending'
        FROM eligible e
        WHERE e.used < v_max
        RETURNING ingestion_id, order_id, source_payload
    ),
    paired AS (
        SELECT e.ingestion_id AS original_id, e.batch_id AS original_batch,
               e.error_message, e.used,
               c.ingestion_id AS replay_id
        FROM (SELECT *, row_number() OVER (ORDER BY ingestion_id) AS rn
                FROM eligible WHERE used < v_max) e
        JOIN (SELECT *, row_number() OVER (ORDER BY ingestion_id) AS rn
                FROM copied) c ON c.rn = e.rn
    ),
    recorded AS (
        INSERT INTO salesops.ingestion_replays
            (original_ingestion_id, original_batch_id, original_error,
             replay_ingestion_id, replay_batch_id, attempt_number, actor)
        SELECT p.original_id, p.original_batch, p.error_message,
               p.replay_id, v_batch, p.used + 1, p_actor
        FROM paired p
        RETURNING 1
    )
    SELECT count(*)::int INTO v_staged FROM recorded;

    INSERT INTO salesops.operational_events
        (event_type, entity_type, entity_id, from_state, to_state, actor,
         reason_code, detail)
    VALUES ('staging_replayed', 'staging_batch', p_original_batch_id::text,
            'failed', 'replay_staged', p_actor, 'REPLAY_STAGED',
            jsonb_build_object(
                'replay_batch_id', v_batch,
                'replay_run_id', v_run,
                'rows_staged', v_staged,
                'rows_skipped_attempts_exhausted', v_skipped,
                'original_rows_modified', FALSE));

    RETURN QUERY SELECT v_batch, v_run, v_staged, v_skipped;
END;
$$;

COMMENT ON FUNCTION salesops.stage_replay_batch(UUID, TEXT) IS
    'Copies a failed batch''s payloads into a new batch under a new ingestion run, '
    'recording provenance. The original rows are never modified. Payloads are copied '
    'verbatim - nothing is repaired on the way through.';


-- -----------------------------------------------------------------------------
-- Loading a staged batch
--
-- ON THE DUPLICATION THIS REPRESENTS - read before changing either copy.
--
-- The validation rules below are the same rules the Stage 3 "Validate Orders"
-- and "Insert Facts" nodes apply. That is a second implementation of one rule
-- set, and it is a real cost.
--
-- The alternative was worse. Replaying a batch means running exactly the
-- validation the original run ran, and that logic lives inside n8n node
-- parameters where nothing in PostgreSQL can reach it. The options were: call
-- the workflow from itself (n8n cannot), rewrite Stage 3 to call this function
-- (a rewrite of a completed, tested stage), or accept two implementations of a
-- rule set that is itself written down.
--
-- What makes the third tolerable: the source of truth is neither copy. It is
-- the Stage 2 validation rules, and both implementations are tested against
-- them independently - the workflow by n8n/tests/test_ingestion_sql.py, this
-- function by the Stage 10 suite. A drift between them fails one of those two.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION salesops.load_staged_batch(p_batch_id UUID)
RETURNS TABLE (
    records_received  INTEGER,
    records_accepted  INTEGER,
    records_rejected  INTEGER,
    records_duplicate INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_received  INTEGER;
    v_rejected  INTEGER;
    v_accepted  INTEGER;
    v_duplicate INTEGER;
BEGIN
    SELECT count(*) INTO v_received
    FROM salesops.raw_orders_staging
    WHERE batch_id = p_batch_id AND processing_status = 'pending';

    -- ---- validate, and dead-letter the failures ----------------------------
    -- Every rule is evaluated for every row, so error_message lists ALL the
    -- reasons rather than only the first.
    WITH typed AS (
        SELECT
            s.ingestion_id,
            btrim(COALESCE(s.source_payload ->> 'order_id', ''))    AS order_id,
            salesops.try_to_date(s.source_payload ->> 'order_date') AS order_date,
            s.source_payload ->> 'region'                           AS region_code,
            s.source_payload ->> 'product'                          AS product_sku,
            s.source_payload ->> 'channel'                          AS channel_code,
            btrim(COALESCE(s.source_payload ->> 'customer_id', '')) AS customer_id,
            salesops.try_to_numeric(s.source_payload ->> 'quantity')      AS quantity,
            salesops.try_to_numeric(s.source_payload ->> 'unit_price')    AS unit_price,
            salesops.try_to_numeric(s.source_payload ->> 'refund_amount') AS refund_amount,
            upper(btrim(COALESCE(s.source_payload ->> 'currency', '')))   AS currency
        FROM salesops.raw_orders_staging s
        WHERE s.batch_id = p_batch_id AND s.processing_status = 'pending'
    ),
    checked AS (
        SELECT t.ingestion_id,
               array_remove(ARRAY[
                   CASE WHEN t.order_id = ''      THEN 'missing order_id' END,
                   CASE WHEN t.order_date IS NULL THEN 'invalid or missing order_date' END,
                   CASE WHEN t.customer_id = ''   THEN 'missing customer_id' END,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM salesops.dim_region r
                                          WHERE r.region_code = t.region_code)
                        THEN 'unknown region' END,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM salesops.dim_product p
                                          WHERE p.product_sku = t.product_sku)
                        THEN 'unknown product' END,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM salesops.dim_channel c
                                          WHERE c.channel_code = t.channel_code)
                        THEN 'unknown channel' END,
                   CASE WHEN t.quantity IS NULL OR t.quantity <= 0
                        THEN 'quantity must be a positive number' END,
                   CASE WHEN t.unit_price IS NULL OR t.unit_price < 0
                        THEN 'unit_price must be a non-negative number' END,
                   CASE WHEN t.refund_amount IS NULL OR t.refund_amount < 0
                        THEN 'refund_amount must be a non-negative number' END,
                   CASE WHEN t.refund_amount IS NOT NULL AND t.quantity IS NOT NULL
                             AND t.unit_price IS NOT NULL
                             AND t.refund_amount > t.quantity * t.unit_price
                        THEN 'refund_amount exceeds gross amount' END,
                   CASE WHEN t.currency !~ '^[A-Z]{3}$'
                        THEN 'currency must be a 3-letter code' END
               ], NULL) AS problems
        FROM typed t
    ),
    rejected AS (
        UPDATE salesops.raw_orders_staging s
        SET processing_status = 'failed',
            processed_at      = now(),
            error_message     = array_to_string(c.problems, '; ')
        FROM checked c
        WHERE s.ingestion_id = c.ingestion_id
          AND cardinality(c.problems) > 0
        RETURNING 1
    )
    SELECT count(*)::int INTO v_rejected FROM rejected;

    -- ---- late-arriving customers -------------------------------------------
    INSERT INTO salesops.dim_customer (customer_id, region_id, first_seen_date)
    SELECT DISTINCT ON (s.source_payload ->> 'customer_id')
        s.source_payload ->> 'customer_id',
        r.region_id,
        salesops.try_to_date(s.source_payload ->> 'order_date')
    FROM salesops.raw_orders_staging s
    JOIN salesops.dim_region r ON r.region_code = s.source_payload ->> 'region'
    WHERE s.batch_id = p_batch_id AND s.processing_status = 'pending'
    ORDER BY s.source_payload ->> 'customer_id',
             salesops.try_to_date(s.source_payload ->> 'order_date')
    ON CONFLICT (customer_id) DO UPDATE
        SET first_seen_date = LEAST(salesops.dim_customer.first_seen_date,
                                    EXCLUDED.first_seen_date);

    -- ---- facts, idempotently ------------------------------------------------
    -- ON CONFLICT (order_id) DO NOTHING is what makes a replay safe to repeat:
    -- an order already in fact_orders is left exactly as it was recorded, and
    -- the staging row settles at 'skipped' rather than 'processed'.
    WITH ready AS (
        SELECT DISTINCT ON (s.source_payload ->> 'order_id')
            s.ingestion_id,
            s.source_payload ->> 'order_id'                          AS order_id,
            salesops.try_to_date(s.source_payload ->> 'order_date')  AS order_date,
            s.source_payload ->> 'customer_id'                       AS customer_id,
            r.region_id, p.product_id, c.channel_id,
            salesops.try_to_numeric(s.source_payload ->> 'quantity')::integer         AS quantity,
            salesops.try_to_numeric(s.source_payload ->> 'unit_price')::numeric(14,4) AS unit_price,
            upper(s.source_payload ->> 'currency')                                    AS currency,
            salesops.try_to_numeric(s.source_payload ->> 'refund_amount')::numeric(18,4) AS refund_amount_local
        FROM salesops.raw_orders_staging s
        JOIN salesops.dim_region  r ON r.region_code  = s.source_payload ->> 'region'
        JOIN salesops.dim_product p ON p.product_sku  = s.source_payload ->> 'product'
        JOIN salesops.dim_channel c ON c.channel_code = s.source_payload ->> 'channel'
        WHERE s.batch_id = p_batch_id AND s.processing_status = 'pending'
        ORDER BY s.source_payload ->> 'order_id', s.ingestion_id
    ),
    inserted AS (
        INSERT INTO salesops.fact_orders
            (order_id, order_date, customer_id, region_id, product_id, channel_id,
             quantity, unit_price, currency, refund_amount_local)
        SELECT order_id, order_date, customer_id, region_id, product_id, channel_id,
               quantity, unit_price, currency, refund_amount_local
        FROM ready
        ON CONFLICT (order_id) DO NOTHING
        RETURNING order_id
    ),
    -- Close out every pending row of the batch, including any duplicate
    -- order_ids collapsed by DISTINCT ON above, so nothing is left at 'pending'.
    marked AS (
        UPDATE salesops.raw_orders_staging tgt
        SET processing_status = CASE WHEN i.order_id IS NOT NULL
                                     THEN 'processed' ELSE 'skipped' END,
            processed_at      = now()
        FROM salesops.raw_orders_staging src
        LEFT JOIN inserted i ON i.order_id = src.source_payload ->> 'order_id'
        WHERE tgt.ingestion_id = src.ingestion_id
          AND src.batch_id = p_batch_id
          AND src.processing_status = 'pending'
        RETURNING tgt.processing_status AS final_status
    )
    SELECT count(*) FILTER (WHERE final_status = 'processed')::int,
           count(*) FILTER (WHERE final_status = 'skipped')::int
      INTO v_accepted, v_duplicate
    FROM marked;

    RETURN QUERY SELECT v_received, COALESCE(v_accepted, 0),
                        COALESCE(v_rejected, 0), COALESCE(v_duplicate, 0);
END;
$$;

COMMENT ON FUNCTION salesops.load_staged_batch(UUID) IS
    'Validates and loads one staged batch, settling every row. Mirrors the Stage 3 '
    'workflow rules - see the header comment for why that duplication exists and what '
    'keeps the two in step.';


-- -----------------------------------------------------------------------------
-- The whole replay, end to end.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION salesops.replay_failed_batch(
    p_original_batch_id UUID,
    p_actor TEXT DEFAULT 'stage10-recovery'
)
RETURNS TABLE (
    replay_batch_id   UUID,
    replay_run_id     BIGINT,
    rows_staged       INTEGER,
    rows_skipped      INTEGER,
    records_accepted  INTEGER,
    records_rejected  INTEGER,
    records_duplicate INTEGER,
    run_status        TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_stage  RECORD;
    v_load   RECORD;
    v_status TEXT;
BEGIN
    SELECT * INTO v_stage FROM salesops.stage_replay_batch(p_original_batch_id, p_actor);

    IF v_stage.rows_staged = 0 THEN
        UPDATE salesops.ingestion_runs
        SET finished_at = now(), status = 'failed',
            error_message = 'REPLAY_NO_ELIGIBLE_ROWS: every failed row in the original '
                            'batch has spent its replay attempts.'
        WHERE run_id = v_stage.replay_run_id;

        RETURN QUERY SELECT v_stage.replay_batch_id, v_stage.replay_run_id,
                            0, v_stage.rows_skipped, 0, 0, 0, 'failed'::text;
        RETURN;
    END IF;

    SELECT * INTO v_load FROM salesops.load_staged_batch(v_stage.replay_batch_id);

    v_status := CASE
                    WHEN v_load.records_rejected > 0 AND v_load.records_accepted = 0
                         AND v_load.records_duplicate = 0 THEN 'failed'
                    WHEN v_load.records_rejected > 0      THEN 'partial'
                    ELSE 'success'
                END;

    UPDATE salesops.ingestion_runs
    SET finished_at       = now(),
        records_received  = v_load.records_received,
        records_accepted  = v_load.records_accepted,
        records_rejected  = v_load.records_rejected,
        records_duplicate = v_load.records_duplicate,
        status            = v_status,
        error_message     = CASE WHEN v_load.records_rejected > 0
                                 THEN format('REPLAY_PARTIAL: %s row(s) failed validation '
                                             'again.', v_load.records_rejected) END
    WHERE run_id = v_stage.replay_run_id;

    -- The outcome per replayed row, read back from where the row actually
    -- settled rather than inferred from the totals.
    UPDATE salesops.ingestion_replays r
    SET outcome = CASE s.processing_status
                      WHEN 'processed' THEN 'succeeded'
                      WHEN 'skipped'   THEN 'duplicate'
                      WHEN 'failed'    THEN 'failed_again'
                      ELSE 'pending'
                  END,
        outcome_detail = s.error_message
    FROM salesops.raw_orders_staging s
    WHERE s.ingestion_id = r.replay_ingestion_id
      AND r.replay_batch_id = v_stage.replay_batch_id;

    RETURN QUERY SELECT v_stage.replay_batch_id, v_stage.replay_run_id,
                        v_stage.rows_staged, v_stage.rows_skipped,
                        v_load.records_accepted, v_load.records_rejected,
                        v_load.records_duplicate, v_status;
END;
$$;

COMMENT ON FUNCTION salesops.replay_failed_batch(UUID, TEXT) IS
    'Stages and loads a replay of a failed batch, then records the per-row outcome. '
    'Idempotent against fact_orders (ON CONFLICT DO NOTHING) and bounded by '
    'max_replay_attempts. Never modifies the original staging rows.';


-- =============================================================================
-- 5. Staging retention
--
-- Deliberately conservative. Only rows that have SETTLED successfully are ever
-- eligible:
--
--     processed / skipped   eligible once older than the retention period
--     pending               never - it is unfinished work
--     failed                never - it is the dead-letter trail, and deleting
--                           it would destroy both the replay source and the
--                           record that something went wrong
--
-- Keeping every failed row forever is stricter than a retention policy needs to
-- be. It is also the only default that cannot lose evidence, and if the volume
-- ever becomes a problem that deserves a deliberate archival decision rather
-- than a number in a config table.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.staging_retention_report AS
WITH cutoff AS (
    SELECT now() - make_interval(
        days => salesops.operational_setting('staging_retention_days')::int) AS before
)
SELECT
    CASE
        WHEN s.processing_status = 'pending' THEN 'protected_pending'
        WHEN s.processing_status = 'failed'  THEN 'protected_failed'
        WHEN s.received_at >= c.before       THEN 'protected_recent'
        ELSE 'eligible'
    END                                          AS disposition,
    s.processing_status,
    count(*)                                     AS rows,
    min(s.received_at)                           AS oldest,
    max(s.received_at)                           AS newest,
    salesops.operational_setting('staging_retention_days')::int AS retention_days
FROM salesops.raw_orders_staging s
CROSS JOIN cutoff c
GROUP BY 1, 2, 6;

COMMENT ON VIEW salesops.staging_retention_report IS
    'What retention would delete, and what it would protect and why - readable before '
    'anything is deleted. ''pending'' and ''failed'' rows are never eligible.';


CREATE OR REPLACE FUNCTION salesops.purge_staging(
    p_dry_run BOOLEAN DEFAULT TRUE,
    p_actor   TEXT DEFAULT 'stage10-recovery'
)
RETURNS TABLE (
    dry_run          BOOLEAN,
    rows_eligible    BIGINT,
    rows_deleted     BIGINT,
    rows_protected   BIGINT,
    retention_days   INTEGER,
    cutoff           TIMESTAMPTZ
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_days      INTEGER := salesops.operational_setting('staging_retention_days')::int;
    v_cutoff    TIMESTAMPTZ := now() - make_interval(days => v_days);
    v_eligible  BIGINT;
    v_protected BIGINT;
    v_deleted   BIGINT := 0;
BEGIN
    -- Dry-run by default. A cleanup function whose safe mode has to be asked
    -- for is a cleanup function that will one day be called without arguments.
    SELECT
        count(*) FILTER (WHERE processing_status IN ('processed', 'skipped')
                           AND received_at < v_cutoff),
        count(*) FILTER (WHERE processing_status IN ('pending', 'failed')
                            OR received_at >= v_cutoff)
      INTO v_eligible, v_protected
    FROM salesops.raw_orders_staging;

    IF NOT p_dry_run AND v_eligible > 0 THEN
        WITH doomed AS (
            DELETE FROM salesops.raw_orders_staging s
            WHERE s.processing_status IN ('processed', 'skipped')
              AND s.received_at < v_cutoff
              -- A row another row was replayed FROM is provenance. The FK would
              -- refuse the delete anyway; saying so here makes the rule findable.
              AND NOT EXISTS (SELECT 1 FROM salesops.ingestion_replays r
                               WHERE r.original_ingestion_id = s.ingestion_id
                                  OR r.replay_ingestion_id   = s.ingestion_id)
            RETURNING 1
        )
        SELECT count(*) INTO v_deleted FROM doomed;

        INSERT INTO salesops.operational_events
            (event_type, entity_type, entity_id, from_state, to_state, actor,
             reason_code, detail)
        VALUES ('staging_purged', 'staging_batch', 'retention-sweep',
                NULL, NULL, p_actor, 'RETENTION_SWEEP',
                jsonb_build_object(
                    'retention_days', v_days,
                    'cutoff', v_cutoff,
                    'rows_deleted', v_deleted,
                    'rows_protected', v_protected,
                    'protected_statuses', ARRAY['pending', 'failed']));
    END IF;

    RETURN QUERY SELECT p_dry_run, v_eligible, v_deleted, v_protected, v_days, v_cutoff;
END;
$$;

COMMENT ON FUNCTION salesops.purge_staging(BOOLEAN, TEXT) IS
    'Deletes settled staging rows older than the retention period. DRY RUN BY DEFAULT. '
    'Never deletes ''pending'' or ''failed'' rows, and never a row involved in a replay. '
    'Idempotent: a second call finds nothing left to delete.';


-- =============================================================================
-- 6. Remediation execution recovery
--
-- Stage 9 left one state that can strand: 'executing'. The action is claimed,
-- the provider has been called, and then the process dies. Nothing in this
-- database can know whether that call landed.
--
-- Both automatic answers are wrong:
--
--     re-execute   might do the thing twice
--     fail it      might claim something did not happen when it did
--
-- So recovery moves it to 'execution_unknown' - a state that is honest about
-- the uncertainty and, crucially, is NOT in the work set the Stage 9 workflow
-- executes from. A human reconciles it against whatever evidence exists, and
-- only then does it become 'executed' or 'failed'.
--
-- The recorded attempt gets outcome 'unknown', which is also new. An attempt
-- was made; what it achieved is not known. Writing it as a failure would be
-- guessing, and writing nothing would lose the fact that the provider was
-- called at all - which is the single most important fact for whoever
-- reconciles it.
--
-- This is a compatibility change to V011, kept to the minimum: two new
-- vocabulary values, four new transitions, and the pending-execution view
-- narrowed so the new state can never be picked up automatically.
-- =============================================================================

ALTER TABLE salesops.remediation_actions
    DROP CONSTRAINT IF EXISTS remediation_actions_status_valid;
ALTER TABLE salesops.remediation_actions
    ADD CONSTRAINT remediation_actions_status_valid
    CHECK (status IN ('proposed', 'approved', 'executing', 'executed',
                      'rejected', 'failed', 'cancelled', 'execution_unknown'));

ALTER TABLE salesops.remediation_actions
    DROP CONSTRAINT IF EXISTS remediation_actions_authorization_recorded;
ALTER TABLE salesops.remediation_actions
    ADD CONSTRAINT remediation_actions_authorization_recorded
    CHECK ((status IN ('proposed', 'rejected', 'cancelled'))
           OR (authorized_by IS NOT NULL AND authorized_at IS NOT NULL));

ALTER TABLE salesops.remediation_events
    DROP CONSTRAINT IF EXISTS remediation_events_to_status_valid;
ALTER TABLE salesops.remediation_events
    ADD CONSTRAINT remediation_events_to_status_valid
    CHECK (to_status IN ('proposed', 'approved', 'executing', 'executed',
                         'rejected', 'failed', 'cancelled', 'execution_unknown'));

ALTER TABLE salesops.remediation_events
    DROP CONSTRAINT IF EXISTS remediation_events_from_status_valid;
ALTER TABLE salesops.remediation_events
    ADD CONSTRAINT remediation_events_from_status_valid
    CHECK (from_status IS NULL
           OR from_status IN ('proposed', 'approved', 'executing', 'executed',
                              'rejected', 'failed', 'cancelled', 'execution_unknown'));

ALTER TABLE salesops.remediation_attempts
    DROP CONSTRAINT IF EXISTS remediation_attempts_outcome_valid;
ALTER TABLE salesops.remediation_attempts
    ADD CONSTRAINT remediation_attempts_outcome_valid
    CHECK (outcome IN ('success', 'retryable_failure', 'permanent_failure', 'unknown'));

COMMENT ON COLUMN salesops.remediation_attempts.outcome IS
    'success | retryable_failure | permanent_failure | unknown. ''unknown'' means the '
    'process died around the provider call: an attempt was made, and what it achieved '
    'is not known. Recording it as a failure would be guessing.';

COMMENT ON COLUMN salesops.remediation_actions.status IS
    'proposed -> approved -> executing -> executed. Terminal: executed, rejected, '
    'cancelled. failed is a resting state permitting a bounded explicit retry. '
    'execution_unknown (V012) is where a crashed execution lands - it is NOT executable '
    'and requires human reconciliation. Enforced by trigger.';


-- The Stage 9 state machine, extended by four transitions. Every V011
-- transition still behaves identically; 'execution_unknown' is added as a
-- destination from 'executing' and as a source requiring reconciliation.
CREATE OR REPLACE FUNCTION salesops.guard_remediation_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    allowed          BOOLEAN;
    max_attempts CONSTANT INTEGER := 3;
BEGIN
    IF (NEW.review_id, NEW.anomaly_id, NEW.decision_id, NEW.calendar_date,
        NEW.decision_version, NEW.severity, NEW.routing, NEW.decision,
        NEW.notification_allowed, NEW.human_review_required,
        NEW.decision_reason_code, NEW.decision_reason_codes,
        NEW.action_type, NEW.policy_version,
        NEW.review_approved_by, NEW.review_approved_at, NEW.review_resolution,
        NEW.hypothesis_id, NEW.hypothesis_status, NEW.request_payload,
        NEW.created_at)
       IS DISTINCT FROM
       (OLD.review_id, OLD.anomaly_id, OLD.decision_id, OLD.calendar_date,
        OLD.decision_version, OLD.severity, OLD.routing, OLD.decision,
        OLD.notification_allowed, OLD.human_review_required,
        OLD.decision_reason_code, OLD.decision_reason_codes,
        OLD.action_type, OLD.policy_version,
        OLD.review_approved_by, OLD.review_approved_at, OLD.review_resolution,
        OLD.hypothesis_id, OLD.hypothesis_status, OLD.request_payload,
        OLD.created_at) THEN
        RAISE EXCEPTION
            'Remediation % carries the authorisation a human gave; its snapshot is '
            'immutable.', OLD.remediation_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = OLD.status THEN
        IF OLD.status IN ('executed', 'rejected', 'cancelled') THEN
            RAISE EXCEPTION
                'Remediation % is %; it is closed.', OLD.remediation_id, OLD.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    allowed := (OLD.status, NEW.status) IN (
        ('proposed',  'approved'),
        ('proposed',  'rejected'),
        ('proposed',  'cancelled'),
        ('approved',  'executing'),
        ('approved',  'cancelled'),
        ('executing', 'executed'),
        ('executing', 'failed'),
        ('failed',    'executing'),
        ('failed',    'cancelled'),
        -- V012: a crashed execution, and the three ways out of it. There is no
        -- ('execution_unknown', 'executing') - reconciliation is what decides
        -- whether the work may be repeated, and it goes via 'failed' to say so.
        ('executing',          'execution_unknown'),
        ('execution_unknown',  'executed'),
        ('execution_unknown',  'failed'),
        ('execution_unknown',  'cancelled')
    );

    IF NOT allowed THEN
        RAISE EXCEPTION
            'Invalid remediation transition % -> % for remediation %.',
            OLD.status, NEW.status, OLD.remediation_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF OLD.status = 'failed' AND NEW.status = 'executing'
       AND OLD.attempt_count >= max_attempts THEN
        RAISE EXCEPTION
            'Remediation % has spent its % attempts; it will not be retried '
            'automatically.', OLD.remediation_id, max_attempts
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = 'approved' THEN
        NEW.authorized_at := COALESCE(NEW.authorized_at, now());
        IF NEW.authorized_by IS NULL OR length(btrim(NEW.authorized_by)) = 0 THEN
            RAISE EXCEPTION
                'Remediation % cannot be authorised without an identifiable actor.',
                OLD.remediation_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF NEW.status = 'executed' THEN
        NEW.executed_at := COALESCE(NEW.executed_at, now());
    END IF;

    INSERT INTO salesops.remediation_events
        (remediation_id, from_status, to_status, actor, reason)
    VALUES (
        OLD.remediation_id, OLD.status, NEW.status,
        COALESCE(NEW.executed_by, NEW.authorized_by, OLD.authorized_by,
                 NEW.review_approved_by),
        COALESCE(NEW.closed_reason, NEW.last_error)
    );

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_remediation_transition() IS
    'Enforces the remediation state machine and the retry budget, keeps the '
    'authorisation snapshot immutable, and appends to remediation_events. V012 adds '
    'execution_unknown for a crashed execution: it has no path back to ''executing'' '
    'except through reconciliation to ''failed'', so recovery can never re-execute.';


-- The Stage 9 work set, narrowed. 'execution_unknown' is deliberately absent:
-- an action whose outcome nobody knows must never be picked up automatically.
CREATE OR REPLACE VIEW salesops.remediation_pending_execution AS
SELECT
    a.remediation_id,
    a.calendar_date,
    a.severity,
    a.action_type,
    a.status,
    a.attempt_count,
    a.review_id,
    a.review_approved_by,
    a.authorized_by,
    a.authorized_at,
    a.last_error,
    a.request_payload
FROM salesops.remediation_actions a
WHERE a.status IN ('approved', 'failed')
  AND a.attempt_count < 3
ORDER BY
    CASE a.severity WHEN 'critical' THEN 0 ELSE 1 END,
    a.authorized_at;

COMMENT ON VIEW salesops.remediation_pending_execution IS
    'Actions a human has authorised that have not yet executed, worst first, with the '
    'retry budget already applied. Excludes ''execution_unknown'': an action whose '
    'outcome nobody knows must never be picked up automatically.';


CREATE OR REPLACE FUNCTION salesops.recover_stale_remediation(
    p_actor TEXT DEFAULT 'stage10-recovery',
    p_dry_run BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    remediation_id BIGINT,
    stale_minutes  NUMERIC,
    attempt_number INTEGER,
    recovered      BOOLEAN
)
LANGUAGE plpgsql
AS $$
-- The OUT parameters are named after the columns they describe, which is right
-- for the caller and ambiguous inside the function body. Resolving a bare
-- reference to the column is the correct default here: every OUT parameter is
-- only ever written by an explicit assignment, never read inside a statement.
#variable_conflict use_column
DECLARE
    v_timeout INTERVAL := make_interval(
        mins => salesops.operational_setting('stale_remediation_timeout_minutes')::int);
    r RECORD;
    v_mins NUMERIC;
    v_attempt INTEGER;
    v_id BIGINT;
BEGIN
    FOR r IN
        SELECT a.remediation_id, a.attempt_count,
               COALESCE((SELECT max(e.occurred_at) FROM salesops.remediation_events e
                          WHERE e.remediation_id = a.remediation_id
                            AND e.to_status = 'executing'), a.created_at) AS claimed_at
        FROM salesops.remediation_actions a
        WHERE a.status = 'executing'
        FOR UPDATE SKIP LOCKED
    LOOP
        v_mins := round(extract(epoch FROM (now() - r.claimed_at)) / 60.0, 1);
        CONTINUE WHEN now() - r.claimed_at < v_timeout;

        IF p_dry_run THEN
            remediation_id := r.remediation_id;
            stale_minutes  := v_mins;
            attempt_number := r.attempt_count + 1;
            recovered      := FALSE;
            RETURN NEXT;
            CONTINUE;
        END IF;

        v_attempt := r.attempt_count + 1;
        v_id      := r.remediation_id;

        -- The attempt is recorded before the state moves, so the fact that the
        -- provider WAS called survives even if this transaction is the last
        -- thing that ever happens to this action.
        INSERT INTO salesops.remediation_attempts
            (remediation_id, attempt_number, outcome, provider,
             error_message, external_side_effect)
        VALUES (v_id, v_attempt, 'unknown', 'unknown',
                format('EXECUTION_UNKNOWN: the executing process did not report an '
                       'outcome within %s minutes (claimed %s minutes ago). Whether '
                       'the provider call completed is not knowable from here.',
                       salesops.operational_setting(
                           'stale_remediation_timeout_minutes')::int, v_mins),
                FALSE)
        ON CONFLICT (remediation_id, attempt_number) DO NOTHING;

        UPDATE salesops.remediation_actions
        SET status        = 'execution_unknown',
            attempt_count = v_attempt,
            executed_by   = NULL,
            last_error    = 'EXECUTION_UNKNOWN: recovered from a stale executing state; '
                            'requires reconciliation before any retry.'
        WHERE salesops.remediation_actions.remediation_id = v_id;

        INSERT INTO salesops.operational_events
            (event_type, entity_type, entity_id, from_state, to_state, actor,
             reason_code, detail)
        VALUES ('stale_remediation_recovered', 'remediation_action',
                v_id::text, 'executing', 'execution_unknown', p_actor,
                'EXECUTION_UNKNOWN',
                jsonb_build_object(
                    'stale_minutes', v_mins,
                    'timeout_minutes',
                        salesops.operational_setting('stale_remediation_timeout_minutes'),
                    'attempt_number', v_attempt,
                    'provider_re_executed', FALSE,
                    'requires_reconciliation', TRUE));

        remediation_id := v_id;
        stale_minutes  := v_mins;
        attempt_number := v_attempt;
        recovered      := TRUE;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION salesops.recover_stale_remediation(TEXT, BOOLEAN) IS
    'Moves remediation actions stranded at ''executing'' into ''execution_unknown''. '
    'NEVER calls a provider and never re-executes: whether the original call landed is '
    'not knowable from here, so the uncertainty is recorded rather than resolved.';


CREATE OR REPLACE FUNCTION salesops.reconcile_remediation(
    p_remediation_id BIGINT,
    p_outcome        TEXT,
    p_actor          TEXT,
    p_evidence       TEXT
)
RETURNS TABLE (remediation_id BIGINT, status TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_current TEXT;
    v_attempts INTEGER;
BEGIN
    IF p_outcome NOT IN ('confirmed_executed', 'confirmed_not_executed') THEN
        RAISE EXCEPTION
            'Reconciliation outcome must be confirmed_executed or '
            'confirmed_not_executed, not %.', p_outcome
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_actor IS NULL OR length(btrim(p_actor)) = 0
       OR p_evidence IS NULL OR length(btrim(p_evidence)) = 0 THEN
        -- Both required. A reconciliation is somebody asserting what happened
        -- outside this database; unattributed, or unexplained, it is a guess
        -- with a timestamp.
        RAISE EXCEPTION
            'Reconciliation requires an actor and a statement of the evidence.'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT a.status, a.attempt_count INTO v_current, v_attempts
    FROM salesops.remediation_actions a WHERE a.remediation_id = p_remediation_id;

    IF v_current IS NULL THEN
        RAISE EXCEPTION 'No remediation action %', p_remediation_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_current <> 'execution_unknown' THEN
        RAISE EXCEPTION
            'Remediation % is %; only an action in ''execution_unknown'' needs '
            'reconciling.', p_remediation_id, v_current
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF p_outcome = 'confirmed_executed' THEN
        UPDATE salesops.remediation_actions
        SET status        = 'executed',
            executed_by   = p_actor,
            attempt_count = GREATEST(v_attempts, 1),
            last_error    = NULL
        WHERE salesops.remediation_actions.remediation_id = p_remediation_id;
    ELSE
        UPDATE salesops.remediation_actions
        SET status     = 'failed',
            last_error = format('RECONCILED_NOT_EXECUTED: %s', left(p_evidence, 400))
        WHERE salesops.remediation_actions.remediation_id = p_remediation_id;
    END IF;

    INSERT INTO salesops.operational_events
        (event_type, entity_type, entity_id, from_state, to_state, actor,
         reason_code, detail)
    VALUES ('remediation_reconciled', 'remediation_action', p_remediation_id::text,
            'execution_unknown',
            CASE WHEN p_outcome = 'confirmed_executed' THEN 'executed' ELSE 'failed' END,
            p_actor,
            CASE WHEN p_outcome = 'confirmed_executed'
                 THEN 'RECONCILED_EXECUTED' ELSE 'RECONCILED_NOT_EXECUTED' END,
            jsonb_build_object('evidence', left(p_evidence, 2000),
                               'attempts_at_reconciliation', v_attempts));

    RETURN QUERY
    SELECT a.remediation_id, a.status FROM salesops.remediation_actions a
    WHERE a.remediation_id = p_remediation_id;
END;
$$;

COMMENT ON FUNCTION salesops.reconcile_remediation(BIGINT, TEXT, TEXT, TEXT) IS
    'A human states what actually happened to an action stranded in execution_unknown. '
    'Requires an actor and a statement of evidence - unattributed or unexplained, a '
    'reconciliation is a guess with a timestamp.';


-- =============================================================================
-- 7. Notification staleness
--
-- Stage 8 already has bounded retry, an explicit abandoned state and full
-- attempt history. What it has no notion of is TIME: a notification sitting at
-- 'failed' with attempts left is retried by the next routing run, and if the
-- routing schedule itself stops, nothing ever notices.
--
-- So this is detection, not a second delivery path. Retrying a stale
-- notification means asking Stage 8 to route again for that date - its own
-- rules, its own idempotency, its own attempt accounting. Nothing here resends
-- anything, and nothing here can touch a notification that is already 'sent'.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.stale_notifications AS
SELECT
    n.notification_id,
    n.calendar_date,
    n.severity,
    n.recipient,
    n.channel,
    n.status,
    n.attempt_count,
    n.created_at,
    n.last_error,
    (SELECT max(a.attempted_at) FROM salesops.notification_attempts a
      WHERE a.notification_id = n.notification_id)          AS last_attempt_at,
    round(extract(epoch FROM (now() - COALESCE(
        (SELECT max(a.attempted_at) FROM salesops.notification_attempts a
          WHERE a.notification_id = n.notification_id),
        n.created_at))) / 60.0, 1)                          AS idle_minutes,
    (n.attempt_count < 3 AND n.status IN ('pending', 'failed')) AS retry_eligible,
    (n.status = 'abandoned' OR n.attempt_count >= 3)            AS terminal
FROM salesops.notifications n
WHERE n.status <> 'sent'
  AND COALESCE(
        (SELECT max(a.attempted_at) FROM salesops.notification_attempts a
          WHERE a.notification_id = n.notification_id),
        n.created_at)
      < now() - make_interval(
          mins => salesops.operational_setting('stale_notification_timeout_minutes')::int);

COMMENT ON VIEW salesops.stale_notifications IS
    'Notifications that are not ''sent'' and have had no attempt for longer than the '
    'configured timeout. Detection only - a delivered notification can never appear '
    'here, and nothing in Stage 10 resends anything itself.';


-- =============================================================================
-- 8. Review ageing
--
-- OPERATIONAL ageing, and nothing else. These labels describe how long a queue
-- item has been waiting; they say nothing about how serious the anomaly is,
-- they are not comparable with Stage 6 severity, and nothing reads them to
-- change a review's state.
--
-- A critical anomaly reviewed within the hour is fine. A minor one unclaimed
-- for a week is not. Those are different sentences about different things, and
-- the labels are deliberately different words so they cannot be confused in a
-- query or in conversation.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.review_ageing AS
WITH thresholds AS (
    SELECT salesops.operational_setting('review_warning_age_hours')           AS warn,
           salesops.operational_setting('review_overdue_age_hours')           AS overdue,
           salesops.operational_setting('review_critical_overdue_age_hours')  AS critical
)
SELECT
    r.review_id,
    r.calendar_date,
    -- Named to be unmistakable: this is Stage 6's word, kept as Stage 6's word.
    r.severity                                        AS anomaly_severity,
    r.status                                          AS review_status,
    r.assigned_to,
    r.created_at,
    r.claimed_at,
    round(extract(epoch FROM (now() - r.created_at)) / 3600.0, 1)  AS age_hours,
    round(extract(epoch FROM (now() - COALESCE(r.claimed_at, r.created_at)))
          / 3600.0, 1)                                AS hours_in_current_state,
    CASE
        WHEN extract(epoch FROM (now() - r.created_at)) / 3600.0 >= t.critical
            THEN 'critical_overdue'
        WHEN extract(epoch FROM (now() - r.created_at)) / 3600.0 >= t.overdue
            THEN 'overdue'
        WHEN extract(epoch FROM (now() - r.created_at)) / 3600.0 >= t.warn
            THEN 'warning'
        ELSE 'fresh'
    END                                               AS ageing_bucket,
    (extract(epoch FROM (now() - r.created_at)) / 3600.0 >= t.overdue) AS escalation_eligible,
    (SELECT e.to_status FROM salesops.review_events e
      WHERE e.review_id = r.review_id
      ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1)  AS last_event_status,
    (SELECT e.occurred_at FROM salesops.review_events e
      WHERE e.review_id = r.review_id
      ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1)  AS last_event_at,
    t.warn     AS warning_after_hours,
    t.overdue  AS overdue_after_hours,
    t.critical AS critical_after_hours
FROM salesops.review_queue r
CROSS JOIN thresholds t
WHERE r.status IN ('pending', 'in_review');

COMMENT ON VIEW salesops.review_ageing IS
    'How long open review items have been waiting. OPERATIONAL ageing only: '
    'fresh | warning | overdue | critical_overdue is not an anomaly severity and is not '
    'comparable with one. Read-only - nothing changes a review''s state on account of '
    'its age, because "nobody has looked at this" is not a decision.';


-- =============================================================================
-- 9. The retry queue
--
-- One place to answer "what failed, and can it be tried again?" across four
-- different kinds of failure that were previously four different queries.
--
-- Every row carries the same columns whatever it describes, because the
-- question an operator asks at 3am is the same question regardless of which
-- subsystem produced the failure.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.operational_retry_queue AS

-- Ingestion runs that failed. Not retryable in place: the ingestion window
-- self-heals on the next scheduled run, so the honest answer is "no action".
SELECT
    'ingestion_run'::text                      AS entity_type,
    r.run_id::text                             AS entity_id,
    r.source                                   AS subsystem,
    left(COALESCE(r.error_message, 'unknown'), 300) AS failure_reason,
    0                                          AS attempt_count,
    NULL::integer                              AS max_attempts,
    r.started_at                               AS first_failure_at,
    COALESCE(r.finished_at, r.started_at)      AS latest_failure_at,
    FALSE                                      AS retry_eligible,
    NULL::timestamptz                          AS next_retry_at,
    TRUE                                       AS terminal,
    'SELF_HEALING_NEXT_RUN'::text              AS disposition
FROM salesops.ingestion_runs r
WHERE r.status = 'failed'

UNION ALL

-- Notifications. Stage 8 owns the retry; this reports its state.
SELECT
    'notification', n.notification_id::text, 'notification-router',
    left(COALESCE(n.last_error, 'not yet attempted'), 300),
    n.attempt_count, 3,
    n.created_at,
    COALESCE((SELECT max(a.attempted_at) FROM salesops.notification_attempts a
               WHERE a.notification_id = n.notification_id), n.created_at),
    (n.status IN ('pending', 'failed') AND n.attempt_count < 3),
    CASE WHEN n.status IN ('pending', 'failed') AND n.attempt_count < 3
         THEN COALESCE((SELECT max(a.attempted_at) FROM salesops.notification_attempts a
                         WHERE a.notification_id = n.notification_id), n.created_at)
              + make_interval(mins => salesops.operational_setting(
                    'retry_backoff_minutes')::int)
    END,
    (n.status = 'abandoned' OR n.attempt_count >= 3),
    CASE WHEN n.status = 'abandoned' THEN 'ABANDONED'
         WHEN n.attempt_count >= 3   THEN 'RETRY_BUDGET_SPENT'
         ELSE 'RETRY_VIA_STAGE8_ROUTING' END
FROM salesops.notifications n
WHERE n.status IN ('pending', 'failed', 'abandoned')

UNION ALL

-- Remediation. execution_unknown is never retry-eligible: it needs a person.
SELECT
    'remediation_action', a.remediation_id::text, 'remediation-executor',
    left(COALESCE(a.last_error, 'unknown'), 300),
    a.attempt_count, 3,
    COALESCE((SELECT min(at.attempted_at) FROM salesops.remediation_attempts at
               WHERE at.remediation_id = a.remediation_id), a.created_at),
    COALESCE((SELECT max(at.attempted_at) FROM salesops.remediation_attempts at
               WHERE at.remediation_id = a.remediation_id), a.created_at),
    (a.status = 'failed' AND a.attempt_count < 3),
    CASE WHEN a.status = 'failed' AND a.attempt_count < 3
         THEN COALESCE((SELECT max(at.attempted_at) FROM salesops.remediation_attempts at
                         WHERE at.remediation_id = a.remediation_id), a.created_at)
              + make_interval(mins => salesops.operational_setting(
                    'retry_backoff_minutes')::int)
    END,
    (a.attempt_count >= 3 AND a.status <> 'execution_unknown'),
    CASE WHEN a.status = 'execution_unknown' THEN 'AWAITING_RECONCILIATION'
         WHEN a.attempt_count >= 3           THEN 'RETRY_BUDGET_SPENT'
         ELSE 'RETRY_VIA_STAGE9_WORKFLOW' END
FROM salesops.remediation_actions a
WHERE a.status IN ('failed', 'execution_unknown')

UNION ALL

-- Failed staging batches, which are the only genuinely replayable failures.
SELECT
    'staging_batch', c.original_batch_id::text, 'mock-sales-api',
    left(COALESCE(c.sample_errors[1], 'unknown'), 300),
    c.max_attempts_used, c.max_attempts,
    c.first_failure_at, c.latest_failure_at,
    c.replay_eligible,
    CASE WHEN c.replay_eligible
         THEN c.latest_failure_at + make_interval(
                  mins => salesops.operational_setting('retry_backoff_minutes')::int)
    END,
    NOT c.replay_eligible,
    CASE WHEN c.replay_eligible THEN 'REPLAYABLE' ELSE 'REPLAY_ATTEMPTS_SPENT' END
FROM salesops.ingestion_replay_candidates c;

COMMENT ON VIEW salesops.operational_retry_queue IS
    'Every failed operational record, in one shape: what failed, why, how many attempts '
    'it has had, when it first and last failed, whether it may be retried, when, and '
    'whether it is terminal. `disposition` is the machine-readable answer to "so what '
    'do I do about it?".';


-- =============================================================================
-- 10. Operational health
--
-- Deterministic, and explained. Every row says WHY it is not healthy through
-- `reason_code`, `observed_value` and `threshold_value`, so a caller can act on
-- the numbers rather than parsing a sentence - and so the status can be
-- recomputed by hand from the same inputs.
--
-- No LLM is involved in any part of this, and no LLM output is read by it. A
-- health signal that a language model could influence would be a health signal
-- nobody could trust during the incident that mattered.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.operational_health AS

-- --- one row per scheduled pipeline -----------------------------------------
WITH pipelines(component, source, max_age_hours) AS (
    VALUES
        ('ingestion',              'mock-sales-api',       6::numeric),
        ('fx_sync',                'frankfurter',          36::numeric),
        ('kpi_refresh',            'kpi-refresh',          36::numeric),
        ('anomaly_detection',      'anomaly-detector',     36::numeric),
        ('anomaly_decision',       'anomaly-decision',     36::numeric),
        ('llm_root_cause',         'llm-root-cause',       36::numeric),
        ('notification_router',    'notification-router',  36::numeric),
        ('remediation_executor',   'remediation-executor', 36::numeric),
        ('operational_maintenance','operational-maintenance', 36::numeric)
),
latest AS (
    SELECT p.component, p.source, p.max_age_hours,
           r.status AS last_status, r.started_at, r.finished_at, r.error_message,
           round(extract(epoch FROM (now() - r.started_at)) / 3600.0, 1) AS age_hours
    FROM pipelines p
    LEFT JOIN LATERAL (
        SELECT * FROM salesops.ingestion_runs ir
        WHERE ir.source = p.source ORDER BY ir.started_at DESC LIMIT 1
    ) r ON TRUE
)
SELECT
    l.component,
    'pipeline'::text                                 AS component_kind,
    CASE
        WHEN l.last_status IS NULL         THEN 'warning'
        WHEN l.last_status = 'failed'      THEN 'failed'
        WHEN l.age_hours > l.max_age_hours THEN 'degraded'
        WHEN l.last_status = 'partial'     THEN 'warning'
        WHEN l.last_status = 'running'
             AND l.age_hours * 60 > salesops.operational_setting(
                 'stale_run_timeout_minutes')                THEN 'degraded'
        ELSE 'healthy'
    END                                              AS status,
    CASE
        WHEN l.last_status IS NULL         THEN 'NEVER_RUN'
        WHEN l.last_status = 'failed'      THEN 'LAST_RUN_FAILED'
        WHEN l.age_hours > l.max_age_hours THEN 'OVERDUE'
        WHEN l.last_status = 'partial'     THEN 'LAST_RUN_PARTIAL'
        WHEN l.last_status = 'running'
             AND l.age_hours * 60 > salesops.operational_setting(
                 'stale_run_timeout_minutes')                THEN 'RUN_STALE'
        ELSE 'OK'
    END                                              AS reason_code,
    l.age_hours                                      AS observed_value,
    l.max_age_hours                                  AS threshold_value,
    'hours_since_last_run'::text                     AS measure,
    l.last_status,
    l.started_at                                     AS last_run_at,
    left(COALESCE(l.error_message, ''), 200)         AS detail
FROM latest l

UNION ALL

-- --- one row per operational condition ---------------------------------------
SELECT * FROM (
    WITH counts AS (
        SELECT
            (SELECT count(*) FROM salesops.ingestion_runs
              WHERE status = 'running'
                AND started_at < now() - make_interval(mins => salesops.operational_setting(
                        'stale_run_timeout_minutes')::int))          AS stale_runs,
            (SELECT count(*) FROM salesops.ingestion_runs
              WHERE status = 'failed'
                AND started_at > now() - INTERVAL '24 hours')        AS failed_runs_24h,
            (SELECT count(*) FROM salesops.notifications
              WHERE status = 'abandoned')                            AS abandoned_notifications,
            (SELECT count(*) FROM salesops.stale_notifications)       AS stale_notifications,
            (SELECT count(*) FROM salesops.review_queue
              WHERE status IN ('pending', 'in_review'))              AS open_reviews,
            (SELECT count(*) FROM salesops.review_ageing
              WHERE ageing_bucket IN ('overdue', 'critical_overdue')) AS overdue_reviews,
            (SELECT count(*) FROM salesops.remediation_actions
              WHERE status = 'execution_unknown')                     AS unknown_executions,
            (SELECT count(*) FROM salesops.remediation_actions
              WHERE status = 'proposed')                              AS unauthorized_actions,
            (SELECT count(*) FROM salesops.ingestion_replay_candidates
              WHERE replay_eligible)                                  AS replay_candidates,
            (SELECT COALESCE(sum(rows), 0) FROM salesops.staging_retention_report
              WHERE disposition = 'eligible')                         AS staging_eligible
    )
    SELECT
        m.component, 'condition'::text,
        CASE WHEN m.value = 0 THEN 'healthy'
             WHEN m.value >= m.degraded_at THEN m.bad_status
             ELSE 'warning' END,
        CASE WHEN m.value = 0 THEN 'OK' ELSE m.reason END,
        m.value::numeric, m.degraded_at::numeric, m.measure,
        NULL::text, NULL::timestamptz, m.detail
    FROM counts c
    CROSS JOIN LATERAL (VALUES
        ('stale_runs',              c.stale_runs,              1::bigint, 'degraded'::text,
         'RUNS_STUCK_RUNNING'::text,      'stuck_run_count'::text,
         'Runs open past the stale timeout. Recovered by the maintenance workflow.'::text),
        ('failed_runs',             c.failed_runs_24h,         2::bigint, 'degraded',
         'RUNS_FAILED_24H',              'failed_run_count',
         'Pipeline runs that failed in the last 24 hours.'),
        ('abandoned_notifications', c.abandoned_notifications, 1::bigint, 'degraded',
         'NOTIFICATIONS_ABANDONED',      'abandoned_count',
         'Notifications that exhausted their retry budget. Nobody was told.'),
        ('stale_notifications',     c.stale_notifications,     1::bigint, 'warning',
         'NOTIFICATIONS_STALE',          'stale_count',
         'Undelivered notifications with no recent attempt.'),
        ('overdue_reviews',         c.overdue_reviews,         1::bigint, 'warning',
         'REVIEWS_OVERDUE',              'overdue_count',
         'Open reviews past the overdue age. Operational ageing, not anomaly severity.'),
        ('open_reviews',            c.open_reviews,            999999::bigint, 'warning',
         'REVIEWS_OPEN',                 'open_count',
         'Reviews awaiting a human. Informational: an open queue is normal.'),
        ('unknown_executions',      c.unknown_executions,      1::bigint, 'degraded',
         'EXECUTION_UNKNOWN',            'unknown_count',
         'Remediation actions whose execution outcome is unknown. Require reconciliation.'),
        ('unauthorized_actions',    c.unauthorized_actions,    999999::bigint, 'warning',
         'ACTIONS_AWAITING_AUTHORIZATION','proposed_count',
         'Proposed actions nobody has authorised. Informational: waiting on a person.'),
        ('replay_candidates',       c.replay_candidates,       1::bigint, 'warning',
         'BATCHES_REPLAYABLE',           'batch_count',
         'Failed staging batches with replay attempts remaining.'),
        ('staging_retention',       c.staging_eligible,        999999::bigint, 'warning',
         'STAGING_ROWS_ELIGIBLE',        'row_count',
         'Settled staging rows past the retention period. Informational until purged.')
    ) AS m(component, value, degraded_at, bad_status, reason, measure, detail)
) conditions;

COMMENT ON VIEW salesops.operational_health IS
    'One row per pipeline and per operational condition, with a deterministic status and '
    'the numbers behind it: reason_code, observed_value, threshold_value, measure. No '
    'LLM output is read anywhere in it - a health signal a language model could '
    'influence would be one nobody could trust during the incident that mattered.';


CREATE OR REPLACE VIEW salesops.operational_health_summary AS
SELECT
    CASE
        WHEN count(*) FILTER (WHERE status = 'failed')   > 0 THEN 'failed'
        WHEN count(*) FILTER (WHERE status = 'degraded') > 0 THEN 'degraded'
        WHEN count(*) FILTER (WHERE status = 'warning')  > 0 THEN 'warning'
        ELSE 'healthy'
    END                                                       AS overall_status,
    count(*)                                                  AS components,
    count(*) FILTER (WHERE status = 'healthy')                AS healthy,
    count(*) FILTER (WHERE status = 'warning')                AS warning,
    count(*) FILTER (WHERE status = 'degraded')               AS degraded,
    count(*) FILTER (WHERE status = 'failed')                 AS failed,
    COALESCE(array_agg(component || ':' || reason_code
             ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,
                      component)
             FILTER (WHERE status <> 'healthy'), '{}')        AS unhealthy
FROM salesops.operational_health;

COMMENT ON VIEW salesops.operational_health_summary IS
    'The worst status across every component, and the list of what is wrong. The overall '
    'status is the worst individual one - a pipeline is not "mostly healthy".';


-- -----------------------------------------------------------------------------
-- ingestion_runs gains two more sources.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild), ''anomaly-detector'' (Stage 5), '
    '''anomaly-decision'' (Stage 6), ''llm-root-cause'' (Stage 7), '
    '''notification-router'' (Stage 8), ''remediation-executor'' (Stage 9), '
    '''ingestion-replay'' and ''operational-maintenance'' (Stage 10). A replay uses its '
    'own source so it can never move the ingestion window. Always filter by source.';

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_recent
    ON salesops.ingestion_runs (source, started_at DESC);


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V012', 'Stage 10 operational reliability: config, audit events, stale-run and '
                'remediation recovery, ingestion replay, staging retention, retry queue, '
                'review ageing, operational health')
ON CONFLICT (version) DO NOTHING;

COMMIT;
