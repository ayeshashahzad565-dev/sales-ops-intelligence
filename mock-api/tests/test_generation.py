"""The generator's determinism and realism claims, tested directly.

These bypass HTTP: the properties being asserted belong to the data model, and
checking them here keeps the API tests about the API.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from statistics import mean, stdev

from app.catalog import REGIONS, Region
from app.generation import OrderGenerator
from tests.conftest import TEST_END_DATE, TEST_HISTORY_DAYS, TEST_SEED


def _history(seed: int = TEST_SEED) -> list[dict]:
    return OrderGenerator(seed).generate_history(TEST_END_DATE, TEST_HISTORY_DAYS)


def test_same_seed_produces_identical_data() -> None:
    """The reproducibility guarantee the README makes."""
    assert _history() == _history()


def test_different_seeds_produce_different_data() -> None:
    assert _history(TEST_SEED) != _history(TEST_SEED + 1)


def test_weekends_are_quieter_than_weekdays() -> None:
    """Weekly seasonality has to be real, or the Stage 5 trend-aware detector
    has nothing to prove itself against."""
    orders = _history()

    per_day: Counter = Counter(row["order_date"] for row in orders)
    weekday_counts = [n for day, n in per_day.items() if day.weekday() < 5]
    weekend_counts = [n for day, n in per_day.items() if day.weekday() >= 5]

    assert mean(weekend_counts) < 0.7 * mean(weekday_counts)


def test_regional_volumes_are_clearly_separated() -> None:
    """Four regions must present four distinguishable baselines, not four copies."""
    counts = Counter(row["region"] for row in _history())

    assert counts[Region.NA] > counts[Region.EMEA] > counts[Region.APAC] > counts[Region.LATAM]
    # The largest region should be several times the smallest.
    assert counts[Region.NA] > 2.5 * counts[Region.LATAM]


def test_products_perform_differently_by_region() -> None:
    orders = _history()

    def share(region: Region, sku: str) -> float:
        regional = [row for row in orders if row["region"] == region]
        return sum(1 for row in regional if row["product"] == sku) / len(regional)

    # SKU-3375 is configured to over-index in EMEA and under-index in LATAM.
    assert share(Region.EMEA, "SKU-3375") > share(Region.LATAM, "SKU-3375")


def test_currencies_follow_the_regional_billing_rules() -> None:
    orders = _history()

    by_region: dict[str, set[str]] = {}
    for row in orders:
        by_region.setdefault(row["region"], set()).add(row["currency"])

    for region, profile in REGIONS.items():
        assert by_region[region] <= set(profile.currency_weights)

    # EMEA bills in two currencies; that is what makes FX normalisation real work.
    assert by_region[Region.EMEA] == {"EUR", "GBP"}


def test_repeat_customers_exist() -> None:
    """A uniform customer draw would make `dim_customer` meaningless."""
    counts = Counter(row["customer_id"] for row in _history())

    repeat_share = sum(1 for n in counts.values() if n > 1) / len(counts)
    assert repeat_share > 0.5
    assert max(counts.values()) > 10


def test_refund_rate_sits_in_a_realistic_band() -> None:
    """Baseline refunds must be low but non-zero - a refund_spike has to have
    something to spike above."""
    orders = _history()

    refunded = sum(1 for row in orders if row["refund_amount"] > 0)
    rate = refunded / len(orders)
    assert 0.02 < rate < 0.09


def test_jpy_prices_have_no_minor_units() -> None:
    jpy = [row for row in _history() if row["currency"] == "JPY"]

    assert jpy
    assert all(float(row["unit_price"]).is_integer() for row in jpy)


def test_daily_volume_is_noisy_not_flat() -> None:
    """Unrealistically clean data would make anomaly detection trivially easy.

    Measured as the coefficient of variation of weekday order counts, with
    weekends excluded so the weekly cycle is not mistaken for noise. The band
    is two-sided on purpose: too little spread and a detector looks better than
    it is; too much and a genuine anomaly cannot clear the noise floor.
    """
    orders = _history()

    weekday_counts = Counter(
        row["order_date"] for row in orders if row["order_date"].weekday() < 5
    )
    values = list(weekday_counts.values())
    coefficient_of_variation = stdev(values) / mean(values)

    assert 0.05 < coefficient_of_variation < 0.50


def test_history_covers_the_requested_window() -> None:
    orders = _history()
    dates = {row["order_date"] for row in orders}

    assert max(dates) == TEST_END_DATE
    assert min(dates) == TEST_END_DATE - timedelta(days=TEST_HISTORY_DAYS - 1)


def test_generated_ids_continue_the_sequence() -> None:
    generator = OrderGenerator(TEST_SEED)

    batch = generator.generate_for_date(TEST_END_DATE, count=5, start_sequence=9_000)
    ids = [row["order_id"] for row in batch]

    assert ids == [f"ORD-{TEST_END_DATE.year}-{9_000 + i:06d}" for i in range(5)]
