"""Unit tests for the robust estimators.

Every expected value here is hand-worked from the definition rather than copied
from a previous run of the code. A test that asserts "the function returns what
the function returned" would pass through any algebra mistake unchanged.
"""

from __future__ import annotations

import pytest

from analytics import statistics as robust

# --- median -------------------------------------------------------------------

def test_median_of_odd_length_is_the_middle_value() -> None:
    assert robust.median([3.0, 1.0, 2.0]) == 2.0


def test_median_of_even_length_averages_the_two_middle_values() -> None:
    assert robust.median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_ignores_input_order() -> None:
    assert robust.median([5.0, 1.0, 4.0, 2.0, 3.0]) == 3.0


def test_median_resists_a_single_extreme_value() -> None:
    """The property the whole detector rests on.

    One corrupted observation moves the mean by ~2000; it moves the median by 0.
    """
    clean = [10.0, 11.0, 12.0, 13.0, 14.0]
    contaminated = [10.0, 11.0, 12.0, 13.0, 10_000.0]

    assert robust.median(clean) == robust.median(contaminated) == 12.0

    mean_clean = sum(clean) / len(clean)
    mean_contaminated = sum(contaminated) / len(contaminated)
    assert mean_contaminated > mean_clean * 100


def test_median_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValueError):
        robust.median([])


# --- MAD ----------------------------------------------------------------------

def test_mad_matches_a_hand_worked_example() -> None:
    # values  [1, 2, 3, 4, 5] -> median 3
    # |x - 3| [2, 1, 0, 1, 2] -> median 1
    assert robust.median_absolute_deviation([1.0, 2.0, 3.0, 4.0, 5.0]) == 1.0


def test_mad_is_zero_when_more_than_half_the_values_are_identical() -> None:
    # values  [5, 5, 5, 5, 99] -> median 5
    # |x - 5| [0, 0, 0, 0, 94] -> median 0
    assert robust.median_absolute_deviation([5.0, 5.0, 5.0, 5.0, 99.0]) == 0.0


def test_mad_accepts_a_supplied_centre() -> None:
    # |x - 0| for [1, 2, 3] -> median 2
    assert robust.median_absolute_deviation([1.0, 2.0, 3.0], centre=0.0) == 2.0


def test_mean_absolute_deviation_matches_a_hand_worked_example() -> None:
    # values [5, 5, 5, 5, 99], median 5, deviations [0,0,0,0,94], mean 94/5
    assert robust.mean_absolute_deviation([5.0, 5.0, 5.0, 5.0, 99.0]) == pytest.approx(18.8)


# --- robust scale -------------------------------------------------------------

