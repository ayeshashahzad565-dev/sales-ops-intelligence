-- =============================================================================
-- V011  Stage 9: human-approved remediation
--
--   Stage 5  how unusual it was         anomaly_daily
--   Stage 6  whether it matters         anomaly_decisions      <- DECIDES
--   Stage 7  why it might have happened anomaly_hypotheses     <- EXPLAINS
--   Stage 8  who needs to know          notifications/reviews  <- DELIVERS
--   Stage 9  what a human authorised     THIS FILE             <- EXECUTES
--
-- The governing sentence, now true of the schema and not only the README:
--
--     The LLM proposes. Deterministic rules decide. A human approves.
--     Only then may remediation execute.
--
-- Three separate gates, in three separate places
-- ----------------------------------------------
-- 1. AUTHORISATION  A remediation action can only be created from a review that
--    a human moved to 'approved'. Not 'resolved' - that is the outcome for
--    "reviewed, nothing to do". Not 'dismissed' - dismissal is the opposite of
--    approval. Enforced by guard_remediation_authorization() at INSERT.
--
-- 2. ELIGIBILITY    Which action may be taken for which severity is reference
--    data, and the link is a FOREIGN KEY. An ineligible pair is not a rule
--    somebody has to remember; it is a write the database refuses.
--
-- 3. EXECUTION      Authorisation is not execution. An approved action sits at
--    rest until something explicitly executes it, and the claim into
--    'executing' is a conditional UPDATE, so the provider is called once per
--    logical action however many callers race for it.
--
-- What is deliberately absent
-- ---------------------------
-- No foreign key to anomaly_daily, anomaly_decisions, anomaly_hypotheses or
-- review_queue. The V009 header sets out why in full; the short version is that
-- a FK without CASCADE can block a Stage 6 re-decision or a Stage 4 KPI
-- rebuild, and a FK with CASCADE silently erases history that a human
-- authorised. Instead every reference is a plain BIGINT validated by a trigger
-- at INSERT, and the audit view reports whether the authorisation still matches
-- the live decision.
--
-- Nothing here computes severity, re-reads a hypothesis, or asks a model
-- anything. There is no column in this file an LLM is allowed to write.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Stage 8 compatibility: the approval state
--
-- Stage 8 shipped with 'resolved' meaning "a human looked at it and closed it
-- out". That single state cannot distinguish "confirmed, and something should
-- be done" from "confirmed, and nothing should be done" - and treating either
-- reading as authorisation would be guessing at what a person meant.
--
-- So one new terminal state, and 'resolved' keeps its existing meaning exactly:
--
--     in_review -> approved    confirmed, and remediation is authorised
--     in_review -> resolved    reviewed and closed WITHOUT remediation
--
-- This is the smallest change that makes approval explicit. Every Stage 8
-- transition that worked before still works, unchanged, and every existing row
-- is untouched: no backfill, no rewrite, no default that reinterprets history.
-- =============================================================================

ALTER TABLE salesops.review_queue
    ADD COLUMN IF NOT EXISTS approved_by TEXT,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

COMMENT ON COLUMN salesops.review_queue.approved_by IS
    'The human who authorised remediation. Set by the state-machine trigger on the '
    'transition into ''approved'' and required by it - an approval with no identifiable '
    'actor authorises nothing.';
COMMENT ON COLUMN salesops.review_queue.approved_at IS
    'When remediation was authorised. Snapshotted onto every remediation action created '
    'from this review.';

-- The status vocabulary gains one value. Rewritten rather than widened in place
-- because a CHECK cannot be extended; the previous four values are preserved
-- verbatim.
ALTER TABLE salesops.review_queue
    DROP CONSTRAINT IF EXISTS review_queue_status_valid;
ALTER TABLE salesops.review_queue
    ADD CONSTRAINT review_queue_status_valid
    CHECK (status IN ('pending', 'in_review', 'resolved', 'dismissed', 'approved'));

-- 'approved' is terminal, so it obeys the same two invariants as the other two
-- terminal states: it says what was concluded, and when.
ALTER TABLE salesops.review_queue
    DROP CONSTRAINT IF EXISTS review_queue_terminal_has_resolution;
ALTER TABLE salesops.review_queue
    ADD CONSTRAINT review_queue_terminal_has_resolution
    CHECK ((status IN ('resolved', 'dismissed', 'approved')) = (resolution IS NOT NULL));

ALTER TABLE salesops.review_queue
    DROP CONSTRAINT IF EXISTS review_queue_terminal_has_timestamp;
ALTER TABLE salesops.review_queue
    ADD CONSTRAINT review_queue_terminal_has_timestamp
    CHECK ((status IN ('resolved', 'dismissed', 'approved')) = (reviewed_at IS NOT NULL));

