-- =============================================================================
-- V010  Stage 8: notification delivery and the human-review queue
--
--   Stage 5  how unusual it was        anomaly_daily
--   Stage 6  whether it matters        anomaly_decisions      <- DECIDES
--   Stage 7  why it might have happened anomaly_hypotheses    <- EXPLAINS
--   Stage 8  who needs to know          THIS FILE             <- DELIVERS
--
-- Stage 6 decides. Stage 7 explains. Stage 8 delivers and queues review.
-- Stage 8 cannot change Stage 6. Stage 8 executes no business action.
--
-- Where the eligibility rules actually live
-- ----------------------------------------
-- Nowhere in this file is severity computed, and nothing here re-derives who
-- should be told. Every notification and every review item carries a SNAPSHOT of
-- the Stage 6 verdict that routed it, and CHECK constraints require that
-- snapshot to be an eligible one:
--
--   a notification can only exist for  notification_allowed AND auto_notify
--   a review item can only exist for   human_review_required AND human_review
--
-- Those CHECKs are safe here in a way they would not have been in V009. The
-- snapshot columns are Stage 8's own immutable copy - no later process updates
-- them - so a constraint on them can never block a Stage 6 re-decision. And a
-- guard trigger verifies the snapshot against the live decision at insert time,
-- so a caller cannot fabricate an eligible-looking snapshot for an anomaly Stage
-- 6 routed to no_action.
--
-- Between them: sending a notification for a no_action anomaly is not a bug that
-- code review has to catch. It is a write the database refuses.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Notifications
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.notifications (
    notification_id        BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- --- what this is about ---------------------------------------------------
    anomaly_id             BIGINT      NOT NULL,
    decision_id            BIGINT      NOT NULL,
    -- Nullable on purpose: Stage 7 may have failed, and a Stage 6 decision that
    -- permits notification must still be delivered without an explanation.
    hypothesis_id          BIGINT,
    calendar_date          DATE        NOT NULL,

    -- --- Stage 6 verdict, snapshotted (see header) ---------------------------
    decision_version       TEXT        NOT NULL,
    severity               TEXT        NOT NULL,
    routing                TEXT        NOT NULL,
    decision               TEXT        NOT NULL,
    notification_allowed   BOOLEAN     NOT NULL,
    human_review_required  BOOLEAN     NOT NULL,

    -- --- delivery -------------------------------------------------------------
    channel                TEXT        NOT NULL,
    recipient              TEXT        NOT NULL,
    subject                TEXT        NOT NULL,

    -- The rendered notification. Contains business evidence only - never a
    -- credential, a header, or a provider URL. The schema validation suite scans
    -- for secret-shaped column names; the Stage 8 test suite scans the payloads
    -- themselves.
    payload                JSONB       NOT NULL,

    -- Whether Stage 7 had anything to say. 'unavailable' is a first-class state,
    -- not an error: the notification goes out carrying deterministic evidence and
    -- says plainly that no AI analysis was available.
    hypothesis_status      TEXT        NOT NULL DEFAULT 'unavailable',

    status                 TEXT        NOT NULL DEFAULT 'pending',
    attempt_count          INTEGER     NOT NULL DEFAULT 0,

    provider               TEXT,
    provider_message_id    TEXT,
    last_error             TEXT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at                TIMESTAMPTZ,

    -- Idempotency. One notification per anomaly, per decision version, per
    -- channel, per recipient - so a rerun cannot deliver the same finding twice
    -- to the same person.
    CONSTRAINT notifications_unique_delivery
        UNIQUE (anomaly_id, decision_version, channel, recipient),

    CONSTRAINT notifications_anomaly_fk
        FOREIGN KEY (anomaly_id) REFERENCES salesops.anomaly_daily (anomaly_id),
    CONSTRAINT notifications_decision_fk
        FOREIGN KEY (decision_id) REFERENCES salesops.anomaly_decisions (decision_id)
        ON DELETE CASCADE,
    -- SET NULL, not CASCADE: deleting a regenerated hypothesis must not delete
    -- the record that something was delivered to a person.
    CONSTRAINT notifications_hypothesis_fk
        FOREIGN KEY (hypothesis_id) REFERENCES salesops.anomaly_hypotheses (hypothesis_id)
        ON DELETE SET NULL,
    CONSTRAINT notifications_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    CONSTRAINT notifications_status_valid
        CHECK (status IN ('pending', 'sent', 'failed', 'abandoned')),
    CONSTRAINT notifications_hypothesis_status_valid
        CHECK (hypothesis_status IN ('available', 'unavailable')),
    CONSTRAINT notifications_channel_valid
        CHECK (channel IN ('webhook', 'email')),
    CONSTRAINT notifications_severity_valid
        CHECK (severity IN ('none', 'minor', 'major', 'critical')),
    CONSTRAINT notifications_routing_valid
        CHECK (routing IN ('no_action', 'auto_notify', 'human_review')),
    CONSTRAINT notifications_decision_valid
        CHECK (decision IN ('no_action', 'action_required')),

    -- ---- eligibility, enforced rather than trusted --------------------------
    -- Stage 6 permits automated notification for exactly one routing. A row that
    -- claims otherwise is refused.
    CONSTRAINT notifications_only_for_eligible_decisions
        CHECK (notification_allowed
               AND routing  = 'auto_notify'
               AND decision = 'action_required'),

    CONSTRAINT notifications_attempts_non_negative
        CHECK (attempt_count >= 0),
    -- A delivery that succeeded took at least one attempt and has a time.
    CONSTRAINT notifications_sent_is_consistent
        CHECK (status <> 'sent' OR (sent_at IS NOT NULL AND attempt_count > 0)),
    -- ...and one that has not been delivered must not claim a delivery time.
    CONSTRAINT notifications_unsent_has_no_sent_at
        CHECK (status = 'sent' OR sent_at IS NULL),
    CONSTRAINT notifications_failure_has_reason
        CHECK (status NOT IN ('failed', 'abandoned') OR last_error IS NOT NULL),
    -- A hypothesis id and 'unavailable' cannot both be true.
    CONSTRAINT notifications_hypothesis_status_agrees
        CHECK ((hypothesis_status = 'available') = (hypothesis_id IS NOT NULL)),

    CONSTRAINT notifications_recipient_present
        CHECK (length(btrim(recipient)) > 0),
    CONSTRAINT notifications_subject_present
        CHECK (length(btrim(subject)) > 0)
);

