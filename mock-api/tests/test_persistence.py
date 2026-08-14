"""Durability across restarts.

`docker compose restart mock-api` must not reset the demo. Each test here
simulates a restart by tearing down the app and building a new one against the
same data directory - the same thing a container restart does, minus Docker.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import TEST_END_DATE, build_settings

TARGET_DATE = TEST_END_DATE - timedelta(days=4)


def _restart(settings: Settings) -> TestClient:
    """Return a client for a brand-new app on the same data directory."""
    return TestClient(create_app(settings))


def test_history_is_written_to_disk_on_first_start(settings: Settings) -> None:
    with TestClient(create_app(settings)):
        pass

    assert settings.orders_path.exists()
    assert settings.anomalies_path.exists()

    lines = settings.orders_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) > 2_000
    # JSONL: every line is a standalone object, so the file streams and greps.
    first = json.loads(lines[0])
    assert set(first) == {
        "order_id", "order_date", "region", "product", "channel",
        "customer_id", "quantity", "unit_price", "currency", "refund_amount",
    }


def test_history_is_not_regenerated_on_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        original = first.get("/orders").json()

    with _restart(settings) as second:
        assert second.get("/orders").json() == original


def test_generated_orders_survive_a_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        created = first.post("/admin/generate-orders", json={"count": 17}).json()
        expected_total = created["total_orders"]

    with _restart(settings) as second:
        body = second.get("/orders").json()
        assert body["count"] == expected_total
        stored_ids = {row["order_id"] for row in body["orders"]}
        assert {row["order_id"] for row in created["orders"]} <= stored_ids


def test_injected_anomalies_survive_a_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        record = first.post(
            "/admin/inject-anomaly",
            json={
                "type": "regional_drop",
                "region": "APAC",
                "date": TARGET_DATE.isoformat(),
                "severity": 0.7,
            },
        ).json()
        orders_after_injection = first.get("/orders").json()

    with _restart(settings) as second:
        # The mutated order data persisted, not just the record of it.
        assert second.get("/orders").json() == orders_after_injection

        anomalies = second.get("/admin/anomalies").json()
        assert anomalies["count"] == 1
        assert anomalies["anomalies"][0]["anomaly_id"] == record["anomaly_id"]
        assert anomalies["anomalies"][0]["removed_order_ids"] == record["removed_order_ids"]


def test_order_ids_stay_unique_when_generation_spans_restarts(settings: Settings) -> None:
    """The sequence counter is rebuilt from disk, so it must not restart at 1."""
    with TestClient(create_app(settings)) as first:
        first.post("/admin/generate-orders", json={"count": 20})

    with _restart(settings) as second:
        second.post("/admin/generate-orders", json={"count": 20})
        ids = [row["order_id"] for row in second.get("/orders").json()["orders"]]

    assert len(ids) == len(set(ids))


def test_anomaly_ids_continue_after_a_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        first.post(
            "/admin/inject-anomaly",
            json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.3},
        )

    with _restart(settings) as second:
        record = second.post(
            "/admin/inject-anomaly",
            json={"type": "refund_spike", "date": TARGET_DATE.isoformat(), "severity": 0.3},
        ).json()

        assert record["anomaly_id"] == "ANOM-0002"
        assert second.get("/admin/anomalies").json()["count"] == 2


def test_a_separate_data_directory_gets_its_own_order_book(tmp_path: Path) -> None:
    one = build_settings(tmp_path / "one")
    two = build_settings(tmp_path / "two")

    with TestClient(create_app(one)) as client_one:
        client_one.post("/admin/generate-orders", json={"count": 40})
        count_one = client_one.get("/orders").json()["count"]

    with TestClient(create_app(two)) as client_two:
        count_two = client_two.get("/orders").json()["count"]

    assert count_one == count_two + 40


def test_same_seed_and_end_date_reproduce_the_dataset(tmp_path: Path) -> None:
    """Two fresh volumes, same configuration, identical data."""
    one = build_settings(tmp_path / "a")
    two = build_settings(tmp_path / "b")

    with TestClient(create_app(one)) as client_one, TestClient(create_app(two)) as client_two:
        assert client_one.get("/orders").json() == client_two.get("/orders").json()


def test_a_different_seed_produces_a_different_dataset(tmp_path: Path) -> None:
    one = build_settings(tmp_path / "a")
    two = build_settings(tmp_path / "b", seed=999)

    with TestClient(create_app(one)) as client_one, TestClient(create_app(two)) as client_two:
        assert client_one.get("/orders").json() != client_two.get("/orders").json()


def test_no_temp_files_are_left_behind(settings: Settings) -> None:
    """Writes go through a temp file and an atomic rename; nothing should linger."""
    with TestClient(create_app(settings)) as client:
        client.post("/admin/generate-orders", json={"count": 5})
        client.post(
            "/admin/inject-anomaly",
            json={"type": "revenue_drop", "date": TARGET_DATE.isoformat(), "severity": 0.5},
        )

    assert list(settings.data_dir.glob("*.tmp")) == []