-- An approval is an act by a named person at a known time. Both, or neither.
ALTER TABLE salesops.review_queue
    DROP CONSTRAINT IF EXISTS review_queue_approval_is_attributable;
ALTER TABLE salesops.review_queue
    ADD CONSTRAINT review_queue_approval_is_attributable
    CHECK ((status = 'approved')
           = (approved_by IS NOT NULL AND approved_at IS NOT NULL));

-- You cannot authorise action on something you have just called a false alarm.
-- 'expected_business_variation' is excluded for the same reason: it says the
-- movement was normal, and normal movements do not need remediating.
ALTER TABLE salesops.review_queue
    DROP CONSTRAINT IF EXISTS review_queue_approval_needs_confirmation;
ALTER TABLE salesops.review_queue
    ADD CONSTRAINT review_queue_approval_needs_confirmation
    CHECK (status <> 'approved'
           OR resolution IN ('confirmed', 'requires_follow_up'));

ALTER TABLE salesops.review_events
    DROP CONSTRAINT IF EXISTS review_events_to_status_valid;
ALTER TABLE salesops.review_events
    ADD CONSTRAINT review_events_to_status_valid
    CHECK (to_status IN ('pending', 'in_review', 'resolved', 'dismissed', 'approved'));

ALTER TABLE salesops.review_events
    DROP CONSTRAINT IF EXISTS review_events_from_status_valid;
ALTER TABLE salesops.review_events
    ADD CONSTRAINT review_events_from_status_valid
    CHECK (from_status IS NULL
           OR from_status IN ('pending', 'in_review', 'resolved', 'dismissed', 'approved'));

COMMENT ON COLUMN salesops.review_queue.status IS
    'pending -> in_review -> resolved | dismissed | approved. pending -> dismissed is '
    'allowed (triaged without claiming); in_review -> pending releases a claim. '
    'resolved, dismissed and approved are terminal. ''approved'' is the ONLY state that '
    'authorises Stage 9 remediation; ''resolved'' means reviewed and closed without it. '
    'Enforced by trigger.';


