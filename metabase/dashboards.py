"""The dashboard catalogue: what Metabase is asked to render, and how.

Declarative on purpose. Every card here is a SELECT against a V013 view, so the
numbers on the dashboard and the numbers in the database are the same numbers by
construction rather than by discipline - there is no arithmetic in this file,
and no threshold, and no severity.

Two rules the catalogue enforces, both checked by tests rather than by review:

  * no card contains a database password, a webhook URL or an API key. Cards are
    SQL and layout; credentials live in the environment and reach Metabase only
    through the connection it stores;

  * no EXECUTIVE card selects a free-text column written by the language model.
    LLM_TEXT_COLUMNS below is the list, and the investigation dashboard is the
    only place any of them appears - underneath the deterministic evidence, with
    the model named and the lack of verification stated.

Grid: Metabase lays dashboards out on 24 columns. Sizes below are (width, height)
in grid units.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# The columns that are model output. Named once, here, so the rule that keeps
# them off the executive dashboard has exactly one definition.
# ---------------------------------------------------------------------------
LLM_TEXT_COLUMNS = (
    "llm_summary",
    "llm_primary_hypothesis",
    "llm_alternative_hypotheses",
    "llm_supporting_evidence",
    "llm_recommended_checks",
    "llm_missing_evidence",
)

COLLECTION_NAME = "Sales Ops Intelligence"
COLLECTION_DESCRIPTION = (
    "Stage 11. Read-only views over the pipeline: observed facts, statistical "
    "signals, deterministic decisions, unverified model hypotheses, human "
    "review, remediation and operational health - each labelled as what it is."
)

DATABASE_NAME = "Sales Ops Analytics (read-only)"

# The dashboard parameter that drives the investigation view. Fixed id so that
# re-provisioning updates the same parameter instead of adding a second one.
INCIDENT_DATE_PARAM_ID = "a1b2c3d4"
INCIDENT_DATE_TAG_ID = "0f2c9a11-0000-4000-8000-00000000d001"

# The order generator anchors its 90-day window to today unless
# MOCK_API_HISTORY_END_DATE pins it, so the incident bootstrap.sh injects lands
# on a different calendar date every day. A literal here would drift away from
# the data within a day of being written, and the panel would default to a date
# with nothing on it.
#
# bootstrap.sh resolves the date once, injects into it and records it in .env.
# That file is read here rather than only the environment, because provisioning
# is meant to be re-runnable on its own - and a dashboard rebuilt on Tuesday
# must still point at Monday's incident.
INCIDENT_DATE_OFFSET_DAYS = 10


def _recorded_incident_date() -> str | None:
    """The injected date, from the environment or from .env. None if neither."""
    from_env = os.environ.get("SALESOPS_INCIDENT_DATE", "").strip()
    if from_env:
        return from_env
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "SALESOPS_INCIDENT_DATE":
            return value.strip() or None
    return None


DEFAULT_INCIDENT_DATE = _recorded_incident_date() or (
    date.today() - timedelta(days=INCIDENT_DATE_OFFSET_DAYS)
).isoformat()

_INCIDENT_DATE_TAG = {
    "incident_date": {
        "id": INCIDENT_DATE_TAG_ID,
        "name": "incident_date",
        "display-name": "Incident date",
        "type": "date",
        "required": True,
        "default": DEFAULT_INCIDENT_DATE,
    }
}


# ===========================================================================
# The visual vocabulary
# ===========================================================================
# Colour carries meaning here or it is not used. There is no decorative palette:
# every hex below is bound to a state the pipeline actually stores, and the two
# vocabularies Stage 6 and Stage 10 keep apart stay apart on screen too - an
# anomaly is critical/major/minor, a component is healthy/warning/degraded, and
# no colour is shared between a severity and a health status by accident.
CRITICAL = "#C4304A"   # a deep red; loud enough to find, not an alarm klaxon
MAJOR    = "#D97706"   # amber - needs a person, but not tonight
MINOR    = "#5B677D"   # slate - real, and deliberately quiet
GOOD     = "#0F9D6E"   # resolved, healthy, executed
NEUTRAL  = "#64748B"   # states that are neither good nor bad

# Series colours. The measured line is the subject; the baseline it is judged
# against is a reference and recedes, because drawing them as equals is what
# makes a chart hard to read at a glance.
MEASURED = "#4C7DF0"
BASELINE = "#94A3B8"


def _fmt(column, **settings):
    """A column_settings entry. Metabase keys these by a JSON-encoded ref."""
    return {json.dumps(["name", column]): settings}


def _money(column, decimals=0, **extra):
    """Currency formatting. `extra` merges INTO the same column entry.

    Both helpers key on the column name, so composing them by merging two dicts
    would silently drop whichever came first - the currency symbol would vanish
    the moment anyone asked for a mini bar as well.
    """
    return _fmt(column, number_style="currency", currency="USD",
                currency_style="symbol", currency_in_header=False,
                decimals=decimals, **extra)


def _percent(column, decimals=1):
    return _fmt(column, number_style="percent", decimals=decimals)


def _plain(column, decimals=0):
    return _fmt(column, number_style="decimal", decimals=decimals)


def _paint(column, value, colour):
    """Colour a cell when it holds exactly `value`."""
    return {"type": "single", "columns": [column], "operator": "=",
            "value": value, "color": colour, "highlight_row": False}


def _severity_colours(column):
    """Stage 6's vocabulary. Used wherever a severity is rendered."""
    return [
        _paint(column, "critical", CRITICAL),
        _paint(column, "major", MAJOR),
        _paint(column, "minor", MINOR),
        _paint(column, "none", NEUTRAL),
    ]


