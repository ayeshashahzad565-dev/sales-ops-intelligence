"""Retrieval, filtering, schema and identity guarantees for `GET /orders`."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog import CURRENCIES, PRODUCTS, Channel, Region
from app.models import Order
from tests.conftest import TEST_END_DATE, TEST_HISTORY_DAYS, TEST_START_DATE


def test_returns_the_full_history(client: TestClient) -> None:
    body = client.get("/orders").json()

    assert body["count"] == len(body["orders"])
    # 90 days across four regions; the exact figure moves with the seed, so
    # assert the order of magnitude rather than a brittle constant.
    assert 2_500 < body["count"] < 6_000


def test_every_order_validates_against_the_schema(client: TestClient) -> None:
    """The response is the pipeline's contract, so all of it must be well-formed."""
    orders = client.get("/orders").json()["orders"]

    for row in orders:
        Order(**row)  # raises on any violation


def test_orders_use_known_reference_data(client: TestClient) -> None:
    orders = client.get("/orders").json()["orders"]

    known_skus = {product.sku for product in PRODUCTS}
    for row in orders:
        assert row["region"] in {r.value for r in Region}
        assert row["channel"] in {c.value for c in Channel}
        assert row["product"] in known_skus
        assert row["currency"] in CURRENCIES
        assert row["quantity"] >= 1
        assert row["unit_price"] >= 0
        # A refund can never exceed what the line was worth.
        assert row["refund_amount"] <= row["quantity"] * row["unit_price"] + 0.01


def test_order_ids_are_globally_unique(client: TestClient) -> None:
    orders = client.get("/orders").json()["orders"]
    ids = [row["order_id"] for row in orders]

    assert len(ids) == len(set(ids))


def test_results_are_sorted_and_stable(client: TestClient) -> None:
    """Ingestion diffs batches, so ordering has to be deterministic."""
    first = client.get("/orders").json()["orders"]
    second = client.get("/orders").json()["orders"]

    assert first == second

    keys = [(row["order_date"], row["order_id"]) for row in first]
    assert keys == sorted(keys)


def test_history_spans_the_configured_window(client: TestClient) -> None:
    orders = client.get("/orders").json()["orders"]
    dates = {date.fromisoformat(row["order_date"]) for row in orders}

    assert min(dates) == TEST_START_DATE
    assert max(dates) == TEST_END_DATE
    # Weekends are quiet but never empty, so every day should appear.
    assert len(dates) == TEST_HISTORY_DAYS


# --- date filtering ----------------------------------------------------------

def test_filters_to_a_single_day(client: TestClient) -> None:
    target = TEST_END_DATE - timedelta(days=3)

    body = client.get("/orders", params={"from": target.isoformat(), "to": target.isoformat()}).json()

    assert body["count"] > 0
    assert {row["order_date"] for row in body["orders"]} == {target.isoformat()}


def test_filters_are_inclusive_on_both_bounds(client: TestClient) -> None:
    start = TEST_END_DATE - timedelta(days=6)

    body = client.get(
        "/orders", params={"from": start.isoformat(), "to": TEST_END_DATE.isoformat()}
    ).json()

    dates = {date.fromisoformat(row["order_date"]) for row in body["orders"]}
    assert min(dates) == start
    assert max(dates) == TEST_END_DATE
    assert len(dates) == 7


def test_open_ended_bounds_work_independently(client: TestClient) -> None:
    cutoff = TEST_END_DATE - timedelta(days=10)

    upper_only = client.get("/orders", params={"to": cutoff.isoformat()}).json()
    lower_only = client.get("/orders", params={"from": cutoff.isoformat()}).json()
    everything = client.get("/orders").json()

    assert all(date.fromisoformat(r["order_date"]) <= cutoff for r in upper_only["orders"])
    assert all(date.fromisoformat(r["order_date"]) >= cutoff for r in lower_only["orders"])
    # The cutoff day appears in both halves, so they overlap by exactly one day.
    on_cutoff = sum(1 for r in everything["orders"] if r["order_date"] == cutoff.isoformat())
    assert upper_only["count"] + lower_only["count"] == everything["count"] + on_cutoff


def test_window_outside_the_history_returns_empty(client: TestClient) -> None:
    body = client.get("/orders", params={"from": "2020-01-01", "to": "2020-01-31"}).json()

    assert body == {"count": 0, "orders": []}


def test_reversed_window_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/orders",
        params={"from": TEST_END_DATE.isoformat(), "to": TEST_START_DATE.isoformat()},
    )

    assert response.status_code == 422
    assert "must not be later than" in response.json()["detail"]


def test_malformed_date_is_rejected(client: TestClient) -> None:
    assert client.get("/orders", params={"from": "09-08-2026"}).status_code == 422


@pytest.mark.parametrize("limit", [1, 50, 500])
def test_limit_caps_the_result(client: TestClient, limit: int) -> None:
    body = client.get("/orders", params={"limit": limit}).json()

    assert body["count"] == limit
    assert len(body["orders"]) == limit


def test_limit_is_applied_after_filtering(client: TestClient) -> None:
    target = TEST_END_DATE - timedelta(days=2)

    body = client.get(
        "/orders",
        params={"from": target.isoformat(), "to": target.isoformat(), "limit": 3},
    ).json()

    assert body["count"] == 3
    assert {row["order_date"] for row in body["orders"]} == {target.isoformat()}


def test_zero_limit_is_rejected(client: TestClient) -> None:
    assert client.get("/orders", params={"limit": 0}).status_code == 422
