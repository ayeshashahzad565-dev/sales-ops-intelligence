"""The order book: in-memory state with flat-file persistence.

Persistence choice
------------------
Orders live in `orders.jsonl` (one JSON object per line) and injected anomalies
in `anomalies.json`, both on a Docker named volume at `/data`.

Why not a database: this service *simulates* the upstream system the pipeline
reads from. Giving it its own database would mean the project's first real
schema belonged to the mock rather than to the analytics model, and would add a
component whose only job is to hold a few thousand rows the pipeline is about
to copy out anyway. JSONL is inspectable with `head`, diffable, trivially
seedable, and needs no migration story.

Why the whole file is rewritten on every mutation: anomaly injection *edits and
deletes* existing rows, so append-only does not fit. At a few thousand orders a
full rewrite is sub-millisecond, and doing it atomically (temp file plus
`os.replace`) means a crash mid-write cannot leave a half-written order book.

Concurrency: FastAPI runs `def` endpoints in a thread pool, so mutations really
can overlap. A single re-entrant lock guards all state; contention is
irrelevant at this scale and correctness is not.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, date, datetime
from pathlib import Path

from app.anomalies import AnomalyOutcome, AnomalyType, apply_anomaly
from app.catalog import REGIONS, Region
from app.config import Settings
from app.generation import OrderGenerator, parse_order_sequence

logger = logging.getLogger(__name__)


class OrderStore:
    """Owns every order and anomaly the service knows about."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._generator = OrderGenerator(settings.seed)
        self._orders: list[dict] = []
        self._anomalies: list[dict] = []
        self._next_sequence = 1

    # -- lifecycle ------------------------------------------------------------

    def bootstrap(self) -> None:
        """Load persisted state, or generate the historical baseline if absent.

        Generation happens exactly once per data volume. Changing
        `MOCK_API_SEED` or `MOCK_API_HISTORY_DAYS` afterwards has no effect
        until the volume is removed - the persisted file wins, because silently
        regenerating history under a running pipeline would be worse.
        """
        with self._lock:
            self._settings.data_dir.mkdir(parents=True, exist_ok=True)

            if self._settings.orders_path.exists():
                self._orders = self._read_orders()
                self._anomalies = self._read_anomalies()
                logger.info(
                    "Loaded %d orders and %d anomalies from %s",
                    len(self._orders),
                    len(self._anomalies),
                    self._settings.data_dir,
                )
            else:
                self._orders = self._generator.generate_history(
                    end_date=self._settings.history_end_date,
                    history_days=self._settings.history_days,
                )
                self._anomalies = []
                logger.info(
                    "Generated %d orders across %d days ending %s (seed=%d)",
                    len(self._orders),
                    self._settings.history_days,
                    self._settings.history_end_date,
                    self._settings.seed,
                )
                self._persist()

            self._next_sequence = self._compute_next_sequence()

    # -- queries --------------------------------------------------------------

    @property
    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)

    @property
    def date_range(self) -> tuple[date | None, date | None]:
        """Oldest and newest order dates, or (None, None) when empty."""
        with self._lock:
            if not self._orders:
                return None, None
            dates = [order["order_date"] for order in self._orders]
            return min(dates), max(dates)

    def current_business_date(self) -> date:
        """The date new orders should be stamped with.

        The later of today and the newest stored date, so a pinned historical
        dataset still accepts new orders on its own last day.
        """
        _, latest = self.date_range
        today = datetime.now(UTC).date()
        return max(latest, today) if latest else today

    def list_orders(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return orders in the window, sorted by (order_date, order_id).

        Both bounds are inclusive. Returns copies, so callers cannot mutate
        stored state by accident.
        """
        with self._lock:
            selected = [
                dict(order)
                for order in self._orders
                if (date_from is None or order["order_date"] >= date_from)
                and (date_to is None or order["order_date"] <= date_to)
            ]

        selected.sort(key=lambda order: (order["order_date"], order["order_id"]))
        return selected[:limit] if limit is not None else selected

    def list_anomalies(self) -> list[dict]:
        """Return every injected anomaly, newest last."""
        with self._lock:
            return [dict(record) for record in self._anomalies]

    # -- mutations ------------------------------------------------------------

    def generate_orders(self, count: int, order_date: date | None = None) -> tuple[date, list[dict]]:
        """Append `count` new orders and persist.

        Returns the date used and the new orders.
        """
        with self._lock:
            target_date = order_date or self.current_business_date()

            new_orders = self._generator.generate_for_date(
                order_date=target_date,
                count=count,
                start_sequence=self._next_sequence,
            )
            self._orders.extend(new_orders)
            self._next_sequence += len(new_orders)
            self._persist()

            return target_date, [dict(order) for order in new_orders]

    def inject_anomaly(
        self,
        anomaly_type: AnomalyType,
        target_date: date,
        region: Region | None,
        severity: float,
    ) -> dict:
        """Mutate the order book so `anomaly_type` becomes statistically visible.

        Raises:
            LookupError: if no orders match the date/region selector.
        """
        with self._lock:
            index = len(self._anomalies) + 1

            # Raises LookupError when nothing matches; the route maps that to 409.
            self._orders, outcome = apply_anomaly(
                orders=self._orders,
                anomaly_type=anomaly_type,
                target_date=target_date,
                region=region,
                severity=severity,
                injection_index=index,
            )

            record = self._build_record(index, anomaly_type, target_date, region, severity, outcome)
            self._anomalies.append(record)
            self._persist()

            return dict(record)

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _build_record(
        index: int,
        anomaly_type: AnomalyType,
        target_date: date,
        region: Region | None,
        severity: float,
        outcome: AnomalyOutcome,
    ) -> dict:
        return {
            "anomaly_id": f"ANOM-{index:04d}",
            "anomaly_type": anomaly_type.value,
            "anomaly_date": target_date,
            "region": region.value if region else None,
            "region_name": REGIONS[region].display_name if region else None,
            "severity": severity,
            "injected_at": datetime.now(UTC),
            "orders_matched": outcome.orders_matched,
            "orders_modified": outcome.orders_modified,
            "orders_removed": outcome.orders_removed,
            "revenue_before": outcome.revenue_before,
            "revenue_after": outcome.revenue_after,
            "refund_before": outcome.refund_before,
            "refund_after": outcome.refund_after,
            "removed_order_ids": outcome.removed_order_ids,
            "description": outcome.description,
        }

    def _compute_next_sequence(self) -> int:
        if not self._orders:
            return 1
        return max(parse_order_sequence(order["order_id"]) for order in self._orders) + 1

    # -- persistence ----------------------------------------------------------

    def _persist(self) -> None:
        """Write both files atomically. Caller must hold the lock."""
        lines = "".join(
            json.dumps(_encode_order(order), separators=(",", ":")) + "\n"
            for order in self._orders
        )
        _atomic_write(self._settings.orders_path, lines)

        payload = json.dumps(
            [_encode_anomaly(record) for record in self._anomalies], indent=2
        )
        _atomic_write(self._settings.anomalies_path, payload + "\n")

    def _read_orders(self) -> list[dict]:
        orders: list[dict] = []
        with self._settings.orders_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    orders.append(_decode_order(json.loads(raw)))
                except (ValueError, KeyError) as exc:
                    raise ValueError(
                        f"{self._settings.orders_path}:{line_number} is not a valid order row"
                    ) from exc
        return orders

    def _read_anomalies(self) -> list[dict]:
        if not self._settings.anomalies_path.exists():
            return []
        raw = self._settings.anomalies_path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        return [_decode_anomaly(record) for record in json.loads(raw)]


# --- (de)serialisation -------------------------------------------------------
#
# In memory, `order_date` is a `date` so comparisons are cheap and correct. On
# disk it is an ISO string. These four functions are the only place that differs.

def _encode_order(order: dict) -> dict:
    return {**order, "order_date": order["order_date"].isoformat()}


def _decode_order(row: dict) -> dict:
    return {**row, "order_date": date.fromisoformat(row["order_date"])}


def _encode_anomaly(record: dict) -> dict:
    return {
        **record,
        "anomaly_date": record["anomaly_date"].isoformat(),
        "injected_at": record["injected_at"].isoformat(),
    }


def _decode_anomaly(record: dict) -> dict:
    return {
        **record,
        "anomaly_date": date.fromisoformat(record["anomaly_date"]),
        "injected_at": datetime.fromisoformat(record["injected_at"]),
    }


def _atomic_write(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then rename over the target.

    `os.replace` is atomic on POSIX and Windows, so a reader either sees the
    previous complete file or the new complete file - never a partial one.
    """
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)
