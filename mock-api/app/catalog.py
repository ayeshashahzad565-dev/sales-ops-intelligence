"""The business catalog: regions, channels, products, and pricing rules.

This is the static reference data the generator draws from. It is the closest
thing this service has to the dimension tables the pipeline will build in
Stage 2, so the shapes here are chosen to survive that translation.

Every number is a deliberate modelling choice, not a magic constant - the
comments say what each one is asserting about the business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Region(StrEnum):
    """Sales regions, stored as short codes the way an ERP would emit them."""

    NA = "NA"
    EMEA = "EMEA"
    APAC = "APAC"
    LATAM = "LATAM"


class Channel(StrEnum):
    """Acquisition channels."""

    WEB = "web"
    MOBILE = "mobile"
    PARTNER = "partner"
    FIELD_SALES = "field_sales"


# --- Currencies --------------------------------------------------------------
#
# `list_price_factor` converts a USD reference price into a plausible LOCAL LIST
# PRICE. It is NOT an exchange rate, and it deliberately does not track one:
# companies set local list prices on round numbers and revise them slowly, so
# they drift away from spot FX. Stage 3 normalises revenue to USD using live
# Frankfurter rates, and the two will not agree exactly. That disagreement is
# realistic and is the reason FX normalisation has to be a real pipeline step.
#
# `minor_units` drives rounding: JPY has none, so JPY prices are whole numbers.

@dataclass(frozen=True)
class CurrencyProfile:
    code: str
    list_price_factor: float
    minor_units: int


CURRENCIES: dict[str, CurrencyProfile] = {
    "USD": CurrencyProfile("USD", 1.00, 2),
    "EUR": CurrencyProfile("EUR", 0.95, 2),
    "GBP": CurrencyProfile("GBP", 0.82, 2),
    "JPY": CurrencyProfile("JPY", 158.0, 0),
    "BRL": CurrencyProfile("BRL", 5.60, 2),
}


def round_money(amount: float, currency: str) -> float:
    """Round to the currency's minor unit. JPY becomes a whole number."""
    profile = CURRENCIES.get(currency)
    digits = profile.minor_units if profile else 2
    value = round(amount, digits)
    return float(value) if digits else float(int(value))


# --- Regions -----------------------------------------------------------------

@dataclass(frozen=True)
class RegionProfile:
    """One sales region's operating characteristics."""

    code: Region
    display_name: str
    faker_locale: str

    # Mean orders on a normal weekday. Regions differ by roughly 4x, which is
    # what makes "regional volume collapsed" a detectable event rather than noise.
    weekday_order_mean: float

    # Currencies this region bills in, with sampling weights. EMEA bills in two,
    # which is what makes currency normalisation non-trivial downstream.
    currency_weights: dict[str, float]

    # Channel mix differs by region: APAC skews mobile, LATAM skews partner.
    channel_weights: dict[Channel, float]

    # Baseline share of orders that attract a refund. The refund_spike anomaly
    # has to push clear of this to be detectable.
    refund_rate: float

    # Size of the customer base. Smaller pools produce more repeat purchasing.
    customer_pool_size: int

    # Local price positioning relative to the USD reference price.
    price_index: float


REGIONS: dict[Region, RegionProfile] = {
    Region.NA: RegionProfile(
        code=Region.NA,
        display_name="North America",
        faker_locale="en_US",
        weekday_order_mean=18.0,
        currency_weights={"USD": 1.0},
        channel_weights={
            Channel.WEB: 0.46,
            Channel.MOBILE: 0.28,
            Channel.PARTNER: 0.16,
            Channel.FIELD_SALES: 0.10,
        },
        refund_rate=0.042,
        customer_pool_size=420,
        price_index=1.00,
    ),
    Region.EMEA: RegionProfile(
        code=Region.EMEA,
        display_name="Europe, Middle East & Africa",
        faker_locale="de_DE",
        weekday_order_mean=13.0,
        currency_weights={"EUR": 0.68, "GBP": 0.32},
        channel_weights={
            Channel.WEB: 0.42,
            Channel.MOBILE: 0.22,
            Channel.PARTNER: 0.24,
            Channel.FIELD_SALES: 0.12,
        },
        refund_rate=0.055,  # stronger statutory return rights
        customer_pool_size=310,
        price_index=1.06,
    ),
    Region.APAC: RegionProfile(
        code=Region.APAC,
        display_name="Asia Pacific",
        faker_locale="ja_JP",
        weekday_order_mean=9.0,
        currency_weights={"JPY": 1.0},
        channel_weights={
            Channel.WEB: 0.30,
            Channel.MOBILE: 0.48,  # mobile-first market
            Channel.PARTNER: 0.16,
            Channel.FIELD_SALES: 0.06,
        },
        refund_rate=0.028,
        customer_pool_size=240,
        price_index=1.12,
    ),
    Region.LATAM: RegionProfile(
        code=Region.LATAM,
        display_name="Latin America",
        faker_locale="pt_BR",
        weekday_order_mean=5.0,
        currency_weights={"BRL": 1.0},
        channel_weights={
            Channel.WEB: 0.26,
            Channel.MOBILE: 0.30,
            Channel.PARTNER: 0.36,  # heavily reseller-led
            Channel.FIELD_SALES: 0.08,
        },
        refund_rate=0.061,
        customer_pool_size=150,
        price_index=0.88,
    ),
}


