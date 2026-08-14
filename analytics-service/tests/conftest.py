"""Shared fixtures and builders for the Stage 5 tests.

The synthetic series mirrors the live dataset in the two ways that matter:

* **Seasonality.** Weekday revenue is roughly double weekend revenue, as in the
  real data. A flat series would let a detector with no calendar awareness pass
  every test in this suite.

* **Dispersion.** Day-to-day variation is realistic (revenue swings about
  +/-18%), because the live generator applies lognormal noise with sigma 0.20.
  This matters more than it looks: an unrealistically smooth series makes the
  MAD tiny, and then an ordinary 30% day scores as a 5-sigma event. Tests built
  on a too-clean series would demand a detector far twitchier than the real data
  can support.

Everything is a fixed function of the day index - never a random draw, not even
a seeded one. Reproducibility is a property the detector is tested for, and
fixtures that could vary between runs would undermine those tests.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from analytics.models import KpiObservation

# Close to the live dataset (weekday ~12k, weekend ~5.8k).
WEEKDAY_REVENUE = 12_000.0
WEEKEND_REVENUE = 5_800.0
WEEKDAY_ORDERS = 52
WEEKEND_ORDERS = 23
BASE_AOV = 240.0
BASE_REFUND_RATE = 0.022

SERIES_START = date(2026, 5, 12)   # a Tuesday

# Distinguishes "argument not supplied" from "explicitly None". Without it,
# make_observation(net_revenue_usd=None) would silently fill in a default and a
# test for missing-money handling would test nothing.
_UNSET: Any = object()


def _wobble(index: int, amplitude: float, period: int, phase: float) -> float:
    """A smooth deterministic multiplier around 1.0.

    Sine rather than a repeating list of literals so values are spread evenly
    instead of clustering at hard extremes, and each metric gets its own period
    so the four signals are not perfectly correlated. Perfectly correlated
    signals would make every ordinary day score four times its true evidence.

    The periods are coprime with 7, so a given weekday sees the whole range of
    the cycle rather than the same point every week.
    """
    return 1.0 + amplitude * math.sin(2.0 * math.pi * index / period + phase)


def make_observation(
    calendar_date: date,
    *,
    net_revenue_usd: float | None = _UNSET,
    average_order_value_usd: float | None = _UNSET,
    refund_rate: float | None = _UNSET,
    orders_count: int | None = _UNSET,
    is_complete: bool = True,
    fx_completeness_pct: float = 100.0,
    orders_pending_fx: int = 0,
) -> KpiObservation:
    """One KPI observation, defaulting to a typical day of its own weekday.

    Passing an explicit None for a money field keeps it None, which is how the
    missing-money path is exercised.
    """
    iso_weekday = calendar_date.isoweekday()
    is_weekend = iso_weekday >= 6

    if net_revenue_usd is _UNSET:
        net_revenue_usd = WEEKEND_REVENUE if is_weekend else WEEKDAY_REVENUE
    if orders_count is _UNSET:
        orders_count = WEEKEND_ORDERS if is_weekend else WEEKDAY_ORDERS
    if average_order_value_usd is _UNSET:
        average_order_value_usd = BASE_AOV
    if refund_rate is _UNSET:
        refund_rate = BASE_REFUND_RATE

    return KpiObservation(
        calendar_date=calendar_date,
        day_of_week=iso_weekday,
        is_weekend=is_weekend,
        orders_count=int(orders_count),
        net_revenue_usd=net_revenue_usd,
        average_order_value_usd=average_order_value_usd,
        refund_rate=refund_rate,
        orders_pending_fx=orders_pending_fx,
        fx_completeness_pct=fx_completeness_pct,
        is_complete=is_complete,
    )


def build_series(days: int = 84, start: date = SERIES_START) -> list[KpiObservation]:
    """A clean seasonal series containing no anomalies.

    "Clean" means no injected event - not noise-free. Every day varies, and the
    detector is expected to call all of them normal.
    """
    observations: list[KpiObservation] = []

    for index in range(days):
        current = start + timedelta(days=index)
        is_weekend = current.isoweekday() >= 6

        revenue_base = WEEKEND_REVENUE if is_weekend else WEEKDAY_REVENUE
        orders_base = WEEKEND_ORDERS if is_weekend else WEEKDAY_ORDERS

        observations.append(
            make_observation(
                current,
                # Revenue is the noisiest, matching the live lognormal noise.
                net_revenue_usd=revenue_base * _wobble(index, 0.18, 13, 0.0),
                # Order value is steadier than the volume that drives revenue.
                average_order_value_usd=BASE_AOV * _wobble(index, 0.08, 11, 1.0),
                # Refund rates are proportionally the noisiest of all.
                refund_rate=BASE_REFUND_RATE * _wobble(index, 0.30, 19, 0.5),
                orders_count=round(orders_base * _wobble(index, 0.10, 17, 2.0)),
            )
        )

    return observations


def replace(
    series: list[KpiObservation],
    observation: KpiObservation,
) -> list[KpiObservation]:
    """Return a copy of `series` with `observation` substituted by date."""
    return [
        observation if o.calendar_date == observation.calendar_date else o
        for o in series
    ]
