"""Mock Sales/Orders API - application factory.

Stands in for the internal ERP / order-management REST API that a real Sales &
Revenue Ops pipeline would ingest from. Such systems are never public, so
simulating one is the realistic pattern - and it keeps the pipeline demo
reproducible instead of dependent on a third party's uptime.

The factory exists so tests can build an app against a temporary data directory
without touching the environment or the real `/data` volume. The store is built
during the lifespan rather than at import time, so importing this module has no
filesystem side effects.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config import Settings
from app.routes import SERVICE_VERSION, admin_router, public_router
from app.store import OrderStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

OPENAPI_TAGS = [
    {"name": "ops", "description": "Liveness probes. Used by the container health check."},
    {"name": "orders",
     "description": "Read-only order data. This is what the n8n pipeline consumes."},
    {
        "name": "admin",
        "description": (
            "Operator controls for demos and testing. Generates orders and injects "
            "anomalies by rewriting the underlying data. The analytics pipeline "
            "never calls these."
        ),
    },
]

DESCRIPTION = """
Simulated upstream order-management system for the Sales & Revenue Operations
Intelligence Pipeline.

Serves a seeded 90-day synthetic order history with realistic weekday
seasonality, regional volume differences, multi-currency billing, repeat
customers and a baseline refund rate. Operators can append fresh orders and
inject controlled anomalies that the downstream detector must discover on its
own - injection changes real order rows, never a flag.
""".strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Overrides the environment-derived configuration. Tests pass a
            temporary data directory and a pinned history end date here.
    """
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = OrderStore(resolved)
        store.bootstrap()
        app.state.store = store
        yield

    app = FastAPI(
        title="Mock Sales/Orders API",
        version=SERVICE_VERSION,
        summary="Simulated upstream order-management system for the Sales & Revenue Ops pipeline.",
        description=DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.include_router(public_router)
    app.include_router(admin_router)
    return app


app = create_app()
