"""`POST /admin/inject-anomaly` and `GET /admin/anomalies`.

The load-bearing assertions here are the ones that measure the *order data*
before and after injection. An endpoint that recorded an anomaly without moving
the numbers would pass a naive test and fail the whole project's premise, so
every scenario test recomputes the affected KPI from `GET /orders` rather than
trusting the injection's own report.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_END_DATE

TARGET_DATE = TEST_END_DATE - timedelta(days=5)


# --- measurement helpers, computed from the public order feed ----------------

def _orders_on(client: TestClient, day, region: str | None = None) -> list[dict]:
    rows = client.get(
        "/orders", params={"from": day.isoformat(), "to": day.isoformat()}
    ).json()["orders"]
    return [r for r in rows if region is None or r["region"] == region]


def _gross_revenue(rows: list[dict]) -> float:
    return sum(r["quantity"] * r["unit_price"] for r in rows)


def _refund_total(rows: list[dict]) -> float:
    return sum(r["refund_amount"] for r in rows)


def _refund_rate(rows: list[dict]) -> float:
    gross = _gross_revenue(rows)
    return _refund_total(rows) / gross if gross else 0.0


# --- revenue_drop ------------------------------------------------------------

def test_revenue_drop_reduces_revenue_in_the_underlying_orders(client: TestClient) -> None:
    before = _orders_on(client, TARGET_DATE, "NA")
    revenue_before = _gross_revenue(before)

    response = client.post(
        "/admin/inject-anomaly",
        json={
            "type": "revenue_drop",
            "region": "North America",
            "date": TARGET_DATE.isoformat(),
            "severity": 0.6,
        },
    )
    assert response.status_code == 201

    after = _orders_on(client, TARGET_DATE, "NA")
    revenue_after = _gross_revenue(after)

    # ~60% of revenue removed; allow slack for per-currency rounding.
    assert revenue_after == pytest.approx(revenue_before * 0.4, rel=0.05)


def test_revenue_drop_leaves_order_volume_untouched(client: TestClient) -> None:
    """This is what distinguishes it from regional_drop for the detector."""
    before = len(_orders_on(client, TARGET_DATE, "NA"))

    client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "region": "NA", "date": TARGET_DATE.isoformat(), "severity": 0.5},
    )

    assert len(_orders_on(client, TARGET_DATE, "NA")) == before


def test_revenue_drop_never_leaves_a_refund_above_the_line_value(client: TestClient) -> None:
    """Refunds must scale with price, or the data becomes internally inconsistent."""
    client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.8},
    )

    for row in _orders_on(client, TARGET_DATE):
        assert row["refund_amount"] <= row["quantity"] * row["unit_price"] + 0.01


def test_revenue_drop_without_region_affects_every_region(client: TestClient) -> None:
    before = {r["order_id"]: r["unit_price"] for r in _orders_on(client, TARGET_DATE)}

    client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.5},
    )

    after = _orders_on(client, TARGET_DATE)
    assert {r["region"] for r in after} == {"NA", "EMEA", "APAC", "LATAM"}
    assert all(r["unit_price"] < before[r["order_id"]] for r in after)


def test_revenue_drop_leaves_other_days_alone(client: TestClient) -> None:
    """Containment matters: a leak would make the detector's job artificially easy."""
    neighbour = TARGET_DATE - timedelta(days=1)
    before = _gross_revenue(_orders_on(client, neighbour))

    client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.7},
    )

    assert _gross_revenue(_orders_on(client, neighbour)) == pytest.approx(before)


# --- refund_spike ------------------------------------------------------------

def test_refund_spike_raises_the_refund_rate(client: TestClient) -> None:
    before = _orders_on(client, TARGET_DATE, "EMEA")
    rate_before = _refund_rate(before)

    response = client.post(
        "/admin/inject-anomaly",
        json={
            "type": "refund_spike",
            "region": "EMEA",
            "date": TARGET_DATE.isoformat(),
            "severity": 0.7,
        },
    )
    assert response.status_code == 201

    after = _orders_on(client, TARGET_DATE, "EMEA")

    assert _refund_rate(after) > rate_before
    assert _refund_rate(after) > 0.3  # clears the ~5% baseline decisively


