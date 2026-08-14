"""Robust statistics.

Pure functions over plain floats. No I/O, no configuration, no database - so
every formula here can be unit-tested against hand-worked examples, which is the
only way to be sure a detector is measuring what it claims to.

Why robust statistics rather than mean and standard deviation
-------------------------------------------------------------
The baseline for any given day is built from previous comparable days, and some
of those previous days may themselves have been anomalous. Mean and standard
deviation have a breakdown point of 0: a single extreme value drags the mean
toward itself and inflates the standard deviation, which shifts the baseline
toward the anomaly and widens the band around it. The effect is doubly
perverse - one past anomaly makes the next one harder to see.

The median and the median absolute deviation have a breakdown point of 50%:
up to half the baseline can be arbitrarily corrupted before either moves
materially. That is the property this module is built on.

Why no numpy
------------
The series is ~90 observations and every operation here is a sort or a mean over
a handful of values. Explicit arithmetic in the standard library keeps each
formula visible in the code that computes it and makes the unit tests exact,
with no dependency on a library's floating-point implementation. If this ever
had to run over millions of rows, numpy would earn its place; at this size it
would only hide the mathematics.
"""

from __future__ import annotations

import statistics as stdlib_statistics
from typing import Sequence

# Scale factor converting MAD into a standard-deviation-equivalent for normally
# distributed data: 1 / Phi^-1(0.75) = 1 / 0.674489... = 1.4826.
# Applying it makes a robust z-score directly comparable to an ordinary z-score,
# which is what lets the conventional 3.5 threshold below carry its usual meaning.
MAD_TO_SIGMA = 1.4826

# The equivalent factor for the mean absolute deviation: sqrt(pi / 2) = 1.2533.
# Used only when MAD collapses to zero (see robust_z_score).
MEAN_AD_TO_SIGMA = 1.2533

# Iglewicz & Hoaglin (1993), "Volume 16: How to Detect and Handle Outliers":
# label an observation an outlier when its modified z-score exceeds 3.5.
ROBUST_Z_SIGNIFICANT = 3.5

# Floor on dispersion, as a fraction of |median|.
#
# Discrete, tightly-clustered series break naive robust scaling. Saturday order
# counts in the live data run 22, 23, 23, 23, 24, 23, 24 - the MAD is exactly 0,
# the mean-absolute-deviation fallback gives a scale of 0.54, and a perfectly
# ordinary 30-order Saturday lands at z = 13.
#
# That number is arithmetically correct and practically meaningless: it reflects
# a dispersion estimated from seven near-identical integers, not a real claim
# about how much Saturdays vary. Refusing to believe any series varies by less
# than 5% of its own median keeps a small sample from manufacturing certainty.
#
# The floor only ever binds on series that are already implausibly smooth; for
# revenue (median ~12,000, scale ~2,400) it is nowhere near active.
MIN_RELATIVE_SCALE = 0.05


def median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence.

    Even-length sequences average the two middle values, which is the standard
    definition and what `statistics.median` implements.
    """
    if not values:
        raise ValueError("median requires at least one observation")
    return float(stdlib_statistics.median(values))


def median_absolute_deviation(values: Sequence[float], centre: float | None = None) -> float:
    """MAD: the median of the absolute deviations from the centre.

        MAD = median(|x_i - median(x)|)

    Returned unscaled. `robust_z_score` applies MAD_TO_SIGMA.
    """
    if not values:
        raise ValueError("MAD requires at least one observation")
    mid = median(values) if centre is None else centre
    return float(stdlib_statistics.median([abs(value - mid) for value in values]))


def mean_absolute_deviation(values: Sequence[float], centre: float | None = None) -> float:
    """Mean of the absolute deviations from the centre.

    The fallback dispersion measure when MAD is zero. Less robust than MAD - its
    breakdown point is 0 - but it only comes into play when MAD has already
    collapsed, which means more than half the baseline is identical.
    """
    if not values:
        raise ValueError("mean absolute deviation requires at least one observation")
    mid = median(values) if centre is None else centre
    return float(sum(abs(value - mid) for value in values) / len(values))


def robust_scale(values: Sequence[float], centre: float | None = None) -> float | None:
    """A standard-deviation-equivalent dispersion, or None if there is none.

    Tries MAD first, falls back to the mean absolute deviation when MAD is zero
    (which happens when more than half the baseline shares one value), and
    returns None when both are zero.

    None means "this baseline has no spread, so deviation from it cannot be
    expressed in standard deviations". Returning 0 would produce a division by
    zero; returning a small epsilon would manufacture significance out of an
    arbitrary constant. None is the honest answer and the caller records it.
    """
    if not values:
        raise ValueError("robust_scale requires at least one observation")

    mid = median(values) if centre is None else centre

    # Never trust a dispersion smaller than this share of the level itself.
    floor = MIN_RELATIVE_SCALE * abs(mid)

    mad = median_absolute_deviation(values, mid)
    if mad > 0:
        return max(MAD_TO_SIGMA * mad, floor)

    mean_ad = mean_absolute_deviation(values, mid)
    if mean_ad > 0:
        return max(MEAN_AD_TO_SIGMA * mean_ad, floor)

    # Every value is identical. If the level itself is non-zero the floor still
    # gives a usable scale; only a baseline of all-zeros has nothing to offer.
    return floor if floor > 0 else None


def robust_z_score(observation: float, baseline: Sequence[float]) -> float | None:
    """Modified z-score of `observation` against `baseline`.

        z = (x - median(baseline)) / (1.4826 * MAD(baseline))

    Signed: negative means below the baseline, positive above. The caller
    decides whether direction matters for a given signal.

    Returns None when the baseline has no dispersion - see `robust_scale`.
    """
    if not baseline:
        raise ValueError("robust_z_score requires a non-empty baseline")

    mid = median(baseline)
    scale = robust_scale(baseline, mid)
    if scale is None:
        return None

    return (observation - mid) / scale


def percent_deviation(observation: float, baseline_median: float) -> float | None:
    """Percentage difference from the baseline median.

        pct = 100 * (x - median) / median

    Returns None when the median is zero, where a percentage is undefined.
    Reported for human readability; the score itself uses the robust z, which
    stays meaningful when a median approaches zero.
    """
    if baseline_median == 0:
        return None
    return 100.0 * (observation - baseline_median) / baseline_median


def absolute_deviation(observation: float, baseline_median: float) -> float:
    """Plain difference from the baseline median, in the metric's own units.

    Used for rates, where a percentage change against a near-zero baseline is
    numerically unstable: a refund rate moving 0.02 -> 0.35 is +1,650% as a
    percentage, a figure that says more about the small denominator than about
    the move.
    """
    return observation - baseline_median
