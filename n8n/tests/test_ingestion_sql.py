"""Behavioural tests for the Orders Ingestion pipeline's SQL.

The Mock API only ever emits valid orders, so the rejection paths cannot be
exercised by calling it. This harness feeds controlled payloads through the
pipeline instead - but through *the real SQL*, not a copy of it: every statement
is extracted from `n8n/workflows/orders-ingestion.json` at run time and executed
verbatim. If someone edits a node's query, this test runs the edited query. A
hand-copied fixture would drift from production the first time either changed.

Everything runs inside one transaction that ROLLBACKs, so it is safe against a
populated database and leaves nothing behind.

Usage (from the repo root, with the stack running):
    python n8n/tests/test_ingestion_sql.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "orders-ingestion.json"

BATCH_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
BATCH_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def node_sql(workflow: dict, node_name: str) -> str:
    """Return a node's query, with its trailing semicolon removed."""
    for node in workflow["nodes"]:
        if node["name"] == node_name:
            return node["parameters"]["query"].rstrip().rstrip(";")
    raise SystemExit(f"Node not found in workflow: {node_name!r}")


def bind(sql: str, batch: uuid.UUID) -> str:
    """Substitute the single $1 batch-id placeholder with a literal.

    Safe here because the only value ever bound is a UUID this file generates.
    """
    return sql.replace("$1", f"'{batch}'")


def order(**overrides) -> str:
    """A valid order payload as JSON, with fields overridden per scenario."""
    payload = {
        "order_id": "TEST-VALID-1",
        "order_date": "2026-06-15",
        "region": "NA",
        "product": "SKU-1042",
        "channel": "web",
        "customer_id": "CUST-TEST-0001",
        "quantity": 2,
        "unit_price": 100.00,
        "currency": "USD",
        "refund_amount": 0.00,
    }
    payload.update(overrides)
    return json.dumps(payload).replace("'", "''")