def test_refund_spike_leaves_gross_revenue_unchanged(client: TestClient) -> None:
    """A quality failure, not a demand failure - so gross revenue must not move."""
    before = _gross_revenue(_orders_on(client, TARGET_DATE, "EMEA"))

    client.post(
        "/admin/inject-anomaly",
        json={"type": "refund_spike", "region": "EMEA", "date": TARGET_DATE.isoformat(), "severity": 0.6},
    )

    assert _gross_revenue(_orders_on(client, TARGET_DATE, "EMEA")) == pytest.approx(before)


def test_refund_spike_scales_with_severity(client: TestClient) -> None:
    low_day = TARGET_DATE
    high_day = TARGET_DATE - timedelta(days=1)

    client.post(
        "/admin/inject-anomaly",
        json={"type": "refund_spike", "date": low_day.isoformat(), "severity": 0.2},
    )
    client.post(
        "/admin/inject-anomaly",
        json={"type": "refund_spike", "date": high_day.isoformat(), "severity": 0.9},
    )

    assert _refund_rate(_orders_on(client, high_day)) > _refund_rate(_orders_on(client, low_day))


# --- regional_drop -----------------------------------------------------------

def test_regional_drop_removes_orders(client: TestClient) -> None:
    before = _orders_on(client, TARGET_DATE, "APAC")

    response = client.post(
        "/admin/inject-anomaly",
        json={
            "type": "regional_drop",
            "region": "Asia Pacific",
            "date": TARGET_DATE.isoformat(),
            "severity": 0.8,
        },
    )
    assert response.status_code == 201

    after = _orders_on(client, TARGET_DATE, "APAC")

    assert len(after) < len(before)
    assert len(after) == pytest.approx(len(before) * 0.2, abs=1)
    assert _gross_revenue(after) < _gross_revenue(before)


def test_regional_drop_is_scoped_to_its_region(client: TestClient) -> None:
    other_before = len(_orders_on(client, TARGET_DATE, "NA"))

    client.post(
        "/admin/inject-anomaly",
        json={"type": "regional_drop", "region": "APAC", "date": TARGET_DATE.isoformat(), "severity": 0.9},
    )

    assert len(_orders_on(client, TARGET_DATE, "NA")) == other_before


def test_regional_drop_records_the_removed_ids(client: TestClient) -> None:
    """Deletion is destructive, so it has to stay auditable."""
    before_ids = {r["order_id"] for r in _orders_on(client, TARGET_DATE, "APAC")}

    record = client.post(
        "/admin/inject-anomaly",
        json={"type": "regional_drop", "region": "APAC", "date": TARGET_DATE.isoformat(), "severity": 0.5},
    ).json()

    after_ids = {r["order_id"] for r in _orders_on(client, TARGET_DATE, "APAC")}
    removed = set(record["removed_order_ids"])

    assert removed
    assert removed == before_ids - after_ids
    assert record["orders_removed"] == len(removed)


# --- the anomaly record ------------------------------------------------------

def test_injection_returns_a_populated_record(client: TestClient) -> None:
    record = client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "region": "NA", "date": TARGET_DATE.isoformat(), "severity": 0.6},
    ).json()

    assert record["anomaly_id"] == "ANOM-0001"
    assert record["type"] == "revenue_drop"
    assert record["date"] == TARGET_DATE.isoformat()
    assert record["region"] == "NA"
    assert record["region_name"] == "North America"
    assert record["severity"] == 0.6
    assert record["orders_matched"] > 0
    assert record["orders_modified"] > 0
    assert record["revenue_before"] and record["revenue_after"]
    assert record["description"]
    # Money is reported per currency, never as an invented cross-currency total.
    assert set(record["revenue_before"]) == {"USD"}


