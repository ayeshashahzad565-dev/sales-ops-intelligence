"""Stage 7 orchestration: who gets analysed, what they are shown, what is kept.

The shape of a run
------------------
1. Ask Stage 6 which decisions are actionable. Nothing else is ever considered.
2. Drop the ones already analysed by this prompt version and model, unless a
   regeneration was explicitly requested.
3. For each survivor: build an evidence package, render the prompt, call the
   provider, validate the response, persist it.
4. Count what happened and report it honestly.

Failure isolation
-----------------
Step 3 is wrapped per anomaly. One timeout, one malformed response, one rejected
schema affects that anomaly and nothing else - the run continues, the remaining
analyses are generated, and the failure is counted and named. Critically, a
failure changes nothing in Stage 6: this module never writes to
`anomaly_decisions`, never issues an UPDATE against it, and has no code path
that could downgrade a severity or clear a review flag. The absence is the
safety property, and the test suite asserts it against the live table.

A run where some analyses failed is reported `partial`. A run where every one
failed is `failed`. Neither is `success`, because a caller reading only the
status has to be able to trust it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date

import psycopg

from analytics import repository
from analytics.config import Settings
from analytics.llm import prompts
from analytics.llm.models import (
    EvidenceItem,
    EvidencePackage,
    GeneratedHypothesis,
    HistoricalObservation,
)
from analytics.llm.provider import LLMProvider, ProviderError

logger = logging.getLogger(__name__)

DEFAULT_DECISION_VERSION = "stage6-v1"


@dataclass(frozen=True)
class AnalysisFailure:
    """One anomaly that could not be analysed, and why.

    Kept structured rather than concatenated into a log line: the run ledger
    records it, and an operator needs to know which date failed, not just that
    something did.
    """

    calendar_date: str
    anomaly_id: int
    reason: str


@dataclass
class AnalysisRunSummary:
    """What one Stage 7 run did. Shaped for the ingestion_runs ledger."""

    decision_version: str
    prompt_version: str
    model_provider: str
    model_name: str

    #: Actionable Stage 6 decisions found.
    eligible: int = 0
    #: Sent to the model.
    processed: int = 0
    #: Validated and persisted.
    succeeded: int = 0
    #: Attempted and failed. Stage 6 is untouched for every one of these.
    failed: int = 0
    #: Already analysed by this prompt version and model.
    skipped_existing: int = 0
    #: Regeneration removed an existing analysis before rewriting it.
    regenerated: int = 0

    failures: list[AnalysisFailure] = field(default_factory=list)
    analysed_dates: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Derived from what actually happened, never from having finished.

        An empty eligible set is a success: Stage 6 found nothing actionable,
        which is the normal state of a healthy week and not a fault.
        """
        if self.failed and self.succeeded == 0 and self.processed > 0:
            return "failed"
        if self.failed:
            return "partial"
        return "success"

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status
        return payload


