"""`POST /admin/generate-orders` - fresh data for repeated ingestion runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.models import Order
from tests.conftest import TEST_END_DATE


def test_generates_the_requested_number_of_orders(client: TestClient) -> None:
    before = client.get("/orders").json()["count"]

    response = client.post("/admin/generate-orders", json={"count": 25})

    assert response.status_code == 201
    body = response.json()
    assert body["generated"] == 25
    assert len(body["orders"]) == 25
    assert body["total_orders"] == before + 25
    assert client.get("/orders").json()["count"] == before + 25


def test_new_orders_land_on_the_current_business_date(client: TestClient) -> None:
    """The pipeline polls "today", so generated orders have to show up there.

    "Today" means UTC, matching `OrderStore.current_business_date`. Comparing
    against a local `date.today()` made this test fail for the hour each day
    when the host's local date is ahead of UTC - a real flake, not a code bug.
    """
    body = client.post("/admin/generate-orders", json={"count": 10}).json()

    expected = max(TEST_END_DATE, datetime.now(UTC).date())
    assert body["order_date"] == expected.isoformat()
    assert {row["order_date"] for row in body["orders"]} == {expected.isoformat()}


def test_explicit_order_date_is_honoured(client: TestClient) -> None:
    target = TEST_END_DATE - timedelta(days=30)

    body = client.post(
        "/admin/generate-orders", json={"count": 5, "order_date": target.isoformat()}
    ).json()

    assert body["order_date"] == target.isoformat()
    assert {row["order_date"] for row in body["orders"]} == {target.isoformat()}


def test_generated_orders_validate_against_the_schema(client: TestClient) -> None:
    body = client.post("/admin/generate-orders", json={"count": 40}).json()

    for row in body["orders"]:
        Order(**row)


def test_order_ids_stay_unique_across_repeated_generation(client: TestClient) -> None:
    """The uniqueness guarantee that makes `order_id` a safe idempotency key."""
    for _ in range(5):
        client.post("/admin/generate-orders", json={"count": 30})

    ids = [row["order_id"] for row in client.get("/orders").json()["orders"]]

    assert len(ids) == len(set(ids))


def test_generated_orders_are_retrievable_by_date_filter(client: TestClient) -> None:
    body = client.post("/admin/generate-orders", json={"count": 12}).json()
    order_date = body["order_date"]

    filtered = client.get("/orders", params={"from": order_date, "to": order_date}).json()
    generated_ids = {row["order_id"] for row in body["orders"]}

    assert generated_ids <= {row["order_id"] for row in filtered["orders"]}


def test_count_defaults_when_body_is_empty(client: TestClient) -> None:
    body = client.post("/admin/generate-orders", json={}).json()

    assert body["generated"] == 25


def test_rejects_out_of_range_counts(client: TestClient) -> None:
    assert client.post("/admin/generate-orders", json={"count": 0}).status_code == 422
    assert client.post("/admin/generate-orders", json={"count": 1001}).status_code == 422
    assert client.post("/admin/generate-orders", json={"count": "many"}).status_code == 422


def test_rejects_malformed_order_date(client: TestClient) -> None:
    response = client.post(
        "/admin/generate-orders", json={"count": 5, "order_date": "not-a-date"}
    )

    assert response.status_code == 422
