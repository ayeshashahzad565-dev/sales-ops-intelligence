-- =============================================================================
-- V009  Stage 7: LLM root-cause hypotheses
--
--   Stage 5  how unusual it was     anomaly_daily        statistics
--   Stage 6  whether it matters     anomaly_decisions    deterministic SQL
--   Stage 7  why it might have happened                  <- THIS FILE, an LLM
--
-- This is the first table in the warehouse whose contents come from a language
-- model. Everything about its design follows from that one fact.
--
-- What the model may write
-- ------------------------
-- Prose and structured lists: a summary, one primary hypothesis, supporting
-- evidence, alternatives, what is missing, what to check next. Explanation only.
--
-- What the model may NOT write
-- ----------------------------
-- severity, routing, decision. Those three columns exist here as a SNAPSHOT of
-- the Stage 6 verdict this hypothesis was written about - copied out of
-- anomaly_decisions by the service, never supplied by the model, and verified
-- against the live decision by a trigger before any row is accepted.
--
-- The model's response schema has no field for them at all, so an attempt to
-- return one fails validation before it ever reaches SQL. This table is the
-- second line: even a bug in the service cannot land a hypothesis whose
-- recorded severity disagrees with the decision it points at.
--
-- Why the guard is a trigger and not a foreign key
-- -----------------------------------------------
-- A composite FK onto (decision_id, severity, routing, decision) would be
-- tempting and is wrong in both available flavours. Without ON UPDATE CASCADE a
-- later Stage 6 re-run that changed a severity would be BLOCKED by the existence
-- of a hypothesis - Stage 7 output would be able to stop Stage 6, which inverts
-- the whole architecture. With ON UPDATE CASCADE the snapshot would be silently
-- rewritten underneath prose that still describes the old verdict, producing a
-- row that quietly contradicts itself.
--
-- A trigger fires only on writes to THIS table. It cannot block Stage 6, and it
-- cannot rewrite history. If a re-decision later moves a severity, the drift
-- becomes visible through anomaly_hypothesis_audit rather than being hidden by
-- either mechanism.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS salesops.anomaly_hypotheses (
    hypothesis_id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- --- what this explains ---------------------------------------------------
    anomaly_id             BIGINT      NOT NULL,
    decision_id            BIGINT      NOT NULL,
    calendar_date          DATE        NOT NULL,
    decision_version       TEXT        NOT NULL,

    -- --- Stage 6 verdict, snapshotted (see header) ---------------------------
    severity               TEXT        NOT NULL,
    routing                TEXT        NOT NULL,
    decision               TEXT        NOT NULL,

    -- --- the model's output ---------------------------------------------------
    summary                TEXT        NOT NULL,

    -- Confidence in the EXPLANATION, not in the anomaly. Stage 5 established
    -- that the day was unusual and Stage 6 established that it matters; neither
    -- is the model's to revisit. This says how strongly the available evidence
    -- supports the proposed cause.
    confidence             TEXT        NOT NULL,

    primary_hypothesis     TEXT        NOT NULL,

    -- JSONB rather than text: these are lists of structured items and they get
    -- queried ("which metrics are cited most often?"). Shape is enforced by the
    -- service's schema validation before insert; the CHECKs below enforce only
    -- that each is an array and that the ones which must not be empty are not.
    supporting_evidence    JSONB       NOT NULL,
    alternative_hypotheses JSONB       NOT NULL,
    missing_evidence       JSONB       NOT NULL,
    recommended_checks     JSONB       NOT NULL,

    -- --- provenance -----------------------------------------------------------
    model_provider         TEXT        NOT NULL,
    model_name             TEXT        NOT NULL,
    prompt_version         TEXT        NOT NULL,

    -- SHA-256 of the exact evidence package the model was shown. Two rows with
    -- the same digest were reasoning over identical inputs; a different digest
    -- explains a different answer without needing the prompt to be re-run.
    evidence_digest        TEXT        NOT NULL,

    -- Optional: not every provider returns these, and a missing one must not
    -- stop a valid hypothesis being stored.
    request_id             TEXT,
    prompt_tokens          INTEGER,
    completion_tokens      INTEGER,
    latency_ms             INTEGER,
    json_mode              TEXT,

    generated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- --- identity -------------------------------------------------------------
    -- One official analysis per (anomaly, decision version, prompt version,
    -- model). A re-run produces no duplicate; changing the prompt or the model
    -- produces a NEW row beside the old one rather than overwriting it, which is
    -- what makes historical reasoning auditable.
    CONSTRAINT anomaly_hypotheses_unique_generation
        UNIQUE (anomaly_id, decision_version, prompt_version, model_name),

    CONSTRAINT anomaly_hypotheses_anomaly_fk
        FOREIGN KEY (anomaly_id) REFERENCES salesops.anomaly_daily (anomaly_id),

    -- Deleting a decision takes its explanations with it: a hypothesis about a
    -- verdict that no longer exists is not an audit record, it is debris.
    CONSTRAINT anomaly_hypotheses_decision_fk
        FOREIGN KEY (decision_id) REFERENCES salesops.anomaly_decisions (decision_id)
        ON DELETE CASCADE,

    CONSTRAINT anomaly_hypotheses_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    -- Same closed vocabularies as Stage 6. Declared again rather than inherited
    -- so an out-of-range value cannot be stored even if the guard trigger were
    -- ever dropped.
    CONSTRAINT anomaly_hypotheses_severity_valid
        CHECK (severity IN ('none', 'minor', 'major', 'critical')),
    CONSTRAINT anomaly_hypotheses_routing_valid
        CHECK (routing IN ('no_action', 'auto_notify', 'human_review')),
    CONSTRAINT anomaly_hypotheses_decision_valid
        CHECK (decision IN ('no_action', 'action_required')),

    -- Deliberately NOT constrained to decision = 'action_required'. Stage 7 only
    -- analyses actionable decisions and its test suite proves it - but if a
    -- later Stage 6 re-run downgraded a decision, a CHECK here would make the
    -- existing hypothesis block that downgrade. Stage 7 must never be able to
    -- stop Stage 6. Eligibility is a service rule; this table only records.

    CONSTRAINT anomaly_hypotheses_confidence_valid
        CHECK (confidence IN ('low', 'medium', 'high')),

    -- An empty explanation is not an explanation. Guards against a provider
    -- returning well-formed JSON with nothing in it.
    CONSTRAINT anomaly_hypotheses_summary_present
        CHECK (length(btrim(summary)) > 0),
    CONSTRAINT anomaly_hypotheses_primary_present
        CHECK (length(btrim(primary_hypothesis)) > 0),

    CONSTRAINT anomaly_hypotheses_lists_are_arrays
        CHECK (jsonb_typeof(supporting_evidence)    = 'array'
           AND jsonb_typeof(alternative_hypotheses) = 'array'
           AND jsonb_typeof(missing_evidence)       = 'array'
           AND jsonb_typeof(recommended_checks)     = 'array'),

    -- A hypothesis with no supporting evidence is speculation wearing a schema.
    CONSTRAINT anomaly_hypotheses_evidence_present
        CHECK (jsonb_array_length(supporting_evidence) > 0),

    CONSTRAINT anomaly_hypotheses_prompt_version_format
        CHECK (prompt_version ~ '^stage[0-9]+-prompt-v[0-9]+$'),

    CONSTRAINT anomaly_hypotheses_tokens_non_negative
        CHECK ((prompt_tokens     IS NULL OR prompt_tokens     >= 0)
           AND (completion_tokens IS NULL OR completion_tokens >= 0)
           AND (latency_ms        IS NULL OR latency_ms        >= 0))
);