-- The Stage 8 state machine, extended by exactly one transition.
--
-- Replaces salesops.guard_review_transition() from V010. Every previously legal
-- move is still legal and still behaves identically; ('in_review','approved')
-- is added, and 'approved' joins the terminal set whose resolution and notes
-- can no longer be edited.
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
        -- to prevent. An approved item is closed hardest of all - remediation
        -- has been authorised against exactly this text.
        IF OLD.status IN ('resolved', 'dismissed', 'approved')
           AND (NEW.resolution   IS DISTINCT FROM OLD.resolution
             OR NEW.review_notes IS DISTINCT FROM OLD.review_notes) THEN
            RAISE EXCEPTION
                'Review % is %; its resolution and notes are final.',
                OLD.review_id, OLD.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        -- An approval, once made, is not re-attributable.
        IF OLD.status = 'approved'
           AND (NEW.approved_by IS DISTINCT FROM OLD.approved_by
             OR NEW.approved_at IS DISTINCT FROM OLD.approved_at) THEN
            RAISE EXCEPTION
                'Review % is approved; its approving actor and timestamp are final.',
                OLD.review_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        RETURN NEW;
    END IF;

    allowed := (OLD.status, NEW.status) IN (
        ('pending',   'in_review'),
        ('pending',   'dismissed'),
        ('in_review', 'resolved'),
        ('in_review', 'dismissed'),
        ('in_review', 'approved'),
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
    ELSIF NEW.status = 'approved' THEN
        NEW.reviewed_at := COALESCE(NEW.reviewed_at, now());
        NEW.approved_at := COALESCE(NEW.approved_at, now());
        NEW.approved_by := COALESCE(NEW.approved_by, NEW.assigned_to, OLD.assigned_to);

        IF NEW.approved_by IS NULL OR length(btrim(NEW.approved_by)) = 0 THEN
            RAISE EXCEPTION
                'Review % cannot be approved without an identifiable actor.',
                OLD.review_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    INSERT INTO salesops.review_events
        (review_id, from_status, to_status, actor, resolution, note_excerpt)
    VALUES (
        OLD.review_id, OLD.status, NEW.status,
        COALESCE(NEW.approved_by, NEW.assigned_to, OLD.assigned_to),
        NEW.resolution,
        left(NEW.review_notes, 500)
    );

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_review_transition() IS
    'Enforces the review state machine, maintains its timestamps, and appends to '
    'review_events. V011 adds in_review -> approved, the only state that authorises '
    'Stage 9 remediation. Terminal resolutions, notes and approvals cannot be edited.';


-- =============================================================================
-- 2. The action vocabulary
--
-- A closed set, in a table rather than a CHECK, for the reason the Stage 6
-- reason codes are: an operator can look up what an action means without
-- reading this file, and a new action cannot be introduced by a typo in an
-- INSERT - or by a language model, or by an HTTP caller.
--
-- All three are REQUESTS FOR HUMAN WORK. None of them moves money, changes an
-- order, contacts a customer or touches inventory. That is not a limitation of
-- the provider; it is the scope. This project has no downstream system capable
-- of safely executing a financial or operational mutation, and inventing one
-- would mean writing a fake ERP to pretend against.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.remediation_action_types (
    action_type        TEXT PRIMARY KEY,
    description        TEXT    NOT NULL,

    -- What a person is being asked to do, in the imperative, for the payload.
    request_summary    TEXT    NOT NULL,

    -- TRUE would mean "executing this changes state in a system outside this
    -- warehouse". Every action here is FALSE, and the CHECK below keeps it that
    -- way: adding a mutating action must be a deliberate migration that removes
    -- this constraint, not a quiet INSERT.
    mutates_external_state BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT remediation_action_types_format
        CHECK (action_type ~ '^[a-z][a-z_]+[a-z]$'),
    CONSTRAINT remediation_action_types_are_review_requests
        CHECK (NOT mutates_external_state)
);

COMMENT ON TABLE salesops.remediation_action_types IS
    'Closed vocabulary of remediation actions. Every one is a request for human '
    'investigation or review - none mutates state in an external business system, and '
    'the mutates_external_state CHECK is what stops one being added by accident.';

INSERT INTO salesops.remediation_action_types
    (action_type, description, request_summary) VALUES
    ('create_investigation',
     'Open an investigation into the anomaly. The broadest of the three: it asks for '
     'the cause to be established, without presuming which team owns it.',
     'Investigate the cause of this revenue anomaly and record what is found.'),
    ('request_operations_review',
     'Ask the operations team to review the day''s fulfilment, order handling and '
     'systems for a process failure consistent with the observed movement.',
     'Review operational systems and processes for this date.'),
    ('request_refund_review',
     'Ask finance or fraud to re-examine the refunds issued on this date. The most '
     'invasive of the three - it puts already-settled money back under scrutiny - so '
     'Stage 6 must have graded the day critical before it becomes available.',
     'Re-examine the refunds issued on this date and confirm each was legitimate.')
ON CONFLICT (action_type) DO UPDATE
    SET description     = EXCLUDED.description,
        request_summary = EXCLUDED.request_summary;


-- =============================================================================
-- 3. Eligibility: which action is permitted for which severity
--
-- Derived from Stage 6's own routing contract (V008), not invented beside it:
--
--     severity   routing        what Stage 6 already said
--     --------   ------------   ------------------------------------------
--     none       no_action      not an anomaly worth acting on
--     minor      auto_notify    tell somebody; no human decision needed
--     major      human_review   a person must look
--     critical   human_review   a person must look
--
-- Remediation begins from an APPROVED REVIEW, and only 'major' and 'critical'
-- produce review items at all. So:
--
--   none      never eligible. There is no review, no approval, nothing.
--   minor     never eligible IN PRACTICE, and not by a separate rule: Stage 6
--             routes it to auto_notify, so no review item is ever created, so
--             there is nothing to approve. If minor ever needs remediating, the
--             honest fix is to change Stage 6's routing - not to open a side
--             door into Stage 9 that bypasses human review.
--   major     investigation and operations review.
--   critical  those two, plus refund review.
--
-- Refund review is held back to 'critical' deliberately. It asks people to
-- re-open settled financial transactions, which is the most disruptive thing in
-- the vocabulary, and Stage 6 has already published which days it considers
-- worth that. Reusing its answer is better than adding a second opinion here.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.remediation_action_eligibility (
    policy_version TEXT NOT NULL,
    severity       TEXT NOT NULL,
    action_type    TEXT NOT NULL,
    rationale      TEXT NOT NULL,

    PRIMARY KEY (policy_version, severity, action_type),

    CONSTRAINT remediation_eligibility_action_fk
        FOREIGN KEY (action_type) REFERENCES salesops.remediation_action_types (action_type),
    CONSTRAINT remediation_eligibility_severity_valid
        CHECK (severity IN ('none', 'minor', 'major', 'critical')),
    -- The two severities Stage 6 does not route to a human can never appear
    -- here. Stated as a constraint so the table cannot drift into contradicting
    -- V008's routing contract.
    CONSTRAINT remediation_eligibility_needs_human_routing
        CHECK (severity IN ('major', 'critical'))
);

COMMENT ON TABLE salesops.remediation_action_eligibility IS
    'Which remediation action is permitted for which Stage 6 severity, versioned. '
    'remediation_actions carries a composite FK into this table, so an ineligible '
    '(severity, action) pair is a foreign key violation rather than a missed check.';

INSERT INTO salesops.remediation_action_eligibility
    (policy_version, severity, action_type, rationale) VALUES
    ('stage9-v1', 'major',    'create_investigation',
     'Stage 6 required a human; establishing the cause is the least invasive response.'),
    ('stage9-v1', 'major',    'request_operations_review',
     'Operational review reads systems and processes. It changes nothing and can be '
     'asked for on any day a person was already required to look.'),
    ('stage9-v1', 'critical', 'create_investigation',
     'Available at every severity that reaches human review.'),
    ('stage9-v1', 'critical', 'request_operations_review',
     'Available at every severity that reaches human review.'),
    ('stage9-v1', 'critical', 'request_refund_review',
     'Re-opens settled financial transactions. Restricted to the severity Stage 6 '
     'already grades as the most serious, rather than gated on a second opinion here.')
ON CONFLICT (policy_version, severity, action_type) DO UPDATE
    SET rationale = EXCLUDED.rationale;


-- =============================================================================
-- 4. Remediation actions
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.remediation_actions (
    remediation_id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- --- what authorised this, snapshotted (see header) ----------------------
    -- Plain BIGINTs, no foreign keys. Validated against the live rows once, at
    -- INSERT, by guard_remediation_authorization().
    review_id              BIGINT      NOT NULL,
    anomaly_id             BIGINT      NOT NULL,
    decision_id            BIGINT      NOT NULL,
    hypothesis_id          BIGINT,
    calendar_date          DATE        NOT NULL,

    -- Stage 6 verdict as it stood when a human approved it
    decision_version       TEXT        NOT NULL,
    severity               TEXT        NOT NULL,
    routing                TEXT        NOT NULL,
    decision               TEXT        NOT NULL,
    notification_allowed   BOOLEAN     NOT NULL,
    human_review_required  BOOLEAN     NOT NULL,
    decision_reason_code   TEXT        NOT NULL,
    decision_reason_codes  TEXT[]      NOT NULL DEFAULT '{}',

    -- Stage 7, if there was any. Recorded for provenance only: nothing about
    -- the hypothesis affects whether this action is permitted, and the columns
    -- are here so an auditor can see what the approver had in front of them.
    hypothesis_status      TEXT        NOT NULL DEFAULT 'unavailable',
    hypothesis_prompt_version TEXT,
    hypothesis_model_name  TEXT,

    -- Stage 8 approval
    review_approved_by     TEXT        NOT NULL,
    review_approved_at     TIMESTAMPTZ NOT NULL,
    review_resolution      TEXT        NOT NULL,

    -- --- the action ----------------------------------------------------------
    action_type            TEXT        NOT NULL,
    policy_version         TEXT        NOT NULL DEFAULT 'stage9-v1',
    request_payload        JSONB       NOT NULL,

    status                 TEXT        NOT NULL DEFAULT 'proposed',

    -- proposed -> approved: this specific action is authorised for execution
    authorized_by          TEXT,
    authorized_at          TIMESTAMPTZ,

    -- approved -> executing -> executed
    executed_by            TEXT,
    executed_at            TIMESTAMPTZ,

    attempt_count          INTEGER     NOT NULL DEFAULT 0,
    provider               TEXT,
    provider_reference     TEXT,
    last_error             TEXT,

    -- rejected / cancelled
    closed_reason          TEXT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- --- idempotency ---------------------------------------------------------
    -- Derived from immutable identity, and STORED so it is visible in a SELECT
    -- rather than being a rule that lives only in an ON CONFLICT clause. The
    -- decision_version is part of it because a re-decided anomaly is a
    -- different authorisation, not the same one again.
    idempotency_key        TEXT
        GENERATED ALWAYS AS (
            review_id::text || ':' || action_type || ':' || decision_version
        ) STORED,

    CONSTRAINT remediation_actions_idempotent UNIQUE (idempotency_key),

    -- Eligibility, as a foreign key. An action type not permitted for this
    -- severity has no row to reference, so the INSERT fails.
    CONSTRAINT remediation_actions_eligible_fk
        FOREIGN KEY (policy_version, severity, action_type)
        REFERENCES salesops.remediation_action_eligibility
                   (policy_version, severity, action_type),

    CONSTRAINT remediation_actions_status_valid
        CHECK (status IN ('proposed', 'approved', 'executing', 'executed',
                          'rejected', 'failed', 'cancelled')),
    CONSTRAINT remediation_actions_severity_valid
        CHECK (severity IN ('major', 'critical')),
    CONSTRAINT remediation_actions_hypothesis_status_valid
        CHECK (hypothesis_status IN ('available', 'unavailable')),
    CONSTRAINT remediation_actions_hypothesis_status_agrees
        CHECK ((hypothesis_status = 'available') = (hypothesis_id IS NOT NULL)),

    -- ---- the authorisation contract, enforced rather than trusted -----------
    -- The same technique V010 uses for delivery eligibility, applied to
    -- authorisation. These columns are Stage 9's own immutable copy, so a
    -- constraint on them can never block a Stage 6 re-decision.
    CONSTRAINT remediation_actions_only_for_actionable_decisions
        CHECK (decision = 'action_required'
               AND routing = 'human_review'
               AND human_review_required),
    CONSTRAINT remediation_actions_approver_present
        CHECK (length(btrim(review_approved_by)) > 0),
    CONSTRAINT remediation_actions_resolution_confirms
        CHECK (review_resolution IN ('confirmed', 'requires_follow_up')),

    -- ---- state invariants ---------------------------------------------------
    -- Executable only once authorised: every state at or beyond 'approved'
    -- carries the actor who authorised it.
    CONSTRAINT remediation_actions_authorization_recorded
        CHECK ((status IN ('proposed', 'rejected', 'cancelled'))
               OR (authorized_by IS NOT NULL AND authorized_at IS NOT NULL)),
    CONSTRAINT remediation_actions_executed_is_recorded
        CHECK (status <> 'executed'
               OR (executed_at IS NOT NULL AND executed_by IS NOT NULL
                   AND attempt_count > 0)),
    -- ...and one that has not executed must not claim an execution time.
    CONSTRAINT remediation_actions_unexecuted_has_no_time
        CHECK (status = 'executed' OR executed_at IS NULL),
    CONSTRAINT remediation_actions_failure_has_reason
        CHECK (status <> 'failed' OR last_error IS NOT NULL),
    CONSTRAINT remediation_actions_closure_has_reason
        CHECK (status NOT IN ('rejected', 'cancelled') OR closed_reason IS NOT NULL),
    CONSTRAINT remediation_actions_attempts_non_negative
        CHECK (attempt_count >= 0),
    CONSTRAINT remediation_actions_closed_reason_bounded
        CHECK (closed_reason IS NULL OR length(closed_reason) <= 2000)
);

COMMENT ON TABLE salesops.remediation_actions IS
    'Stage 9 remediation. One row per (approved review, action type, decision version) - '
    'the idempotency key. Exists only for a review a human moved to ''approved'', and '
    'only for an action the eligibility policy permits at that severity. Creating one is '
    'not executing one.';
COMMENT ON COLUMN salesops.remediation_actions.status IS
    'proposed -> approved -> executing -> executed. Terminal: executed, rejected, '
    'cancelled. failed is a resting state that permits a bounded explicit retry. '
    'Enforced by trigger.';
COMMENT ON COLUMN salesops.remediation_actions.idempotency_key IS
    'review_id:action_type:decision_version. Generated, stored and unique - a repeated '
    'approval finds the existing action instead of creating a second one.';
COMMENT ON COLUMN salesops.remediation_actions.request_payload IS
    'What the provider is asked to do. Business content only - never a credential, an '
    'authorization header or a provider URL.';
COMMENT ON COLUMN salesops.remediation_actions.hypothesis_id IS
    'Provenance only. What Stage 7 said has no bearing on whether this action is '
    'permitted; it is recorded so an auditor can see what the approver was shown.';

CREATE INDEX IF NOT EXISTS idx_remediation_actions_executable
    ON salesops.remediation_actions (status, created_at)
    WHERE status IN ('approved', 'failed');
CREATE INDEX IF NOT EXISTS idx_remediation_actions_review
    ON salesops.remediation_actions (review_id);
CREATE INDEX IF NOT EXISTS idx_remediation_actions_decision
    ON salesops.remediation_actions (decision_id);


-- =============================================================================
-- 5. Execution attempts
--
-- Every call handed to a provider, not just the last one. The difference
-- between "it failed" and "it timed out twice and then the recorder accepted
-- it" lives here, and so does the proof that one logical action produced one
-- successful provider call.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.remediation_attempts (
    attempt_id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    remediation_id     BIGINT      NOT NULL,
    attempt_number     INTEGER     NOT NULL,

    outcome            TEXT        NOT NULL,
    provider           TEXT        NOT NULL,
    provider_reference TEXT,
    error_message      TEXT,
    latency_ms         INTEGER,

    -- The provider's own statement about whether anything outside this
    -- warehouse changed. The development provider always records FALSE, and
    -- says so in the audit view, so a reader is never left inferring it.
    external_side_effect BOOLEAN   NOT NULL DEFAULT FALSE,

    attempted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT remediation_attempts_unique
        UNIQUE (remediation_id, attempt_number),
    CONSTRAINT remediation_attempts_action_fk
        FOREIGN KEY (remediation_id)
        REFERENCES salesops.remediation_actions (remediation_id) ON DELETE CASCADE,
    CONSTRAINT remediation_attempts_outcome_valid
        CHECK (outcome IN ('success', 'retryable_failure', 'permanent_failure')),
    CONSTRAINT remediation_attempts_number_positive
        CHECK (attempt_number > 0),
    CONSTRAINT remediation_attempts_failure_has_reason
        CHECK (outcome = 'success' OR error_message IS NOT NULL),
    CONSTRAINT remediation_attempts_latency_non_negative
        CHECK (latency_ms IS NULL OR latency_ms >= 0)
);

COMMENT ON TABLE salesops.remediation_attempts IS
    'One row per provider call. At most one ''success'' per action, because a successful '
    'attempt moves the action to the terminal ''executed'' state and nothing re-executes it.';
COMMENT ON COLUMN salesops.remediation_attempts.external_side_effect IS
    'Whether the provider changed state outside this warehouse. Always FALSE for the '
    'development recording provider - it records a request and returns; it contacts nothing.';

CREATE INDEX IF NOT EXISTS idx_remediation_attempts_action
    ON salesops.remediation_attempts (remediation_id, attempt_number);


-- =============================================================================
-- 6. Remediation history
--
-- Who moved it, when, from what to what, and why. Every transition, including
-- the one that created the row.
-- =============================================================================
CREATE TABLE IF NOT EXISTS salesops.remediation_events (
    event_id       BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    remediation_id BIGINT      NOT NULL,

    from_status    TEXT,
    to_status      TEXT        NOT NULL,
    actor          TEXT,
    reason         TEXT,

    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT remediation_events_action_fk
        FOREIGN KEY (remediation_id)
        REFERENCES salesops.remediation_actions (remediation_id) ON DELETE CASCADE,
    CONSTRAINT remediation_events_to_status_valid
        CHECK (to_status IN ('proposed', 'approved', 'executing', 'executed',
                             'rejected', 'failed', 'cancelled')),
    CONSTRAINT remediation_events_from_status_valid
        CHECK (from_status IS NULL
               OR from_status IN ('proposed', 'approved', 'executing', 'executed',
                                  'rejected', 'failed', 'cancelled')),
    CONSTRAINT remediation_events_reason_bounded
        CHECK (reason IS NULL OR length(reason) <= 2000)
);

COMMENT ON TABLE salesops.remediation_events IS
    'Every remediation state transition, with actor, reason and timestamp. Append-only '
    'in practice, and written by the trigger rather than by the caller - a transition '
    'that happened without an event is not reachable.';

CREATE INDEX IF NOT EXISTS idx_remediation_events_action
    ON salesops.remediation_events (remediation_id, occurred_at);


-- =============================================================================
-- 7. The authorisation guard
--
-- Fires once, at INSERT, and answers the only question that matters: did a
-- human actually approve this, and is the snapshot an honest copy of what they
-- approved?
--
-- Deliberately NOT re-run on UPDATE. Stage 6 may re-decide, Stage 7 may
-- regenerate; neither may retroactively invalidate or rewrite an action a
-- person authorised. The audit view reports whether the authorisation still
-- matches the live decision, which is the honest way to surface a drift that
-- has already happened.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_remediation_authorization()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
BEGIN
    SELECT review_id, status, resolution, approved_by, approved_at,
           anomaly_id, decision_id, hypothesis_id, calendar_date, decision_version,
           severity, routing, decision, notification_allowed, human_review_required,
           hypothesis_status
    INTO r
    FROM salesops.review_queue
    WHERE review_id = NEW.review_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No Stage 8 review % exists', NEW.review_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- Gate 1. Not 'resolved' (closed without remediation), not 'dismissed'
    -- (the opposite of approval), and certainly not 'pending' or 'in_review'.
    IF r.status <> 'approved' THEN
        RAISE EXCEPTION
            'Review % is %; remediation requires an approved review. '
            '(''resolved'' means reviewed and closed WITHOUT remediation.)',
            NEW.review_id, r.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF r.approved_by IS NULL OR r.approved_at IS NULL THEN
        RAISE EXCEPTION
            'Review % has no identifiable approving actor.', NEW.review_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Gate 2. The snapshot must be what the reviewer actually approved. A
    -- caller cannot hand in an eligible-looking severity for a review that
    -- carries a different one.
    IF (NEW.anomaly_id, NEW.decision_id, NEW.calendar_date, NEW.decision_version,
        NEW.severity, NEW.routing, NEW.decision,
        NEW.notification_allowed, NEW.human_review_required)
       IS DISTINCT FROM
       (r.anomaly_id, r.decision_id, r.calendar_date, r.decision_version,
        r.severity, r.routing, r.decision,
        r.notification_allowed, r.human_review_required) THEN
        RAISE EXCEPTION
            'Remediation snapshot does not match review %. The review is '
            '(anomaly %, decision %, %, %, %, %); the action claims '
            '(anomaly %, decision %, %, %, %, %).',
            NEW.review_id,
            r.anomaly_id, r.decision_id, r.calendar_date, r.severity, r.routing, r.decision,
            NEW.anomaly_id, NEW.decision_id, NEW.calendar_date,
            NEW.severity, NEW.routing, NEW.decision
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF (NEW.review_approved_by, NEW.review_approved_at, NEW.review_resolution)
       IS DISTINCT FROM (r.approved_by, r.approved_at, r.resolution) THEN
        RAISE EXCEPTION
            'Remediation approval snapshot does not match review %: it was approved '
            'by % at % with resolution %.',
            NEW.review_id, r.approved_by, r.approved_at, r.resolution
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- A new action starts unexecuted. Nothing may arrive pre-executed.
    IF NEW.status <> 'proposed' THEN
        RAISE EXCEPTION
            'A remediation action is created as ''proposed''; % is not an opening state. '
            'Authorisation and execution are separate operations.',
            NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION salesops.guard_remediation_authorization() IS
    'Rejects any remediation action not backed by an approved Stage 8 review with a '
    'matching snapshot and an identifiable approver. Fires only on Stage 9 INSERTs; '
    'cannot block Stage 6, Stage 7 or Stage 8.';

DROP TRIGGER IF EXISTS trg_guard_remediation_authorization ON salesops.remediation_actions;
CREATE TRIGGER trg_guard_remediation_authorization
    BEFORE INSERT ON salesops.remediation_actions
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_remediation_authorization();


-- =============================================================================
-- 8. The remediation state machine
--
--   proposed  -> approved     this specific action is authorised
--   proposed  -> rejected     this action should not be taken       (terminal)
--   proposed  -> cancelled                                          (terminal)
--   approved  -> executing    claimed for execution
--   approved  -> cancelled    authorised, then thought better of    (terminal)
--   executing -> executed     the provider accepted it              (terminal)
--   executing -> failed       the provider did not
--   failed    -> executing    explicit retry, within the attempt budget
--   failed    -> cancelled    stop trying                           (terminal)
--
-- Why 'proposed' and 'approved' are separate states rather than one
-- ------------------------------------------------------------------
-- Approving the REVIEW answers "is this anomaly real and does it warrant a
-- response?". Authorising the ACTION answers "is THIS the response?". They are
-- different judgements, and a system that collapses them cannot record a
-- reviewer who confirmed an anomaly but rejected the action proposed for it.
-- In practice the same person usually makes both calls; the record still keeps
-- them apart, and a rejection at the second gate leaves the first intact.
--
-- Why 'executing' exists
-- ----------------------
-- It is the claim. Entering it is a conditional UPDATE from 'approved' or
-- 'failed', so two concurrent callers cannot both proceed: the second finds no
-- row to move and does nothing. That, and not a lock held across a network
-- call, is what makes "the provider is called once per logical action" true.
--
-- 'executed' has no outgoing transition at all. An action that has run cannot
-- run again, however it is asked.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.guard_remediation_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    allowed          BOOLEAN;
    max_attempts CONSTANT INTEGER := 3;
BEGIN
    -- Snapshot columns are immutable. Stage 6 re-deciding must not rewrite what
    -- a human authorised, and neither must a caller.
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
        ('failed',    'cancelled')
    );

    IF NOT allowed THEN
        RAISE EXCEPTION
            'Invalid remediation transition % -> % for remediation %.',
            OLD.status, NEW.status, OLD.remediation_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- The retry budget. Without it a permanently broken action would be
    -- re-executed by every scheduled run for the rest of the system's life.
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
    'authorisation snapshot immutable, and appends to remediation_events. ''executed'' '
    'has no outgoing transition: an action that has run cannot run again.';

