"""Database access.

The only module here that talks to PostgreSQL. Everything statistical is pure
and lives elsewhere, so the mathematics can be tested without a database and the
SQL can be reviewed without reading the mathematics.

Two rules hold throughout:

* KPI values are READ, never recomputed. `salesops.kpi_daily` is authoritative;
  duplicating its definitions in Python would create a second source of truth
  that drifts the first time either changes.

* Every value reaches SQL as a bound parameter. No data is ever interpolated
  into a statement.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from analytics.models import DetectionResult, KpiObservation

# Calendar attributes come from dim_date rather than being derived from the date
# in Python, so "which day of week is this" has one definition in the project.
_SELECT_KPI = """
    SELECT
        k.calendar_date,
        d.day_of_week,
        d.is_weekend,
        k.orders_count,
        k.net_revenue_usd,
        k.average_order_value_usd,
        k.refund_rate,
        k.orders_pending_fx,
        k.fx_completeness_pct,
        k.is_complete
    FROM salesops.kpi_daily k
    JOIN salesops.dim_date  d ON d.calendar_date = k.calendar_date
    ORDER BY k.calendar_date
"""

_SELECT_ALREADY_DETECTED = """
    SELECT calendar_date
    FROM salesops.anomaly_daily
    WHERE detector_version = %(detector_version)s
"""

# ON CONFLICT DO UPDATE, not DO NOTHING - and the contrast with fact_orders is
# deliberate. A fact is an immutable observation of something that happened; a
# detection is a derived opinion about it. If the KPI inputs change (a backfill,
# an FX correction), the detection SHOULD change with them. Freezing it would
# leave a verdict describing data that no longer exists.
_UPSERT_ANOMALY = """
    INSERT INTO salesops.anomaly_daily (
        calendar_date, detector_version,
        anomaly_score, is_anomaly,
        revenue_deviation_pct, revenue_robust_z,
        aov_deviation_pct, aov_robust_z,
        refund_rate_deviation, refund_robust_z,
        orders_deviation_pct, orders_robust_z,
        revenue_baseline_median,
        baseline_status, baseline_kind, baseline_size,
        dominant_signal, signal_count, detected_at
    ) VALUES (
        %(calendar_date)s, %(detector_version)s,
        %(anomaly_score)s, %(is_anomaly)s,
        %(revenue_deviation_pct)s, %(revenue_robust_z)s,
        %(aov_deviation_pct)s, %(aov_robust_z)s,
        %(refund_rate_deviation)s, %(refund_robust_z)s,
        %(orders_deviation_pct)s, %(orders_robust_z)s,
        %(revenue_baseline_median)s,
        %(baseline_status)s, %(baseline_kind)s, %(baseline_size)s,
        %(dominant_signal)s, %(signal_count)s, now()
    )
    ON CONFLICT (calendar_date, detector_version) DO UPDATE SET
        anomaly_score         = EXCLUDED.anomaly_score,
        is_anomaly            = EXCLUDED.is_anomaly,
        revenue_deviation_pct = EXCLUDED.revenue_deviation_pct,
        revenue_robust_z      = EXCLUDED.revenue_robust_z,
        aov_deviation_pct     = EXCLUDED.aov_deviation_pct,
        aov_robust_z          = EXCLUDED.aov_robust_z,
        refund_rate_deviation = EXCLUDED.refund_rate_deviation,
        refund_robust_z       = EXCLUDED.refund_robust_z,
        orders_deviation_pct  = EXCLUDED.orders_deviation_pct,
        orders_robust_z       = EXCLUDED.orders_robust_z,
        revenue_baseline_median = EXCLUDED.revenue_baseline_median,
        baseline_status       = EXCLUDED.baseline_status,
        baseline_kind         = EXCLUDED.baseline_kind,
        baseline_size         = EXCLUDED.baseline_size,
        dominant_signal       = EXCLUDED.dominant_signal,
        signal_count          = EXCLUDED.signal_count,
        detected_at           = EXCLUDED.detected_at
"""


# =============================================================================
# Stage 7: evidence in, hypotheses out
#
# These live here rather than in analytics/llm/ so the rule at the top of this
# module keeps holding: one module talks to PostgreSQL. It also keeps the llm
# package pure - prompts and validation are testable with no database, and the
# SQL is reviewable without reading prompt text.
# =============================================================================

# Eligible = Stage 6 said act. Nothing else is ever sent to a model: normal days,
# insufficient-history days and no_action decisions are not merely uninteresting,
# they are the majority, and analysing them would spend money to explain that
# nothing happened.
#
# `already_analysed` is the idempotency test, evaluated in SQL so a re-run does
# not build 11 evidence packages before discovering it has nothing to do.
_SELECT_ELIGIBLE_DECISIONS = """
    SELECT
        d.decision_id,
        d.anomaly_id,
        d.calendar_date,
        d.decision_version,
        d.severity,
        d.routing,
        d.decision,
        d.decision_reason_code,
        d.anomaly_score,
        d.signal_count,
        d.baseline_status,
        d.business_impact_tier,
        d.expected_net_revenue_usd,
        d.actual_net_revenue_usd,
        d.revenue_delta_usd,
        d.revenue_delta_pct,
        d.revenue_robust_z,
        d.aov_robust_z,
        d.refund_robust_z,
        d.orders_robust_z,
        d.revenue_deviation_pct,
        d.aov_deviation_pct,
        d.refund_rate_deviation,
        d.orders_deviation_pct,
        dd.day_name,
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
        EXISTS (
            SELECT 1 FROM salesops.anomaly_hypotheses h
            WHERE h.anomaly_id       = d.anomaly_id
              AND h.decision_version = d.decision_version
              AND h.prompt_version   = %(prompt_version)s
              AND h.model_name       = %(model_name)s
        ) AS already_analysed
    FROM salesops.anomaly_decisions d
    JOIN salesops.dim_date  dd ON dd.calendar_date = d.calendar_date
    LEFT JOIN salesops.kpi_daily k ON k.calendar_date = d.calendar_date
    WHERE d.decision_version = %(decision_version)s
      AND d.decision = 'action_required'
    ORDER BY d.calendar_date
"""

_SELECT_REASON_CODES = """
    SELECT reason_code
    FROM salesops.anomaly_decision_reasons
    WHERE decision_id = %(decision_id)s
    ORDER BY reason_code