COMMENT ON TABLE salesops.anomaly_hypotheses IS
    'Stage 7 LLM root-cause hypotheses. Explanation only: the severity, routing and '
    'decision columns are a read-only snapshot of the Stage 6 verdict, verified by '
    'trigger. The model cannot write them and has no field for them in its schema.';

COMMENT ON COLUMN salesops.anomaly_hypotheses.confidence IS
    'How strongly the available evidence supports the PROPOSED EXPLANATION. Not '
    'confidence that the anomaly is real - Stages 5 and 6 settled that.';
COMMENT ON COLUMN salesops.anomaly_hypotheses.severity IS
    'Snapshot of anomaly_decisions.severity at generation time. Authoritative copy '
    'lives in Stage 6; this one exists so the hypothesis records the verdict it was '
    'written about. Never supplied by the model.';
COMMENT ON COLUMN salesops.anomaly_hypotheses.evidence_digest IS
    'SHA-256 of the exact evidence package sent to the model. Identical digest means '
    'identical inputs, which is how two differing answers get explained.';
COMMENT ON COLUMN salesops.anomaly_hypotheses.prompt_version IS
    'Bumped whenever the prompt changes materially. A new version creates a new row '
    'rather than overwriting the old reasoning.';
COMMENT ON COLUMN salesops.anomaly_hypotheses.json_mode IS
    'Which structured-output mode the provider actually honoured: schema or object. '
    'Recorded because it affects how much the transport guaranteed before validation.';


-- The operator's query: what has been explained, most recent first.
CREATE INDEX IF NOT EXISTS idx_anomaly_hypotheses_date
    ON salesops.anomaly_hypotheses (calendar_date DESC);

-- Eligibility lookup: "does this anomaly already have an analysis?"
CREATE INDEX IF NOT EXISTS idx_anomaly_hypotheses_decision
    ON salesops.anomaly_hypotheses (decision_id);