def run_analysis(
    settings: Settings,
    provider: LLMProvider,
    decision_version: str = DEFAULT_DECISION_VERSION,
    prompt_version: str = prompts.PROMPT_VERSION,
    regenerate: bool = False,
    only_dates: list[date] | None = None,
    limit: int | None = None,
) -> AnalysisRunSummary:
    """Generate hypotheses for actionable Stage 6 decisions.

    Args:
        regenerate: Replace existing analyses for this prompt version and model.
            Off by default and never set by the schedule - a nightly job that
            silently rewrote yesterday's reasoning would destroy the audit trail
            it exists to create.
        only_dates: Restrict to specific dates. For targeted re-analysis and for
            the live validation of a named anomaly.
        limit: Cap the number analysed. A cost guard, not a filter - what it
            drops is logged rather than silently omitted.
    """
    summary = AnalysisRunSummary(
        decision_version=decision_version,
        prompt_version=prompt_version,
        model_provider=provider.name,
        model_name=provider.model,
    )

    with repository.connect(settings.dsn) as connection:
        candidates = repository.load_actionable_decisions(
            connection,
            decision_version=decision_version,
            prompt_version=prompt_version,
            model_name=provider.model,
        )

        if only_dates:
            wanted = set(only_dates)
            candidates = [row for row in candidates if row["calendar_date"] in wanted]

        summary.eligible = len(candidates)

        pending = [row for row in candidates if regenerate or not row["already_analysed"]]
        summary.skipped_existing = len(candidates) - len(pending)

        if limit is not None and len(pending) > limit:
            logger.warning(
                "Analysis limit %d reached: %d of %d eligible anomalies were not analysed",
                limit, len(pending) - limit, len(pending),
            )
            pending = pending[:limit]

        for row in pending:
            summary.processed += 1
            try:
                _analyse_one(
                    connection=connection,
                    provider=provider,
                    row=row,
                    prompt_version=prompt_version,
                    regenerate=regenerate,
                    summary=summary,
                )
            except Exception as exc:
                # Committed and rolled back per anomaly, so one failure cannot
                # take the successful analyses down with it.
                connection.rollback()
                summary.failed += 1
                summary.failures.append(
                    AnalysisFailure(
                        calendar_date=row["calendar_date"].isoformat(),
                        anomaly_id=row["anomaly_id"],
                        reason=_describe(exc),
                    )
                )
                logger.warning(
                    "Analysis failed for %s: %s", row["calendar_date"], _describe(exc)
                )

    logger.info(
        "Stage 7 complete (%s): %d eligible, %d processed, %d succeeded, %d failed, "
        "%d already analysed",
        summary.status, summary.eligible, summary.processed,
        summary.succeeded, summary.failed, summary.skipped_existing,
    )
    return summary


def _analyse_one(
    connection: psycopg.Connection,
    provider: LLMProvider,
    row: dict,
    prompt_version: str,
    regenerate: bool,
    summary: AnalysisRunSummary,
) -> None:
    package = build_evidence_package(connection, row)

    if regenerate:
        removed = repository.delete_hypothesis(
            connection,
            anomaly_id=row["anomaly_id"],
            decision_version=row["decision_version"],
            prompt_version=prompt_version,
            model_name=provider.model,
        )
        if removed:
            summary.regenerated += removed

    hypothesis, metadata = provider.complete(
        system_prompt=prompts.SYSTEM_PROMPT,
        user_message=prompts.build_user_message(package),
        json_schema=prompts.response_json_schema(),
    )

    # The schema already rejected unknown fields and constrained the confidence
    # vocabulary. This is the grounding check the schema cannot express: every
    # metric cited as support must be one the model was actually shown.
    hypothesis.assert_cites_known_metrics(package.metric_vocabulary)

    generated = GeneratedHypothesis(
        anomaly_id=row["anomaly_id"],
        decision_id=row["decision_id"],
        calendar_date=row["calendar_date"],
        decision_version=row["decision_version"],
        # Copied from the Stage 6 row, never from the response. The model has no
        # field for these; the database re-checks them against the live decision
        # before accepting the insert.
        severity=row["severity"],
        routing=row["routing"],
        decision=row["decision"],
        hypothesis=hypothesis,
        metadata=metadata,
        evidence_digest=package.digest(),
    )

    hypothesis_id = repository.save_hypothesis(connection, _to_payload(generated, prompt_version))
    connection.commit()

    if hypothesis_id is None:
        # Another run inserted the same generation between our eligibility read
        # and this write. Its analysis is as valid as ours would have been.
        summary.skipped_existing += 1
        logger.info("Hypothesis for %s already existed; kept it", row["calendar_date"])
        return

    summary.succeeded += 1
    summary.analysed_dates.append(row["calendar_date"].isoformat())


