"""HTTP surface.

Two routers, because they have two different audiences:

* `public_router`  - what the n8n ingestion pipeline consumes. Read-only, and
  shaped like an internal ERP endpoint: a filtered envelope, not a table dump.
* `admin_router`   - what the demo operator drives. Mutating, and never called
  by the pipeline.

Keeping them apart is what makes it obvious, later, that no part of the pipeline
depends on being told where the anomalies are.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.models import (
    AnomaliesResponse,
    AnomalyRecord,
    GenerateOrdersRequest,
    GenerateOrdersResponse,
    HealthResponse,
    InjectAnomalyRequest,
    Order,
    OrdersResponse,
)
from app.store import OrderStore

SERVICE_NAME = "mock-sales-api"
SERVICE_VERSION = "1.0.0"

MAX_ORDERS_PAGE = 20_000


def get_store(request: Request) -> OrderStore:
    """Resolve the store built during application startup."""
    return request.app.state.store


public_router = APIRouter()
admin_router = APIRouter(prefix="/admin", tags=["admin"])


# --- public ------------------------------------------------------------------

@public_router.get(
    "/health",
    response_model=HealthResponse,
    tags=["ops"],
    summary="Liveness probe",
)
def health() -> HealthResponse:
    """Return `ok` if the service can serve traffic.

    Backs the container health check, so it stays dependency-free: no disk read,
    no outbound call. A degraded order book is not a reason to restart the
    container.
    """
    return HealthResponse(status="ok", service=SERVICE_NAME, version=SERVICE_VERSION)


@public_router.get(
    "/orders",
    response_model=OrdersResponse,
    tags=["orders"],
    summary="List orders",
    responses={422: {"description": "`from` is later than `to`."}},
)
def list_orders(
    store: OrderStore = Depends(get_store),
    date_from: date | None = Query(
        default=None,
        alias="from",
        description="Inclusive lower bound on `order_date` (YYYY-MM-DD).",
    ),
    date_to: date | None = Query(
        default=None,
        alias="to",
        description="Inclusive upper bound on `order_date` (YYYY-MM-DD).",
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        le=MAX_ORDERS_PAGE,
        description="Cap on returned orders, applied after filtering and sorting.",
    ),
) -> OrdersResponse:
    """Return orders, optionally restricted to a date window.

    Results are sorted by `(order_date, order_id)`, so a given window always
    comes back in the same order - the ingestion workflow can rely on that when
    diffing batches.

    The intended ingestion pattern is a date window per run
    (`?from=2026-08-08&to=2026-08-08`) rather than offset pagination: it is
    naturally idempotent and it survives new orders arriving mid-backfill.
    """
    if date_from and date_to and date_from > date_to:
        # Literal 422 rather than `status.HTTP_422_*`: Starlette renamed that
        # constant, and the requirements pin a range that spans the rename.
        raise HTTPException(
            status_code=422,
            detail=f"`from` ({date_from}) must not be later than `to` ({date_to})",
        )

    rows = store.list_orders(date_from=date_from, date_to=date_to, limit=limit)
    return OrdersResponse(count=len(rows), orders=[Order(**row) for row in rows])


# --- admin -------------------------------------------------------------------

@admin_router.post(
    "/generate-orders",
    response_model=GenerateOrdersResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate new orders",
)
def generate_orders(
    payload: GenerateOrdersRequest,
    store: OrderStore = Depends(get_store),
) -> GenerateOrdersResponse:
    """Append newly generated orders to the order book.

    Orders land on the current business date by default, so calling this between
    ingestion runs is how you simulate a live system producing fresh data.
    `order_id` stays globally unique: the store owns a strictly increasing
    sequence that survives restarts.
    """
    order_date, new_orders = store.generate_orders(
        count=payload.count, order_date=payload.order_date
    )
    return GenerateOrdersResponse(
        generated=len(new_orders),
        order_date=order_date,
        total_orders=store.order_count,
        orders=[Order(**row) for row in new_orders],
    )


@admin_router.post(
    "/inject-anomaly",
    response_model=AnomalyRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Inject a controlled anomaly",
    responses={
        409: {"description": "No orders match the requested date and region."},
        422: {"description": "Unknown type or region, severity out of range, or missing region."},
    },
)
def inject_anomaly(
    payload: InjectAnomalyRequest,
    store: OrderStore = Depends(get_store),
) -> AnomalyRecord:
    """Change the underlying orders so an anomaly becomes statistically detectable.

    This rewrites real order rows - it does not set a flag. Downstream detection
    has to rediscover the event from the data, which is the point.

    Scenarios:

    * `revenue_drop` - cuts unit prices, leaving order count flat. Presents as an
      average-order-value collapse.
    * `refund_spike` - converts a share of orders to full refunds, leaving gross
      revenue flat. Presents as a refund-rate jump.
    * `regional_drop` - deletes a share of one region's orders. Presents as
      volume and revenue falling together. `region` is required.

    Injections compound: applying the same one twice stacks the effect.
    """
    try:
        record = store.inject_anomaly(
            anomaly_type=payload.anomaly_type,
            target_date=payload.anomaly_date,
            region=payload.region,
            severity=payload.severity,
        )
    except LookupError:
        oldest, newest = store.date_range
        scope = f"region {payload.region.value}" if payload.region else "any region"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No orders found for {payload.anomaly_date} in {scope}. "
                f"The order book covers {oldest} to {newest}."
            ),
        ) from None

    return AnomalyRecord(**record)


@admin_router.get(
    "/anomalies",
    response_model=AnomaliesResponse,
    summary="List injected anomalies",
)
def list_anomalies(store: OrderStore = Depends(get_store)) -> AnomaliesResponse:
    """Return every anomaly injected into this order book, oldest first.

    For the demo operator and for test assertions only. The analytics pipeline
    must never read this - it exists to check the detector's answer against, not
    to supply it.
    """
    records = store.list_anomalies()
    return AnomaliesResponse(
        count=len(records), anomalies=[AnomalyRecord(**record) for record in records]
    )