def test_region_accepts_codes_and_display_names(client: TestClient) -> None:
    for spelling in ["NA", "na", "North America", "north_america", "americas"]:
        response = client.post(
            "/admin/inject-anomaly",
            json={
                "type": "revenue_drop",
                "region": spelling,
                "date": TARGET_DATE.isoformat(),
                "severity": 0.1,
            },
        )
        assert response.status_code == 201, spelling
        assert response.json()["region"] == "NA"


def test_anomalies_endpoint_lists_every_injection(client: TestClient) -> None:
    assert client.get("/admin/anomalies").json() == {"count": 0, "anomalies": []}

    client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.4},
    )
    client.post(
        "/admin/inject-anomaly",
        json={"type": "refund_spike", "region": "EMEA", "date": TARGET_DATE.isoformat(), "severity": 0.5},
    )

    body = client.get("/admin/anomalies").json()

    assert body["count"] == 2
    assert [a["anomaly_id"] for a in body["anomalies"]] == ["ANOM-0001", "ANOM-0002"]
    assert [a["type"] for a in body["anomalies"]] == ["revenue_drop", "refund_spike"]


def test_severity_defaults_when_omitted(client: TestClient) -> None:
    record = client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat()},
    ).json()

    assert record["severity"] == 0.5


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"type": "nonsense", "date": "2026-08-04"}, "unknown anomaly type"),
        ({"type": "revenue_drop", "date": "2026-08-04", "severity": 0}, "severity must exceed 0"),
        ({"type": "revenue_drop", "date": "2026-08-04", "severity": 1.5}, "severity above 1"),
        ({"type": "revenue_drop", "date": "2026-08-04", "severity": -0.2}, "negative severity"),
        ({"type": "revenue_drop", "date": "2026-08-04", "region": "Atlantis"}, "unknown region"),
        ({"type": "revenue_drop", "date": "04-08-2026"}, "malformed date"),
        ({"type": "revenue_drop"}, "missing date"),
        ({"date": "2026-08-04"}, "missing type"),
        ({"type": "regional_drop", "date": "2026-08-04"}, "regional_drop needs a region"),
    ],
)
def test_invalid_parameters_are_rejected(client: TestClient, payload: dict, reason: str) -> None:
    response = client.post("/admin/inject-anomaly", json=payload)

    assert response.status_code == 422, reason


def test_unknown_region_error_lists_the_valid_options(client: TestClient) -> None:
    response = client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "region": "Atlantis"},
    )

    assert "EMEA" in response.text and "LATAM" in response.text


def test_date_with_no_orders_is_a_conflict_not_a_silent_noop(client: TestClient) -> None:
    """Silently succeeding here would waste a demo take."""
    response = client.post(
        "/admin/inject-anomaly",
        json={"type": "revenue_drop", "date": "2020-01-01", "severity": 0.5},
    )

    assert response.status_code == 409
    assert "No orders found" in response.json()["detail"]
    # The error tells the operator what range is actually available.
    assert str(TEST_END_DATE) in response.json()["detail"]
    assert client.get("/admin/anomalies").json()["count"] == 0


def test_failed_injection_does_not_modify_data(client: TestClient) -> None:
    before = client.get("/orders").json()

    client.post(
        "/admin/inject-anomaly",
        json={"type": "regional_drop", "region": "LATAM", "date": "2019-06-06", "severity": 0.9},
    )

    assert client.get("/orders").json() == before


def test_injections_compound(client: TestClient) -> None:
    """Documented behaviour: applying the same anomaly twice stacks."""
    revenue_before = _gross_revenue(_orders_on(client, TARGET_DATE, "NA"))

    for _ in range(2):
        client.post(
            "/admin/inject-anomaly",
            json={"type": "revenue_drop", "region": "NA", "date": TARGET_DATE.isoformat(), "severity": 0.5},
        )

    revenue_after = _gross_revenue(_orders_on(client, TARGET_DATE, "NA"))

    assert revenue_after == pytest.approx(revenue_before * 0.25, rel=0.05)