def build_evidence_package(connection: psycopg.Connection, row: dict) -> EvidencePackage:
    """Assemble everything one anomaly's analysis may be based on.

    Every number gets a metric name, a formatted display value, a source table
    and a date. The formatting is not decoration: a refund rate handed over as
    `0.3575` invites the model to guess at scale, and leaves a reviewer reading
    the stored evidence digest unable to tell what was compared with what.
    """
    calendar_date: date = row["calendar_date"]
    reason_codes = repository.load_decision_reason_codes(connection, row["decision_id"])
    same_weekday, preceding = repository.load_history(connection, calendar_date)

    kpi = tuple(
        item for item in (
            _money_item("net_revenue_usd", row["net_revenue_usd"], calendar_date,
                        "actual net USD revenue for the day"),
            _money_item("expected_net_revenue_usd", row["expected_net_revenue_usd"],
                        calendar_date,
                        f"median of prior {row['day_name']}s, the Stage 5 baseline"),
            _money_item("revenue_delta_usd", row["revenue_delta_usd"], calendar_date,
                        "actual minus expected; negative is a shortfall"),
            _ratio_item("revenue_delta_pct", row["revenue_delta_pct"], calendar_date,
                        "percent difference from the baseline"),
            _money_item("gross_revenue_usd", row["gross_revenue_usd"], calendar_date),
            _money_item("refund_amount_usd", row["refund_amount_usd"], calendar_date),
            _money_item("average_order_value_usd", row["average_order_value_usd"], calendar_date),
            _percent_item("refund_rate", row["refund_rate"], calendar_date,
                          "refunds as a share of gross revenue"),
            _count_item("orders_count", row["orders_count"], calendar_date),
            _count_item("customers_count", row["customers_count"], calendar_date),
            _count_item("units_sold", row["units_sold"], calendar_date),
            _money_item("rolling_7d_net_revenue_usd", row["rolling_7d_net_revenue_usd"],
                        calendar_date, "trailing 7-calendar-day mean, ending this date"),
            _money_item("rolling_28d_net_revenue_usd", row["rolling_28d_net_revenue_usd"],
                        calendar_date, "trailing 28-calendar-day mean, ending this date"),
        ) if item is not None
    )

    statistics = tuple(
        item for item in (
            _number_item("anomaly_score", row["anomaly_score"], calendar_date,
                         "Stage 5 weighted score; the flag threshold is 2.5",
                         source="anomaly_decisions"),
            _z_item("revenue_robust_z", row["revenue_robust_z"], calendar_date),
            _z_item("aov_robust_z", row["aov_robust_z"], calendar_date),
            _z_item("refund_robust_z", row["refund_robust_z"], calendar_date),
            _z_item("orders_robust_z", row["orders_robust_z"], calendar_date),
            _ratio_item("aov_deviation_pct", row["aov_deviation_pct"], calendar_date,
                        "percent difference from the same-weekday baseline",
                        source="anomaly_decisions"),
            _ratio_item("orders_deviation_pct", row["orders_deviation_pct"], calendar_date,
                        "percent difference from the same-weekday baseline",
                        source="anomaly_decisions"),
            _rate_points_item("refund_rate_deviation", row["refund_rate_deviation"],
                              calendar_date),
            _number_item("signal_count", row["signal_count"], calendar_date,
                         "how many of the four signals were individually significant",
                         source="anomaly_decisions", decimals=0),
            EvidenceItem(
                metric="baseline_status",
                display=str(row["baseline_status"]),
                source="anomaly_decisions",
                as_of=calendar_date,
                note="'scored' means a real baseline existed",
            ),
            EvidenceItem(
                metric="business_impact_tier",
                display=str(row["business_impact_tier"]),
                source="anomaly_decisions",
                as_of=calendar_date,
                note="Stage 6 classification of the revenue difference",
            ),
        ) if item is not None
    )

    history = tuple(
        [_observation(entry, "same_weekday") for entry in same_weekday]
        + [_observation(entry, "preceding_day") for entry in preceding]
    )

    package = EvidencePackage(
        calendar_date=calendar_date,
        day_name=row["day_name"],
        severity=row["severity"],
        routing=row["routing"],
        decision=row["decision"],
        decision_version=row["decision_version"],
        decision_reason_codes=reason_codes,
        kpi=kpi,
        statistics=statistics,
        history=history,
        unavailable_sources=prompts.UNAVAILABLE_SOURCES,
    )
    package.assert_no_future_data()
    return package


