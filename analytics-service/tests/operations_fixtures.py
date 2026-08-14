"""Shared setup for the Stage 10 suites.

Stage 10 is about states that only occur when something has gone wrong, so
almost every test here has to manufacture a failure first: a run abandoned at
`running`, a batch of dead-letter payloads, an execution stranded mid-call.

Two rules keep that safe against a live warehouse.

**Everything created is tagged and removed.** Fixtures use recognisable ids
(`STAGE10-TEST-*`, sources prefixed `stage10-test`) and the teardown deletes
exactly those. Nothing sweeps by date or by status, because a sweep would take
real operational history with it.

**Thresholds are moved, never timestamps.** Where a test needs something to be
stale, it lowers the configured timeout rather than backdating a row. That
exercises the same code path the real timeout does, restores cleanly, and never
leaves a row claiming to be older than it is.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import uuid

import pytest

from analytics.config import Settings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Everything this suite creates carries one of these markers.
TEST_SOURCE = "stage10-test"
TEST_ORDER_PREFIX = "STAGE10-TEST-"
TEST_ACTOR = "stage10-tests@example.invalid"

#: Reference values that actually exist in the live dimensions. A fixture using
#: a plausible-looking SKU would be dead-lettered by validation and the test
#: would pass for the wrong reason.
VALID_REGION = "EMEA"
VALID_CHANNEL = "web"


def load_env_file() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def make_settings() -> Settings:
    env_file = load_env_file()
    password = os.getenv("ANALYTICS_DB_PASSWORD") or env_file.get("POSTGRES_PASSWORD")
    if not password:
        pytest.skip("No database password available - is the stack configured?")
    return Settings(
        host=os.getenv("ANALYTICS_DB_HOST", "localhost"),
        port=int(os.getenv("ANALYTICS_DB_PORT") or env_file.get("POSTGRES_HOST_PORT") or 5432),
        database=os.getenv("ANALYTICS_DB_NAME") or env_file.get("POSTGRES_DB") or "salesops",
        user=os.getenv("ANALYTICS_DB_USER") or env_file.get("POSTGRES_USER") or "salesops",
        password=password,
    )


def query(connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchall()


def execute(connection, sql: str, params: dict | None = None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
    connection.commit()


def valid_product(connection) -> str:
    return query(connection, "SELECT product_sku FROM salesops.dim_product LIMIT 1")[0]["product_sku"]


# =============================================================================
# Manufacturing failures
# =============================================================================


def make_run(connection, status: str = "running", age_minutes: int = 0,
             source: str = TEST_SOURCE) -> int:
    """An ingestion_runs row of a given age and state."""
    rows = query(connection, """
        INSERT INTO salesops.ingestion_runs
            (batch_id, source, window_from, window_to, status, started_at,
             finished_at, error_message)
        VALUES (gen_random_uuid(), %(source)s, CURRENT_DATE, CURRENT_DATE, %(status)s,
                now() - make_interval(mins => %(age)s),
                CASE WHEN %(status)s <> 'running' THEN now() END,
                CASE WHEN %(status)s = 'failed' THEN 'pre-existing failure' END)
        RETURNING run_id
    """, {"source": source, "status": status, "age": age_minutes})
    connection.commit()
    return rows[0]["run_id"]


def make_failed_batch(connection, recoverable: int = 1, permanent: int = 1,
                      duplicate: int = 0) -> str:
    """A dead-letter batch containing payloads that take known paths on replay.

    `recoverable` rows are valid and new: they load on replay.
    `permanent` rows name a region that does not exist: they fail again, forever.
    `duplicate` rows copy an order already in fact_orders: they settle as skipped.
    """
    batch = str(uuid.uuid4())
    sku = valid_product(connection)

    for _ in range(recoverable):
        execute(connection, """
            INSERT INTO salesops.raw_orders_staging
                (batch_id, order_id, source_payload, processing_status, error_message)
            VALUES (%(batch)s::uuid, %(oid)s,
                    jsonb_build_object(
                        'order_id', %(oid)s::text, 'order_date', '2026-07-15',
                        'region', %(region)s::text, 'product', %(sku)s::text, 'channel', %(channel)s::text,
                        'customer_id', 'CUST-STAGE10-1', 'quantity', 2,
                        'unit_price', 100.00, 'refund_amount', 0, 'currency', 'EUR'),
                    'failed', 'transient dimension lookup failure')
        """, {"batch": batch, "oid": f"{TEST_ORDER_PREFIX}OK-{uuid.uuid4().hex[:8]}",
              "region": VALID_REGION, "sku": sku, "channel": VALID_CHANNEL})

    for _ in range(permanent):
        execute(connection, """
            INSERT INTO salesops.raw_orders_staging
                (batch_id, order_id, source_payload, processing_status, error_message)
            VALUES (%(batch)s::uuid, %(oid)s,
                    jsonb_build_object(
                        'order_id', %(oid)s::text, 'order_date', '2026-07-15',
                        'region', 'NOWHERE', 'product', %(sku)s::text, 'channel', %(channel)s::text,
                        'customer_id', 'CUST-STAGE10-2', 'quantity', 1,
                        'unit_price', 10.00, 'refund_amount', 0, 'currency', 'EUR'),
                    'failed', 'unknown region')
        """, {"batch": batch, "oid": f"{TEST_ORDER_PREFIX}BAD-{uuid.uuid4().hex[:8]}",
              "sku": sku, "channel": VALID_CHANNEL})

    # `index` is load-bearing here, unlike the two loops above: it walks
    # fact_orders one row at a time so each duplicate names a different order.
    for index in range(duplicate):
        execute(connection, """
            INSERT INTO salesops.raw_orders_staging
                (batch_id, order_id, source_payload, processing_status, error_message)
            SELECT %(batch)s::uuid, f.order_id,
                   jsonb_build_object(
                       'order_id', f.order_id, 'order_date', f.order_date::text,
                       'region', r.region_code, 'product', p.product_sku,
                       'channel', c.channel_code, 'customer_id', f.customer_id,
                       'quantity', f.quantity, 'unit_price', f.unit_price,
                       'refund_amount', f.refund_amount_local, 'currency', f.currency),
                   'failed', 'transient dimension lookup failure'
            FROM salesops.fact_orders f
            JOIN salesops.dim_region  r USING (region_id)
            JOIN salesops.dim_product p USING (product_id)
            JOIN salesops.dim_channel c USING (channel_id)
            OFFSET %(offset)s LIMIT 1
        """, {"batch": batch, "offset": index})

    return batch


def make_old_staging(connection, status: str, age_days: int = 200) -> str:
    order_id = f"{TEST_ORDER_PREFIX}AGED-{uuid.uuid4().hex[:8]}"
    execute(connection, """
        INSERT INTO salesops.raw_orders_staging
            (batch_id, order_id, source_payload, processing_status, received_at,
             processed_at, error_message)
        VALUES (gen_random_uuid(), %(oid)s, '{"note":"stage10 retention fixture"}'::jsonb,
                %(status)s, now() - make_interval(days => %(age)s),
                CASE WHEN %(status)s <> 'pending' THEN now() - make_interval(days => %(age)s) END,
                CASE WHEN %(status)s = 'failed' THEN 'stage10 retention fixture' END)
    """, {"oid": order_id, "status": status, "age": age_days})
    return order_id


@contextlib.contextmanager
def threshold(connection, key: str, value):
    """Temporarily move an operational threshold, then put it back.

    Moving the threshold rather than backdating a row is deliberate: it drives
    the same code path the real timeout does, it restores cleanly, and it never
    leaves a row in the database claiming to be older than it is.
    """
    original = query(connection, """
        SELECT config_value FROM salesops.operational_config WHERE config_key = %(k)s
    """, {"k": key})[0]["config_value"]
    execute(connection, """
        UPDATE salesops.operational_config SET config_value = %(v)s WHERE config_key = %(k)s
    """, {"k": key, "v": value})
    try:
        yield
    finally:
        execute(connection, """
            UPDATE salesops.operational_config SET config_value = %(v)s WHERE config_key = %(k)s
        """, {"k": key, "v": original})


# =============================================================================
# Cleanup
# =============================================================================


def purge_test_data(connection) -> None:
    """Remove exactly what this suite created, and nothing else.

    Every delete is keyed on a marker this suite owns. Nothing sweeps by date or
    by status - a sweep would take real operational history with it, which is
    the precise mistake Stage 10 exists to make impossible.
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            DELETE FROM salesops.ingestion_replays r
            USING salesops.raw_orders_staging s
            WHERE (s.ingestion_id = r.original_ingestion_id
                OR s.ingestion_id = r.replay_ingestion_id)
              AND s.order_id LIKE %(prefix)s
        """, {"prefix": f"{TEST_ORDER_PREFIX}%"})
        cursor.execute("""
            DELETE FROM salesops.fact_orders WHERE order_id LIKE %(prefix)s
        """, {"prefix": f"{TEST_ORDER_PREFIX}%"})
        cursor.execute("""
            DELETE FROM salesops.raw_orders_staging WHERE order_id LIKE %(prefix)s
        """, {"prefix": f"{TEST_ORDER_PREFIX}%"})
        cursor.execute("""
            DELETE FROM salesops.ingestion_runs WHERE source LIKE %(src)s
        """, {"src": f"{TEST_SOURCE}%"})
        cursor.execute("""
            DELETE FROM salesops.dim_customer WHERE customer_id LIKE 'CUST-STAGE10-%'
        """)
    connection.commit()


# =============================================================================
# Cross-stage fingerprints
# =============================================================================


def stage6_fingerprint(connection) -> str:
    return query(connection, """
        SELECT md5(string_agg(
            decision_id || '|' || severity || '|' || routing || '|' || decision || '|' ||
            notification_allowed || '|' || human_review_required || '|' ||
            decision_reason_code, ',' ORDER BY decision_id)) AS f
        FROM salesops.anomaly_decisions
    """)[0]["f"]


def stage7_fingerprint(connection) -> str:
    """Content, not identity.

    Keyed on calendar_date rather than hypothesis_id: the Stage 7 suite's
    regeneration test legitimately deletes and re-creates rows, so an id-based
    fingerprint would report a change that is not one.
    """
    return query(connection, """
        SELECT md5(COALESCE(string_agg(
            calendar_date || '|' || summary || '|' || confidence || '|' ||
            primary_hypothesis, ',' ORDER BY calendar_date), '')) AS f
        FROM salesops.anomaly_hypotheses
    """)[0]["f"]


def stage8_fingerprint(connection) -> str:
    return query(connection, """
        SELECT md5(COALESCE(string_agg(
            notification_id || '|' || status || '|' || recipient || '|' ||
            (sent_at IS NOT NULL)::text, ',' ORDER BY notification_id), '')) AS f
        FROM salesops.notifications
    """)[0]["f"]


def review_state_fingerprint(connection) -> str:
    """Authorisation state only. Deliberately excludes anything time-derived,
    because ageing changes how a review is described and must never change what
    it authorises."""
    return query(connection, """
        SELECT md5(COALESCE(string_agg(
            review_id || '|' || status || '|' || COALESCE(assigned_to, '') || '|' ||
            COALESCE(resolution, '') || '|' || COALESCE(approved_by, ''),
            ',' ORDER BY review_id), '')) AS f
        FROM salesops.review_queue
    """)[0]["f"]


def stage9_fingerprint(connection) -> str:
    return query(connection, """
        SELECT md5(COALESCE(string_agg(
            remediation_id || '|' || status || '|' || action_type || '|' ||
            review_approved_by || '|' || COALESCE(executed_by, ''),
            ',' ORDER BY remediation_id), '')) AS f
        FROM salesops.remediation_actions
    """)[0]["f"]


def warehouse_fingerprint(connection) -> str:
    return query(connection, """
        SELECT md5(concat_ws('|',
            (SELECT count(*) || coalesce(sum(net_amount_usd)::text, '')
               FROM salesops.fact_orders),
            (SELECT count(*) || coalesce(sum(net_revenue_usd)::text, '')
               FROM salesops.kpi_daily),
            (SELECT count(*) || coalesce(sum(anomaly_score)::text, '')
               FROM salesops.anomaly_daily)
        )) AS f
    """)[0]["f"]


def all_fingerprints(connection) -> dict[str, str]:
    return {
        "stage6": stage6_fingerprint(connection),
        "stage7": stage7_fingerprint(connection),
        "stage8": stage8_fingerprint(connection),
        "reviews": review_state_fingerprint(connection),
        "stage9": stage9_fingerprint(connection),
        "warehouse": warehouse_fingerprint(connection),
    }