def build_script(workflow: dict) -> str:
    validate = bind(node_sql(workflow, "Validate Orders"), BATCH_A)
    customers = bind(node_sql(workflow, "Resolve Customers"), BATCH_A)
    facts = bind(node_sql(workflow, "Insert Facts"), BATCH_A)
    facts_b = bind(node_sql(workflow, "Insert Facts"), BATCH_B)
    customers_b = bind(node_sql(workflow, "Resolve Customers"), BATCH_B)

    # (order_id, payload, expect_rejected, expected_reason_fragment)
    scenarios = [
        ("TEST-VALID-1", order(order_id="TEST-VALID-1"), False, None),
        ("TEST-VALID-2", order(order_id="TEST-VALID-2", region="EMEA", currency="EUR",
                               product="SKU-3375", channel="partner",
                               customer_id="CUST-TEST-0002"), False, None),
        ("TEST-VALID-3", order(order_id="TEST-VALID-3", quantity=1, refund_amount=100.00),
         False, None),

        ("TEST-BAD-QTY", order(order_id="TEST-BAD-QTY", quantity=0),
         True, "quantity must be greater than zero"),
        ("TEST-NEG-QTY", order(order_id="TEST-NEG-QTY", quantity=-5),
         True, "quantity must be greater than zero"),
        ("TEST-NEG-PRICE", order(order_id="TEST-NEG-PRICE", unit_price=-10),
         True, "unit_price must not be negative"),
        ("TEST-NEG-REFUND", order(order_id="TEST-NEG-REFUND", refund_amount=-1),
         True, "refund_amount must not be negative"),
        ("TEST-BIG-REFUND", order(order_id="TEST-BIG-REFUND", quantity=1,
                                  unit_price=10, refund_amount=999),
         True, "refund_amount exceeds gross amount"),
        # Case is normalised, not rejected: ISO 4217 codes are case-insensitive
        # and the same upper() runs on insert, so the warehouse always stores
        # the canonical form. Asserted below to be 'USD' in fact_orders.
        ("TEST-LOWER-CCY", order(order_id="TEST-LOWER-CCY", currency="usd"), False, None),

        ("TEST-BAD-CCY", order(order_id="TEST-BAD-CCY", currency="US"),
         True, "currency is not a 3-letter ISO code"),
        ("TEST-UNSUP-CCY", order(order_id="TEST-UNSUP-CCY", currency="XXX"),
         True, "unsupported currency"),
        ("TEST-BAD-DATE", order(order_id="TEST-BAD-DATE", order_date="15/06/2026"),
         True, "order_date is missing or not a valid date"),
        ("TEST-NO-ID", order(order_id=""), True, "order_id is missing or empty"),
        ("TEST-NO-CUST", order(order_id="TEST-NO-CUST", customer_id=""),
         True, "customer_id is missing or empty"),
        ("TEST-QTY-TEXT", order(order_id="TEST-QTY-TEXT", quantity="lots"),
         True, "quantity is missing or not numeric"),

        # Reference-data drift: these must be rejected, never auto-created.
        ("TEST-BAD-REGION", order(order_id="TEST-BAD-REGION", region="ANTARCTICA"),
         True, "unknown region: ANTARCTICA"),
        ("TEST-BAD-PRODUCT", order(order_id="TEST-BAD-PRODUCT", product="SKU-9999"),
         True, "unknown product: SKU-9999"),
        ("TEST-BAD-CHANNEL", order(order_id="TEST-BAD-CHANNEL", channel="telepathy"),
         True, "unknown channel: telepathy"),

        # Multiple simultaneous failures: the message must list all of them.
        ("TEST-MULTI-BAD", order(order_id="TEST-MULTI-BAD", quantity=-1,
                                 unit_price=-1, currency="ZZ", region="NOWHERE"),
         True, "unknown region"),
    ]

    expected_rejected = sum(1 for _, _, bad, _ in scenarios if bad)
    expected_valid = len(scenarios) - expected_rejected

    staging_rows = ",\n        ".join(
        f"('{BATCH_A}'::uuid, '{oid}', '{payload}'::jsonb)"
        for oid, payload, _, _ in scenarios
    )

    # Look rows up by the staging `order_id` column, not by the payload: one
    # scenario deliberately carries an empty order_id inside its payload, so the
    # payload is not a reliable handle on the row under test.
    reason_checks = "\n".join(
        f"""
    SELECT count(*) INTO n FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_A}'::uuid
      AND order_id = '{oid}'
      AND processing_status = 'failed'
      AND error_message LIKE '%{fragment}%';
    PERFORM pg_temp.check('reject', '{oid} -> {fragment}', n = 1);"""
        for oid, _, bad, fragment in scenarios if bad
    )

    valid_b_rows = ",\n        ".join(
        f"('{BATCH_B}'::uuid, '{oid}', '{payload}'::jsonb)"
        for oid, payload, bad, _ in scenarios if not bad
    )

    return f"""\
\\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE test_results (
    id SERIAL PRIMARY KEY, section TEXT, name TEXT, passed BOOLEAN, detail TEXT
) ON COMMIT DROP;

CREATE OR REPLACE FUNCTION pg_temp.check(
    p_section TEXT, p_name TEXT, p_passed BOOLEAN, p_detail TEXT DEFAULT ''
) RETURNS VOID LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO test_results (section, name, passed, detail)
    VALUES (p_section, p_name, COALESCE(p_passed, FALSE), p_detail);
END;
$fn$;

-- Snapshot the reference dimensions so we can prove nothing was auto-created.
CREATE TEMP TABLE dim_before AS
SELECT (SELECT count(*) FROM salesops.dim_region)  AS regions,
       (SELECT count(*) FROM salesops.dim_product) AS products,
       (SELECT count(*) FROM salesops.dim_channel) AS channels,
       (SELECT count(*) FROM salesops.fact_orders) AS facts;

-- ============================================================ BATCH A =======
INSERT INTO salesops.ingestion_runs (batch_id, window_from, window_to, status)
VALUES ('{BATCH_A}'::uuid, DATE '2026-06-01', DATE '2026-06-30', 'running');

INSERT INTO salesops.raw_orders_staging (batch_id, order_id, source_payload) VALUES
        {staging_rows};

CREATE TEMP TABLE r_validate AS
{validate};

CREATE TEMP TABLE r_customers AS
{customers};

CREATE TEMP TABLE r_facts AS
{facts};

DO $do$
DECLARE n INTEGER; t TEXT;
BEGIN
    SELECT records_rejected INTO n FROM r_validate;
    PERFORM pg_temp.check('validate', 'rejects exactly {expected_rejected} invalid orders',
                          n = {expected_rejected}, format('got %s', n));

    SELECT records_valid INTO n FROM r_validate;
    PERFORM pg_temp.check('validate', 'passes exactly {expected_valid} valid orders',
                          n = {expected_valid}, format('got %s', n));

    SELECT records_accepted INTO n FROM r_facts;
    PERFORM pg_temp.check('facts', 'inserts only the valid orders',
                          n = {expected_valid}, format('got %s', n));

    -- Invalid records must never reach the fact table.
    SELECT count(*) INTO n FROM salesops.fact_orders WHERE order_id LIKE 'TEST-BAD%'
        OR order_id LIKE 'TEST-NEG%' OR order_id LIKE 'TEST-NO-%'
        OR order_id LIKE 'TEST-MULTI%' OR order_id LIKE 'TEST-UNSUP%'
        OR order_id LIKE 'TEST-QTY%';
    PERFORM pg_temp.check('facts', 'no invalid order reached fact_orders', n = 0, format('%s leaked', n));

    -- Lowercase input is normalised on the way in, so the warehouse never holds
    -- two spellings of the same currency.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_id = 'TEST-LOWER-CCY' AND currency = 'USD';
    PERFORM pg_temp.check('facts', 'lowercase currency normalised to USD on insert', n = 1);

    -- Every rejected row keeps its payload and gains a reason.
    SELECT count(*) INTO n FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_A}'::uuid AND processing_status = 'failed'
      AND (error_message IS NULL OR error_message = '');
    PERFORM pg_temp.check('deadletter', 'every rejection has an error_message', n = 0);

    SELECT count(*) INTO n FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_A}'::uuid AND processing_status = 'failed'
      AND (source_payload IS NULL OR source_payload = '{{}}'::jsonb);
    PERFORM pg_temp.check('deadletter', 'rejected payloads are preserved intact', n = 0);

    -- Nothing left stuck mid-pipeline.
    SELECT count(*) INTO n FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_A}'::uuid AND processing_status = 'pending';
    PERFORM pg_temp.check('deadletter', 'no row left at pending', n = 0, format('%s stuck', n));

    -- Reference-data drift must be visible, not absorbed.
    SELECT count(*) INTO n FROM salesops.dim_region;
    PERFORM pg_temp.check('dimensions', 'unknown region did NOT create a dim_region row',
                          n = (SELECT regions FROM dim_before), format('now %s', n));
    SELECT count(*) INTO n FROM salesops.dim_product;
    PERFORM pg_temp.check('dimensions', 'unknown product did NOT create a dim_product row',
                          n = (SELECT products FROM dim_before), format('now %s', n));
    SELECT count(*) INTO n FROM salesops.dim_channel;
    PERFORM pg_temp.check('dimensions', 'unknown channel did NOT create a dim_channel row',
                          n = (SELECT channels FROM dim_before), format('now %s', n));

    -- Customers are discovered from valid orders only.
    SELECT count(*) INTO n FROM salesops.dim_customer WHERE customer_id LIKE 'CUST-TEST-%';
    PERFORM pg_temp.check('dimensions', 'customers upserted from valid orders only',
                          n = 2, format('got %s', n));
    SELECT count(*) INTO n FROM salesops.dim_customer
    WHERE customer_id LIKE 'CUST-TEST-%' AND customer_name IS NOT NULL;
    PERFORM pg_temp.check('dimensions', 'no customer name was fabricated', n = 0);

    -- A record failing several rules reports all of them, not just the first.
    SELECT error_message INTO t FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_A}'::uuid AND source_payload ->> 'order_id' = 'TEST-MULTI-BAD';
    PERFORM pg_temp.check('deadletter', 'multi-failure record lists every reason',
        t LIKE '%unknown region%' AND t LIKE '%quantity%' AND t LIKE '%unit_price%'
        AND t LIKE '%currency%', COALESCE(t, 'null'));
END;
$do$;

DO $do$
DECLARE n INTEGER;
BEGIN
{reason_checks}
END;
$do$;

-- ============================================================ BATCH B =======
-- The same valid orders arriving again, as an overlapping window would deliver.
INSERT INTO salesops.ingestion_runs (batch_id, window_from, window_to, status)
VALUES ('{BATCH_B}'::uuid, DATE '2026-06-01', DATE '2026-06-30', 'running');

INSERT INTO salesops.raw_orders_staging (batch_id, order_id, source_payload) VALUES
        {valid_b_rows};

CREATE TEMP TABLE r_customers_b AS
{customers_b};

CREATE TEMP TABLE r_facts_b AS
{facts_b};

DO $do$
DECLARE n INTEGER; before_facts INTEGER;
BEGIN
    SELECT records_accepted INTO n FROM r_facts_b;
    PERFORM pg_temp.check('idempotency', 're-ingest accepts 0 new orders', n = 0, format('got %s', n));

    SELECT records_duplicate INTO n FROM r_facts_b;
    PERFORM pg_temp.check('idempotency', 're-ingest reports {expected_valid} duplicates',
                          n = {expected_valid}, format('got %s', n));

    SELECT (SELECT facts FROM dim_before) INTO before_facts;
    SELECT count(*) INTO n FROM salesops.fact_orders;
    PERFORM pg_temp.check('idempotency', 'fact_orders grew by exactly {expected_valid}',
                          n = before_facts + {expected_valid}, format('grew by %s', n - before_facts));

    SELECT count(*) INTO n FROM salesops.raw_orders_staging
    WHERE batch_id = '{BATCH_B}'::uuid AND processing_status = 'skipped';
    PERFORM pg_temp.check('idempotency', 'duplicate staging rows marked skipped',
                          n = {expected_valid}, format('got %s', n));

    -- Immutability: the second arrival must not have rewritten the first.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_id = 'TEST-VALID-1' AND quantity = 2 AND unit_price = 100.0000;
    PERFORM pg_temp.check('idempotency', 'existing fact row was not overwritten', n = 1);
END;
$do$;

\\echo ''
\\echo '============ INGESTION PIPELINE SQL TEST RESULTS ============'

SELECT CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END AS result,
       section, name, NULLIF(detail, '') AS detail
FROM test_results ORDER BY id;

SELECT count(*) AS total,
       count(*) FILTER (WHERE passed) AS passed,
       count(*) FILTER (WHERE NOT passed) AS failed
FROM test_results;

DO $do$
DECLARE failed INTEGER; names TEXT;
BEGIN
    SELECT count(*), string_agg(name, '; ') INTO failed, names
    FROM test_results WHERE NOT passed;
    IF failed > 0 THEN
        RAISE EXCEPTION 'INGESTION SQL TESTS FAILED: % check(s) -> %', failed, names;
    END IF;
    RAISE NOTICE 'All ingestion pipeline SQL checks passed.';
END;
$do$;

ROLLBACK;
"""


def main() -> int:
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    script = build_script(workflow)

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "salesops", "-d", "salesops", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        cwd=REPO_ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