# =============================================================================
# Helpers
# =============================================================================


def _observation(entry: dict, relation: str) -> HistoricalObservation:
    return HistoricalObservation(
        calendar_date=entry["calendar_date"],
        day_name=entry["day_name"],
        relation=relation,  # type: ignore[arg-type]
        net_revenue_usd=_as_float(entry["net_revenue_usd"]),
        orders_count=entry["orders_count"],
        average_order_value_usd=_as_float(entry["average_order_value_usd"]),
        refund_rate=_as_float(entry["refund_rate"]),
    )


def _money_item(metric, value, as_of, note=None, source="kpi_daily") -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    return EvidenceItem(metric, f"${number:,.2f}", source, as_of, note)


def _percent_item(metric, value, as_of, note=None, source="kpi_daily") -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    return EvidenceItem(metric, f"{number * 100:.2f}%", source, as_of, note)


def _ratio_item(metric, value, as_of, note=None, source="anomaly_decisions") -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    return EvidenceItem(metric, f"{number:+.2f}%", source, as_of, note)


def _rate_points_item(metric, value, as_of, source="anomaly_decisions") -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    # Percentage POINTS, not percent. Baseline refund rates sit near 0.02, so a
    # percentage change against them reads as thousands of percent for an
    # ordinary move; the note says so rather than leaving the model to infer it.
    return EvidenceItem(
        metric,
        f"{number * 100:+.2f} percentage points",
        source,
        as_of,
        "absolute change in refund rate versus the same-weekday baseline",
    )


def _number_item(metric, value, as_of, note=None, source="anomaly_decisions",
                 decimals=2) -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    return EvidenceItem(metric, f"{number:,.{decimals}f}", source, as_of, note)


def _count_item(metric, value, as_of, source="kpi_daily") -> EvidenceItem | None:
    if value is None:
        return None
    return EvidenceItem(metric, f"{int(value):,}", source, as_of)


def _z_item(metric, value, as_of) -> EvidenceItem | None:
    number = _as_float(value)
    if number is None:
        return None
    significance = "significant" if abs(number) >= 3.5 else "not individually significant"
    return EvidenceItem(
        metric, f"{number:+.2f}", "anomaly_decisions", as_of,
        f"robust z against prior same-weekday values; {significance}",
    )


def _as_float(value) -> float | None:
    return None if value is None else float(value)


def _to_payload(generated: GeneratedHypothesis, prompt_version: str) -> dict:
    hypothesis = generated.hypothesis
    metadata = generated.metadata
    return {
        "anomaly_id": generated.anomaly_id,
        "decision_id": generated.decision_id,
        "calendar_date": generated.calendar_date,
        "decision_version": generated.decision_version,
        "severity": generated.severity,
        "routing": generated.routing,
        "decision": generated.decision,
        "summary": hypothesis.summary,
        "confidence": hypothesis.confidence,
        "primary_hypothesis": hypothesis.primary_hypothesis,
        "supporting_evidence": json.dumps(
            [item.model_dump() for item in hypothesis.supporting_evidence]
        ),
        "alternative_hypotheses": json.dumps(
            [item.model_dump() for item in hypothesis.alternative_hypotheses]
        ),
        "missing_evidence": json.dumps(hypothesis.missing_evidence),
        "recommended_checks": json.dumps(hypothesis.recommended_checks),
        "model_provider": metadata.provider,
        "model_name": metadata.model,
        "prompt_version": prompt_version,
        "evidence_digest": generated.evidence_digest,
        "request_id": metadata.request_id,
        "prompt_tokens": metadata.prompt_tokens,
        "completion_tokens": metadata.completion_tokens,
        "latency_ms": metadata.latency_ms,
        "json_mode": metadata.json_mode,
    }


def _describe(exc: Exception) -> str:
    """A short failure reason safe to store and log.

    Never includes the request body or headers, so a credential cannot reach the
    run ledger by way of an exception message.
    """
    if isinstance(exc, ProviderError):
        return str(exc)[:400]
    return f"{type(exc).__name__}: {exc}"[:400]
