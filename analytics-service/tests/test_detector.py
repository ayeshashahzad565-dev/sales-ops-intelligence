"""Detection behaviour.

These tests describe what the detector should CONCLUDE, using synthetic series
whose shape mirrors the live data. None of them reference a real calendar date -
the detector has to reach its verdicts from the numbers.
"""

from __future__ import annotations

from analytics import statistics as robust
from analytics.detector import (
    ANOMALY_SCORE_THRESHOLD,
    DETECTOR_VERSION,
    ROBUST_Z_CAP,
    detect,
    detect_series,
)
from analytics.models import BaselineKind, BaselineStatus
from tests.conftest import (
    BASE_AOV,
    BASE_REFUND_RATE,
    WEEKDAY_ORDERS,
    WEEKDAY_REVENUE,
    WEEKEND_REVENUE,
    build_series,
    make_observation,
)


def _late_weekday(series, weekday: int = 3):
    """A late-series observation of the given ISO weekday, with full history."""
    return next(o for o in reversed(series) if o.day_of_week == weekday)


def _scored(series, target):
    result = detect(series, target)
    assert result.baseline_status is BaselineStatus.SCORED, result.baseline_status
    return result


# --- the headline case: a revenue collapse with corroboration -----------------

def test_a_severe_revenue_drop_with_aov_and_refund_corroboration_is_detected() -> None:
    """The shape of the injected anomaly: price cut plus refund spike.

    Revenue and AOV collapse together while order COUNT stays normal - demand
    did not disappear, each order simply became worth far less.
    """
    series = build_series(days=84)
    normal = _late_weekday(series)

    anomalous = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.39,
        average_order_value_usd=BASE_AOV * 0.37,
        refund_rate=0.357,
        orders_count=WEEKDAY_ORDERS,       # unchanged
    )
    series = [anomalous if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, anomalous)

    assert result.is_anomaly
    assert result.anomaly_score >= ANOMALY_SCORE_THRESHOLD

    # The interesting part: revenue on its own sits BELOW the 3.5 level at which
    # a single signal is called an outlier, so a per-signal threshold detector
    # would have missed this day entirely. It is caught because the evidence
    # combines - which is the whole argument for a multi-signal score.
    revenue = result.signal("revenue")
    assert abs(revenue.robust_z) < robust.ROBUST_Z_SIGNIFICANT
    assert revenue.contribution < ANOMALY_SCORE_THRESHOLD
    assert result.anomaly_score > revenue.contribution

    assert revenue.robust_z < 0 and revenue.deviation < -50
    assert result.signal("aov").robust_z < 0
    assert result.signal("refund").robust_z > 0
    # Volume is the signal that stayed put - that is diagnostic information.
    assert abs(result.signal("orders").robust_z) < 1.0


def test_corroborated_evidence_outscores_the_same_revenue_drop_alone() -> None:
    """The point of a multi-signal score."""
    series = build_series(days=84)
    normal = _late_weekday(series)

    revenue_only = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.39,
        orders_count=int(WEEKDAY_ORDERS * 0.39),
        average_order_value_usd=BASE_AOV,
        refund_rate=BASE_REFUND_RATE,
    )
    corroborated = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.39,
        average_order_value_usd=BASE_AOV * 0.37,
        refund_rate=0.357,
        orders_count=WEEKDAY_ORDERS,
    )

    def score(observation):
        replaced = [observation if o.calendar_date == normal.calendar_date else o
                    for o in series]
        return _scored(replaced, observation).anomaly_score

    assert score(corroborated) > score(revenue_only)


def test_an_elevated_refund_rate_strengthens_the_evidence() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    def score(refund_rate: float) -> float:
        observation = make_observation(
            normal.calendar_date,
            net_revenue_usd=WEEKDAY_REVENUE * 0.6,
            average_order_value_usd=BASE_AOV * 0.6,
            refund_rate=refund_rate,
            orders_count=WEEKDAY_ORDERS,
        )
        replaced = [observation if o.calendar_date == normal.calendar_date else o
                    for o in series]
        return _scored(replaced, observation).anomaly_score

    assert score(0.30) > score(BASE_REFUND_RATE)