COMMENT ON TABLE salesops.notifications IS
    'Stage 8 automated notifications. One row per (anomaly, decision version, channel, '
    'recipient). Exists only for decisions Stage 6 marked notification_allowed - '
    'enforced by CHECK, not by convention.';
COMMENT ON COLUMN salesops.notifications.status IS
    'pending = created, not yet attempted | sent = delivered | '
    'failed = retryable, a later run will try again | '
    'abandoned = permanent failure, or the retry budget is exhausted';
COMMENT ON COLUMN salesops.notifications.hypothesis_status IS
    'available = a Stage 7 analysis was attached | unavailable = Stage 7 produced '
    'nothing for this anomaly. Delivery proceeds either way, saying so plainly.';
COMMENT ON COLUMN salesops.notifications.payload IS
    'The rendered notification. Business evidence only - never a credential, an '
    'authorization header, or a provider URL.';

CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON salesops.notifications (status, created_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_notifications_decision
    ON salesops.notifications (decision_id);


-- =============================================================================
-- 2. Delivery attempts
--
-- Every attempt, not just the last one. `notifications.attempt_count` says how
-- many; this says what happened in each, which is the difference between "it
-- failed" and "it timed out twice then the provider rejected the recipient".
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.notification_attempts (
    attempt_id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id     BIGINT      NOT NULL,
    attempt_number      INTEGER     NOT NULL,

    outcome             TEXT        NOT NULL,
    provider            TEXT        NOT NULL,
    provider_message_id TEXT,
    status_code         INTEGER,
    error_message       TEXT,
    latency_ms          INTEGER,

    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT notification_attempts_unique
        UNIQUE (notification_id, attempt_number),
    CONSTRAINT notification_attempts_notification_fk
        FOREIGN KEY (notification_id) REFERENCES salesops.notifications (notification_id)
        ON DELETE CASCADE,

    -- The distinction that drives retry policy. A timeout is worth trying again;
    -- an invalid recipient never will be, and retrying it three times only
    -- delays the moment someone notices.
    CONSTRAINT notification_attempts_outcome_valid
        CHECK (outcome IN ('success', 'retryable_failure', 'permanent_failure')),
    CONSTRAINT notification_attempts_number_positive
        CHECK (attempt_number > 0),
    CONSTRAINT notification_attempts_failure_has_reason
        CHECK (outcome = 'success' OR error_message IS NOT NULL),
    CONSTRAINT notification_attempts_latency_non_negative
        CHECK (latency_ms IS NULL OR latency_ms >= 0)
);

COMMENT ON TABLE salesops.notification_attempts IS
    'One row per delivery attempt. outcome distinguishes retryable from permanent '
    'failure, which is what bounds the retry policy.';

CREATE INDEX IF NOT EXISTS idx_notification_attempts_notification
    ON salesops.notification_attempts (notification_id, attempt_number);


-- =============================================================================
-- 3. The human-review queue
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.review_queue (
    review_id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    anomaly_id             BIGINT      NOT NULL,
    decision_id            BIGINT      NOT NULL,
    hypothesis_id          BIGINT,
    calendar_date          DATE        NOT NULL,

    -- --- Stage 6 verdict, snapshotted ----------------------------------------
    decision_version       TEXT        NOT NULL,
    severity               TEXT        NOT NULL,
    routing                TEXT        NOT NULL,
    decision               TEXT        NOT NULL,
    notification_allowed   BOOLEAN     NOT NULL,
    human_review_required  BOOLEAN     NOT NULL,

    hypothesis_status      TEXT        NOT NULL DEFAULT 'unavailable',

    -- --- review state ---------------------------------------------------------
    status                 TEXT        NOT NULL DEFAULT 'pending',
    assigned_to            TEXT,

    -- Free text written by a person. Treated as untrusted throughout: length
    -- bounded here, never rendered into a notification payload, never
    -- concatenated into anything executed or sent to a model.
    review_notes           TEXT,
    resolution             TEXT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at             TIMESTAMPTZ,
    reviewed_at            TIMESTAMPTZ,

    -- Idempotency: one queue item per anomaly per decision version.
    CONSTRAINT review_queue_unique_item
        UNIQUE (anomaly_id, decision_version),

    CONSTRAINT review_queue_anomaly_fk
        FOREIGN KEY (anomaly_id) REFERENCES salesops.anomaly_daily (anomaly_id),
    CONSTRAINT review_queue_decision_fk
        FOREIGN KEY (decision_id) REFERENCES salesops.anomaly_decisions (decision_id)
        ON DELETE CASCADE,
    CONSTRAINT review_queue_hypothesis_fk
        FOREIGN KEY (hypothesis_id) REFERENCES salesops.anomaly_hypotheses (hypothesis_id)
        ON DELETE SET NULL,
    CONSTRAINT review_queue_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    CONSTRAINT review_queue_status_valid
        CHECK (status IN ('pending', 'in_review', 'resolved', 'dismissed')),
    CONSTRAINT review_queue_hypothesis_status_valid
        CHECK (hypothesis_status IN ('available', 'unavailable')),
    CONSTRAINT review_queue_severity_valid
        CHECK (severity IN ('none', 'minor', 'major', 'critical')),
    CONSTRAINT review_queue_routing_valid
        CHECK (routing IN ('no_action', 'auto_notify', 'human_review')),
    CONSTRAINT review_queue_decision_valid
        CHECK (decision IN ('no_action', 'action_required')),

    -- ---- eligibility, enforced rather than trusted --------------------------
    -- A queue item exists because Stage 6 required a human, never because an
    -- LLM sounded concerned.
    CONSTRAINT review_queue_only_for_eligible_decisions
        CHECK (human_review_required
               AND routing  = 'human_review'
               AND decision = 'action_required'),

    -- ---- state machine invariants -------------------------------------------
    CONSTRAINT review_queue_resolution_valid
        CHECK (resolution IS NULL OR resolution IN (
            'confirmed', 'false_positive', 'expected_business_variation',
            'requires_follow_up')),
    -- A terminal state must say why; a non-terminal one must not pretend to.
    CONSTRAINT review_queue_terminal_has_resolution
        CHECK ((status IN ('resolved', 'dismissed')) = (resolution IS NOT NULL)),
    CONSTRAINT review_queue_terminal_has_timestamp
        CHECK ((status IN ('resolved', 'dismissed')) = (reviewed_at IS NOT NULL)),
    CONSTRAINT review_queue_claimed_has_timestamp
        CHECK (status = 'pending' OR claimed_at IS NOT NULL OR status = 'dismissed'),
    CONSTRAINT review_queue_pending_is_unclaimed
        CHECK (status <> 'pending' OR (claimed_at IS NULL AND assigned_to IS NULL)),

    -- Untrusted input, bounded. Not a security control on its own - it stops a
    -- review note becoming an unbounded write.
    CONSTRAINT review_queue_notes_bounded
        CHECK (review_notes IS NULL OR length(review_notes) <= 4000),
    CONSTRAINT review_queue_assignee_bounded
        CHECK (assigned_to IS NULL OR length(assigned_to) BETWEEN 1 AND 200),

    CONSTRAINT review_queue_timestamps_ordered
        CHECK (claimed_at IS NULL OR claimed_at >= created_at)
);

COMMENT ON TABLE salesops.review_queue IS
    'Stage 8 human-review queue. One row per (anomaly, decision version). Exists only '
    'for decisions Stage 6 marked human_review_required - enforced by CHECK. A reviewer '
    'records status, resolution and notes; the Stage 6 verdict is not theirs to change.';
COMMENT ON COLUMN salesops.review_queue.status IS
    'pending -> in_review -> resolved | dismissed. pending -> dismissed is allowed '
    '(triaged without claiming); in_review -> pending releases a claim. resolved and '
    'dismissed are terminal. Enforced by trigger.';
COMMENT ON COLUMN salesops.review_queue.resolution IS
    'What the reviewer concluded. NOT a new severity, and it triggers no action: '
    'Stage 8 ends at the recorded resolution.';
COMMENT ON COLUMN salesops.review_queue.review_notes IS
    'Untrusted human input. Never rendered into a notification payload and never '
    'concatenated into anything executed or sent to a model.';

CREATE INDEX IF NOT EXISTS idx_review_queue_open
    ON salesops.review_queue (severity, created_at)
    WHERE status IN ('pending', 'in_review');
CREATE INDEX IF NOT EXISTS idx_review_queue_decision
    ON salesops.review_queue (decision_id);


-- =============================================================================
-- 4. Review history
--
-- Who moved it, when, from what to what. The queue row holds current state; this
-- holds how it got there, which is the part an audit actually asks about.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.review_events (
    event_id     BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id    BIGINT      NOT NULL,

    from_status  TEXT,
    to_status    TEXT        NOT NULL,
    actor        TEXT,
    resolution   TEXT,
    note_excerpt TEXT,

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT review_events_review_fk
        FOREIGN KEY (review_id) REFERENCES salesops.review_queue (review_id)
        ON DELETE CASCADE,
    CONSTRAINT review_events_to_status_valid
        CHECK (to_status IN ('pending', 'in_review', 'resolved', 'dismissed')),
    CONSTRAINT review_events_from_status_valid
        CHECK (from_status IS NULL
               OR from_status IN ('pending', 'in_review', 'resolved', 'dismissed')),
    CONSTRAINT review_events_excerpt_bounded
        CHECK (note_excerpt IS NULL OR length(note_excerpt) <= 500)
);

COMMENT ON TABLE salesops.review_events IS
    'Every review state transition, with actor and timestamp. Append-only in practice.';

CREATE INDEX IF NOT EXISTS idx_review_events_review
    ON salesops.review_events (review_id, occurred_at);


-- =============================================================================
-- 5. Snapshot guards
--
-- The same mechanism V009 uses, for the same reason: the snapshot must match the
-- Stage 6 decision it claims to describe. A trigger fires only on writes to the
-- Stage 8 table, so it can never block or alter a Stage 6 re-decision.
--
-- With the eligibility CHECKs above, these two together mean a notification can
-- only exist for an anomaly Stage 6 actually routed to auto_notify, and a review
-- item only for one it routed to human_review. Neither is a rule someone has to
-- remember.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_stage8_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    d RECORD;
BEGIN
    SELECT severity, routing, decision, notification_allowed, human_review_required,
           calendar_date, decision_version, anomaly_id
    INTO d
    FROM salesops.anomaly_decisions
    WHERE decision_id = NEW.decision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No Stage 6 decision % exists', NEW.decision_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF (NEW.severity, NEW.routing, NEW.decision,
        NEW.notification_allowed, NEW.human_review_required)
       IS DISTINCT FROM
       (d.severity, d.routing, d.decision,
        d.notification_allowed, d.human_review_required) THEN
        RAISE EXCEPTION
            'Stage 8 may not restate the Stage 6 verdict. Decision % is '
            '(%, %, %, notify=%, review=%); the % record claims (%, %, %, notify=%, review=%).',
            NEW.decision_id, d.severity, d.routing, d.decision,
            d.notification_allowed, d.human_review_required,
            TG_TABLE_NAME, NEW.severity, NEW.routing, NEW.decision,
            NEW.notification_allowed, NEW.human_review_required
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.anomaly_id, NEW.calendar_date, NEW.decision_version)
       IS DISTINCT FROM (d.anomaly_id, d.calendar_date, d.decision_version) THEN
        RAISE EXCEPTION
            'Stage 8 record context does not match decision %: expected anomaly %, '
            'date %, version %; got anomaly %, date %, version %.',
            NEW.decision_id, d.anomaly_id, d.calendar_date, d.decision_version,
            NEW.anomaly_id, NEW.calendar_date, NEW.decision_version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_stage8_snapshot() IS
    'Rejects any notification or review record whose Stage 6 snapshot disagrees with '
    'the decision it references. Fires only on Stage 8 writes; cannot block Stage 6.';