-- =============================================================================
-- The snapshot guard
--
-- Refuses any hypothesis whose recorded Stage 6 verdict does not match the
-- decision it references. This is what makes "the LLM cannot change severity" a
-- property of the database rather than a promise made by the caller.
--
-- It fires only on writes to anomaly_hypotheses, so it can never block, delay or
-- alter a Stage 6 re-decision.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_hypothesis_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    d RECORD;
BEGIN
    SELECT severity, routing, decision, calendar_date, decision_version, anomaly_id
    INTO d
    FROM salesops.anomaly_decisions
    WHERE decision_id = NEW.decision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No Stage 6 decision % exists', NEW.decision_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF (NEW.severity, NEW.routing, NEW.decision)
       IS DISTINCT FROM (d.severity, d.routing, d.decision) THEN
        RAISE EXCEPTION
            'Stage 7 may not restate the Stage 6 verdict. Decision % is (%, %, %); '
            'the hypothesis claims (%, %, %).',
            NEW.decision_id, d.severity, d.routing, d.decision,
            NEW.severity, NEW.routing, NEW.decision
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- The hypothesis must also be attached to the same anomaly, date and
    -- decision version as the decision it explains. Without this a correct
    -- verdict could still be filed against the wrong day.
    IF (NEW.anomaly_id, NEW.calendar_date, NEW.decision_version)
       IS DISTINCT FROM (d.anomaly_id, d.calendar_date, d.decision_version) THEN
        RAISE EXCEPTION
            'Hypothesis context does not match decision %: expected anomaly %, date %, '
            'version %; got anomaly %, date %, version %.',
            NEW.decision_id, d.anomaly_id, d.calendar_date, d.decision_version,
            NEW.anomaly_id, NEW.calendar_date, NEW.decision_version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_hypothesis_snapshot() IS
    'Rejects any Stage 7 hypothesis whose recorded severity/routing/decision, anomaly, '
    'date or decision version disagrees with the Stage 6 decision it references.';

DROP TRIGGER IF EXISTS trg_guard_hypothesis_snapshot ON salesops.anomaly_hypotheses;
CREATE TRIGGER trg_guard_hypothesis_snapshot
    BEFORE INSERT OR UPDATE ON salesops.anomaly_hypotheses
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_hypothesis_snapshot();


-- =============================================================================
-- Audit view
--
-- One row per hypothesis, joined to the decision it explains, with the drift
-- flag the trigger deliberately does not prevent: if Stage 6 re-decided after
-- this analysis was written, `decision_current` says so instead of the snapshot
-- being quietly rewritten.
-- =============================================================================
CREATE OR REPLACE VIEW salesops.anomaly_hypothesis_audit AS
SELECT
    h.hypothesis_id,
    h.calendar_date,
    dd.day_name,

    -- Stage 6, authoritative
    d.severity      AS decision_severity,
    d.routing       AS decision_routing,
    d.decision      AS decision_decision,
    d.decision_version,
    d.decision_reason_code,

    -- Stage 6 as it stood when this was written
    h.severity      AS analysed_severity,
    (h.severity, h.routing, h.decision)
        IS NOT DISTINCT FROM (d.severity, d.routing, d.decision) AS decision_current,

    -- Stage 5 / Stage 4, for context alongside the reasoning
    d.anomaly_score,
    d.business_impact_tier,
    d.expected_net_revenue_usd,
    d.actual_net_revenue_usd,
    d.revenue_delta_usd,

    -- Stage 7
    h.summary,
    h.confidence,
    h.primary_hypothesis,
    jsonb_array_length(h.supporting_evidence)    AS supporting_evidence_count,
    jsonb_array_length(h.alternative_hypotheses) AS alternative_count,
    jsonb_array_length(h.missing_evidence)       AS missing_evidence_count,
    jsonb_array_length(h.recommended_checks)     AS recommended_check_count,
    h.supporting_evidence,
    h.alternative_hypotheses,
    h.missing_evidence,
    h.recommended_checks,

    h.model_provider,
    h.model_name,
    h.prompt_version,
    h.evidence_digest,
    h.json_mode,
    h.prompt_tokens,
    h.completion_tokens,
    h.latency_ms,
    h.generated_at
FROM salesops.anomaly_hypotheses h
JOIN salesops.anomaly_decisions  d  ON d.decision_id   = h.decision_id
JOIN salesops.dim_date           dd ON dd.calendar_date = h.calendar_date;

COMMENT ON VIEW salesops.anomaly_hypothesis_audit IS
    'Hypotheses beside the Stage 6 decisions they explain. decision_current is FALSE '
    'where Stage 6 has re-decided since the analysis was generated.';


-- -----------------------------------------------------------------------------
-- ingestion_runs gains a sixth pipeline. Same shared ledger, told apart by
-- `source`. Queries deriving a window from max(window_to) must still scope to
-- their own source - see the V005 note.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild), ''anomaly-detector'' (Stage 5), '
    '''anomaly-decision'' (Stage 6), ''llm-root-cause'' (Stage 7). '
    'Always filter by source when reading windows.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V009', 'Stage 7 LLM root-cause hypotheses: anomaly_hypotheses, snapshot guard, audit view')
ON CONFLICT (version) DO NOTHING;

COMMIT;