"""

# Strictly earlier dates only. The WHERE clause is the guarantee, and the
# evidence package asserts it again before any prompt is built - a leak here
# would not raise an error or produce malformed output, it would produce a
# fluent, confident analysis of an anomaly using information nobody had at the
# time.
_SELECT_SAME_WEEKDAY_HISTORY = """
    SELECT k.calendar_date, dd.day_name,
           k.net_revenue_usd, k.orders_count,
           k.average_order_value_usd, k.refund_rate
    FROM salesops.kpi_daily k
    JOIN salesops.dim_date dd ON dd.calendar_date = k.calendar_date
    WHERE k.calendar_date < %(calendar_date)s
      AND dd.day_of_week = (
          SELECT day_of_week FROM salesops.dim_date
          WHERE calendar_date = %(calendar_date)s
      )
      AND k.is_complete
    ORDER BY k.calendar_date DESC
    LIMIT %(limit)s
"""

_SELECT_PRECEDING_DAYS = """
    SELECT k.calendar_date, dd.day_name,
           k.net_revenue_usd, k.orders_count,
           k.average_order_value_usd, k.refund_rate
    FROM salesops.kpi_daily k
    JOIN salesops.dim_date dd ON dd.calendar_date = k.calendar_date
    WHERE k.calendar_date < %(calendar_date)s
    ORDER BY k.calendar_date DESC
    LIMIT %(limit)s
"""

# DO NOTHING, and the contrast with the two stages below it is the whole point.
# anomaly_daily and anomaly_decisions upsert, because a detection and a decision
# are derived opinions that must track their inputs. A hypothesis is a generated
# artefact with provenance: model, prompt version, and the digest of exactly what
# it was shown. Overwriting one silently replaces reasoning that a human may
# already have read and acted on. A changed prompt or model writes a NEW row -
# they are part of the key - and an explicit regeneration is the only way to
# replace an existing one.
_INSERT_HYPOTHESIS = """
    INSERT INTO salesops.anomaly_hypotheses (
        anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision,
        summary, confidence, primary_hypothesis,
        supporting_evidence, alternative_hypotheses,
        missing_evidence, recommended_checks,
        model_provider, model_name, prompt_version, evidence_digest,
        request_id, prompt_tokens, completion_tokens, latency_ms, json_mode
    ) VALUES (
        %(anomaly_id)s, %(decision_id)s, %(calendar_date)s, %(decision_version)s,
        %(severity)s, %(routing)s, %(decision)s,
        %(summary)s, %(confidence)s, %(primary_hypothesis)s,
        %(supporting_evidence)s, %(alternative_hypotheses)s,
        %(missing_evidence)s, %(recommended_checks)s,
        %(model_provider)s, %(model_name)s, %(prompt_version)s, %(evidence_digest)s,
        %(request_id)s, %(prompt_tokens)s, %(completion_tokens)s, %(latency_ms)s, %(json_mode)s
    )
    ON CONFLICT (anomaly_id, decision_version, prompt_version, model_name) DO NOTHING
    RETURNING hypothesis_id
"""

_DELETE_HYPOTHESIS = """
    DELETE FROM salesops.anomaly_hypotheses
    WHERE anomaly_id       = %(anomaly_id)s
      AND decision_version = %(decision_version)s
      AND prompt_version   = %(prompt_version)s
      AND model_name       = %(model_name)s
"""


# =============================================================================
# Stage 8: delivery and review
#
# Eligibility is a SELECT against Stage 6, never a calculation. The predicates
# below are the whole of Stage 8's routing logic, and they read the columns Stage
# 6 owns rather than re-deriving anything from severity.
# =============================================================================

# One query, both destinations. `needs_notification` and `needs_review` are read
# straight off Stage 6 - no CASE on severity, no threshold, nothing that could
# drift from the decision layer.
#
# `already_notified` / `already_queued` make a rerun cheap and are what stop a
# second run delivering the same finding twice.
_SELECT_ROUTABLE_DECISIONS = """
    SELECT
        d.decision_id,
        d.anomaly_id,
        d.calendar_date,
        d.decision_version,
        d.severity,
        d.routing,
        d.decision,
        d.notification_allowed,
        d.human_review_required,
        d.decision_reason_code,
        d.anomaly_score,
        d.signal_count,
        d.business_impact_tier,
        d.expected_net_revenue_usd,
        d.actual_net_revenue_usd,
        d.revenue_delta_usd,
        d.revenue_delta_pct,
        dd.day_name,
        k.orders_count,
        k.average_order_value_usd,
        k.refund_rate,

        (d.notification_allowed
         AND d.routing  = 'auto_notify'
         AND d.decision = 'action_required')  AS needs_notification,

        (d.human_review_required
         AND d.routing  = 'human_review'
         AND d.decision = 'action_required')  AS needs_review,

        h.hypothesis_id,
        h.summary            AS hypothesis_summary,
        h.primary_hypothesis,
        h.confidence         AS hypothesis_confidence,
        h.supporting_evidence,
        h.missing_evidence,
        h.recommended_checks,
        h.model_name,

        n.notification_id,
        n.status            AS notification_status,
        n.attempt_count     AS notification_attempts,
        r.review_id
    FROM salesops.anomaly_decisions d
    JOIN salesops.dim_date dd ON dd.calendar_date = d.calendar_date
    LEFT JOIN salesops.kpi_daily k ON k.calendar_date = d.calendar_date
    -- The newest hypothesis for this decision, if Stage 7 produced one at all.
    -- LEFT JOIN, because a Stage 7 failure must never stop Stage 8 routing.
    LEFT JOIN LATERAL (
        SELECT * FROM salesops.anomaly_hypotheses hh
        WHERE hh.decision_id = d.decision_id
        ORDER BY hh.generated_at DESC
        LIMIT 1
    ) h ON TRUE
    LEFT JOIN salesops.notifications n
           ON n.anomaly_id       = d.anomaly_id
          AND n.decision_version = d.decision_version
          AND n.channel          = %(channel)s
          AND n.recipient        = %(recipient)s
    LEFT JOIN salesops.review_queue r
           ON r.anomaly_id       = d.anomaly_id
          AND r.decision_version = d.decision_version
    WHERE d.decision_version = %(decision_version)s
      AND d.decision = 'action_required'
    ORDER BY d.calendar_date
