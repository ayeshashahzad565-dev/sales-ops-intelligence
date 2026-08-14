"""The liveness probe backs the container health check, so its shape is a contract."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "mock-sales-api"
    assert body["version"]


def test_health_does_not_require_the_order_book(settings) -> None:
    """`/health` must answer even with no data loaded.

    The container health check calls it; if it depended on the order book, a
    data problem would look like a dead container and Docker would restart it
    in a loop.
    """
    from app.routes import health

    assert health().status == "ok"
