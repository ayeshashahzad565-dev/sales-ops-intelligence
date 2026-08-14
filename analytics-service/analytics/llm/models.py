"""What goes into the model, and what is allowed to come back out.

Two halves that never mix:

* The **evidence package** is built from the warehouse. Every number in it
  carries a metric name, a unit, a source table and the date it belongs to, so
  the reasoning the model produces can be checked against the rows it saw.

* The **hypothesis** is what the model returns. It is treated as untrusted
  input: a Pydantic model with `extra="forbid"`, a closed confidence vocabulary,
  and a check that every cited metric actually existed in the evidence package.

Why `extra="forbid"` matters more than it looks
-----------------------------------------------
It is the mechanism that stops the model expanding its own remit. There is no
`severity` field in the response schema, so a model that returns one produces a
validation error rather than a value someone might later read. The specification
requires that an attempted severity override be rejected; this is where that
happens, and it costs one line.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bumped when the response contract changes shape.
HYPOTHESIS_SCHEMA_VERSION = "1"

Confidence = Literal["low", "medium", "high"]


# =============================================================================
# Evidence: what the model is shown
# =============================================================================


@dataclass(frozen=True)
class EvidenceItem:
    """One observed number, with everything needed to check it later.

    `metric` is the machine-readable key the model must cite when it uses this
    item as support. `display` is the human form that goes into the prompt -
    "35.75%" rather than "0.3575" - because a model shown bare floats has to
    guess at scale, and a reviewer reading the transcript afterwards has to
    guess twice.
    """

    metric: str
    display: str
    source: str
    as_of: date | None = None
    note: str | None = None

    def render(self) -> str:
        parts = [f"{self.metric} = {self.display}"]
        if self.note:
            parts.append(f"({self.note})")
        stamp = f"source: {self.source}"
        if self.as_of is not None:
            stamp += f", {self.as_of.isoformat()}"
        parts.append(f"[{stamp}]")
        return " ".join(parts)


@dataclass(frozen=True)
class HistoricalObservation:
    """One prior day, for comparison.

    Only ever built from dates STRICTLY EARLIER than the anomaly. The detector
    has the same rule for the same reason, and here it matters just as much: a
    model shown what happened next can "explain" an anomaly using information
    nobody had at the time, and the explanation will read perfectly.
    """

    calendar_date: date
    day_name: str
    relation: Literal["same_weekday", "preceding_day"]

    net_revenue_usd: float | None
    orders_count: int | None
    average_order_value_usd: float | None
    refund_rate: float | None

    def render(self) -> str:
        return (
            f"{self.calendar_date.isoformat()} ({self.day_name}): "
            f"net_revenue_usd = {_money(self.net_revenue_usd)}, "
            f"orders = {_count(self.orders_count)}, "
            f"average_order_value_usd = {_money(self.average_order_value_usd)}, "
            f"refund_rate = {_percent(self.refund_rate)}"
        )


@dataclass(frozen=True)
class EvidencePackage:
    """Everything one anomaly's analysis is allowed to be based on.

    Compact on purpose. The warehouse holds 90 days of orders; sending all of it
    would cost more, read worse and let the model anchor on whatever it happened
    to notice. What it gets is the day itself, the Stage 5 statistics, the Stage 6
    verdict, and a small window of comparable prior days.
    """

    calendar_date: date
    day_name: str

    # Stage 6 - context, not a question. See prompts.py.
    severity: str
    routing: str
    decision: str
    decision_version: str
    decision_reason_codes: tuple[str, ...]

    kpi: tuple[EvidenceItem, ...]
    statistics: tuple[EvidenceItem, ...]
    history: tuple[HistoricalObservation, ...]

    #: Data the warehouse does not hold. Stated explicitly in the prompt so the
    #: model treats absence as a known gap rather than an invitation.
    unavailable_sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def metric_vocabulary(self) -> frozenset[str]:
        """Every metric name the model may cite as supporting evidence.

        The validator enforces membership. It is the one mechanical defence
        against invented facts: a model that cites `payment_gateway_error_rate`
        as support fails validation, because no such metric was ever shown.
        """
        return frozenset(item.metric for item in (*self.kpi, *self.statistics))

    def digest(self) -> str:
        """SHA-256 over the evidence, stable across runs and machines.

        Persisted with the hypothesis so two differing answers can be shown to
        have come from identical - or different - inputs.
        """
        payload = json.dumps(_serialisable(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def assert_no_future_data(self) -> None:
        """Fail loudly if any historical observation is not strictly earlier.

        Called before every generation. A leak here would not raise an error at
        the provider or produce malformed output - it would produce a fluent,
        confident, wrong analysis, which is the failure mode worth an assertion.
        """
        leaked = [
            observation.calendar_date.isoformat()
            for observation in self.history
            if observation.calendar_date >= self.calendar_date
        ]
        if leaked:
            raise ValueError(
                f"Evidence package for {self.calendar_date.isoformat()} contains "
                f"non-historical observations: {', '.join(sorted(leaked))}"
            )


# =============================================================================
# Hypothesis: what the model returns
# =============================================================================


class SupportingEvidence(BaseModel):
    """One observation the model is using to support its explanation."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(
        description="The exact metric name from the evidence package this rests on."
    )
    observation: str = Field(
        description="What that metric shows, stated as an observation."
    )
    relevance: str = Field(
        description="Why it supports the hypothesis."
    )

    @field_validator("metric", "observation", "relevance")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class AlternativeHypothesis(BaseModel):
    """A competing explanation that also fits the evidence."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    why_plausible: str
    what_would_confirm: str = Field(
        description="The evidence that would distinguish this from the primary hypothesis."
    )

    @field_validator("hypothesis", "why_plausible", "what_would_confirm")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class RootCauseHypothesis(BaseModel):
    """The complete, validated response.

    Note what is absent: no severity, no routing, no decision, no is_anomaly, no
    "should this be escalated". Stage 6 owns all of that, and `extra="forbid"`
    means a model that volunteers one gets a validation failure rather than an
    audience.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="What happened, in two or three sentences, as observation."
    )
    confidence: Confidence = Field(
        description=(
            "How strongly the available evidence supports the primary hypothesis. "
            "Not confidence that the anomaly is real."
        )
    )
    primary_hypothesis: str = Field(
        description="The most plausible explanation the evidence supports."
    )
    supporting_evidence: list[SupportingEvidence] = Field(min_length=1)
    alternative_hypotheses: list[AlternativeHypothesis] = Field(default_factory=list)
    missing_evidence: list[str] = Field(
        default_factory=list,
        description="What would be needed to distinguish between the hypotheses.",
    )
    recommended_checks: list[str] = Field(
        default_factory=list,
        description="Concrete next investigative steps for a human.",
    )

    @field_validator("summary", "primary_hypothesis")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("missing_evidence", "recommended_checks")
    @classmethod
    def _no_blank_entries(cls, values: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in values if entry and entry.strip()]
        return cleaned

    def assert_cites_known_metrics(self, vocabulary: frozenset[str]) -> None:
        """Every supporting metric must be one the model was actually shown.

        This is narrow on purpose. It cannot police the prose - a model can still
        write a speculative sentence in `primary_hypothesis`, and the prompt is
        what discourages that. What it can do is guarantee that the *evidence*
        section is grounded in rows that exist, which is where a fabricated fact
        would do the most damage: presented as an observation, with a metric name,
        looking exactly like the real ones.
        """
        unknown = sorted(
            {
                item.metric
                for item in self.supporting_evidence
                if item.metric not in vocabulary
            }
        )
        if unknown:
            raise ValueError(
                "supporting_evidence cites metrics that were never provided: "
                + ", ".join(unknown)
            )

    @classmethod
    def json_schema_for_provider(cls) -> dict:
        """JSON Schema for providers that support structured output.

        `additionalProperties: false` throughout, mirroring `extra="forbid"`, so
        a provider that honours the schema refuses a stray severity field before
        the response is even returned.
        """
        schema = cls.model_json_schema()
        _harden(schema)
        return schema