"""

# DO NOTHING claims the row without overwriting a delivery that already
# happened. A retry updates the existing row rather than inserting beside it.
_INSERT_NOTIFICATION = """
    INSERT INTO salesops.notifications (
        anomaly_id, decision_id, hypothesis_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required,
        channel, recipient, subject, payload, hypothesis_status, status
    ) VALUES (
        %(anomaly_id)s, %(decision_id)s, %(hypothesis_id)s, %(calendar_date)s,
        %(decision_version)s,
        %(severity)s, %(routing)s, %(decision)s,
        %(notification_allowed)s, %(human_review_required)s,
        %(channel)s, %(recipient)s, %(subject)s, %(payload)s,
        %(hypothesis_status)s, 'pending'
    )
    ON CONFLICT (anomaly_id, decision_version, channel, recipient) DO NOTHING
    RETURNING notification_id
"""

_SELECT_NOTIFICATION = """
    SELECT notification_id, status, attempt_count
    FROM salesops.notifications
    WHERE anomaly_id = %(anomaly_id)s AND decision_version = %(decision_version)s
      AND channel = %(channel)s AND recipient = %(recipient)s
"""

# The attempt number is the caller's, OR one past the highest already recorded -
# whichever is larger.
#
# Normally these are the same: `_deliver` writes the attempt row and the counter
# in one transaction, so `attempt_count` and `max(attempt_number)` cannot drift
# through the ordinary path. They CAN drift if anything ever sets the counter by
# hand - a manual repair, a restore, a migration.
#
# The failure that caused was worth removing. A drift made every subsequent
# retry violate `notification_attempts_unique`, the violation was caught by the
# per-anomaly handler in `run_routing`, and it was counted as a *delivery*
# failure. So a notification would sit at 'failed' forever, retried on every run,
# failing every time, and looking from the outside like a broken webhook. The
# GREATEST costs nothing when the two agree.
_RECORD_ATTEMPT = """
    INSERT INTO salesops.notification_attempts (
        notification_id, attempt_number, outcome, provider,
        provider_message_id, status_code, error_message, latency_ms
    )
    SELECT
        %(notification_id)s,
        GREATEST(
            %(attempt_number)s,
            COALESCE((SELECT max(a.attempt_number) FROM salesops.notification_attempts a
                       WHERE a.notification_id = %(notification_id)s), 0) + 1),
        %(outcome)s, %(provider)s,
        %(provider_message_id)s, %(status_code)s, %(error_message)s, %(latency_ms)s
    RETURNING attempt_number
"""

# sent_at tracks the CURRENT status, not the row's whole history - it is cleared
# whenever the status moves away from 'sent', because a row that is no longer
# delivered must not still claim a delivery time.
#
# The path that reaches this is a resend of an already-delivered notification
# that then fails. Leaving sent_at populated violated
# `notifications_unsent_has_no_sent_at`, which is exactly the contradiction that
# constraint exists to prevent - it was found by pointing the real provider at
# an unreachable host, not by the fake one.
#
# Nothing is lost: notification_attempts keeps every attempt, including the
# successful one and its timestamp, so "it was delivered on Tuesday and a resend
# failed on Thursday" remains answerable.
_UPDATE_NOTIFICATION_RESULT = """
    UPDATE salesops.notifications
    SET status              = %(status)s,
        attempt_count       = %(attempt_count)s,
        provider            = %(provider)s,
        provider_message_id = %(provider_message_id)s,
        last_error          = %(last_error)s,
        sent_at             = CASE WHEN %(status)s = 'sent' THEN now() END
    WHERE notification_id = %(notification_id)s
"""

_INSERT_REVIEW = """
    INSERT INTO salesops.review_queue (
        anomaly_id, decision_id, hypothesis_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required,
        hypothesis_status, status
    ) VALUES (
        %(anomaly_id)s, %(decision_id)s, %(hypothesis_id)s, %(calendar_date)s,
        %(decision_version)s,
        %(severity)s, %(routing)s, %(decision)s,
        %(notification_allowed)s, %(human_review_required)s,
        %(hypothesis_status)s, 'pending'
    )
    ON CONFLICT (anomaly_id, decision_version) DO NOTHING
    RETURNING review_id
"""

_SELECT_REVIEWS = """
    SELECT * FROM salesops.review_queue_audit
    WHERE (%(status)s::text IS NULL OR status = %(status)s)
      AND (%(severity)s::text IS NULL OR queued_severity = %(severity)s)
    ORDER BY
        CASE queued_severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1 ELSE 2 END,
        created_at
    LIMIT %(limit)s
"""

_SELECT_REVIEW = "SELECT * FROM salesops.review_queue_audit WHERE review_id = %(review_id)s"

_SELECT_REVIEW_ROW = """
    SELECT review_id, status, assigned_to FROM salesops.review_queue
    WHERE review_id = %(review_id)s
"""

# The state machine lives in a trigger, so this is an ordinary UPDATE. An
# invalid transition raises rather than being silently accepted, and the
# transition is appended to review_events by the same trigger.
_UPDATE_REVIEW_STATUS = """
    UPDATE salesops.review_queue
    SET status       = %(status)s,
        assigned_to  = COALESCE(%(assigned_to)s, assigned_to),
        resolution   = COALESCE(%(resolution)s, resolution),
        review_notes = COALESCE(%(review_notes)s, review_notes)
    WHERE review_id = %(review_id)s
    RETURNING review_id, status, resolution, assigned_to, claimed_at, reviewed_at
"""

_SELECT_REVIEW_EVENTS = """
    SELECT from_status, to_status, actor, resolution, occurred_at
    FROM salesops.review_events
    WHERE review_id = %(review_id)s
    ORDER BY occurred_at, event_id
