"""Anomaly scoring.

Combines four independently-computed signals into one score. Deterministic,
explainable, and deliberately not a model: no training, no fitted parameters, no
randomness. Given the same KPI series and the same detector version, this
produces the same numbers on every machine, forever.

The scoring formula
-------------------
For each signal s, a robust z-score z_s is computed against that signal's own
calendar-aware baseline (see baseline.py, statistics.py). Then:

    excess_s       = clamp(|z_s| - NOISE_FLOOR, 0, Z_CAP - NOISE_FLOOR)

    contribution_s = w_s * excess_s

    anomaly_score  = sum over available signals of contribution_s

    is_anomaly     = anomaly_score >= ANOMALY_SCORE_THRESHOLD

with weights

    revenue 1.0    aov 0.5    refund 0.5    orders 0.5

Why a noise floor, and not just the sum of |z|
----------------------------------------------
Summing raw |z| across four signals accumulates ordinary variation. For a
well-behaved metric E[|z|] is around 0.8, so four signals produce a score near
2.0 on a completely unremarkable day - the score would have no meaningful zero,
the threshold would sit barely above the noise, and adding a fifth signal later
would silently move every score up.

Subtracting a floor of 1.0 first means a signal contributes only what exceeds
ordinary variation. An ordinary day scores 0. The number is then readable:
"total evidence beyond normal fluctuation, weighted by signal".

This was not the original formula. The first version summed raw |z| and flagged
a day in a synthetic series with no injected event at all - the noise floor was
found by testing, not by theory, which is why the clean-series test exists.

Why these weights and this threshold
------------------------------------
This is *revenue* anomaly detection, so revenue carries full weight and the
others corroborate at half.

The threshold is set so that revenue alone, at the conventional |z| = 3.5
outlier level, is exactly the break-even case:

    ANOMALY_SCORE_THRESHOLD = 1.0 * (3.5 - 1.0) = 2.5

The consequences are the intended ones:

* A large isolated revenue move is still flagged, unaccompanied.

* The three supporting signals can flag a day without revenue: at |z| = 3.5 each
  they total 3.75. A day where order value, refunds and volume all move sharply
  while revenue somehow does not is genuinely strange and worth surfacing.

* Corroborated moderate moves accumulate. Revenue at |z| = 2.5 alone scores 1.5
  and is not flagged; add AOV and refunds at the same level and the total reaches
  3.0, which is. This is the case a single-threshold detector misses, and it is
  why the score is additive rather than a maximum.

Why absolute values
-------------------
Both directions are anomalous. A revenue collapse and a revenue spike are both
worth a human look, and the spec requires spikes be detectable. Direction is
preserved in the signed `robust_z` and `deviation` fields on every signal, so a
consumer can tell which way a day moved; it simply does not change whether the
day is unusual.

Why the cap
-----------
Z_CAP stops one pathological signal from swamping the sum. Once a signal is at
|z| = 10 the day is unmistakably unusual, and letting it reach 60 would only make
`dominant_signal` monotonous and the score incomparable between days.

What this module deliberately does not do
-----------------------------------------
No severity, no cause, no recommended action, no natural language. Stage 5
answers "how unusual, and on which measures". Stage 6 turns that into business
severity with deterministic rules; Stage 7 hypothesises causes with an LLM. The
separation is what allows the deterministic layer to grade the model's output
rather than the reverse.
"""

from __future__ import annotations

from typing import Callable, Sequence

from analytics import statistics as robust
from analytics.baseline import build_baseline
from analytics.models import (
    BaselineKind,
    BaselineStatus,
    DetectionResult,
    KpiObservation,
    SignalResult,
)

DETECTOR_VERSION = "v1.0.0"

# |z| below this is ordinary variation and contributes nothing. Gives the score
# a meaningful zero: an unremarkable day scores 0, not "however many signals
# happen to exist times their average noise".
SIGNAL_NOISE_FLOOR = 1.0

# Ceiling on a single signal's |z| before the floor is subtracted and the weight
# applied. Beyond this a signal is unmistakably extreme and letting it climb
# further would only make scores incomparable between days.
ROBUST_Z_CAP = 10.0

# A day is flagged at or above this. Derived, not chosen:
#   weight(revenue) * (ROBUST_Z_SIGNIFICANT - SIGNAL_NOISE_FLOOR)
# so revenue alone at the textbook 3.5 outlier level is exactly break-even.
ANOMALY_SCORE_THRESHOLD = 1.0 * (robust.ROBUST_Z_SIGNIFICANT - SIGNAL_NOISE_FLOOR)

# Below this FX coverage a KPI row's money columns are understated and scoring
# them would measure a data gap as a business event. 100 means "no order on that
# day is missing a rate".
MIN_FX_COMPLETENESS_PCT = 100.0


class _SignalSpec:
    """How one metric is extracted, weighted, and expressed as a deviation."""

    __slots__ = ("name", "weight", "extract", "deviation_is_absolute")

    def __init__(
        self,
        name: str,
        weight: float,
        extract: Callable[[KpiObservation], float | None],
        deviation_is_absolute: bool = False,
    ) -> None:
        self.name = name
        self.weight = weight
        self.extract = extract
        # Rates use an absolute difference; see statistics.absolute_deviation.
        self.deviation_is_absolute = deviation_is_absolute