def _health_colours(column):
    """Stage 10's vocabulary, which shares no word with Stage 6's."""
    return [
        _paint(column, "failed", CRITICAL),
        _paint(column, "degraded", CRITICAL),
        _paint(column, "warning", MAJOR),
        _paint(column, "healthy", GOOD),
    ]


def _card(key, name, sql, display="table", description="", settings=None, tags=None):
    return {
        "key": key,
        "name": name,
        "description": description,
        "display": display,
        "sql": sql,
        "visualization_settings": settings or {},
        "template_tags": tags or {},
    }


# ===========================================================================
# Executive cards
# ===========================================================================
def _headline_tile(key, name, metric_key, description, value_settings):
    """One KPI, one number, one card.

    The five headline figures used to be a single eight-column table, which put
    the most important number on the dashboard behind a horizontal scrollbar and
    gave it the same visual weight as its own metadata. A number a person is
    meant to read at a glance should be rendered as a number.
    """
    return _card(
        key, name,
        f"""
        SELECT metric_value AS "{name}"
        FROM salesops.exec_headline_kpis
        WHERE metric_key = '{metric_key}'
        """,
        display="scalar",
        description=description,
        settings={"column_settings": value_settings, "scalar.field": name},
    )


CARDS = [
    _headline_tile(
        "exec_kpi_revenue", "Net revenue", "net_revenue_usd",
        "The latest day whose revenue is complete - a day still missing exchange "
        "rates would understate it, so it is not shown here until it is whole.",
        _money("Net revenue"),
    ),
    _headline_tile(
        "exec_kpi_orders", "Orders", "orders_count",
        "Orders on the same complete day.",
        _plain("Orders"),
    ),
    _headline_tile(
        "exec_kpi_aov", "Average order value", "average_order_value_usd",
        "Net revenue divided by orders, as Stage 4 stored it.",
        _money("Average order value", decimals=2),
    ),
    _headline_tile(
        "exec_kpi_refund_rate", "Refund rate", "refund_rate",
        "Refunds as a share of gross revenue.",
        _percent("Refund rate", decimals=2),
    ),
    _card(
        "exec_headline",
        "How each figure compares",
        """
        SELECT metric_label     AS "Metric",
               metric_value     AS "Value",
               comparison_value AS "Compared with",
               comparison_label AS "Comparison",
               delta_pct        AS "Delta %",
               as_of_date       AS "As of (complete day)"
        FROM salesops.exec_headline_kpis
        ORDER BY metric_rank
        """,
        description=(
            "Observed facts only. 'As of' is the latest day whose revenue is "
            "complete. Each metric names the thing it was compared against, "
            "because a percentage without its comparison is not a fact."
        ),
        settings={
            "column_settings": {
                **_plain("Delta %", decimals=2),
            },
            "table.column_formatting": [
                {"type": "range", "columns": ["Delta %"],
                 "colors": [CRITICAL, "#FFFFFF", GOOD], "min_type": "custom",
                 "max_type": "custom", "min_value": -50, "max_value": 50},
            ],
        },
    ),
    _card(
        "exec_revenue_vs_baseline",
        "Net revenue against its baseline",
        """
        SELECT calendar_date              AS "Date",
               net_revenue_usd            AS "Net revenue (USD)",
               baseline_net_revenue_usd   AS "Day-of-week baseline (USD)"
        FROM salesops.exec_kpi_daily
        WHERE calendar_date >= (SELECT max(calendar_date) - 60 FROM salesops.exec_kpi_daily)
        ORDER BY calendar_date
        """,
        display="line",
        description=(
            "The measured series and the baseline Stage 5 judged it against. "
            "Both are stored values; neither is recomputed here."
        ),
        settings={
            "graph.dimensions": ["Date"],
            "graph.metrics": ["Net revenue (USD)", "Day-of-week baseline (USD)"],
            # The measurement is the subject and the baseline is the reference it
            # is read against. Drawn in equally saturated colours the eye cannot
            # tell which is which, and the chart becomes two competing lines
            # rather than one line and its context.
            "series_settings": {
                "Net revenue (USD)": {"color": MEASURED, "line.width": 2},
                "Day-of-week baseline (USD)": {
                    "color": BASELINE, "line.style": "dashed", "line.width": 1,
                },
            },
            "graph.x_axis.title_text": "",
            "graph.y_axis.title_text": "",
            "graph.show_values": False,
            "column_settings": {**_money("Net revenue (USD)"),
                                **_money("Day-of-week baseline (USD)")},
        },
    ),
    _card(
        "exec_orders",
        "Orders per day, last 60 days",
        """
        SELECT calendar_date AS "Date", orders_count AS "Orders"
        FROM salesops.exec_kpi_daily
        WHERE calendar_date >= (SELECT max(calendar_date) - 60 FROM salesops.exec_kpi_daily)
        ORDER BY calendar_date
        """,
        display="bar",
        settings={
            "graph.dimensions": ["Date"], "graph.metrics": ["Orders"],
            "series_settings": {"Orders": {"color": MEASURED}},
            "graph.x_axis.title_text": "", "graph.y_axis.title_text": "",
            "graph.show_values": False,
        },
    ),
    _card(
        "exec_aov",
        "Average order value, last 60 days",
        """
        SELECT calendar_date AS "Date",
               average_order_value_usd AS "AOV (USD)"
        FROM salesops.exec_kpi_daily
        WHERE calendar_date >= (SELECT max(calendar_date) - 60 FROM salesops.exec_kpi_daily)
        ORDER BY calendar_date
        """,
        display="line",
        settings={
            "graph.dimensions": ["Date"], "graph.metrics": ["AOV (USD)"],
            "series_settings": {"AOV (USD)": {"color": MEASURED, "line.width": 2}},
            "graph.x_axis.title_text": "", "graph.y_axis.title_text": "",
            "column_settings": _money("AOV (USD)", decimals=0),
        },
    ),
    _card(
        "exec_refund_rate",
        "Refund rate, last 60 days",
        """
        SELECT calendar_date AS "Date", refund_rate AS "Refund rate"
        FROM salesops.exec_kpi_daily
        WHERE calendar_date >= (SELECT max(calendar_date) - 60 FROM salesops.exec_kpi_daily)
        ORDER BY calendar_date
        """,
        display="line",
        settings={
            "graph.dimensions": ["Date"], "graph.metrics": ["Refund rate"],
            # Amber, not the measured blue: a refund rate rising is the one series
            # on this row where up is bad.
            "series_settings": {"Refund rate": {"color": MAJOR, "line.width": 2}},
            "graph.x_axis.title_text": "", "graph.y_axis.title_text": "",
            "column_settings": _percent("Refund rate", decimals=1),
        },
    ),
    _card(
        "exec_severity",
        "Anomalies by severity",
        """
        SELECT severity                   AS "Severity",
               anomaly_count              AS "Days",
               actionable_count           AS "Actionable",
               review_required_count      AS "Needs a human",
               absolute_revenue_delta_usd AS "Absolute revenue delta (USD)"
        FROM salesops.exec_anomaly_severity_summary
        ORDER BY severity_rank
        """,
        description=(
            "Stage 6 severity. Revenue delta is summed as an ABSOLUTE value: a "
            "shortfall and an unexplained surplus are both anomalies and netting "
            "them off would hide both."
        ),
        settings={
            "table.column_formatting": _severity_colours("Severity"),
            "column_settings": {
                **_money("Absolute revenue delta (USD)"),
                # A count rendered as a bar as well as a number: the shape of the
                # distribution is the point, and four rows of digits do not have
                # a shape.
                **_fmt("Days", show_mini_bar=True),
            },
        },
    ),
    _card(
        "exec_actionable",
        "Actionable anomalies",
        """
        SELECT calendar_date           AS "Date",
               severity                AS "Severity",
               routing                 AS "Routing",
               decision_reason_code    AS "Reason code",
               revenue_delta_usd       AS "Revenue delta (USD)",
               furthest_layer_reached  AS "Furthest layer reached",
               review_status           AS "Review",
               remediation_status      AS "Remediation",
               hypothesis_available    AS "Hypothesis exists"
        FROM salesops.exec_actionable_anomalies
        ORDER BY calendar_date DESC
        """,
        description=(
            "'Hypothesis exists' is a boolean on purpose. What the model said is "
            "on the investigation dashboard, below the evidence, not here."
        ),
        settings={
            "table.column_formatting": _severity_colours("Severity"),
            "column_settings": _money("Revenue delta (USD)", show_mini_bar=True),
        },
    ),
    _card(
        "exec_needs_review",
        "Critical and major anomalies awaiting a human",
        """
        SELECT calendar_date        AS "Date",
               severity             AS "Severity",
               decision_reason_code AS "Reason code",
               revenue_delta_usd    AS "Revenue delta (USD)",
               review_status        AS "Review state",
               review_assigned_to   AS "Assigned to",
               review_approved_by   AS "Approved by"
        FROM salesops.exec_actionable_anomalies
        WHERE human_review_required
          AND (review_status IS NULL OR review_status IN ('pending', 'in_review'))
        ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'major' THEN 2 ELSE 3 END,
                 calendar_date DESC
        """,
        description=(
            "human_review_required is Stage 6's own column, not a severity filter "
            "applied here."
        ),
        settings={
            "table.column_formatting": _severity_colours("Severity"),
            "column_settings": _money("Revenue delta (USD)", show_mini_bar=True),
        },
    ),
    _card(
        "exec_notifications",
        "Notification delivery",
        """
        SELECT notification_status AS "Status",
               severity            AS "Severity",
               notification_count  AS "Notifications",
               total_attempts      AS "Attempts",
               with_error_count    AS "With an error",
               newest_sent_at      AS "Most recent delivery"
        FROM salesops.exec_notification_status
        ORDER BY notification_status, severity
        """,
    ),
    _card(
        "exec_reviews",
        "Review queue",
        """
        SELECT review_status           AS "Review state",
               anomaly_severity        AS "Anomaly severity",
               review_count            AS "Items",
               ageing_warning          AS "Ageing: warning",
               ageing_overdue          AS "Ageing: overdue",
               ageing_critical_overdue AS "Ageing: critical_overdue",
               approved_count          AS "Approved",
               oldest_created_at       AS "Oldest item"
        FROM salesops.exec_review_status
        ORDER BY review_status, anomaly_severity
        """,
        description=(
            "Two vocabularies side by side and deliberately not mixed: "
            "critical/major/minor is the ANOMALY; warning/overdue/critical_overdue "
            "is how long the REVIEW has waited."
        ),
        settings={"table.column_formatting": _severity_colours("Anomaly severity")},
    ),
    _card(
        "exec_remediation",
        "Remediation",
        """
        SELECT remediation_status AS "State",
               action_type        AS "Action",
               anomaly_severity   AS "Anomaly severity",
               action_count       AS "Actions",
               authorized_count   AS "Authorised",
               executed_count     AS "Executed",
               total_attempts     AS "Attempts",
               newest_executed_at AS "Most recent execution"
        FROM salesops.exec_remediation_status
        ORDER BY remediation_status, action_type
        """,
        description=(
            "Authorised and executed are counted separately because they are "
            "different things. The gap between them is Stage 9 working."
        ),
    ),
    _card(
        "exec_health",
        "Pipeline health",
        """
        SELECT component        AS "Component",
               component_kind   AS "Kind",
               health_status    AS "Health",
               reason_code      AS "Reason",
               observed_value   AS "Observed",
               threshold_value  AS "Threshold",
               measure          AS "Measure",
               last_run_at      AS "Last run"
        FROM salesops.exec_pipeline_health
        ORDER BY status_rank, component
        """,
        description=(
            "healthy | warning | degraded | failed. A pipeline's health is not an "
            "anomaly severity and the two vocabularies share no word."
        ),
        settings={"table.column_formatting": _health_colours("Health")},
    ),
    _card(
        "exec_attention",
        "Waiting on someone",
        """
        SELECT entity_type          AS "What",
               entity_id            AS "Id",
               disposition          AS "Disposition",
               failure_reason       AS "Why",
               attempt_count        AS "Attempts",
               max_attempts         AS "Limit",
               retry_eligible       AS "Retryable",
               ageing_bucket        AS "Review ageing",
               hours_since_activity AS "Hours since activity"
        FROM salesops.ops_attention_items
        ORDER BY hours_since_activity DESC NULLS LAST
        """,
        description=(
            "Stale runs, undelivered notifications, unknown executions, replayable "
            "batches and reviews nobody has picked up - one shape for all of them."
        ),
    ),
    _card(
        "exec_timeline",
        "Recent anomaly timeline",
        """
        SELECT calendar_date         AS "Date",
               anomaly_score         AS "Score",
               dominant_signal       AS "Dominant signal",
               signal_count          AS "Signals",
               net_revenue_usd       AS "Net revenue (USD)",
               revenue_deviation_pct AS "Deviation %",
               severity              AS "Severity",
               routing               AS "Routing",
               decision_reason_code  AS "Reason code",
               hypothesis_available  AS "Hypothesis exists"
        FROM salesops.exec_anomaly_timeline
        ORDER BY calendar_date DESC
        LIMIT 30
        """,
    ),
    _card(
        "exec_layers",
        "The eight layers, in reading order",
        """
        SELECT layer_rank         AS "Read in this order",
               layer_label        AS "Layer",
               evidence_kind      AS "Kind of evidence",
               is_model_generated AS "Generated by a language model",
               produced_by_stage  AS "Produced by",
               description        AS "What it is"
        FROM salesops.presentation_layers
        ORDER BY layer_rank
        """,
        description=(
            "The legend. Exactly one layer is model-generated, and the column "
            "saying so is a CHECK constraint in the database, not a note."
        ),
    ),

    # =======================================================================
    # Investigation cards - all parameterised by incident date
    # =======================================================================
    _card(
        "inv_chain",
        "The chain, in order",
        """
        SELECT step_rank          AS "#",
               step               AS "Step",
               layer_label        AS "Layer",
               is_model_generated AS "Model-generated",
               reached            AS "Happened",
               occurred_at        AS "When",
               actor              AS "Actor",
               summary            AS "What"
        FROM salesops.incident_timeline
        WHERE calendar_date = {{incident_date}}
        ORDER BY step_rank
        """,
        description=(
            "Ten steps from orders to operational outcome. Steps that did not "
            "happen are shown with Happened = false and a reason, so a chain that "
            "stopped early looks like a decision rather than missing data."
        ),
        tags=_INCIDENT_DATE_TAG,
    ),
    _card(
        "inv_evidence",
        "Evidence, in reading order",
        """
        SELECT layer_rank         AS "Layer #",
               layer_label        AS "Layer",
               is_model_generated AS "Model-generated",
               label              AS "Field",
               value              AS "Value",
               unit               AS "Unit"
        FROM salesops.anomaly_investigation_detail
        WHERE calendar_date = {{incident_date}}
        ORDER BY layer_rank, line_rank
        """,
        description=(
            "Facts, then statistics, then the deterministic decision, then the "
            "model. The order is the point: a hypothesis read before the decision "
            "is a hypothesis that frames the decision."
        ),
        tags=_INCIDENT_DATE_TAG,
    ),
    _card(
        "inv_decision",
        "Deterministic decision and reason codes",
        """
        SELECT decision_severity              AS "Severity",
               decision_routing               AS "Routing",
               decision_outcome               AS "Decision",
               decision_primary_reason        AS "Primary reason code",
               decision_all_reasons           AS "All reason codes",
               decision_impact_tier           AS "Impact tier",
               decision_expected_revenue_usd  AS "Expected revenue (USD)",
               decision_actual_revenue_usd    AS "Actual revenue (USD)",
               decision_revenue_delta_usd     AS "Delta (USD)",
               decision_revenue_delta_pct     AS "Delta %",
               decision_version               AS "Decision version",
               decided_at                     AS "Decided at"
        FROM salesops.anomaly_investigation
        WHERE calendar_date = {{incident_date}}
        """,
        description=(
            "Reproducible from the stored inputs and the threshold table. No model "
            "took part in any of it."
        ),
        tags=_INCIDENT_DATE_TAG,
        settings={
            "table.column_formatting": _severity_colours("Severity"),
            "column_settings": {
                **_money("Expected revenue (USD)"),
                **_money("Actual revenue (USD)"),
                **_money("Delta (USD)", show_mini_bar=True),
                **_plain("Delta %", decimals=2),
            },
        },
    ),
    _card(
        "inv_hypothesis",
        "LLM hypothesis - unverified",
        """
        WITH h AS (
            SELECT * FROM salesops.anomaly_investigation
            WHERE calendar_date = {{incident_date}}
        )
        SELECT f.rank AS "#", f.name AS "Field", f.value AS "Value"
        FROM h
        CROSS JOIN LATERAL (VALUES
            -- llm_verified is a plain boolean and defaults to false, so on a date
            -- Stage 7 never reached it rendered 'false' beside eleven empty rows.
            -- That reads as "we hold an unverified hypothesis" when the truth is
            -- "we hold none", which is a different and much weaker claim. Say
            -- which one it is.
            (1, 'Verified by this system',
             CASE WHEN h.llm_generated_at IS NULL
                  THEN 'no hypothesis was generated for this date'
                  ELSE h.llm_verified::text END),
            (2, 'Confidence the model stated', h.llm_confidence),
            (3, 'Summary', h.llm_summary),
            (4, 'Primary hypothesis', h.llm_primary_hypothesis),
            (5, 'Alternative hypotheses',
                (SELECT string_agg(i ->> 'hypothesis', '  //  ')
                 FROM jsonb_array_elements(h.llm_alternative_hypotheses) AS i)),
            (6, 'Evidence it cited',
                (SELECT string_agg(i ->> 'observation', '  //  ')
                 FROM jsonb_array_elements(h.llm_supporting_evidence) AS i)),
            (7, 'Checks it recommends',
                (SELECT string_agg(i, '  //  ')
                 FROM jsonb_array_elements_text(h.llm_recommended_checks) AS i)),
            (8, 'Evidence it reported it lacked',
                (SELECT string_agg(i, '  //  ')
                 FROM jsonb_array_elements_text(h.llm_missing_evidence) AS i)),
            (9, 'Provider', h.llm_model_provider),
            (10, 'Model', h.llm_model_name),
            (11, 'Prompt version', h.llm_prompt_version),
            (12, 'Generated at', h.llm_generated_at::text)
        ) AS f(rank, name, value)
        ORDER BY f.rank
        """,
        description=(
            "Generated after the decision and incapable of changing it. 'Verified "
            "by this system' is false on every row that exists, because nothing "
            "here verifies a hypothesis."
        ),
        settings={
            # One record, read down rather than across. As nine columns this was
            # a single row whose prose ran off the right edge behind a scrollbar,
            # under headings the reader could never see - a poor way to present
            # the one panel on the platform that has to be read sceptically.
            #
            # Verification status is the first line, the model's prose follows
            # it, and provenance closes. The JSON arrays are unpacked to the one
            # field in each that carries the claim; rendering them raw would put
            # braces and quote marks in front of a reader who wants a sentence.
            "table.column_formatting": [
                _paint("Value", "false", CRITICAL),
                # Absence is not a finding. Colour it as a gap, not as a verdict.
                _paint("Value", "no hypothesis was generated for this date", NEUTRAL),
            ],
            "column_settings": _fmt("Value", text_wrapping=True),
        },
        tags=_INCIDENT_DATE_TAG,
    ),
    _card(
        "inv_audit",
        "Audit history for this incident",
        """
        SELECT occurred_at  AS "When",
               stream       AS "Stream",
               event_type   AS "Event",
               from_state   AS "From",
               to_state     AS "To",
               actor        AS "Actor",
               reason       AS "Reason",
               version_info AS "Version",
               detail       AS "Detail"
        FROM salesops.audit_event_stream
        WHERE calendar_date = {{incident_date}}
        ORDER BY occurred_at, stream
        """,
        description=(
            "Every recorded transition for this date, from every stage. Machines "
            "are named as machines; people are named as themselves."
        ),
        tags=_INCIDENT_DATE_TAG,
    ),
    _card(
        "inv_picker",
        "Pick an incident",
        """
        SELECT calendar_date            AS "Date",
               decision_severity        AS "Severity",
               decision_primary_reason  AS "Reason code",
               fact_net_revenue_usd     AS "Net revenue (USD)",
               signal_baseline_median_usd AS "Baseline (USD)",
               review_status            AS "Review",
               remediation_status       AS "Remediation"
        FROM salesops.anomaly_investigation
        WHERE decision_outcome = 'action_required'
        ORDER BY calendar_date DESC
        """,
        description="Copy a date from here into the filter above.",
        settings={
            "table.column_formatting": _severity_colours("Severity"),
            "column_settings": {
                **_money("Net revenue (USD)"),
                **_money("Baseline (USD)"),
            },
        },
    ),

    # =======================================================================
    # Operational cards
    # =======================================================================
    _card(
        "ops_runs",
        "Pipeline runs",
        """
        SELECT pipeline                     AS "Pipeline",
               latest_run_status            AS "Latest run",
               latest_run_started_at        AS "Started",
               latest_run_seconds           AS "Duration (s)",
               hours_since_latest_run       AS "Hours since run",
               latest_success_finished_at   AS "Last success",
               hours_since_latest_success   AS "Hours since success",
               running_now                  AS "Running now",
               failed_24h                   AS "Failed 24h",
               partial_24h                  AS "Partial 24h",
               median_seconds_7d            AS "Median duration 7d (s)"
        FROM salesops.ops_pipeline_runs
        ORDER BY pipeline
        """,
        description=(
            "A pipeline whose latest run failed still shows when it last worked. "
            "Duration is a median so one hung run does not become the norm."
        ),
    ),
    _card(
        "ops_health",
        "Health by component",
        """
        SELECT component       AS "Component",
               component_kind  AS "Kind",
               health_status   AS "Health",
               reason_code     AS "Reason",
               observed_value  AS "Observed",
               threshold_value AS "Threshold",
               measure         AS "Measure",
               detail          AS "What it means"
        FROM salesops.exec_pipeline_health
        ORDER BY status_rank, component
        """,
        settings={"table.column_formatting": _health_colours("Health")},
    ),
    _card(
        "ops_stale",
        "Stale, failed and overdue",
        """
        SELECT entity_type          AS "What",
               entity_id            AS "Id",
               disposition          AS "Disposition",
               failure_reason       AS "Why",
               attempt_count        AS "Attempts",
               max_attempts         AS "Limit",
               retry_eligible       AS "Retryable",
               ageing_bucket        AS "Review ageing",
               last_activity_at     AS "Last activity",
               hours_since_activity AS "Hours since"
        FROM salesops.ops_attention_items
        ORDER BY hours_since_activity DESC NULLS LAST
        """,
    ),
    _card(
        "ops_replays",
        "Ingestion replays",
        """
        SELECT outcome        AS "Outcome",
               count(*)       AS "Replays",
               max(attempt_number) AS "Highest attempt",
               max(replayed_at)    AS "Most recent",
               count(DISTINCT original_batch_id) AS "Distinct batches"
        FROM salesops.ingestion_replays
        GROUP BY outcome
        ORDER BY outcome
        """,
        description=(
            "A replay never rewrites the failure it replays. The original staging "
            "rows keep their 'failed' status and their original error forever."
        ),
    ),
    _card(
        "ops_review_ageing",
        "Review ageing",
        """
        SELECT review_id        AS "Review",
               calendar_date    AS "Date",
               anomaly_severity AS "Anomaly severity",
               review_status    AS "State",
               assigned_to      AS "Assigned to",
               age_hours        AS "Age (h)",
               ageing_bucket    AS "Ageing bucket",
               overdue_after_hours AS "Overdue after (h)"
        FROM salesops.review_ageing
        ORDER BY age_hours DESC
        """,
        description=(
            "Operational ageing only. Nothing changes a review's state because of "
            "its age - 'nobody has looked at this' is not a decision."
        ),
        settings={
            "table.column_formatting": _severity_colours("Anomaly severity"),
        },
    ),
    _card(
        "ops_unknown",
        "Executions with an unknown outcome",
        """
        SELECT remediation_id  AS "Action",
               calendar_date   AS "Date",
               severity        AS "Anomaly severity",
               action_type     AS "Action type",
               status          AS "State",
               executed_by     AS "Claimed by",
               attempt_count   AS "Attempts",
               last_error      AS "Last error"
        FROM salesops.remediation_actions
        WHERE status = 'execution_unknown'
        ORDER BY remediation_id
        """,
        description=(
            "A process died around a provider call. Re-running might do the work "
            "twice and failing it might claim it never happened, so Stage 10 does "
            "neither: it records the uncertainty and waits for a person."
        ),
        settings={
            "table.column_formatting": _severity_colours("Anomaly severity"),
        },
    ),
    _card(
        "ops_events",
        "Recent operational events",
        """
        SELECT occurred_at AS "When",
               event_type  AS "Event",
               entity_id   AS "Entity",
               from_state  AS "From",
               to_state    AS "To",
               actor       AS "Actor",
               reason      AS "Reason code",
               detail      AS "Detail"
        FROM salesops.audit_event_stream
        WHERE stream = 'operational'
        ORDER BY occurred_at DESC
        LIMIT 100
        """,
        description="Append-only. The table refuses UPDATE and DELETE by trigger.",
    ),

    # =======================================================================
    # Audit cards
    # =======================================================================
    _card(
        "audit_stream",
        "Every recorded event",
        """
        SELECT occurred_at   AS "When",
               stream        AS "Stream",
               entity_id     AS "Entity",
               calendar_date AS "Business date",
               event_type    AS "Event",
               from_state    AS "From",
               to_state      AS "To",
               actor         AS "Actor",
               reason        AS "Reason",
               version_info  AS "Version / snapshot"
        FROM salesops.audit_event_stream
        ORDER BY occurred_at DESC
        LIMIT 500
        """,
        description=(
            "Six streams in one shape: decision, hypothesis, notification, review, "
            "remediation, operational. Actor, time, both ends of the transition "
            "and the version under which it happened - all as stored."
        ),
    ),
    _card(
        "audit_counts",
        "Events by stream",
        """
        SELECT stream            AS "Stream",
               count(*)          AS "Events",
               count(DISTINCT actor) AS "Distinct actors",
               min(occurred_at)  AS "Earliest",
               max(occurred_at)  AS "Latest"
        FROM salesops.audit_event_stream
        GROUP BY stream
        ORDER BY stream
        """,
    ),
    _card(
        "audit_actors",
        "Who did what",
        """
        SELECT actor              AS "Actor",
               stream             AS "Stream",
               count(*)           AS "Events",
               max(occurred_at)   AS "Most recent"
        FROM salesops.audit_event_stream
        GROUP BY actor, stream
        ORDER BY count(*) DESC
        """,
        description=(
            "Actors are ASSERTED, not authenticated. Nothing in this platform "
            "proves that a caller naming itself dana@finance is dana@finance."
        ),
    ),
]