"""


# =============================================================================
# Stage 9: remediation
#
# Authorisation is a SELECT against Stage 8, never a calculation, and eligibility
# is a foreign key rather than a branch. Nothing below computes severity, reads a
# hypothesis to decide anything, or asks a model a question.
# =============================================================================

# Everything needed to snapshot an authorisation onto a remediation action, in
# one read: the review a human approved, the Stage 6 decision behind it, and the
# provenance of whatever Stage 7 produced.
#
# The Stage 6 columns come from the REVIEW's snapshot for the authorisation
# fields and from the live decision only for the descriptive evidence that goes
# into the request payload. The guard trigger re-checks the first set anyway.
_SELECT_REVIEW_FOR_REMEDIATION = """
    SELECT
        r.review_id,
        r.status,
        r.resolution,
        r.assigned_to,
        r.approved_by,
        r.approved_at,
        r.anomaly_id,
        r.decision_id,
        r.hypothesis_id,
        r.calendar_date,
        r.decision_version,
        r.severity,
        r.routing,
        r.decision,
        r.notification_allowed,
        r.human_review_required,
        r.hypothesis_status,
        d.decision_reason_code,
        d.anomaly_score,
        d.business_impact_tier,
        d.expected_net_revenue_usd,
        d.actual_net_revenue_usd,
        d.revenue_delta_usd,
        d.revenue_delta_pct,
        h.prompt_version AS hypothesis_prompt_version,
        h.model_name     AS hypothesis_model_name
    FROM salesops.review_queue      r
    JOIN salesops.anomaly_decisions d ON d.decision_id = r.decision_id
    LEFT JOIN salesops.anomaly_hypotheses h ON h.hypothesis_id = r.hypothesis_id
    WHERE r.review_id = %(review_id)s
"""

# in_review -> approved. Separate from _UPDATE_REVIEW_STATUS because approval is
# the one transition that records an approving actor, and because an UPDATE that
# could reach 'approved' from the generic review endpoints would make the
# authorisation boundary depend on which caller happened to pass which argument.
#
# The WHERE clause is the concurrency guard: two callers approving the same
# review means the second one updates nothing and reuses the first's approval.
_APPROVE_REVIEW = """
    UPDATE salesops.review_queue
    SET status       = 'approved',
        resolution   = %(resolution)s,
        approved_by  = %(actor)s,
        assigned_to  = COALESCE(assigned_to, %(actor)s),
        review_notes = COALESCE(%(review_notes)s, review_notes)
    WHERE review_id = %(review_id)s
      AND status    = 'in_review'
    RETURNING review_id, status, resolution, approved_by, approved_at, reviewed_at
"""

# DO NOTHING, not DO UPDATE. The idempotency key is
# (review, action type, decision version); a repeated approval must find the
# existing action rather than overwrite one that may already have executed.
_INSERT_REMEDIATION = """
    INSERT INTO salesops.remediation_actions (
        review_id, anomaly_id, decision_id, hypothesis_id, calendar_date,
        decision_version, severity, routing, decision,
        notification_allowed, human_review_required,
        decision_reason_code, decision_reason_codes,
        hypothesis_status, hypothesis_prompt_version, hypothesis_model_name,
        review_approved_by, review_approved_at, review_resolution,
        action_type, policy_version, request_payload, status
    ) VALUES (
        %(review_id)s, %(anomaly_id)s, %(decision_id)s, %(hypothesis_id)s,
        %(calendar_date)s,
        %(decision_version)s, %(severity)s, %(routing)s, %(decision)s,
        %(notification_allowed)s, %(human_review_required)s,
        %(decision_reason_code)s, %(decision_reason_codes)s,
        %(hypothesis_status)s, %(hypothesis_prompt_version)s, %(hypothesis_model_name)s,
        %(review_approved_by)s, %(review_approved_at)s, %(review_resolution)s,
        %(action_type)s, %(policy_version)s, %(request_payload)s, 'proposed'
    )
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING remediation_id
"""

_SELECT_REMEDIATION_BY_KEY = """
    SELECT remediation_id, status, attempt_count
    FROM salesops.remediation_actions
    WHERE review_id = %(review_id)s
      AND action_type = %(action_type)s
      AND decision_version = %(decision_version)s
"""

_SELECT_REMEDIATION_ROW = """
    SELECT remediation_id, status, attempt_count, severity, action_type,
           review_id, anomaly_id, calendar_date, review_approved_by,
           authorized_by, request_payload
    FROM salesops.remediation_actions
    WHERE remediation_id = %(remediation_id)s
"""

_SELECT_REMEDIATION = """
    SELECT * FROM salesops.remediation_audit WHERE remediation_id = %(remediation_id)s
"""

_SELECT_REMEDIATIONS = """
    SELECT * FROM salesops.remediation_audit
    WHERE (%(status)s::text      IS NULL OR status              = %(status)s)
      AND (%(severity)s::text    IS NULL OR authorized_severity = %(severity)s)
      AND (%(action_type)s::text IS NULL OR action_type         = %(action_type)s)
    ORDER BY
        CASE authorized_severity WHEN 'critical' THEN 0 ELSE 1 END,
        created_at DESC
    LIMIT %(limit)s
"""

_SELECT_EXECUTABLE = """
    SELECT remediation_id, calendar_date, severity, action_type, status,
           attempt_count, review_id, review_approved_by, authorized_by
    FROM salesops.remediation_pending_execution
    LIMIT %(limit)s
"""

# proposed -> approved. The status in the WHERE clause is what makes a repeated
# authorisation a no-op instead of a second event in the history.
_AUTHORIZE_REMEDIATION = """
    UPDATE salesops.remediation_actions
    SET status = 'approved', authorized_by = %(actor)s
    WHERE remediation_id = %(remediation_id)s AND status = 'proposed'
    RETURNING remediation_id, status, authorized_by, authorized_at
"""

_CLOSE_REMEDIATION = """
    UPDATE salesops.remediation_actions
    SET status = %(status)s, closed_reason = %(reason)s,
        authorized_by = COALESCE(authorized_by, %(actor)s)
    WHERE remediation_id = %(remediation_id)s
      AND status = ANY(%(from_statuses)s)
    RETURNING remediation_id, status, closed_reason
"""

# The claim, and the whole basis of "the provider is called once per logical
# action". Entering 'executing' is a conditional UPDATE: two concurrent callers
# race here, exactly one wins, and the loser finds no row and does nothing. No
# lock is held across the provider call, because a lock held across a network
# call is a lock held for however long the network feels like taking.
#
# The attempt-budget predicate is repeated here as well as in the trigger. The
# trigger is the enforcement; this is what makes a spent action fall out of the
# work set quietly instead of raising on every scheduled run.
#
# The actor is stamped here as well as on completion, so the `-> executing`
# event names whoever claimed it rather than falling back to whoever authorised
# it. Attributing a scheduled run to the finance manager who approved the action
# three days earlier would be a small lie, and an audit trail made of small lies
# is not one.
_CLAIM_FOR_EXECUTION = """
    UPDATE salesops.remediation_actions
    SET status = 'executing', executed_by = %(actor)s
    WHERE remediation_id = %(remediation_id)s
      AND status IN ('approved', 'failed')
      AND attempt_count < %(max_attempts)s
    RETURNING remediation_id, attempt_count, action_type, severity, review_id,
              anomaly_id, calendar_date, review_approved_by, authorized_by,
              request_payload