DROP TRIGGER IF EXISTS trg_guard_remediation_transition ON salesops.remediation_actions;
CREATE TRIGGER trg_guard_remediation_transition
    BEFORE UPDATE ON salesops.remediation_actions
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_remediation_transition();


CREATE OR REPLACE FUNCTION salesops.record_remediation_created()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO salesops.remediation_events
        (remediation_id, from_status, to_status, actor, reason)
    VALUES (NEW.remediation_id, NULL, NEW.status, NEW.review_approved_by,
            format('Proposed from approved review %s (%s, %s).',
                   NEW.review_id, NEW.severity, NEW.action_type));
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_record_remediation_created ON salesops.remediation_actions;
CREATE TRIGGER trg_record_remediation_created
    AFTER INSERT ON salesops.remediation_actions
    FOR EACH ROW EXECUTE FUNCTION salesops.record_remediation_created();


-- =============================================================================
-- 9. Audit views
-- =============================================================================
CREATE OR REPLACE VIEW salesops.remediation_audit AS
SELECT
    a.remediation_id,
    a.calendar_date,
    dd.day_name,

    -- The chain, end to end, in one row.
    a.anomaly_id,
    a.decision_id,
    a.review_id,
    a.hypothesis_id,

    -- Stage 6 as authorised, beside Stage 6 as it stands now. When these
    -- disagree the action is not invalid - a human authorised it against what
    -- was true at the time - but an auditor should be able to see it.
    a.severity            AS authorized_severity,
    d.severity            AS decision_severity,
    a.routing             AS authorized_routing,
    d.routing             AS decision_routing,
    a.decision_version,
    a.decision_reason_code,
    a.decision_reason_codes,
    (a.severity, a.routing, a.decision, a.notification_allowed, a.human_review_required)
        IS NOT DISTINCT FROM
        (d.severity, d.routing, d.decision, d.notification_allowed, d.human_review_required)
        AS authorization_current,
    d.anomaly_score,
    d.business_impact_tier,
    d.expected_net_revenue_usd,
    d.actual_net_revenue_usd,
    d.revenue_delta_usd,

    -- Stage 7, provenance only
    a.hypothesis_status,
    a.hypothesis_model_name,
    h.confidence          AS hypothesis_confidence,
    h.primary_hypothesis,

    -- Stage 8 approval
    a.review_approved_by,
    a.review_approved_at,
    a.review_resolution,
    r.status              AS review_status,

    -- Stage 9
    a.action_type,
    t.description         AS action_description,
    t.mutates_external_state,
    a.policy_version,
    a.status,
    a.authorized_by,
    a.authorized_at,
    a.executed_by,
    a.executed_at,
    a.attempt_count,
    a.provider,
    a.provider_reference,
    a.last_error,
    a.closed_reason,
    a.created_at,
    a.idempotency_key,

    (SELECT count(*) FROM salesops.remediation_attempts at
      WHERE at.remediation_id = a.remediation_id)                 AS attempts_recorded,
    (SELECT count(*) FROM salesops.remediation_attempts at
      WHERE at.remediation_id = a.remediation_id
        AND at.outcome = 'success')                               AS successful_attempts,
    -- Always FALSE with the development provider, and reported rather than
    -- assumed: a reader must never have to infer whether anything real happened.
    COALESCE((SELECT bool_or(at.external_side_effect)
              FROM salesops.remediation_attempts at
              WHERE at.remediation_id = a.remediation_id), FALSE) AS had_external_side_effect,
    (SELECT count(*) FROM salesops.remediation_events e
      WHERE e.remediation_id = a.remediation_id)                  AS transition_count,
    CASE WHEN a.executed_at IS NOT NULL
         THEN round(extract(epoch FROM (a.executed_at - a.review_approved_at))::numeric, 1)
    END                                                           AS seconds_approval_to_execution