CARDS_BY_KEY = {c["key"]: c for c in CARDS}


def _text(text, row, col, size_x, size_y):
    """A section header.

    Metabase renders markdown from the top of the panel and does not grow the
    panel to fit it, so the height has to match the copy: too small clips the
    last line, too large leaves the heading floating above a pool of empty
    background. Centring vertically makes the second failure harmless, which
    means a heading that wraps on a narrow window still looks deliberate.
    """
    return {"kind": "text", "text": text, "row": row, "col": col,
            "size_x": size_x, "size_y": size_y,
            "settings": {"text.align_vertical": "middle",
                         "text.align_horizontal": "left"}}


def _viz(key, row, col, size_x, size_y, link=None):
    entry = {"kind": "card", "card": key, "row": row, "col": col,
             "size_x": size_x, "size_y": size_y}
    if link:
        entry["link"] = link
    return entry


def _tab(name, cards):
    """One tab, whose rows start again at zero.

    Metabase scopes a dashcard's position to its tab, so two cards on different
    tabs may sit at the same row and column without colliding. Anything that
    walks the layout - provisioning, the overlap test - has to respect that, or
    it either reports collisions that cannot happen or misses ones that can.
    """
    return {"name": name, "cards": cards}


def dashboard_tabs(spec):
    """Every dashboard as a list of tabs, whether or not it declares any.

    A dashboard without tabs is one unnamed tab. Giving both shapes the same
    interface keeps the special case in one function instead of in every caller.
    """
    if "tabs" in spec:
        return spec["tabs"]
    return [{"name": None, "cards": spec["cards"]}]