"""

_RECORD_REMEDIATION_ATTEMPT = """
    INSERT INTO salesops.remediation_attempts (
        remediation_id, attempt_number, outcome, provider,
        provider_reference, error_message, latency_ms, external_side_effect
    ) VALUES (
        %(remediation_id)s, %(attempt_number)s, %(outcome)s, %(provider)s,
        %(provider_reference)s, %(error_message)s, %(latency_ms)s,
        %(external_side_effect)s
    )
"""

# executed_at is set by the trigger on the transition, so it can never disagree
# with the status it describes.
_FINISH_EXECUTION = """
    UPDATE salesops.remediation_actions
    SET status             = %(status)s,
        attempt_count      = %(attempt_count)s,
        provider           = %(provider)s,
        provider_reference = %(provider_reference)s,
        last_error         = %(last_error)s,
        -- Cleared on failure. The claim stamps it so the transition is
        -- attributable while the provider call is in flight; an action that did
        -- not execute must not end up naming somebody as having executed it.
        -- The attempt row keeps the record of who tried.
        executed_by        = CASE WHEN %(status)s = 'executed'
                                  THEN %(actor)s END
    WHERE remediation_id = %(remediation_id)s AND status = 'executing'
    RETURNING remediation_id, status, attempt_count, executed_at
"""

_SELECT_REMEDIATION_EVENTS = """
    SELECT from_status, to_status, actor, reason, occurred_at
    FROM salesops.remediation_events
    WHERE remediation_id = %(remediation_id)s
    ORDER BY occurred_at, event_id
"""

_SELECT_REMEDIATION_ATTEMPTS = """
    SELECT attempt_number, outcome, provider, provider_reference,
           error_message, latency_ms, external_side_effect, attempted_at
    FROM salesops.remediation_attempts
    WHERE remediation_id = %(remediation_id)s
    ORDER BY attempt_number
"""

_SELECT_ACTION_VOCABULARY = """
    SELECT t.action_type, t.description, t.request_summary, t.mutates_external_state,
           COALESCE(
               (SELECT array_agg(e.severity ORDER BY e.severity)
                FROM salesops.remediation_action_eligibility e
                WHERE e.action_type = t.action_type
                  AND e.policy_version = %(policy_version)s),
               '{}') AS eligible_severities
    FROM salesops.remediation_action_types t
    ORDER BY t.action_type
"""

_SELECT_ELIGIBILITY = """
    SELECT severity, action_type, rationale
    FROM salesops.remediation_action_eligibility
    WHERE policy_version = %(policy_version)s
    ORDER BY severity, action_type
"""


# =============================================================================
# Stage 10: operational reliability
#
# Almost every statement here is a call into a V012 function or a SELECT from a
# V012 view. That is the design rather than thin plumbing: recovery has to be
# atomic and has to hold a lock while it decides, and both are properties of a
# single SQL statement rather than of a sequence of Python round trips.
#
# It also means the recovery rules can be exercised with psql during an incident,
# by somebody who cannot or should not be running the service.
# =============================================================================

_SELECT_HEALTH = """
    SELECT component, component_kind, status, reason_code,
           observed_value, threshold_value, measure, last_status, last_run_at, detail
    FROM salesops.operational_health
    ORDER BY CASE status
                 WHEN 'failed' THEN 0 WHEN 'degraded' THEN 1
                 WHEN 'warning' THEN 2 ELSE 3 END,
             component
"""

_SELECT_HEALTH_SUMMARY = "SELECT * FROM salesops.operational_health_summary"

_SELECT_OPERATIONAL_CONFIG = """
    SELECT config_key, config_value, unit, description, updated_at
    FROM salesops.operational_config ORDER BY config_key
"""

_SELECT_RETRY_QUEUE = """
    SELECT * FROM salesops.operational_retry_queue
    WHERE (%(entity_type)s::text IS NULL OR entity_type = %(entity_type)s)
      AND (NOT %(eligible_only)s OR retry_eligible)
    ORDER BY terminal, latest_failure_at DESC
    LIMIT %(limit)s
"""

_SELECT_REPLAY_CANDIDATES = """
    SELECT * FROM salesops.ingestion_replay_candidates ORDER BY first_failure_at
"""

_SELECT_REVIEW_AGEING = """
    SELECT * FROM salesops.review_ageing
    WHERE (%(bucket)s::text IS NULL OR ageing_bucket = %(bucket)s)
    ORDER BY age_hours DESC
"""

_SELECT_STALE_NOTIFICATIONS = (
    "SELECT * FROM salesops.stale_notifications ORDER BY idle_minutes DESC"
)

_SELECT_RETENTION_REPORT = """
    SELECT * FROM salesops.staging_retention_report ORDER BY disposition, processing_status
"""

_SELECT_OPERATIONAL_EVENTS = """
    SELECT event_id, event_type, entity_type, entity_id, from_state, to_state,
           actor, reason_code, detail, occurred_at
    FROM salesops.operational_events
    WHERE (%(entity_type)s::text IS NULL OR entity_type = %(entity_type)s)
      AND (%(entity_id)s::text   IS NULL OR entity_id   = %(entity_id)s)
    ORDER BY occurred_at DESC, event_id DESC
    LIMIT %(limit)s
"""

_RECOVER_STALE_RUNS = """
    SELECT * FROM salesops.recover_stale_runs(%(actor)s, %(dry_run)s)
"""

_RECOVER_STALE_REMEDIATION = """
    SELECT * FROM salesops.recover_stale_remediation(%(actor)s, %(dry_run)s)
"""

_REPLAY_BATCH = """
    SELECT * FROM salesops.replay_failed_batch(%(batch_id)s::uuid, %(actor)s)
"""

_PURGE_STAGING = """
    SELECT * FROM salesops.purge_staging(%(dry_run)s, %(actor)s)
"""

_RECONCILE_REMEDIATION = """
    SELECT * FROM salesops.reconcile_remediation(
        %(remediation_id)s, %(outcome)s, %(actor)s, %(evidence)s)
"""

_RECORD_MAINTENANCE_EVENT = """
    INSERT INTO salesops.operational_events
        (event_type, entity_type, entity_id, actor, reason_code, detail)
    VALUES ('maintenance_run', 'maintenance', %(entity_id)s, %(actor)s,
            %(reason_code)s, %(detail)s)