def test_an_aov_collapse_strengthens_a_revenue_drop() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    def score(aov: float) -> float:
        observation = make_observation(
            normal.calendar_date,
            net_revenue_usd=WEEKDAY_REVENUE * 0.5,
            average_order_value_usd=aov,
            refund_rate=BASE_REFUND_RATE,
            orders_count=WEEKDAY_ORDERS,
        )
        replaced = [observation if o.calendar_date == normal.calendar_date else o
                    for o in series]
        return _scored(replaced, observation).anomaly_score

    assert score(BASE_AOV * 0.4) > score(BASE_AOV)


# --- the discrimination case: normal seasonality is not an anomaly ------------

def test_an_ordinary_weekend_day_is_not_flagged() -> None:
    """The failure mode calendar awareness exists to prevent.

    A typical Sunday sits far below the trailing all-days average, but it is a
    perfectly ordinary Sunday. A detector without day-of-week baselines would
    flag one every week and be switched off within a fortnight.
    """
    series = build_series(days=84)
    sunday = _late_weekday(series, weekday=7)

    result = _scored(series, sunday)

    assert not result.is_anomaly
    assert result.anomaly_score < ANOMALY_SCORE_THRESHOLD


def test_every_weekend_day_in_a_clean_series_is_normal() -> None:
    """Not one lucky Sunday - all of them."""
    series = build_series(days=84)

    results = [r for r in detect_series(series) if r.baseline_status is BaselineStatus.SCORED]
    weekend_dates = {o.calendar_date for o in series if o.is_weekend}
    weekend_results = [r for r in results if r.calendar_date in weekend_dates]

    assert weekend_results
    assert not any(r.is_anomaly for r in weekend_results)


def test_a_clean_seasonal_series_produces_no_anomalies_at_all() -> None:
    series = build_series(days=84)

    results = detect_series(series)

    assert not any(r.is_anomaly for r in results)


def test_a_strong_but_ordinary_weekend_day_stays_normal() -> None:
    """Above-median for its own weekday, far below the all-days mean.

    This is the live 2026-08-02 situation in synthetic form: ~45% of the 7-day
    moving average, yet entirely unremarkable for a Sunday.
    """
    series = build_series(days=84)
    sunday = _late_weekday(series, weekday=7)

    strong = make_observation(sunday.calendar_date, net_revenue_usd=WEEKEND_REVENUE * 1.3,
                              orders_count=int(WEEKDAY_ORDERS * 0.5))
    series = [strong if o.calendar_date == sunday.calendar_date else o for o in series]

    result = _scored(series, strong)
    weekday_median = WEEKDAY_REVENUE

    assert strong.net_revenue_usd < weekday_median * 0.7   # would alarm a blind detector
    assert not result.is_anomaly                            # but is normal for a Sunday


# --- spikes are anomalies too -------------------------------------------------

def test_a_severe_revenue_spike_is_detected() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    spike = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 3.0,
        average_order_value_usd=BASE_AOV * 3.0,
        orders_count=WEEKDAY_ORDERS,
    )
    series = [spike if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, spike)

    assert result.is_anomaly
    assert result.signal("revenue").robust_z > 0
    assert result.signal("revenue").deviation > 0


# --- signal accounting --------------------------------------------------------

def test_a_volume_collapse_shows_in_orders_not_aov() -> None:
    """Distinguishing 'fewer orders' from 'smaller orders'."""
    series = build_series(days=84)
    normal = _late_weekday(series)

    fewer = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.25,
        orders_count=int(WEEKDAY_ORDERS * 0.25),
        average_order_value_usd=BASE_AOV,      # each order is normal-sized
        refund_rate=BASE_REFUND_RATE,
    )
    series = [fewer if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, fewer)

    assert result.is_anomaly
    assert result.signal("orders").robust_z < 0
    assert abs(result.signal("aov").robust_z) < 1.0


def test_dominant_signal_is_the_largest_contributor() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    observation = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.2,
        average_order_value_usd=BASE_AOV * 0.95,
        refund_rate=BASE_REFUND_RATE,
        orders_count=int(WEEKDAY_ORDERS * 0.95),
    )
    series = [observation if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, observation)

    assert result.dominant_signal == "revenue"


def test_signal_count_counts_only_individually_significant_signals() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    observation = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.2,
        average_order_value_usd=BASE_AOV,
        refund_rate=BASE_REFUND_RATE,
        orders_count=WEEKDAY_ORDERS,
    )
    series = [observation if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, observation)

    assert result.signal_count >= 1
    assert result.signal_count <= 4
    significant = [s for s in result.signals if s.is_significant]
    assert len(significant) == result.signal_count