def test_robust_scale_applies_the_mad_to_sigma_factor() -> None:
    # MAD of [1..5] is 1, so the scale is 1.4826 * 1 = 1.4826.
    # The 5%-of-median floor is 0.15, well below it, so it does not bind.
    assert robust.robust_scale([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(1.4826)


def test_robust_scale_falls_back_to_mean_absolute_deviation_when_mad_is_zero() -> None:
    values = [5.0, 5.0, 5.0, 5.0, 99.0]
    assert robust.median_absolute_deviation(values) == 0.0
    # 1.2533 * 18.8 = 23.56, above the 0.25 floor.
    assert robust.robust_scale(values) == pytest.approx(1.2533 * 18.8)


def test_the_relative_floor_binds_on_an_implausibly_smooth_series() -> None:
    """The live Saturday order-count case.

    22, 23, 23, 23, 24, 23, 24 has a MAD of exactly 0 and a mean absolute
    deviation of 3/7, which would scale to 0.54 - a claim that Saturdays vary by
    half an order. The floor refuses to believe it.
    """
    values = [22.0, 23.0, 23.0, 23.0, 24.0, 23.0, 24.0]

    assert robust.median_absolute_deviation(values) == 0.0
    unfloored = robust.MEAN_AD_TO_SIGMA * robust.mean_absolute_deviation(values)
    floor = robust.MIN_RELATIVE_SCALE * 23.0

    assert unfloored < floor
    assert robust.robust_scale(values) == pytest.approx(floor)


def test_the_floor_does_not_bind_on_a_realistically_noisy_series() -> None:
    """It must only rescue degenerate cases, never damp real dispersion."""
    revenue = [9_800.0, 12_400.0, 11_100.0, 13_900.0, 10_500.0, 12_900.0, 11_700.0]

    scale = robust.robust_scale(revenue)
    floor = robust.MIN_RELATIVE_SCALE * robust.median(revenue)

    assert scale is not None
    assert scale > floor


def test_an_entirely_constant_baseline_still_yields_a_usable_scale() -> None:
    """A constant baseline plus a wildly different observation IS an anomaly.

    Returning None here would make the signal contribute nothing, silently
    ignoring the largest deviation in the series. The floor gives a large but
    finite z instead.
    """
    scale = robust.robust_scale([7.0, 7.0, 7.0, 7.0])

    assert scale == pytest.approx(robust.MIN_RELATIVE_SCALE * 7.0)
    z = robust.robust_z_score(99.0, [7.0, 7.0, 7.0, 7.0])
    assert z is not None and z > robust.ROBUST_Z_SIGNIFICANT


def test_scale_is_none_only_when_the_level_itself_is_zero() -> None:
    """The one genuinely undefined case: an all-zero baseline has no scale at
    all, relative or absolute. None is the honest answer."""
    assert robust.robust_scale([0.0, 0.0, 0.0]) is None
    assert robust.robust_z_score(5.0, [0.0, 0.0, 0.0]) is None


# --- robust z-score -----------------------------------------------------------

def test_robust_z_score_matches_a_hand_worked_example() -> None:
    # baseline [1..5]: median 3, MAD 1, scale 1.4826
    # z for 10 = (10 - 3) / 1.4826 = 4.7214...
    z = robust.robust_z_score(10.0, [1.0, 2.0, 3.0, 4.0, 5.0])
    assert z == pytest.approx(7.0 / 1.4826, rel=1e-9)
    assert z == pytest.approx(4.7214, abs=1e-4)


def test_robust_z_score_is_zero_at_the_median() -> None:
    assert robust.robust_z_score(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 0.0


def test_robust_z_score_is_signed() -> None:
    baseline = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert robust.robust_z_score(5.0, baseline) < 0
    assert robust.robust_z_score(95.0, baseline) > 0


def test_robust_z_score_is_symmetric_about_the_median() -> None:
    baseline = [1.0, 2.0, 3.0, 4.0, 5.0]
    below = robust.robust_z_score(3.0 - 4.0, baseline)
    above = robust.robust_z_score(3.0 + 4.0, baseline)
    assert below == pytest.approx(-above)


def test_robust_z_score_is_not_inflated_by_a_contaminated_baseline() -> None:
    """A past anomaly in the baseline must not mask the next one.

    With an ordinary z-score the outlier inflates the standard deviation and the
    new observation looks unremarkable. The robust version is unmoved.
    """
    clean = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 101.0]
    contaminated = clean + [10.0]        # one past collapse

    z_clean = robust.robust_z_score(40.0, clean)
    z_contaminated = robust.robust_z_score(40.0, contaminated)

    assert z_clean is not None and z_contaminated is not None
    assert abs(z_clean) > robust.ROBUST_Z_SIGNIFICANT
    assert abs(z_contaminated) > robust.ROBUST_Z_SIGNIFICANT
    # The verdict barely moves despite 1-in-8 of the baseline being corrupt.
    assert abs(abs(z_contaminated) - abs(z_clean)) < abs(z_clean) * 0.5


def test_robust_z_score_rejects_an_empty_baseline() -> None:
    with pytest.raises(ValueError):
        robust.robust_z_score(1.0, [])


# --- deviations ---------------------------------------------------------------

def test_percent_deviation_matches_a_hand_worked_example() -> None:
    assert robust.percent_deviation(75.0, 100.0) == pytest.approx(-25.0)
    assert robust.percent_deviation(130.0, 100.0) == pytest.approx(30.0)
    assert robust.percent_deviation(100.0, 100.0) == 0.0


def test_percent_deviation_is_none_against_a_zero_baseline() -> None:
    """A percentage of zero is undefined - say so rather than dividing."""
    assert robust.percent_deviation(5.0, 0.0) is None


def test_absolute_deviation_is_used_for_rates() -> None:
    """Why refunds use an absolute difference.

    A refund rate moving 0.02 -> 0.35 is +1,650% as a percentage - a figure
    driven by the tiny denominator rather than by the size of the move.
    """
    assert robust.absolute_deviation(0.35, 0.02) == pytest.approx(0.33)
    assert robust.percent_deviation(0.35, 0.02) == pytest.approx(1650.0)


def test_significance_threshold_is_the_published_value() -> None:
    """Iglewicz & Hoaglin (1993) label |modified z| > 3.5 an outlier."""
    assert robust.ROBUST_Z_SIGNIFICANT == 3.5