"""


def connect(dsn: str) -> psycopg.Connection:
    """Open a connection. The caller owns the transaction boundary."""
    return psycopg.connect(dsn, row_factory=dict_row)


def load_kpi_observations(connection: psycopg.Connection) -> list[KpiObservation]:
    """Read the whole KPI series, oldest first.

    The whole series, not a window: a baseline reaches back up to twenty
    observations and the table is one row per trading day, so reading it all is
    both simpler and cheaper than working out what a partial read would need.
    """
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_KPI)
        return [_to_observation(row) for row in cursor.fetchall()]


def load_detected_dates(connection: psycopg.Connection, detector_version: str) -> set[date]:
    """Dates already carrying a result for this detector version."""
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_ALREADY_DETECTED, {"detector_version": detector_version})
        return {row["calendar_date"] for row in cursor.fetchall()}


def save_results(
    connection: psycopg.Connection,
    results: Iterable[DetectionResult],
) -> int:
    """Upsert detection results. Returns the number written.

    One transaction for the batch: a failure part-way leaves the previous
    results intact rather than a half-updated mixture of two runs.
    """
    payloads = [_to_payload(result) for result in results]
    if not payloads:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(_UPSERT_ANOMALY, payloads)
    return len(payloads)


def _to_observation(row: dict) -> KpiObservation:
    """Map a KPI row onto the value type.

    Decimal -> float happens here and only here. NUMERIC stays authoritative in
    the warehouse; what this service computes are dimensionless statistics.
    """
    return KpiObservation(
        calendar_date=row["calendar_date"],
        day_of_week=int(row["day_of_week"]),
        is_weekend=bool(row["is_weekend"]),
        orders_count=int(row["orders_count"]),
        net_revenue_usd=_optional_float(row["net_revenue_usd"]),
        average_order_value_usd=_optional_float(row["average_order_value_usd"]),
        refund_rate=_optional_float(row["refund_rate"]),
        orders_pending_fx=int(row["orders_pending_fx"]),
        fx_completeness_pct=float(row["fx_completeness_pct"]),
        is_complete=bool(row["is_complete"]),
    )


def _to_payload(result: DetectionResult) -> dict:
    """Flatten a result into the column set, one signal per column pair."""
    payload = {
        "calendar_date": result.calendar_date,
        "detector_version": result.detector_version,
        "anomaly_score": result.anomaly_score,
        "is_anomaly": result.is_anomaly,
        "baseline_status": str(result.baseline_status),
        "baseline_kind": str(result.baseline_kind) if result.baseline_kind else None,
        "baseline_size": result.baseline_size,
        "dominant_signal": result.dominant_signal,
        "signal_count": result.signal_count,
    }

    for name, deviation_column in (
        ("revenue", "revenue_deviation_pct"),
        ("aov", "aov_deviation_pct"),
        ("refund", "refund_rate_deviation"),
        ("orders", "orders_deviation_pct"),
    ):
        signal = result.signal(name)
        payload[deviation_column] = signal.deviation if signal else None
        payload[f"{name}_robust_z"] = signal.robust_z if signal else None

    # The absolute counterpart of revenue_deviation_pct: what this day was
    # EXPECTED to earn. Already computed by the scorer; V007 added the column so
    # Stage 6 can measure business impact against the same number this stage
    # judged against, rather than inverting the percentage to recover it.
    #
    # Nothing about the algorithm changes, so DETECTOR_VERSION is not bumped -
    # a bump would falsely claim these results are not comparable with earlier ones.
    revenue = result.signal("revenue")
    payload["revenue_baseline_median"] = revenue.baseline_median if revenue else None

    return payload


def _optional_float(value) -> float | None:
    return None if value is None else float(value)


# =============================================================================
# Stage 7
# =============================================================================

#: How much history the model is shown. Enough to see the shape of a normal
#: same-weekday, not enough to bury the anomaly - and small enough that a
#: reviewer can check every number in the prompt by hand.
SAME_WEEKDAY_HISTORY_LIMIT = 6
PRECEDING_DAY_HISTORY_LIMIT = 5


def load_actionable_decisions(
    connection: psycopg.Connection,
    decision_version: str,
    prompt_version: str,
    model_name: str,
) -> list[dict]:
    """Stage 6 decisions eligible for analysis, newest generation state included.

    Every row carries `already_analysed` for this exact
    (decision version, prompt version, model) combination, which is what makes a
    re-run cheap and a regeneration explicit.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            _SELECT_ELIGIBLE_DECISIONS,
            {
                "decision_version": decision_version,
                "prompt_version": prompt_version,
                "model_name": model_name,
            },
        )
        return list(cursor.fetchall())


def load_decision_reason_codes(connection: psycopg.Connection, decision_id: int) -> tuple[str, ...]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REASON_CODES, {"decision_id": decision_id})
        return tuple(row["reason_code"] for row in cursor.fetchall())


def load_history(
    connection: psycopg.Connection,
    calendar_date: date,
    same_weekday_limit: int = SAME_WEEKDAY_HISTORY_LIMIT,
    preceding_limit: int = PRECEDING_DAY_HISTORY_LIMIT,
) -> tuple[list[dict], list[dict]]:
    """Prior same-weekday observations and prior consecutive days.

    Both queries are bounded by `calendar_date < target`. Nothing later than the
    anomaly can reach the model through this path.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            _SELECT_SAME_WEEKDAY_HISTORY,
            {"calendar_date": calendar_date, "limit": same_weekday_limit},
        )
        same_weekday = list(cursor.fetchall())

        cursor.execute(
            _SELECT_PRECEDING_DAYS,
            {"calendar_date": calendar_date, "limit": preceding_limit},
        )
        preceding = list(cursor.fetchall())

    return same_weekday, preceding


def save_hypothesis(connection: psycopg.Connection, payload: dict) -> int | None:
    """Insert one hypothesis. Returns its id, or None if one already existed.

    None is a normal outcome, not an error: it means another run got there first,
    and the existing analysis stands.
    """
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_HYPOTHESIS, payload)
        row = cursor.fetchone()
        return row["hypothesis_id"] if row else None


def load_routable_decisions(
    connection: psycopg.Connection,
    decision_version: str,
    channel: str,
    recipient: str,
) -> list[dict]:
    """Actionable Stage 6 decisions, with their routing already answered.

    `needs_notification` and `needs_review` come from Stage 6's own columns.
    Stage 8 never asks "is this severe enough" - that question was settled two
    stages ago and asking it again is how two layers start to disagree.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            _SELECT_ROUTABLE_DECISIONS,
            {
                "decision_version": decision_version,
                "channel": channel,
                "recipient": recipient,
            },
        )
        return list(cursor.fetchall())


