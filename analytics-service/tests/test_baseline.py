"""Baseline construction: what a day is compared against.

The two invariants under test are the ones that break a detector silently if
they are wrong - future leakage, and blind (non-calendar) comparison.
"""

from __future__ import annotations

from analytics import baseline as baseline_module
from analytics.baseline import build_baseline, eligible_history
from analytics.models import BaselineKind
from tests.conftest import (
    WEEKDAY_REVENUE,
    WEEKEND_REVENUE,
    build_series,
    make_observation,
)

REVENUE = lambda o: o.net_revenue_usd  # noqa: E731 - terse extractor for tests


# --- no future leakage --------------------------------------------------------

def test_history_excludes_the_target_date_itself() -> None:
    """A baseline containing the day being judged pulls the median toward it."""
    series = build_series(days=84)
    target = series[70]

    history = eligible_history(series, target)

    assert all(o.calendar_date < target.calendar_date for o in history)
    assert target.calendar_date not in {o.calendar_date for o in history}


def test_history_excludes_every_future_date() -> None:
    series = build_series(days=84)
    target = series[40]

    history = eligible_history(series, target)

    assert max(o.calendar_date for o in history) < target.calendar_date
    assert len(history) == 40


def test_a_later_anomaly_cannot_affect_an_earlier_baseline() -> None:
    """Order of arrival must not change a verdict about the past."""
    series = build_series(days=84)
    target = series[40]

    before = build_baseline(series, target, REVENUE)

    # Corrupt a date AFTER the target.
    corrupted = list(series)
    corrupted[60] = make_observation(corrupted[60].calendar_date, net_revenue_usd=1.0)
    after = build_baseline(corrupted, target, REVENUE)

    assert before is not None and after is not None
    assert before.values == after.values


# --- calendar awareness -------------------------------------------------------

def test_a_sunday_is_compared_against_prior_sundays() -> None:
    series = build_series(days=84)
    sunday = next(o for o in series[60:] if o.day_of_week == 7)

    result = build_baseline(series, sunday, REVENUE)

    assert result is not None
    assert result.kind is BaselineKind.DAY_OF_WEEK
    # Weekend revenue is ~5,800; a blind baseline would sit near the ~9,000
    # all-days average and make every Sunday look like a 35% collapse.
    # Bounds are expressed against the weekday level rather than as literals, so
    # they state "weekend-scale" rather than restating the fixture's noise.
    assert all(value < WEEKDAY_REVENUE * 0.75 for value in result.values)


def test_a_wednesday_is_compared_against_prior_wednesdays() -> None:
    series = build_series(days=84)
    wednesday = next(o for o in series[60:] if o.day_of_week == 3)

    result = build_baseline(series, wednesday, REVENUE)

    assert result is not None
    assert result.kind is BaselineKind.DAY_OF_WEEK
    assert all(value > WEEKEND_REVENUE * 1.4 for value in result.values)


def test_weekday_and_weekend_baselines_are_materially_different() -> None:
    """If these overlapped, calendar awareness would be decorative."""
    series = build_series(days=84)
    saturday = next(o for o in series[60:] if o.day_of_week == 6)
    thursday = next(o for o in series[60:] if o.day_of_week == 4)

    weekend = build_baseline(series, saturday, REVENUE)
    weekday = build_baseline(series, thursday, REVENUE)

    assert weekend is not None and weekday is not None
    assert max(weekend.values) < min(weekday.values)


# --- window sizing ------------------------------------------------------------

def test_the_day_of_week_window_is_capped() -> None:
    """The baseline tracks the recent regime, not the whole history."""
    series = build_series(days=84)          # 12 of each weekday
    target = series[-1]

    result = build_baseline(series, target, REVENUE)

    assert result is not None
    assert result.size == baseline_module.MAX_DAY_OF_WEEK_OBSERVATIONS


def test_the_window_holds_the_most_recent_observations() -> None:
    series = build_series(days=84)
    target = series[-1]

    result = build_baseline(series, target, REVENUE)
    same_weekday = [
        o for o in series
        if o.day_of_week == target.day_of_week and o.calendar_date < target.calendar_date
    ]
    expected = tuple(
        o.net_revenue_usd
        for o in sorted(same_weekday, key=lambda o: o.calendar_date)[
            -baseline_module.MAX_DAY_OF_WEEK_OBSERVATIONS:
        ]
    )

    assert result is not None
    assert result.values == expected


# --- insufficient history -----------------------------------------------------

def test_no_baseline_at_the_very_start_of_a_series() -> None:
    """Nothing to compare against, so nothing is invented."""
    series = build_series(days=84)

    assert build_baseline(series, series[0], REVENUE) is None


def test_day_type_fallback_is_used_before_enough_same_weekday_history() -> None:
    """A young warehouse gets a coarser baseline rather than none at all."""
    series = build_series(days=84)

    # Day 20: ~2-3 prior same-weekday observations (below the min of 6), but
    # ~14 prior weekdays (above the day-type min of 10).
    target = next(o for o in series[18:26] if not o.is_weekend)
    result = build_baseline(series, target, REVENUE)

    assert result is not None
    assert result.kind is BaselineKind.DAY_TYPE


def test_the_fallback_still_separates_weekdays_from_weekends() -> None:
    series = build_series(days=84)
    weekend_target = next(o for o in series[18:26] if o.is_weekend)

    result = build_baseline(series, weekend_target, REVENUE)

    if result is not None and result.kind is BaselineKind.DAY_TYPE:
        assert all(value < 7_000 for value in result.values)


def test_the_series_eventually_graduates_to_day_of_week_baselines() -> None:
    series = build_series(days=84)

    kinds = []
    for target in series:
        result = build_baseline(series, target, REVENUE)
        kinds.append(None if result is None else result.kind)

    assert kinds[0] is None
    assert BaselineKind.DAY_TYPE in kinds
    assert kinds[-1] is BaselineKind.DAY_OF_WEEK


# --- incomplete observations are not baseline material ------------------------

def test_incomplete_kpi_rows_are_excluded_from_history() -> None:
    """An understated day in the baseline would depress the median."""
    series = build_series(days=84)
    series[30] = make_observation(series[30].calendar_date, is_complete=False,
                                  fx_completeness_pct=40.0, orders_pending_fx=9)

    history = eligible_history(series, series[60])

    assert all(o.is_complete for o in history)
    assert series[30].calendar_date not in {o.calendar_date for o in history}


def test_rows_missing_money_are_excluded_from_history() -> None:
    series = build_series(days=84)
    series[30] = make_observation(series[30].calendar_date, net_revenue_usd=None)

    history = eligible_history(series, series[60])

    assert all(o.has_money for o in history)


# --- determinism --------------------------------------------------------------

def test_baseline_is_independent_of_input_ordering() -> None:
    """Part of the reproducibility guarantee."""
    series = build_series(days=84)
    target = series[-1]

    forwards = build_baseline(series, target, REVENUE)
    backwards = build_baseline(list(reversed(series)), target, REVENUE)

    assert forwards is not None and backwards is not None
    assert forwards.values == backwards.values
