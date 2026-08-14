"""Value types passed between the detector's layers.

Frozen dataclasses rather than dicts: the baseline builder and the scorer are
independently testable, and a typed boundary between them means a renamed field
fails at import rather than silently reading None three layers later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class BaselineStatus(StrEnum):
    """Whether an observation could be judged, and if not why not."""

    SCORED = "scored"
    # Too few prior comparable observations to say anything defensible.
    INSUFFICIENT_HISTORY = "insufficient_history"
    # The day's KPI row was missing FX coverage, so its money columns are
    # understated. Scoring it would measure an FX gap as a revenue collapse.
    INCOMPLETE_KPI = "incomplete_kpi"


class BaselineKind(StrEnum):
    """Which set of prior observations the comparison was made against."""

    # Preferred: prior observations of the same weekday.
    DAY_OF_WEEK = "day_of_week"
    # Fallback: prior weekdays, or prior weekend days, as a class.
    DAY_TYPE = "day_type"


@dataclass(frozen=True)
class KpiObservation:
    """One row of salesops.kpi_daily, plus the calendar attributes it is judged by.

    Money arrives from PostgreSQL as Decimal and is converted to float here. That
    is deliberate and narrow: NUMERIC remains authoritative for every monetary
    value in the warehouse, and nothing in this service writes money back. What
    it computes are dimensionless statistics, where float is the right type and
    Decimal would only add friction.
    """

    calendar_date: date
    day_of_week: int          # ISO: 1 = Monday .. 7 = Sunday
    is_weekend: bool

    orders_count: int
    net_revenue_usd: float | None
    average_order_value_usd: float | None
    refund_rate: float | None

    orders_pending_fx: int
    fx_completeness_pct: float
    is_complete: bool

    @property
    def has_money(self) -> bool:
        """True when every money-derived signal is present."""
        return (
            self.net_revenue_usd is not None
            and self.average_order_value_usd is not None
            and self.refund_rate is not None
        )


@dataclass(frozen=True)
class Baseline:
    """The prior observations one signal is compared against."""

    kind: BaselineKind
    values: tuple[float, ...]

    @property
    def size(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class SignalResult:
    """One metric's evidence: how far it moved, and how unusual that is."""

    name: str
    observed: float
    baseline_median: float
    #: Percent for revenue/aov/orders; absolute rate difference for refunds.
    deviation: float | None
    robust_z: float | None
    #: |z| capped and weighted - what this signal actually contributes to the score.
    contribution: float
    #: True when |z| alone clears the significance threshold.
    is_significant: bool


@dataclass(frozen=True)
class DetectionResult:
    """Everything Stage 5 has to say about one calendar date.

    Statistical evidence only. No severity, no cause, no recommended action -
    Stage 6 owns business severity and Stage 7 owns reasoning.
    """

    calendar_date: date
    detector_version: str
    baseline_status: BaselineStatus
    baseline_kind: BaselineKind | None
    baseline_size: int | None

    anomaly_score: float | None
    is_anomaly: bool

    signals: tuple[SignalResult, ...]
    dominant_signal: str | None
    signal_count: int

    def signal(self, name: str) -> SignalResult | None:
        for result in self.signals:
            if result.name == name:
                return result
        return None