def claim_notification(connection: psycopg.Connection, payload: dict) -> dict:
    """Create the notification row, or return the existing one.

    Returns `{notification_id, status, attempt_count}`. A row that already exists
    is returned untouched, which is what makes a rerun safe.
    """
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_NOTIFICATION, payload)
        row = cursor.fetchone()
        if row:
            return {
                "notification_id": row["notification_id"],
                "status": "pending",
                "attempt_count": 0,
                "created": True,
            }

        cursor.execute(_SELECT_NOTIFICATION, {
            "anomaly_id": payload["anomaly_id"],
            "decision_version": payload["decision_version"],
            "channel": payload["channel"],
            "recipient": payload["recipient"],
        })
        existing = cursor.fetchone()
        return {**existing, "created": False}


def record_attempt(connection: psycopg.Connection, payload: dict) -> int:
    """Record one delivery attempt. Returns the number it was actually given."""
    with connection.cursor() as cursor:
        cursor.execute(_RECORD_ATTEMPT, payload)
        row = cursor.fetchone()
        return int(row["attempt_number"]) if row else int(payload["attempt_number"])


def update_notification_result(connection: psycopg.Connection, payload: dict) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_UPDATE_NOTIFICATION_RESULT, payload)


def create_review(connection: psycopg.Connection, payload: dict) -> int | None:
    """Queue a review item. Returns its id, or None if one already existed."""
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_REVIEW, payload)
        row = cursor.fetchone()
        return row["review_id"] if row else None


def list_reviews(
    connection: psycopg.Connection,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            _SELECT_REVIEWS,
            {"status": status, "severity": severity, "limit": limit},
        )
        return list(cursor.fetchall())


def get_review(connection: psycopg.Connection, review_id: int) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REVIEW, {"review_id": review_id})
        return cursor.fetchone()


def get_review_row(connection: psycopg.Connection, review_id: int) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REVIEW_ROW, {"review_id": review_id})
        return cursor.fetchone()


def update_review_status(connection: psycopg.Connection, payload: dict) -> dict | None:
    """Move a review item along the state machine.

    No transition logic here on purpose: the trigger owns it. An invalid move
    raises, so a caller cannot reach a state by writing to the column directly.
    """
    with connection.cursor() as cursor:
        cursor.execute(_UPDATE_REVIEW_STATUS, payload)
        return cursor.fetchone()


def review_events(connection: psycopg.Connection, review_id: int) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REVIEW_EVENTS, {"review_id": review_id})
        return list(cursor.fetchall())


# =============================================================================
# Stage 9
# =============================================================================

#: Three attempts, matching Stage 8's delivery budget and the trigger that
#: enforces it. Declared in both places on purpose: the database is the
#: enforcement, and this is what keeps a spent action out of the work set so it
#: does not raise on every scheduled run.
MAX_EXECUTION_ATTEMPTS = 3


def load_review_for_remediation(connection: psycopg.Connection, review_id: int) -> dict | None:
    """The review, its Stage 6 decision, and its Stage 7 provenance in one read."""
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REVIEW_FOR_REMEDIATION, {"review_id": review_id})
        return cursor.fetchone()


def approve_review(connection: psycopg.Connection, payload: dict) -> dict | None:
    """in_review -> approved. Returns None if the review was not in_review.

    None is not an error on its own: an already-approved review returns None
    here and the caller reuses the existing approval, which is what makes a
    repeated approval idempotent rather than a second authorisation.
    """
    with connection.cursor() as cursor:
        cursor.execute(_APPROVE_REVIEW, payload)
        return cursor.fetchone()


def create_remediation_action(connection: psycopg.Connection, payload: dict) -> dict:
    """Create the action, or return the existing one for this idempotency key.

    Returns `{remediation_id, status, attempt_count, created}`. An action that
    already exists is returned untouched - including one that has already
    executed, which is precisely the case a second approval must not disturb.
    """
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_REMEDIATION, payload)
        row = cursor.fetchone()
        if row:
            return {
                "remediation_id": row["remediation_id"],
                "status": "proposed",
                "attempt_count": 0,
                "created": True,
            }

        cursor.execute(_SELECT_REMEDIATION_BY_KEY, {
            "review_id": payload["review_id"],
            "action_type": payload["action_type"],
            "decision_version": payload["decision_version"],
        })
        existing = cursor.fetchone()
        return {**existing, "created": False}


def authorize_remediation(
    connection: psycopg.Connection, remediation_id: int, actor: str
) -> dict | None:
    """proposed -> approved. None if it was not proposed."""
    with connection.cursor() as cursor:
        cursor.execute(
            _AUTHORIZE_REMEDIATION, {"remediation_id": remediation_id, "actor": actor}
        )
        return cursor.fetchone()


def close_remediation(
    connection: psycopg.Connection,
    remediation_id: int,
    status: str,
    actor: str,
    reason: str,
    from_statuses: Sequence[str],
) -> dict | None:
    """Move an action to `rejected` or `cancelled`. None if it was not eligible."""
    with connection.cursor() as cursor:
        cursor.execute(_CLOSE_REMEDIATION, {
            "remediation_id": remediation_id,
            "status": status,
            "actor": actor,
            "reason": reason,
            "from_statuses": list(from_statuses),
        })
        return cursor.fetchone()


def claim_remediation_for_execution(
    connection: psycopg.Connection,
    remediation_id: int,
    actor: str = "unknown",
    max_attempts: int = MAX_EXECUTION_ATTEMPTS,
) -> dict | None:
    """Claim an authorised action for execution. None if it was not claimable.

    None covers every reason an action must not run: still `proposed` and so
    unauthorised, already `executing` under another caller, already `executed`,
    rejected, cancelled, or out of retry budget. The caller does not need to
    distinguish them to behave correctly - it simply does not call the provider.
    """
    with connection.cursor() as cursor:
        cursor.execute(_CLAIM_FOR_EXECUTION, {
            "remediation_id": remediation_id,
            "actor": actor,
            "max_attempts": max_attempts,
        })
        return cursor.fetchone()


