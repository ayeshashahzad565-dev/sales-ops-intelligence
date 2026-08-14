"""Calendar-aware baseline construction.

The question this module answers is "what should this day be compared against?",
and getting it wrong is the single easiest way to build a detector that fires
every weekend and misses real events.

Why day-of-week matters here specifically
-----------------------------------------
In this dataset weekday net revenue runs ~11-13k and weekend ~5.7-6.4k. A Sunday
is roughly 45% of the trailing 7-day mean *by construction*, every single week.
A detector comparing days against an undifferentiated recent average would rank
"it is Sunday" as its strongest and most frequent finding.

So each day is compared against prior observations of the same weekday. Nothing
about Sunday is hard-coded: the rule is "same weekday", and the data supplies the
consequences.

Two invariants
--------------
1. Only observations STRICTLY BEFORE the target date are eligible. A baseline
   that includes the day being judged pulls the median toward it and hides
   exactly the deviation being looked for. Nothing here can see the future.

2. Only observations with complete FX coverage are eligible. An understated
   revenue figure in the baseline would depress the median and make a genuine
   drop look ordinary.

A past anomaly still sits in the baseline of later days - that is unavoidable
without knowing in advance which days were anomalous. It is why the dispersion
measures in `statistics.py` are robust ones: up to half the baseline can be
corrupted before the median or the MAD moves materially.
"""

from __future__ import annotations

from typing import Sequence

from analytics.models import Baseline, BaselineKind, KpiObservation

# A same-weekday baseline needs enough weeks behind it to have a defensible
# median and spread. Six is the smallest sample where a MAD is not dominated by
# one observation, and it costs six weeks of warm-up on a fresh warehouse.
MIN_DAY_OF_WEEK_OBSERVATIONS = 6

# Cap the window so the baseline tracks the recent regime rather than averaging
# over a business that has since grown. Eight same-weekday observations is about
# two months of that weekday.
MAX_DAY_OF_WEEK_OBSERVATIONS = 8

# Fallback tier: weekday-vs-weekend as a class. Accumulates roughly five times
# faster than a single weekday, so a young warehouse gets a usable - if coarser -
# baseline instead of nothing. Weekday multipliers span 0.92-1.12 and weekend
# 0.41-0.48, so each class is internally consistent enough to compare within.
MIN_DAY_TYPE_OBSERVATIONS = 10
MAX_DAY_TYPE_OBSERVATIONS = 20


def eligible_history(
    observations: Sequence[KpiObservation],
    target: KpiObservation,
) -> list[KpiObservation]:
    """Observations that may inform `target`'s baseline.

    Strictly earlier, complete FX coverage, and money present.
    """
    return [
        observation
        for observation in observations
        if observation.calendar_date < target.calendar_date
        and observation.is_complete
        and observation.has_money
    ]


def build_baseline(
    observations: Sequence[KpiObservation],
    target: KpiObservation,
    extract,
) -> Baseline | None:
    """Choose the comparison set for one metric of one date.

    Args:
        observations: The full KPI series, in any order.
        target: The date being judged.
        extract: Callable taking a KpiObservation and returning the metric value.

    Returns:
        The baseline to compare against, preferring same-weekday and falling back
        to same-day-type. None when neither tier has enough history, which the
        caller records as `insufficient_history` rather than guessing.
    """
    history = eligible_history(observations, target)

    same_weekday = [o for o in history if o.day_of_week == target.day_of_week]
    if len(same_weekday) >= MIN_DAY_OF_WEEK_OBSERVATIONS:
        window = _most_recent(same_weekday, MAX_DAY_OF_WEEK_OBSERVATIONS)
        return Baseline(
            kind=BaselineKind.DAY_OF_WEEK,
            values=tuple(float(extract(o)) for o in window),
        )

    same_day_type = [o for o in history if o.is_weekend == target.is_weekend]
    if len(same_day_type) >= MIN_DAY_TYPE_OBSERVATIONS:
        window = _most_recent(same_day_type, MAX_DAY_TYPE_OBSERVATIONS)
        return Baseline(
            kind=BaselineKind.DAY_TYPE,
            values=tuple(float(extract(o)) for o in window),
        )

    return None


def _most_recent(
    observations: Sequence[KpiObservation],
    limit: int,
) -> list[KpiObservation]:
    """The `limit` latest observations, oldest first.

    Sorted explicitly rather than trusting input order, so the window is the same
    whatever order the rows arrived in - which is part of the reproducibility
    guarantee.
    """
    ordered = sorted(observations, key=lambda o: o.calendar_date)
    return ordered[-limit:]