# =============================================================================
# Generation result: hypothesis + provenance
# =============================================================================


@dataclass(frozen=True)
class GenerationMetadata:
    """Where a hypothesis came from. Never contains credentials."""

    provider: str
    model: str
    prompt_version: str
    json_mode: str
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class GeneratedHypothesis:
    """A validated hypothesis, the evidence it came from, and its provenance."""

    anomaly_id: int
    decision_id: int
    calendar_date: date
    decision_version: str
    severity: str
    routing: str
    decision: str
    hypothesis: RootCauseHypothesis
    metadata: GenerationMetadata
    evidence_digest: str


# =============================================================================
# Helpers
# =============================================================================


def _money(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:,.2f}"


def _percent(value: float | None) -> str:
    return "unavailable" if value is None else f"{value * 100:.2f}%"


def _count(value: int | None) -> str:
    return "unavailable" if value is None else f"{value:,}"


def _serialisable(package: EvidencePackage) -> dict:
    payload = asdict(package)
    payload["calendar_date"] = package.calendar_date.isoformat()
    for observation in payload["history"]:
        observation["calendar_date"] = observation["calendar_date"].isoformat()
    for item in (*payload["kpi"], *payload["statistics"]):
        if item["as_of"] is not None:
            item["as_of"] = item["as_of"].isoformat()
    return payload


def _harden(node: object) -> None:
    """Recursively forbid additional properties in a generated JSON Schema."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            # Structured-output modes generally require every property to be
            # listed as required; optional fields are expressed as defaults on
            # our side, so this stays consistent with the Pydantic model.
            node["required"] = sorted(node["properties"].keys())
        for value in node.values():
            _harden(value)
    elif isinstance(node, list):
        for value in node:
            _harden(value)