def record_remediation_attempt(connection: psycopg.Connection, payload: dict) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_RECORD_REMEDIATION_ATTEMPT, payload)


def finish_remediation_execution(connection: psycopg.Connection, payload: dict) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(_FINISH_EXECUTION, payload)
        return cursor.fetchone()


def get_remediation_row(connection: psycopg.Connection, remediation_id: int) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REMEDIATION_ROW, {"remediation_id": remediation_id})
        return cursor.fetchone()


def get_remediation(connection: psycopg.Connection, remediation_id: int) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REMEDIATION, {"remediation_id": remediation_id})
        return cursor.fetchone()


def list_remediations(
    connection: psycopg.Connection,
    status: str | None = None,
    severity: str | None = None,
    action_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REMEDIATIONS, {
            "status": status,
            "severity": severity,
            "action_type": action_type,
            "limit": limit,
        })
        return list(cursor.fetchall())


def list_executable_remediations(connection: psycopg.Connection, limit: int = 100) -> list[dict]:
    """Authorised actions waiting to run, worst first.

    Read from `remediation_pending_execution`, which already applies the retry
    budget and excludes anything still `proposed`. The scheduled workflow never
    selects work by severity itself and never sees an unauthorised action.
    """
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_EXECUTABLE, {"limit": limit})
        return list(cursor.fetchall())


def remediation_events(connection: psycopg.Connection, remediation_id: int) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REMEDIATION_EVENTS, {"remediation_id": remediation_id})
        return list(cursor.fetchall())


def remediation_attempts(connection: psycopg.Connection, remediation_id: int) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_REMEDIATION_ATTEMPTS, {"remediation_id": remediation_id})
        return list(cursor.fetchall())


def load_action_vocabulary(connection: psycopg.Connection, policy_version: str) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_ACTION_VOCABULARY, {"policy_version": policy_version})
        return list(cursor.fetchall())


def load_action_eligibility(connection: psycopg.Connection, policy_version: str) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(_SELECT_ELIGIBILITY, {"policy_version": policy_version})
        return list(cursor.fetchall())


# =============================================================================
# Stage 10
# =============================================================================


def _rows(connection: psycopg.Connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return list(cursor.fetchall())


def _row(connection: psycopg.Connection, sql: str, params: dict | None = None) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchone()


def operational_health(connection: psycopg.Connection) -> list[dict]:
    return _rows(connection, _SELECT_HEALTH)


def operational_health_summary(connection: psycopg.Connection) -> dict:
    return _row(connection, _SELECT_HEALTH_SUMMARY) or {}


def operational_config(connection: psycopg.Connection) -> list[dict]:
    return _rows(connection, _SELECT_OPERATIONAL_CONFIG)


def retry_queue(
    connection: psycopg.Connection,
    entity_type: str | None = None,
    eligible_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    return _rows(connection, _SELECT_RETRY_QUEUE, {
        "entity_type": entity_type, "eligible_only": eligible_only, "limit": limit,
    })


def replay_candidates(connection: psycopg.Connection) -> list[dict]:
    return _rows(connection, _SELECT_REPLAY_CANDIDATES)


def review_ageing(connection: psycopg.Connection, bucket: str | None = None) -> list[dict]:
    return _rows(connection, _SELECT_REVIEW_AGEING, {"bucket": bucket})


def stale_notifications(connection: psycopg.Connection) -> list[dict]:
    return _rows(connection, _SELECT_STALE_NOTIFICATIONS)


def retention_report(connection: psycopg.Connection) -> list[dict]:
    return _rows(connection, _SELECT_RETENTION_REPORT)


def operational_events(
    connection: psycopg.Connection,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    return _rows(connection, _SELECT_OPERATIONAL_EVENTS, {
        "entity_type": entity_type, "entity_id": entity_id, "limit": limit,
    })


def recover_stale_runs(
    connection: psycopg.Connection,
    actor: str = "stage10-recovery",
    dry_run: bool = False,
) -> list[dict]:
    """Close runs abandoned at 'running'. Repeats no work."""
    return _rows(connection, _RECOVER_STALE_RUNS, {"actor": actor, "dry_run": dry_run})


def recover_stale_remediation(
    connection: psycopg.Connection,
    actor: str = "stage10-recovery",
    dry_run: bool = False,
) -> list[dict]:
    """Move crashed executions to 'execution_unknown'. Calls no provider."""
    return _rows(connection, _RECOVER_STALE_REMEDIATION, {"actor": actor, "dry_run": dry_run})


def replay_failed_batch(
    connection: psycopg.Connection,
    batch_id: str,
    actor: str = "stage10-recovery",
) -> dict:
    """Replay a failed staging batch into a new one.

    Takes a batch id and nothing else. The payloads come from the database, so
    there is no path by which a caller can inject an order into the warehouse
    through this endpoint - which is the whole reason it is shaped this way.
    """
    return _row(connection, _REPLAY_BATCH, {"batch_id": batch_id, "actor": actor}) or {}


def purge_staging(
    connection: psycopg.Connection,
    dry_run: bool = True,
    actor: str = "stage10-recovery",
) -> dict:
    return _row(connection, _PURGE_STAGING, {"dry_run": dry_run, "actor": actor}) or {}


def reconcile_remediation(
    connection: psycopg.Connection,
    remediation_id: int,
    outcome: str,
    actor: str,
    evidence: str,
) -> dict:
    return _row(connection, _RECONCILE_REMEDIATION, {
        "remediation_id": remediation_id, "outcome": outcome,
        "actor": actor, "evidence": evidence,
    }) or {}


def record_maintenance_event(connection: psycopg.Connection, payload: dict) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_RECORD_MAINTENANCE_EVENT, payload)


def delete_hypothesis(
    connection: psycopg.Connection,
    anomaly_id: int,
    decision_version: str,
    prompt_version: str,
    model_name: str,
) -> int:
    """Remove an existing analysis so it can be regenerated. Returns rows deleted.

    Only ever called when regeneration is explicitly requested. Nothing in the
    scheduled path can reach it, which is what stops a nightly run quietly
    replacing reasoning a human has already read.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            _DELETE_HYPOTHESIS,
            {
                "anomaly_id": anomaly_id,
                "decision_version": decision_version,
                "prompt_version": prompt_version,
                "model_name": model_name,
            },
        )
        return cursor.rowcount