DROP TRIGGER IF EXISTS trg_guard_notification_snapshot ON salesops.notifications;
CREATE TRIGGER trg_guard_notification_snapshot
    BEFORE INSERT OR UPDATE ON salesops.notifications
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_stage8_snapshot();

DROP TRIGGER IF EXISTS trg_guard_review_snapshot ON salesops.review_queue;
CREATE TRIGGER trg_guard_review_snapshot
    BEFORE INSERT OR UPDATE ON salesops.review_queue
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_stage8_snapshot();


-- =============================================================================
-- 6. The review state machine
--
-- Declared as data, enforced in one place. A reviewer moves an item along a
-- defined path or the write is refused - there is no "set status to whatever".
--
--   pending    -> in_review    a reviewer claims it
--   pending    -> dismissed    triaged away without claiming
--   in_review  -> resolved     reviewed and closed out
--   in_review  -> dismissed    reviewed and judged not worth pursuing
--   in_review  -> pending      claim released, back to the queue
--   resolved   -> (terminal)
--   dismissed  -> (terminal)
--
-- Releasing a claim is included deliberately: without it, an item claimed by
-- someone who then goes on holiday is stuck in in_review forever, and the only
-- remedy is a manual UPDATE that bypasses every rule in this file.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_review_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    allowed BOOLEAN;
BEGIN
    IF NEW.status = OLD.status THEN
        -- Notes or assignee edited without a transition. Terminal items are
        -- closed: reopening the record for editing would let a resolution be
        -- rewritten after the fact, which is the one thing an audit trail exists
        -- to prevent.
        IF OLD.status IN ('resolved', 'dismissed')
           AND (NEW.resolution   IS DISTINCT FROM OLD.resolution
             OR NEW.review_notes IS DISTINCT FROM OLD.review_notes) THEN
            RAISE EXCEPTION
                'Review % is %; its resolution and notes are final.',
                OLD.review_id, OLD.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    allowed := (OLD.status, NEW.status) IN (
        ('pending',   'in_review'),
        ('pending',   'dismissed'),
        ('in_review', 'resolved'),
        ('in_review', 'dismissed'),
        ('in_review', 'pending')
    );

    IF NOT allowed THEN
        RAISE EXCEPTION
            'Invalid review transition % -> % for review %.',
            OLD.status, NEW.status, OLD.review_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Timestamps follow the transition rather than being set by the caller, so
    -- they cannot disagree with the state they describe.
    IF NEW.status = 'in_review' THEN
        NEW.claimed_at := COALESCE(NEW.claimed_at, now());
    ELSIF NEW.status = 'pending' THEN
        NEW.claimed_at  := NULL;
        NEW.assigned_to := NULL;
    ELSIF NEW.status IN ('resolved', 'dismissed') THEN
        NEW.reviewed_at := COALESCE(NEW.reviewed_at, now());
    END IF;

    INSERT INTO salesops.review_events
        (review_id, from_status, to_status, actor, resolution, note_excerpt)
    VALUES (
        OLD.review_id, OLD.status, NEW.status,
        COALESCE(NEW.assigned_to, OLD.assigned_to),
        NEW.resolution,
        left(NEW.review_notes, 500)
    );

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_review_transition() IS
    'Enforces the review state machine, maintains its timestamps, and appends to '
    'review_events. Terminal resolutions and notes cannot be edited afterwards.';