SIGNALS: tuple[_SignalSpec, ...] = (
    _SignalSpec("revenue", 1.0, lambda o: o.net_revenue_usd),
    _SignalSpec("aov", 0.5, lambda o: o.average_order_value_usd),
    _SignalSpec("refund", 0.5, lambda o: o.refund_rate, deviation_is_absolute=True),
    _SignalSpec("orders", 0.5, lambda o: float(o.orders_count)),
)


def detect(
    observations: Sequence[KpiObservation],
    target: KpiObservation,
    detector_version: str = DETECTOR_VERSION,
) -> DetectionResult:
    """Score one date against the rest of the series.

    `observations` may contain any dates; only those strictly before `target`
    and with complete FX coverage are ever used as baseline (see baseline.py).
    """
    # An understated day cannot be judged. Recorded, not silently dropped, so
    # the gap is visible in the results table rather than as a missing row.
    if not target.is_complete or target.fx_completeness_pct < MIN_FX_COMPLETENESS_PCT:
        return _unscored(target, detector_version, BaselineStatus.INCOMPLETE_KPI)

    if not target.has_money:
        return _unscored(target, detector_version, BaselineStatus.INCOMPLETE_KPI)

    results: list[SignalResult] = []
    baseline_kind: BaselineKind | None = None
    baseline_size: int | None = None

    for spec in SIGNALS:
        observed = spec.extract(target)
        if observed is None:
            continue

        baseline = build_baseline(observations, target, spec.extract)
        if baseline is None:
            # Every signal shares the same history, so if one lacks a baseline
            # they all do. Nothing about this date can be judged.
            return _unscored(target, detector_version, BaselineStatus.INSUFFICIENT_HISTORY)

        # All four signals use the same tier, so recording the last is accurate.
        baseline_kind = baseline.kind
        baseline_size = baseline.size

        results.append(_score_signal(spec, float(observed), baseline.values))

    if not results:
        return _unscored(target, detector_version, BaselineStatus.INSUFFICIENT_HISTORY)

    score = sum(result.contribution for result in results)

    # Ranked by contribution, then by name so ties resolve identically on every
    # run - reproducibility extends to the explainability fields.
    ranked = sorted(results, key=lambda r: (-r.contribution, r.name))
    dominant = ranked[0].name if ranked[0].contribution > 0 else None

    return DetectionResult(
        calendar_date=target.calendar_date,
        detector_version=detector_version,
        baseline_status=BaselineStatus.SCORED,
        baseline_kind=baseline_kind,
        baseline_size=baseline_size,
        anomaly_score=score,
        is_anomaly=score >= ANOMALY_SCORE_THRESHOLD,
        signals=tuple(results),
        dominant_signal=dominant,
        signal_count=sum(1 for result in results if result.is_significant),
    )


def detect_series(
    observations: Sequence[KpiObservation],
    targets: Sequence[KpiObservation] | None = None,
    detector_version: str = DETECTOR_VERSION,
) -> list[DetectionResult]:
    """Score `targets` (default: every observation) against the full series.

    Results come back in calendar order, which makes any diff between two runs
    line up row for row.
    """
    scope = list(targets if targets is not None else observations)
    scope.sort(key=lambda o: o.calendar_date)
    return [detect(observations, target, detector_version) for target in scope]


def _score_signal(
    spec: _SignalSpec,
    observed: float,
    baseline_values: Sequence[float],
) -> SignalResult:
    baseline_median = robust.median(baseline_values)
    z = robust.robust_z_score(observed, baseline_values)

    if spec.deviation_is_absolute:
        deviation = robust.absolute_deviation(observed, baseline_median)
    else:
        deviation = robust.percent_deviation(observed, baseline_median)

    # z is None when the baseline has no dispersion at all. The signal then
    # contributes nothing rather than an invented number, and the NULL is
    # persisted so the gap is visible.
    if z is None:
        contribution = 0.0
        significant = False
    else:
        # Only the part of the deviation that exceeds ordinary variation counts.
        excess = min(abs(z), ROBUST_Z_CAP) - SIGNAL_NOISE_FLOOR
        contribution = spec.weight * max(excess, 0.0)
        significant = abs(z) >= robust.ROBUST_Z_SIGNIFICANT

    return SignalResult(
        name=spec.name,
        observed=observed,
        baseline_median=baseline_median,
        deviation=deviation,
        robust_z=z,
        contribution=contribution,
        is_significant=significant,
    )


def _unscored(
    target: KpiObservation,
    detector_version: str,
    status: BaselineStatus,
) -> DetectionResult:
    """A recorded non-verdict.

    is_anomaly is False and anomaly_score is None: absence of evidence is not
    evidence of normality, and a downstream consumer must be able to tell the
    difference between "we looked and it was fine" and "we could not look".
    baseline_status is how it tells.
    """
    return DetectionResult(
        calendar_date=target.calendar_date,
        detector_version=detector_version,
        baseline_status=status,
        baseline_kind=None,
        baseline_size=None,
        anomaly_score=None,
        is_anomaly=False,
        signals=(),
        dominant_signal=None,
        signal_count=0,
    )
