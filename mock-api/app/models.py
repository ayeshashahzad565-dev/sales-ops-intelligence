"""Request and response schemas for the Mock Sales/Orders API.

`Order`'s field names are the contract the ingestion pipeline is built against,
so they deliberately mirror the columns of the future `salesops.fact_orders`
table rather than being shaped for convenience here.

Two fields on the admin models are named `anomaly_type` / `anomaly_date` in
Python but aliased to `type` / `date` in JSON. `type` and `date` shadow a
builtin and an imported symbol respectively; the alias keeps the wire format
exactly as specified without booby-trapping the class body.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.anomalies import AnomalyType
from app.catalog import Channel, Region, resolve_region

MAX_GENERATE_COUNT = 1_000


class Order(BaseModel):
    """A single sales order line as an upstream order-management system emits it.

    Monetary values are plain JSON numbers, which is what real order APIs
    return. They are converted to NUMERIC on the way into Postgres; no
    financial arithmetic happens in this service.
    """

    order_id: str = Field(
        description="Stable upstream identifier. Used as the pipeline's idempotency key.",
        examples=["ORD-2026-000101"],
    )
    order_date: date = Field(description="Date the order was placed (UTC).")
    region: Region = Field(description="Sales region code.", examples=["EMEA"])
    product: str = Field(description="Product SKU.", examples=["SKU-1042"])
    channel: Channel = Field(description="Acquisition channel.", examples=["web"])
    customer_id: str = Field(
        description="Stable customer identifier.", examples=["CUST-EMEA-0031"]
    )
    quantity: int = Field(ge=1, description="Units ordered.")
    unit_price: float = Field(ge=0, description="Price per unit, in `currency`.")
    currency: str = Field(
        pattern=r"^[A-Z]{3}$",
        description="ISO 4217 code. Normalised to USD during ingestion.",
        examples=["EUR"],
    )
    refund_amount: float = Field(
        ge=0,
        description="Amount refunded so far, in `currency`. 0 when not refunded.",
    )


class OrdersResponse(BaseModel):
    """Envelope returned by `GET /orders`."""

    count: int = Field(description="Number of orders in this response.")
    orders: list[Order]


class HealthResponse(BaseModel):
    """Envelope returned by `GET /health`."""

    status: str = Field(description="`ok` when the service is able to serve traffic.")
    service: str
    version: str


# --- admin: order generation -------------------------------------------------

class GenerateOrdersRequest(BaseModel):
    """Body for `POST /admin/generate-orders`."""

    count: int = Field(
        default=25,
        ge=1,
        le=MAX_GENERATE_COUNT,
        description="How many orders to create.",
    )
    order_date: date | None = Field(
        default=None,
        description=(
            "Date to stamp the new orders with. Defaults to the current business "
            "date - the later of today (UTC) and the newest date already stored."
        ),
    )


class GenerateOrdersResponse(BaseModel):
    """Envelope returned by `POST /admin/generate-orders`."""

    generated: int = Field(description="Number of orders created.")
    order_date: date = Field(description="Date the new orders were stamped with.")
    total_orders: int = Field(description="Size of the order book after generation.")
    orders: list[Order] = Field(description="The newly created orders.")


# --- admin: anomaly injection ------------------------------------------------

class InjectAnomalyRequest(BaseModel):
    """Body for `POST /admin/inject-anomaly`.

    `region` accepts either a code (`NA`) or a display name (`North America`),
    case-insensitively, and is normalised to a code.
    """

    model_config = ConfigDict(populate_by_name=True)

    anomaly_type: AnomalyType = Field(
        alias="type",
        description="Which scenario to inject.",
        examples=["revenue_drop"],
    )
    anomaly_date: date = Field(
        alias="date",
        description="Business date to affect. Must be a date with orders in it.",
    )
    region: Region | None = Field(
        default=None,
        description=(
            "Region to affect. Omit to affect every region on that date. "
            "Required for `regional_drop`."
        ),
        examples=["North America"],
    )
    severity: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        description=(
            "Magnitude, 0-1 exclusive of 0. Interpreted per scenario: share of "
            "price removed, share of orders refunded, or share of orders deleted."
        ),
    )

    @field_validator("region", mode="before")
    @classmethod
    def _normalise_region(cls, value: object) -> object:
        """Accept display names and lowercase codes; reject anything unknown."""
        if value is None or isinstance(value, Region):
            return value
        # resolve_region raises ValueError, which pydantic renders as a 422.
        return resolve_region(str(value))

    @model_validator(mode="after")
    def _require_region_for_regional_drop(self) -> InjectAnomalyRequest:
        if self.anomaly_type is AnomalyType.REGIONAL_DROP and self.region is None:
            raise ValueError("`region` is required for anomaly type `regional_drop`")
        return self


class AnomalyRecord(BaseModel):
    """An injected anomaly, as returned by `GET /admin/anomalies`.

    `revenue_*` and `refund_*` are keyed by currency because this service holds
    no exchange rates - see `anomalies.AnomalyOutcome`.
    """

    model_config = ConfigDict(populate_by_name=True)

    anomaly_id: str = Field(examples=["ANOM-0001"])
    anomaly_type: AnomalyType = Field(alias="type")
    anomaly_date: date = Field(alias="date")
    region: Region | None = Field(default=None, description="Affected region, if scoped.")
    region_name: str | None = Field(default=None, description="Human-readable region name.")
    severity: float
    injected_at: datetime = Field(description="Wall-clock time of injection (UTC).")

    orders_matched: int = Field(description="Orders selected by the date/region filter.")
    orders_modified: int = Field(description="Orders whose values were changed.")
    orders_removed: int = Field(description="Orders deleted from the order book.")

    revenue_before: dict[str, float]
    revenue_after: dict[str, float]
    refund_before: dict[str, float]
    refund_after: dict[str, float]

    removed_order_ids: list[str] = Field(
        default_factory=list,
        description="Ids deleted by `regional_drop`, so a destructive change stays auditable.",
    )
    description: str = Field(description="Plain-language summary of what changed.")


class AnomaliesResponse(BaseModel):
    """Envelope returned by `GET /admin/anomalies`."""

    count: int
    anomalies: list[AnomalyRecord]