FROM salesops.remediation_actions        a
JOIN salesops.remediation_action_types   t  ON t.action_type    = a.action_type
JOIN salesops.dim_date                   dd ON dd.calendar_date = a.calendar_date
LEFT JOIN salesops.anomaly_decisions     d  ON d.decision_id    = a.decision_id
LEFT JOIN salesops.review_queue          r  ON r.review_id      = a.review_id
LEFT JOIN salesops.anomaly_hypotheses    h  ON h.hypothesis_id  = a.hypothesis_id;

COMMENT ON VIEW salesops.remediation_audit IS
    'The full chain for one remediation: what was detected, what Stage 6 decided, what '
    'Stage 7 guessed, who approved it, who authorised the action, whether it executed, '
    'and whether the authorisation still matches the live decision. No secrets: no '
    'provider URL, credential or header appears anywhere in it.';


-- The queue an operator actually works: authorised, not yet run.
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
    'retry budget already applied. This is what the Stage 9 workflow executes - it never '
    'selects work by severity itself, and never sees a proposed-but-unauthorised action.';


-- -----------------------------------------------------------------------------
-- ingestion_runs gains an eighth pipeline.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild), ''anomaly-detector'' (Stage 5), '
    '''anomaly-decision'' (Stage 6), ''llm-root-cause'' (Stage 7), '
    '''notification-router'' (Stage 8), ''remediation-executor'' (Stage 9). '
    'Always filter by source when reading windows.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V011', 'Stage 9 remediation: action vocabulary, severity eligibility, actions, '
                'attempts, events, audit views, and the review approval state')
ON CONFLICT (version) DO NOTHING;

COMMIT;
