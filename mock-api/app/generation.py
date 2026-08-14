"""Deterministic synthetic order generation.

Determinism contract
--------------------
Given the same `seed`, `history_days` and `end_date`, `generate_history` returns
a byte-identical list of orders. Nothing here reads the clock or the global
`random` module: every draw comes from a `random.Random` instance owned by the
generator, and Faker is seeded per-instance rather than class-wide.

Realism model
-------------
Daily order volume per region is:

    round(weekday_mean x weekday_factor x growth_trend x lognormal_noise)

with an occasional "quiet day" knocked out on top. That gives four properties
the Stage 5 detector needs to be tested against honestly:

* weekly seasonality strong enough that a naive z-score would false-positive
  on every weekend if it ignored the day of week;
* right-skewed daily noise, because revenue is bounded below by zero and has a
  long upper tail - normally-distributed noise would be too tidy;
* a mild upward growth trend, so "expected value" is not simply the mean;
* genuine regional and product heterogeneity, so a per-region detector sees
  four distinguishable baselines rather than four copies of one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from faker import Faker

from app.catalog import (
    CURRENCIES,
    PRODUCTS,
    REGIONS,
    WEEKDAY_FACTORS,
    Channel,
    Product,
    Region,
    round_money,
)

# Total growth applied linearly across the history window (+14% over 90 days).
GROWTH_OVER_WINDOW = 0.14

# Multiplicative daily noise. sigma=0.20 on a lognormal gives roughly +/-20%
# typical swing with a long upper tail - close to observed B2B order counts.
DAILY_NOISE_SIGMA = 0.20

# Probability a region has an unexplained slow day (holiday, outage, whatever).
# Small enough not to swamp the injected anomalies it has to be distinguished from.
QUIET_DAY_PROBABILITY = 0.03
QUIET_DAY_FACTOR = 0.55

# Promotional discounts applied to list price, with weights.
DISCOUNT_LADDER: tuple[tuple[float, float], ...] = (
    (1.00, 0.72),  # full list price
    (0.90, 0.14),
    (0.85, 0.09),
    (0.75, 0.05),
)


@dataclass(frozen=True)
class Customer:
    """A member of a region's customer base.

    `loyalty` is a sampling weight, drawn from a Pareto distribution so that a
    minority of accounts generate a majority of orders. Without it, "repeat
    customer" behaviour disappears into a uniform draw.
    """

    customer_id: str
    name: str
    region: Region
    loyalty: float


class OrderGenerator:
    """Seeded generator for synthetic sales orders."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rng = random.Random(seed)
        self._customers = self._build_customer_base(seed)

    # -- customer base --------------------------------------------------------

    def _build_customer_base(self, seed: int) -> dict[Region, list[Customer]]:
        """Create a per-region customer base with locale-appropriate names.

        This is the one place Faker genuinely earns its place: it produces the
        realistic company names that will back `dim_customer` in Stage 2. The
        numeric and categorical order fields are drawn from explicit
        distributions instead, because those need to be tuned deliberately.
        """
        rng = random.Random(seed ^ 0x5EED)
        base: dict[Region, list[Customer]] = {}

        for offset, (region, profile) in enumerate(REGIONS.items()):
            fake = Faker(profile.faker_locale)
            fake.seed_instance(seed + offset * 1_000)

            customers: list[Customer] = []
            for index in range(profile.customer_pool_size):
                # Pareto tail, clamped so one account cannot dominate a region.
                loyalty = min(rng.paretovariate(1.35), 12.0)
                customers.append(
                    Customer(
                        customer_id=f"CUST-{region.value}-{index:04d}",
                        name=fake.company(),
                        region=region,
                        loyalty=loyalty,
                    )
                )
            base[region] = customers

        return base

    @property
    def customers(self) -> dict[Region, list[Customer]]:
        """The generated customer base, keyed by region."""
        return self._customers

    # -- volume model ---------------------------------------------------------

    def _daily_order_count(
        self,
        region: Region,
        order_date: date,
        day_index: int,
        total_days: int,
        rng: random.Random,
    ) -> int:
        profile = REGIONS[region]

        weekday_factor = WEEKDAY_FACTORS[order_date.weekday()]
        trend = 1.0 + GROWTH_OVER_WINDOW * (day_index / max(total_days - 1, 1))
        noise = rng.lognormvariate(0.0, DAILY_NOISE_SIGMA)

        expected = profile.weekday_order_mean * weekday_factor * trend * noise

        if rng.random() < QUIET_DAY_PROBABILITY:
            expected *= QUIET_DAY_FACTOR

        return max(0, round(expected))

    # -- per-order attribute draws --------------------------------------------

    def _pick_product(self, region: Region, rng: random.Random) -> Product:
        weights = [p.weight * p.regional_appeal.get(region, 1.0) for p in PRODUCTS]
        return rng.choices(PRODUCTS, weights=weights, k=1)[0]

    def _pick_channel(self, region: Region, rng: random.Random) -> Channel:
        weights = REGIONS[region].channel_weights
        return rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

    def _pick_currency(self, region: Region, rng: random.Random) -> str:
        weights = REGIONS[region].currency_weights
        return rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

    def _pick_customer(self, region: Region, rng: random.Random) -> Customer:
        pool = self._customers[region]
        return rng.choices(pool, weights=[c.loyalty for c in pool], k=1)[0]

    def _pick_quantity(self, product: Product, channel: Channel, rng: random.Random) -> int:
        # Partner resellers buy consumables by the case; everyone else buys a few.
        if channel is Channel.PARTNER and product.bulk_friendly and rng.random() < 0.38:
            return rng.randint(10, 60)

        roll = rng.random()
        if roll < 0.62:
            return 1
        if roll < 0.85:
            return 2
        if roll < 0.95:
            return 3
        return rng.randint(4, 8)

    def _unit_price(
        self, product: Product, region: Region, currency: str, rng: random.Random
    ) -> float:
        # strict=True: DISCOUNT_LADDER is pairs by construction, and a
        # malformed entry should fail here rather than silently truncate
        # the ladder and skew every price the generator produces.
        discounts, weights = zip(*DISCOUNT_LADDER, strict=True)
        discount = rng.choices(discounts, weights=weights, k=1)[0]

        price = (
            product.base_price_usd
            * REGIONS[region].price_index
            * CURRENCIES[currency].list_price_factor
            * discount
        )
        # Small residual jitter for contract-specific pricing.
        price *= rng.uniform(0.985, 1.015)
        return round_money(price, currency)

    def _refund_amount(
        self,
        line_value: float,
        region: Region,
        channel: Channel,
        currency: str,
        rng: random.Random,
    ) -> float:
        rate = REGIONS[region].refund_rate
        if channel is Channel.MOBILE:
            rate *= 1.35  # impulse purchases come back more often
        elif channel is Channel.FIELD_SALES:
            rate *= 0.55  # qualified, negotiated deals stick

        if rng.random() >= rate:
            return 0.0

        # Most refunds are the whole order; the rest are partial returns.
        if rng.random() < 0.62:
            return round_money(line_value, currency)
        return round_money(line_value * rng.uniform(0.20, 0.80), currency)

    # -- public API -----------------------------------------------------------

    def generate_for_date(
        self,
        order_date: date,
        count: int,
        start_sequence: int,
        rng: random.Random | None = None,
    ) -> list[dict]:
        """Generate exactly `count` orders dated `order_date`.

        Regions are sampled in proportion to their weekday means, so an ad-hoc
        batch has the same regional mix as the historical baseline.
        """
        # String seeds are hashed with SHA-512 by `random.Random`, so this is
        # reproducible across processes regardless of PYTHONHASHSEED.
        rng = rng or random.Random(
            f"{self._seed}|{order_date.isoformat()}|{count}|{start_sequence}"
        )

        regions = list(REGIONS.keys())
        region_weights = [REGIONS[r].weekday_order_mean for r in regions]

        orders: list[dict] = []
        for offset in range(count):
            region = rng.choices(regions, weights=region_weights, k=1)[0]
            orders.append(self._build_order(region, order_date, start_sequence + offset, rng))
        return orders

    def generate_history(
        self,
        end_date: date,
        history_days: int,
        start_sequence: int = 1,
    ) -> list[dict]:
        """Generate `history_days` of orders ending on `end_date` inclusive.

        Returns orders sorted by (order_date, order_id).
        """
        rng = random.Random(self._seed)
        start_date = end_date - timedelta(days=history_days - 1)

        orders: list[dict] = []
        sequence = start_sequence

        for day_index in range(history_days):
            order_date = start_date + timedelta(days=day_index)
            for region in REGIONS:
                daily_count = self._daily_order_count(
                    region, order_date, day_index, history_days, rng
                )
                for _ in range(daily_count):
                    orders.append(self._build_order(region, order_date, sequence, rng))
                    sequence += 1

        return orders

    # -- internals ------------------------------------------------------------

    def _build_order(
        self,
        region: Region,
        order_date: date,
        sequence: int,
        rng: random.Random,
    ) -> dict:
        product = self._pick_product(region, rng)
        channel = self._pick_channel(region, rng)
        currency = self._pick_currency(region, rng)
        customer = self._pick_customer(region, rng)
        quantity = self._pick_quantity(product, channel, rng)
        unit_price = self._unit_price(product, region, currency, rng)

        line_value = unit_price * quantity
        refund_amount = self._refund_amount(line_value, region, channel, currency, rng)

        return {
            "order_id": format_order_id(order_date, sequence),
            "order_date": order_date,
            "region": region.value,
            "product": product.sku,
            "channel": channel.value,
            "customer_id": customer.customer_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": currency,
            "refund_amount": refund_amount,
        }


def format_order_id(order_date: date, sequence: int) -> str:
    """Build an order id.

    The sequence is global and strictly increasing across the whole store, so
    uniqueness does not depend on the date component. The year prefix is there
    because upstream ERP systems conventionally include one, and because it
    makes ids readable at a glance.
    """
    return f"ORD-{order_date.year}-{sequence:06d}"


def parse_order_sequence(order_id: str) -> int:
    """Recover the sequence number from an order id, or 0 if unparseable."""
    tail = order_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0