def test_no_single_signal_can_dominate_without_limit() -> None:
    """The cap keeps scores comparable between days."""
    series = build_series(days=84)
    normal = _late_weekday(series)

    absurd = make_observation(normal.calendar_date, net_revenue_usd=1.0,
                              average_order_value_usd=BASE_AOV, refund_rate=BASE_REFUND_RATE,
                              orders_count=WEEKDAY_ORDERS)
    series = [absurd if o.calendar_date == normal.calendar_date else o for o in series]

    result = _scored(series, absurd)

    assert result.signal("revenue").contribution <= ROBUST_Z_CAP * 1.0


# --- unscorable observations --------------------------------------------------

def test_insufficient_history_is_reported_not_guessed() -> None:
    series = build_series(days=84)

    result = detect(series, series[0])

    assert result.baseline_status is BaselineStatus.INSUFFICIENT_HISTORY
    assert result.anomaly_score is None
    assert not result.is_anomaly
    assert result.signals == ()


def test_an_incomplete_kpi_row_is_never_scored() -> None:
    """An FX gap understates revenue; scoring it would report a data problem
    as a business event - precisely the false positive that destroys trust."""
    series = build_series(days=84)
    normal = _late_weekday(series)

    incomplete = make_observation(
        normal.calendar_date,
        net_revenue_usd=WEEKDAY_REVENUE * 0.3,   # looks like a collapse
        is_complete=False,
        fx_completeness_pct=42.0,
        orders_pending_fx=30,
    )
    series = [incomplete if o.calendar_date == normal.calendar_date else o for o in series]

    result = detect(series, incomplete)

    assert result.baseline_status is BaselineStatus.INCOMPLETE_KPI
    assert not result.is_anomaly
    assert result.anomaly_score is None


def test_a_row_missing_money_is_not_scored() -> None:
    series = build_series(days=84)
    normal = _late_weekday(series)

    missing = make_observation(normal.calendar_date, net_revenue_usd=None)
    series = [missing if o.calendar_date == normal.calendar_date else o for o in series]

    result = detect(series, missing)

    assert result.baseline_status is BaselineStatus.INCOMPLETE_KPI
    assert not result.is_anomaly


def test_an_unscored_result_is_distinguishable_from_a_normal_one() -> None:
    """Absence of evidence must not read as evidence of normality."""
    series = build_series(days=84)

    unscored = detect(series, series[0])
    normal = _scored(series, _late_weekday(series))

    assert unscored.is_anomaly == normal.is_anomaly == False  # noqa: E712
    assert unscored.anomaly_score is None
    assert normal.anomaly_score is not None
    assert unscored.baseline_status is not normal.baseline_status


# --- determinism --------------------------------------------------------------

def test_detection_is_reproducible() -> None:
    series = build_series(days=84)

    first = detect_series(series)
    second = detect_series(series)

    assert [(r.calendar_date, r.anomaly_score, r.is_anomaly) for r in first] == \
           [(r.calendar_date, r.anomaly_score, r.is_anomaly) for r in second]


def test_detection_is_independent_of_input_ordering() -> None:
    series = build_series(days=84)

    forwards = detect_series(series)
    backwards = detect_series(list(reversed(series)))

    assert [(r.calendar_date, r.anomaly_score) for r in forwards] == \
           [(r.calendar_date, r.anomaly_score) for r in backwards]


def test_results_are_returned_in_calendar_order() -> None:
    series = build_series(days=84)

    results = detect_series(list(reversed(series)))

    dates = [r.calendar_date for r in results]
    assert dates == sorted(dates)


def test_detector_version_is_recorded_on_every_result() -> None:
    series = build_series(days=84)

    results = detect_series(series)

    assert all(r.detector_version == DETECTOR_VERSION for r in results)
    assert DETECTOR_VERSION.startswith("v")


def test_a_mature_series_uses_day_of_week_baselines() -> None:
    series = build_series(days=84)

    results = detect_series(series)
    late = [r for r in results[-14:] if r.baseline_status is BaselineStatus.SCORED]

    assert late
    assert all(r.baseline_kind is BaselineKind.DAY_OF_WEEK for r in late)