# Accepted spellings for the `region` parameter on /admin/inject-anomaly, so an
# operator can type "North America" as readily as "NA". Keys are normalised
# (lowercased, separators collapsed to single spaces) by `resolve_region`.
REGION_ALIASES: dict[str, Region] = {
    "na": Region.NA,
    "north america": Region.NA,
    "namer": Region.NA,
    "americas": Region.NA,
    "emea": Region.EMEA,
    "europe": Region.EMEA,
    "europe middle east africa": Region.EMEA,
    "europe, middle east & africa": Region.EMEA,
    "apac": Region.APAC,
    "asia pacific": Region.APAC,
    "asia": Region.APAC,
    "latam": Region.LATAM,
    "latin america": Region.LATAM,
    "south america": Region.LATAM,
}


def resolve_region(value: str) -> Region:
    """Map a user-supplied region string onto a canonical `Region`.

    Raises:
        ValueError: with the accepted values listed, if nothing matches.
    """
    normalised = " ".join(str(value).replace("_", " ").replace("-", " ").lower().split())
    region = REGION_ALIASES.get(normalised)
    if region is None:
        accepted = ", ".join(sorted({r.value for r in Region}))
        raise ValueError(
            f"unknown region {value!r}; expected one of: {accepted} "
            "(display names also accepted)"
        )
    return region


# --- Products ----------------------------------------------------------------

@dataclass(frozen=True)
class Product:
    """A sellable SKU."""

    sku: str
    name: str
    base_price_usd: float

    # Global popularity weight, before regional adjustment.
    weight: float

    # Bought in bulk through the partner channel (accessories, consumables).
    bulk_friendly: bool = False

    # Per-region multipliers on `weight`. Absent regions default to 1.0.
    # This is what makes "product performance differs by region" true in the data.
    regional_appeal: dict[Region, float] = field(default_factory=dict)


PRODUCTS: tuple[Product, ...] = (
    Product(
        sku="SKU-1042",
        name="Atlas Core Licence",
        base_price_usd=149.00,
        weight=1.00,
        regional_appeal={Region.NA: 1.25, Region.LATAM: 0.70},
    ),
    Product(
        sku="SKU-2210",
        name="Atlas Field Kit",
        base_price_usd=79.50,
        weight=0.85,
        bulk_friendly=True,
        regional_appeal={Region.LATAM: 1.60, Region.APAC: 1.15},
    ),
    Product(
        sku="SKU-3375",
        name="Atlas Enterprise Suite",
        base_price_usd=289.00,
        weight=0.55,
        regional_appeal={Region.EMEA: 1.40, Region.LATAM: 0.45},
    ),
    Product(
        sku="SKU-4180",
        name="Atlas Analytics Add-on",
        base_price_usd=59.00,
        weight=0.70,
        regional_appeal={Region.NA: 1.20, Region.APAC: 0.80},
    ),
    Product(
        sku="SKU-5031",
        name="Atlas Sensor Pack",
        base_price_usd=24.90,
        weight=0.95,
        bulk_friendly=True,
        regional_appeal={Region.APAC: 1.45},
    ),
    Product(
        sku="SKU-6604",
        name="Atlas Support Plan",
        base_price_usd=199.00,
        weight=0.40,
        regional_appeal={Region.EMEA: 1.20, Region.NA: 1.10},
    ),
)


# Weekday demand multipliers. B2B-shaped: midweek peak, weekend trough. This is
# the seasonality that a naive z-score misses and the Stage 5 trend-aware
# detector is meant to handle - so it has to be present in the data.
WEEKDAY_FACTORS: dict[int, float] = {
    0: 1.06,  # Monday
    1: 1.12,
    2: 1.10,
    3: 1.04,
    4: 0.92,  # Friday
    5: 0.48,  # Saturday
    6: 0.41,  # Sunday
}