DROP TRIGGER IF EXISTS trg_guard_review_transition ON salesops.review_queue;
CREATE TRIGGER trg_guard_review_transition
    BEFORE UPDATE ON salesops.review_queue
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_review_transition();

-- The opening event, so history starts where the item did.
CREATE OR REPLACE FUNCTION salesops.record_review_created()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO salesops.review_events (review_id, from_status, to_status, actor)
    VALUES (NEW.review_id, NULL, NEW.status, NULL);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_record_review_created ON salesops.review_queue;
CREATE TRIGGER trg_record_review_created
    AFTER INSERT ON salesops.review_queue
    FOR EACH ROW EXECUTE FUNCTION salesops.record_review_created();


-- =============================================================================
-- 7. Audit views
-- =============================================================================
CREATE OR REPLACE VIEW salesops.notification_audit AS
SELECT
    n.notification_id,
    n.calendar_date,
    dd.day_name,

    -- Stage 6, authoritative, beside the snapshot that routed this delivery
    d.severity   AS decision_severity,
    d.routing    AS decision_routing,
    d.decision   AS decision_decision,
    n.severity   AS notified_severity,
    (n.severity, n.routing, n.decision, n.notification_allowed, n.human_review_required)
        IS NOT DISTINCT FROM
        (d.severity, d.routing, d.decision, d.notification_allowed, d.human_review_required)
        AS decision_current,
    n.decision_version,
    d.decision_reason_code,
    d.anomaly_score,
    d.revenue_delta_usd,

    -- Stage 7
    n.hypothesis_status,
    h.confidence      AS hypothesis_confidence,
    h.primary_hypothesis,
    h.model_name,

    -- Stage 8
    n.channel,
    n.recipient,
    n.subject,
    n.status,
    n.attempt_count,
    n.provider,
    n.provider_message_id,
    n.last_error,
    n.created_at,
    n.sent_at,
    (SELECT count(*) FROM salesops.notification_attempts a
      WHERE a.notification_id = n.notification_id)                    AS attempts_recorded,
    (SELECT max(a.attempted_at) FROM salesops.notification_attempts a
      WHERE a.notification_id = n.notification_id)                    AS last_attempt_at
