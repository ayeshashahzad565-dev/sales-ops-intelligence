"""Controlled anomaly injection.

Design rule
-----------
These functions mutate the *underlying order data*. Nothing here writes an
"is_anomaly" flag, and no downstream consumer is told an anomaly happened. The
Stage 5 detector has to rediscover the event from the numbers alone - which is
the only way the detector is actually being tested rather than being handed the
answer.

Each strategy targets a different KPI, so the three are distinguishable:

    revenue_drop   lowers unit prices    -> revenue and AOV fall, order count flat
    refund_spike   raises refunds        -> refund rate jumps, gross revenue flat
    regional_drop  removes orders        -> order count and revenue fall together

This module is pure: it takes a list of order dicts, returns a new list and a
report. Persistence and validation live elsewhere.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from app.catalog import Region, round_money


class AnomalyType(StrEnum):
    """Scenarios that can be injected."""

    REVENUE_DROP = "revenue_drop"
    REFUND_SPIKE = "refund_spike"
    REGIONAL_DROP = "regional_drop"


@dataclass
class AnomalyOutcome:
    """What an injection actually did, for the operator-facing audit record.

    Money is reported per currency rather than as a single total. The service
    has no exchange rates - inventing some here would create a second,
    conflicting source of FX truth alongside the Frankfurter integration that
    Stage 3 owns. Per-currency figures are exact and need no rates.
    """

    orders_matched: int = 0
    orders_modified: int = 0
    orders_removed: int = 0
    revenue_before: dict[str, float] = field(default_factory=dict)
    revenue_after: dict[str, float] = field(default_factory=dict)
    refund_before: dict[str, float] = field(default_factory=dict)
    refund_after: dict[str, float] = field(default_factory=dict)
    removed_order_ids: list[str] = field(default_factory=list)
    description: str = ""


# --- measurement helpers -----------------------------------------------------

def line_value(order: dict) -> float:
    """Gross value of an order line, before refunds, in its own currency."""
    return order["quantity"] * order["unit_price"]


def revenue_by_currency(orders: list[dict]) -> dict[str, float]:
    """Gross revenue per currency."""
    totals: dict[str, float] = defaultdict(float)
    for order in orders:
        totals[order["currency"]] += line_value(order)
    return {currency: round(value, 2) for currency, value in sorted(totals.items())}


def refunds_by_currency(orders: list[dict]) -> dict[str, float]:
    """Refunded amount per currency."""
    totals: dict[str, float] = defaultdict(float)
    for order in orders:
        totals[order["currency"]] += order["refund_amount"]
    return {currency: round(value, 2) for currency, value in sorted(totals.items())}


def select_orders(
    orders: list[dict],
    target_date: date,
    region: Region | None,
) -> list[dict]:
    """Return the orders an injection would act on, in stable order."""
    return [
        order
        for order in orders
        if order["order_date"] == target_date
        and (region is None or order["region"] == region.value)
    ]


# --- strategies --------------------------------------------------------------
#
# Each takes the full order list and returns (new_order_list, outcome). Matched
# orders are mutated in place except for regional_drop, which filters.

def apply_revenue_drop(
    orders: list[dict],
    matched: list[dict],
    severity: float,
    rng: random.Random,
) -> tuple[list[dict], AnomalyOutcome]:
    """Cut unit prices so revenue falls by roughly `severity`.

    Order count is deliberately left untouched: a revenue drop with flat volume
    is an average-order-value collapse (discounting error, pricing bug, mix
    shift), which is a different diagnosis from lost demand. Refunds are scaled
    by the same factor so no order ends up refunded for more than it was worth.
    """
    retained = 1.0 - severity
    outcome = AnomalyOutcome(
        orders_matched=len(matched),
        revenue_before=revenue_by_currency(matched),
        refund_before=refunds_by_currency(matched),
    )

    for order in matched:
        currency = order["currency"]
        order["unit_price"] = round_money(order["unit_price"] * retained, currency)
        if order["refund_amount"] > 0:
            order["refund_amount"] = round_money(order["refund_amount"] * retained, currency)
        outcome.orders_modified += 1

    outcome.revenue_after = revenue_by_currency(matched)
    outcome.refund_after = refunds_by_currency(matched)
    outcome.description = (
        f"Reduced unit price by {severity:.0%} on {outcome.orders_modified} orders; "
        "order volume unchanged, so this presents as an average-order-value collapse."
    )
    return orders, outcome


def apply_refund_spike(
    orders: list[dict],
    matched: list[dict],
    severity: float,
    rng: random.Random,
) -> tuple[list[dict], AnomalyOutcome]:
    """Fully refund a `severity`-scaled share of matched orders.

    Baseline refund rates sit around 3-6% of orders, so the target share is
    mapped onto 5%-90% to guarantee the result clears the noise floor even at
    low severity. Gross revenue is untouched: this is a quality or fulfilment
    failure, not a demand failure.
    """
    target_share = min(0.90, 0.05 + 0.85 * severity)

    outcome = AnomalyOutcome(
        orders_matched=len(matched),
        revenue_before=revenue_by_currency(matched),
        refund_before=refunds_by_currency(matched),
    )

    already_refunded = [o for o in matched if o["refund_amount"] > 0]
    candidates = [o for o in matched if o["refund_amount"] <= 0]

    target_count = round(len(matched) * target_share)
    to_convert = max(0, target_count - len(already_refunded))
    # Any injection at all must move the number, even on a tiny matched set.
    to_convert = max(1, to_convert) if candidates else 0
    to_convert = min(to_convert, len(candidates))

    # Shuffle a copy so selection is reproducible but not simply the first N.
    shuffled = list(candidates)
    rng.shuffle(shuffled)

    for order in shuffled[:to_convert]:
        order["refund_amount"] = round_money(line_value(order), order["currency"])
        outcome.orders_modified += 1

    # Top up existing partial refunds to full, so the rate moves as intended.
    for order in already_refunded:
        full = round_money(line_value(order), order["currency"])
        if order["refund_amount"] < full:
            order["refund_amount"] = full
            outcome.orders_modified += 1

    outcome.revenue_after = revenue_by_currency(matched)
    outcome.refund_after = refunds_by_currency(matched)
    outcome.description = (
        f"Converted {outcome.orders_modified} of {outcome.orders_matched} orders to full "
        f"refunds (target refund share {target_share:.0%}); gross revenue unchanged."
    )
    return orders, outcome


def apply_regional_drop(
    orders: list[dict],
    matched: list[dict],
    severity: float,
    rng: random.Random,
) -> tuple[list[dict], AnomalyOutcome]:
    """Delete a `severity` share of a region's orders for the day.

    Deletion is the honest representation of lost demand: an order that never
    happened leaves no row. Removed ids are recorded on the anomaly so the
    change stays auditable despite being destructive.
    """
    outcome = AnomalyOutcome(
        orders_matched=len(matched),
        revenue_before=revenue_by_currency(matched),
        refund_before=refunds_by_currency(matched),
    )

    remove_count = min(len(matched), max(1, round(len(matched) * severity)))

    shuffled = list(matched)
    rng.shuffle(shuffled)
    removed = shuffled[:remove_count]
    removed_ids = {order["order_id"] for order in removed}

    surviving = [order for order in orders if order["order_id"] not in removed_ids]
    still_matched = [order for order in matched if order["order_id"] not in removed_ids]

    outcome.orders_removed = len(removed_ids)
    outcome.removed_order_ids = sorted(removed_ids)
    outcome.revenue_after = revenue_by_currency(still_matched)
    outcome.refund_after = refunds_by_currency(still_matched)
    outcome.description = (
        f"Removed {outcome.orders_removed} of {outcome.orders_matched} orders "
        f"({severity:.0%} of the day's volume); order count and revenue fall together."
    )
    return surviving, outcome


STRATEGIES = {
    AnomalyType.REVENUE_DROP: apply_revenue_drop,
    AnomalyType.REFUND_SPIKE: apply_refund_spike,
    AnomalyType.REGIONAL_DROP: apply_regional_drop,
}


def apply_anomaly(
    orders: list[dict],
    anomaly_type: AnomalyType,
    target_date: date,
    region: Region | None,
    severity: float,
    injection_index: int,
) -> tuple[list[dict], AnomalyOutcome]:
    """Apply `anomaly_type` to `orders` and report what changed.

    The RNG is seeded from the injection's own parameters, so replaying the same
    sequence of injections against the same dataset gives the same result -
    which is what makes a scripted demo reproducible.

    Returns:
        The full order list after mutation, and the outcome report.

    Raises:
        LookupError: if no orders match the date/region selector.
    """
    matched = select_orders(orders, target_date, region)
    if not matched:
        raise LookupError("no orders match the requested date and region")

    rng = random.Random(
        f"{anomaly_type.value}|{region.value if region else 'ALL'}|"
        f"{target_date.isoformat()}|{severity}|{injection_index}"
    )
    return STRATEGIES[anomaly_type](orders, matched, severity, rng)