def dashboard_cards(spec):
    """Every placed card on a dashboard, tabs flattened."""
    return [entry for tab in dashboard_tabs(spec) for entry in tab["cards"]]


#: Clicking a row that names a date opens that date's investigation. This is the
#: only navigation on the platform, and it runs in the one direction the
#: architecture allows: from a summary down into the evidence beneath it, never
#: from a conclusion back up into anything that could change it.
TO_INVESTIGATION = {
    "dashboard": "investigation",
    "parameter_id": INCIDENT_DATE_PARAM_ID,
    "column": "Date",
}


# ===========================================================================
# Dashboards
# ===========================================================================
DASHBOARDS = [
    {
        "key": "executive",
        "name": "Executive Overview",
        "description": (
            "Revenue, anomalies, and what has been done about them. Every figure "
            "is deterministic; the only nod to the language model is whether a "
            "hypothesis exists."
        ),
        "parameters": [],
        # Five tabs rather than eighty rows of scrolling. The split follows the
        # order the pipeline itself runs in, so moving through the tabs is
        # moving downstream: what happened, what was unusual about it, who was
        # asked, and whether the machinery that did all that is healthy.
        "tabs": [
            _tab("Overview", [
                # Metabase neither grows nor shrinks a text panel to fit its
                # markdown, so the height is part of the copy.
                _text(
                    "# Sales & Revenue Operations\n"
                    "**Observed facts** are measured. **Statistical signals** say a day "
                    "was unusual. **Business decisions** come from fixed thresholds. "
                    "Nothing on this page was written by a language model; the *How to "
                    "read* tab says which layer every panel belongs to.",
                    0, 0, 24, 3),

                # Four numbers read at a glance, before anything asks the reader
                # to interpret a chart or scan a table.
                _viz("exec_kpi_revenue", 3, 0, 6, 3),
                _viz("exec_kpi_orders", 3, 6, 6, 3),
                _viz("exec_kpi_aov", 3, 12, 6, 3),
                _viz("exec_kpi_refund_rate", 3, 18, 6, 3),

                _text("## Revenue against its own day-of-week baseline", 6, 0, 24, 1),
                _viz("exec_revenue_vs_baseline", 7, 0, 24, 7),
                _viz("exec_orders", 14, 0, 8, 5),
                _viz("exec_aov", 14, 8, 8, 5),
                _viz("exec_refund_rate", 14, 16, 8, 5),
                _viz("exec_headline", 19, 0, 24, 5),
            ]),

            _tab("Anomalies", [
                _text("## First a statistical signal, then a deterministic decision\n"
                      "Click any row to open that day's investigation.",
                      0, 0, 24, 2),
                _viz("exec_severity", 2, 0, 24, 5),
                _viz("exec_timeline", 7, 0, 24, 7, link=TO_INVESTIGATION),
                _viz("exec_actionable", 14, 0, 24, 7, link=TO_INVESTIGATION),
            ]),

            _tab("Human review", [
                _text("## Nothing on this tab happened without a person", 0, 0, 24, 1),
                _viz("exec_needs_review", 1, 0, 24, 6, link=TO_INVESTIGATION),
                _viz("exec_reviews", 7, 0, 24, 5),
                _viz("exec_notifications", 12, 0, 12, 5),
                _viz("exec_remediation", 12, 12, 12, 5),
            ]),

            _tab("Operations", [
                _text("## Operational health, in its own vocabulary", 0, 0, 24, 1),
                _viz("exec_health", 1, 0, 24, 7),
                _viz("exec_attention", 8, 0, 24, 6),
            ]),

            _tab("How to read", [
                _text("## Every panel belongs to exactly one layer\n"
                      "One of the eight is model-generated. A `CHECK` constraint in the "
                      "database makes it impossible for a second one to claim it is.",
                      0, 0, 24, 2),
                _viz("exec_layers", 2, 0, 24, 8),
            ]),
        ],
    },
    {
        "key": "investigation",
        "name": "Anomaly Investigation",
        "description": (
            "One anomaly, layer by layer, in the order it must be read. Defaults to "
            "the incident bootstrap.sh injected; change the date filter for any other."
        ),
        "parameters": [
            {
                "id": INCIDENT_DATE_PARAM_ID,
                "name": "Incident date",
                "slug": "incident_date",
                "type": "date/single",
                "sectionId": "date",
                "default": DEFAULT_INCIDENT_DATE,
            }
        ],
        "parameter_tag": "incident_date",
        "cards": [
            _text(
                "# Anomaly investigation\n"
                "Read downwards. **1** what happened → **2** what was unusual → "
                "**3** what the rules concluded → **4** what a language model *guessed*. "
                "The model ran last and could not change anything above it.",
                0, 0, 24, 3),
            _viz("inv_chain", 3, 0, 24, 7),
            _viz("inv_decision", 10, 0, 24, 5),
            _viz("inv_evidence", 15, 0, 24, 12),
            _text(
                "## ⚠ Below this line: language-model output\n"
                "Unverified. Generated **after** the decision above, from the same "
                "stored evidence, and incapable of changing it. Treat it as a lead to "
                "check, never as a finding.",
                27, 0, 24, 3),
            _viz("inv_hypothesis", 30, 0, 24, 12),
            _text("## Audit history", 42, 0, 24, 1),
            _viz("inv_audit", 43, 0, 24, 7),
            _viz("inv_picker", 50, 0, 24, 6),
        ],
    },
    {
        "key": "operational",
        "name": "Operational Health",
        "description": (
            "Stage 10. healthy | warning | degraded | failed - never an anomaly "
            "severity."
        ),
        "parameters": [],
        "cards": [
            _text(
                "# Operational health\n"
                "This page describes the **pipeline**, not the business. Its vocabulary "
                "is `healthy | warning | degraded | failed`, and it shares no word with "
                "anomaly severity so the two can never be read as the same scale.",
                0, 0, 24, 3),
            _viz("ops_health", 3, 0, 24, 8),
            _viz("ops_runs", 11, 0, 24, 8),
            _text("## Waiting, stuck, or unresolved", 19, 0, 24, 1),
            _viz("ops_stale", 20, 0, 14, 7),
            _viz("ops_replays", 20, 14, 10, 7),
            _viz("ops_review_ageing", 27, 0, 14, 6),
            _viz("ops_unknown", 27, 14, 10, 6),
            _viz("ops_events", 33, 0, 24, 8),
        ],
    },
    {
        "key": "audit",
        "name": "Audit Trail",
        "description": (
            "Every state transition the platform recorded, with its actor, its time "
            "and its version."
        ),
        "parameters": [],
        "cards": [
            _text(
                "# Audit trail\n"
                "Six streams, one shape. **Actors are asserted, not authenticated** - "
                "this platform records who a caller *said* they were. That is a known "
                "limitation, not an oversight.",
                0, 0, 24, 3),
            _viz("audit_counts", 3, 0, 12, 5),
            _viz("audit_actors", 3, 12, 12, 5),
            _viz("audit_stream", 8, 0, 24, 12),
        ],
    },
]