FROM salesops.notifications      n
JOIN salesops.anomaly_decisions  d  ON d.decision_id   = n.decision_id
JOIN salesops.dim_date           dd ON dd.calendar_date = n.calendar_date
LEFT JOIN salesops.anomaly_hypotheses h ON h.hypothesis_id = n.hypothesis_id;

COMMENT ON VIEW salesops.notification_audit IS
    'What was delivered, to whom, through what, when, after how many attempts - beside '
    'the Stage 6 decision and Stage 7 hypothesis it carried. No secrets: the payload, '
    'provider URL and credentials are all absent.';


CREATE OR REPLACE VIEW salesops.review_queue_audit AS
SELECT
    r.review_id,
    r.calendar_date,
    dd.day_name,

    d.severity  AS decision_severity,
    d.routing   AS decision_routing,
    d.decision  AS decision_decision,
    r.severity  AS queued_severity,
    (r.severity, r.routing, r.decision, r.notification_allowed, r.human_review_required)
        IS NOT DISTINCT FROM
        (d.severity, d.routing, d.decision, d.notification_allowed, d.human_review_required)
        AS decision_current,
    r.decision_version,
    d.decision_reason_code,
    d.anomaly_score,
    d.business_impact_tier,
    d.expected_net_revenue_usd,
    d.actual_net_revenue_usd,
    d.revenue_delta_usd,

    r.hypothesis_status,
    h.summary            AS hypothesis_summary,
    h.confidence         AS hypothesis_confidence,
    h.primary_hypothesis,
    h.supporting_evidence,
    h.missing_evidence,
    h.recommended_checks,
    h.model_name,

    r.status,
    r.assigned_to,
    r.resolution,
    r.review_notes,
    r.created_at,
    r.claimed_at,
    r.reviewed_at,
    (SELECT count(*) FROM salesops.review_events e
      WHERE e.review_id = r.review_id)                                AS transition_count,
    CASE WHEN r.reviewed_at IS NOT NULL
         THEN round(extract(epoch FROM (r.reviewed_at - r.created_at))::numeric, 1)
    END                                                               AS seconds_to_resolution
FROM salesops.review_queue      r
JOIN salesops.anomaly_decisions d  ON d.decision_id   = r.decision_id
JOIN salesops.dim_date          dd ON dd.calendar_date = r.calendar_date
LEFT JOIN salesops.anomaly_hypotheses h ON h.hypothesis_id = r.hypothesis_id;

COMMENT ON VIEW salesops.review_queue_audit IS
    'What happened, why it was escalated, what Stage 6 decided, what Stage 7 '
    'hypothesised and what it could not show, who reviewed it, and what they concluded.';


-- -----------------------------------------------------------------------------
-- ingestion_runs gains a seventh pipeline.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild), ''anomaly-detector'' (Stage 5), '
    '''anomaly-decision'' (Stage 6), ''llm-root-cause'' (Stage 7), '
    '''notification-router'' (Stage 8). Always filter by source when reading windows.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V010', 'Stage 8 delivery layer: notifications, attempts, review queue, review events, audit views')
ON CONFLICT (version) DO NOTHING;

COMMIT;
