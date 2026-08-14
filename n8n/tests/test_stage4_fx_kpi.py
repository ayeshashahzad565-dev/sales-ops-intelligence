"""Behavioural tests for Stage 4: FX synchronisation and the daily KPI layer.

Same approach as the Stage 3 harness: every statement under test is extracted
from the committed workflow JSON at run time, so the tests exercise the SQL that
production actually runs. Edit a node's query and this suite runs the edited
query. A hand-copied fixture would drift the first time either changed.

The KPI rebuild is the exception, and deliberately so: it lives in
`salesops.refresh_kpi_daily()` because its DELETE and INSERT must be atomic.
Tests call the function, which is the same thing the workflow calls.

Everything runs inside one transaction that ROLLBACKs, so the suite is safe
against the populated live database and leaves nothing behind.

Usage (from the repo root, with the stack running):
    python n8n/tests/test_stage4_fx_kpi.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FX_WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "fx-rate-sync.json"
INGEST_WORKFLOW = REPO_ROOT / "n8n" / "workflows" / "orders-ingestion.json"

BATCH = uuid.UUID("cccccccc-0000-4000-8000-000000000004")

# An isolated window, far from any ingested data, so the fixtures below cannot
# collide with the live warehouse. Asserted in the suite rather than assumed.
D_THU = "2025-02-06"   # Thursday  - rate published
D_FRI = "2025-02-07"   # Friday    - rate published
D_SAT = "2025-02-08"   # Saturday  - NO rate: must carry forward from Friday
D_SUN = "2025-02-09"   # Sunday    - NO rate: must carry forward from Friday
D_MON = "2025-02-10"   # Monday    - rate published
D_STALE = "2025-01-02"  # far older than the staleness bound


def node_sql(workflow: dict, node_name: str) -> str:
    for node in workflow["nodes"]:
        if node["name"] == node_name:
            return node["parameters"]["query"].rstrip().rstrip(";")
    raise SystemExit(f"Node not found: {node_name!r}")


def build_script() -> str:
    fx = json.loads(FX_WORKFLOW.read_text(encoding="utf-8"))
    ingest = json.loads(INGEST_WORKFLOW.read_text(encoding="utf-8"))

    # Stage 4 made ingestion_runs a shared ledger, which put the Orders
    # Ingestion window calculation at risk. Run its real SQL to prove it.
    open_ingest_run = node_sql(ingest, "Create Batch Context").replace("$1", "'test-exec'")

    # $1 currency, $2 rates-json  ->  bound per invocation below
    store_rates = node_sql(fx, "Store Rates")
    attach_fx = node_sql(fx, "Attach FX To Orders")

    def store(currency: str, rates: dict) -> str:
        payload = json.dumps(rates).replace("'", "''")
        return (store_rates
                .replace("$1", f"'{currency}'")
                .replace("$2", f"'{payload}'"))

    # Frankfurter's real shape: business days only, weekends simply absent.
    eur_rates = {
        D_THU: {"USD": 1.1000},
        D_FRI: {"USD": 1.2000},
        D_MON: {"USD": 1.3000},
    }
    # A currency whose only rate is far in the past, to prove the staleness bound.
    chf_rates = {D_STALE: {"USD": 1.5000}}

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

CREATE TEMP TABLE live_before AS
SELECT (SELECT count(*) FROM salesops.exchange_rates)                      AS rate_rows,
       (SELECT count(*) FROM salesops.exchange_rates WHERE source='identity') AS identity_rows,
       (SELECT count(*) FROM salesops.fact_orders WHERE exchange_rate_to_usd IS NULL) AS pending;


-- =============================================================================
-- 0. Preconditions
-- =============================================================================
DO $do$
DECLARE n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_date BETWEEN DATE '{D_STALE}' AND DATE '{D_MON}';
    PERFORM pg_temp.check('precondition', 'the isolated test window holds no ingested orders',
                          n = 0, format('%s real orders - pick another window', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE rate_date BETWEEN DATE '{D_STALE}' AND DATE '{D_MON}' AND currency <> 'USD';
    PERFORM pg_temp.check('precondition', 'the isolated window holds no non-USD rates',
                          n = 0, format('%s rates found', n));
END;
$do$;


-- =============================================================================
-- 1. The USD identity contract survives everything
-- =============================================================================
DO $do$
DECLARE n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency = 'USD' AND source = 'identity';
    PERFORM pg_temp.check('fx-identity', 'USD identity rates are present', n > 0, format('%s rows', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency = 'USD' AND rate_to_usd <> 1.0;
    PERFORM pg_temp.check('fx-identity', 'every USD rate is exactly 1.0', n = 0, format('%s wrong', n));

    -- Frankfurter has no USD/USD pair; asking for one would be asking for a
    -- number that is definitionally 1.
    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency = 'USD' AND source = 'frankfurter';
    PERFORM pg_temp.check('fx-identity', 'no USD row was ever sourced from frankfurter',
                          n = 0, format('%s rows', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE source = 'identity' AND currency <> 'USD';
    PERFORM pg_temp.check('fx-identity', 'identity is used for USD only', n = 0, format('%s rows', n));
END;
$do$;


-- =============================================================================
-- 2. No fabricated rates, anywhere
-- =============================================================================
DO $do$
DECLARE n INTEGER; v TEXT;
BEGIN
    SELECT count(*), string_agg(DISTINCT source, ', ') INTO n, v
    FROM salesops.exchange_rates WHERE source NOT IN ('identity', 'frankfurter');
    PERFORM pg_temp.check('fx-provenance', 'every rate has a declared provenance',
                          n = 0, COALESCE(v, 'none'));

    SELECT count(*) INTO n FROM salesops.exchange_rates WHERE rate_to_usd <= 0;
    PERFORM pg_temp.check('fx-provenance', 'no non-positive rates', n = 0, format('%s rows', n));

    -- Rates only exist for currencies the business actually bills in.
    SELECT count(*) INTO n FROM salesops.exchange_rates x
    WHERE NOT EXISTS (SELECT 1 FROM salesops.fact_orders f WHERE f.currency = x.currency);
    PERFORM pg_temp.check('fx-provenance', 'no rates for currencies the data never uses',
                          n = 0, format('%s rows', n));
END;
$do$;


-- =============================================================================
-- 3. Storing a Frankfurter payload
-- =============================================================================
CREATE TEMP TABLE r_store_1 AS
{store("EUR", eur_rates)};

DO $do$
DECLARE n INTEGER; r NUMERIC;
BEGIN
    SELECT rates_received INTO n FROM r_store_1;
    PERFORM pg_temp.check('fx-store', 'received count matches the payload', n = 3, format('got %s', n));

    SELECT rates_inserted INTO n FROM r_store_1;
    PERFORM pg_temp.check('fx-store', 'all three business days inserted', n = 3, format('got %s', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date BETWEEN DATE '{D_THU}' AND DATE '{D_MON}';
    PERFORM pg_temp.check('fx-store', 'stored exactly the published days, weekends absent',
                          n = 3, format('got %s', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date IN (DATE '{D_SAT}', DATE '{D_SUN}');
    PERFORM pg_temp.check('fx-store', 'no weekend row was invented', n = 0, format('%s invented', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date=DATE '{D_FRI}' AND source='frankfurter';
    PERFORM pg_temp.check('fx-store', 'stored rows carry source=frankfurter', n = 1);

    SELECT rate_to_usd INTO r FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date=DATE '{D_FRI}';
    PERFORM pg_temp.check('fx-store', 'rate value stored verbatim (1.2000)', r = 1.2, format('%s', r));
END;
$do$;


-- =============================================================================
-- 4. Re-storing the same payload is a no-op (idempotency)
-- =============================================================================
CREATE TEMP TABLE r_store_2 AS
{store("EUR", eur_rates)};

DO $do$
DECLARE n INTEGER; r NUMERIC;
BEGIN
    SELECT rates_inserted INTO n FROM r_store_2;
    PERFORM pg_temp.check('fx-idempotency', 're-storing inserts 0 new rates', n = 0, format('got %s', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date BETWEEN DATE '{D_THU}' AND DATE '{D_MON}';
    PERFORM pg_temp.check('fx-idempotency', 'no duplicate rate rows', n = 3, format('got %s', n));
END;
$do$;

-- A revised rate for a date already stored must NOT overwrite it: the rate we
-- applied is the rate we keep, so a report re-run reproduces itself.
CREATE TEMP TABLE r_store_3 AS
{store("EUR", {D_FRI: {"USD": 9.9999}})};

DO $do$
DECLARE r NUMERIC;
BEGIN
    SELECT rate_to_usd INTO r FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date=DATE '{D_FRI}';
    PERFORM pg_temp.check('fx-idempotency', 'a revised rate does not overwrite history',
                          r = 1.2, format('now %s', r));
END;
$do$;


-- =============================================================================
-- 5. Attachment, including the weekend carry-forward
-- =============================================================================
-- Orders across Thu/Fri/Sat/Sun/Mon plus one far outside the staleness bound.
INSERT INTO salesops.dim_customer (customer_id, region_id, first_seen_date)
SELECT 'CUST-FXTEST-01', region_id, DATE '{D_THU}' FROM salesops.dim_region WHERE region_code='EMEA'
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO salesops.fact_orders (
    order_id, order_date, customer_id, region_id, product_id, channel_id,
    quantity, unit_price, currency, refund_amount_local)
SELECT v.oid, v.d::date, 'CUST-FXTEST-01', r.region_id, p.product_id, c.channel_id,
       1, 100.0000, v.ccy, 0
FROM (VALUES
    ('FX-THU', '{D_THU}', 'EUR'),
    ('FX-FRI', '{D_FRI}', 'EUR'),
    ('FX-SAT', '{D_SAT}', 'EUR'),
    ('FX-SUN', '{D_SUN}', 'EUR'),
    ('FX-MON', '{D_MON}', 'EUR'),
    ('FX-STALE', '{D_MON}', 'CHF')
) AS v(oid, d, ccy)
CROSS JOIN salesops.dim_region  r
CROSS JOIN salesops.dim_product p
CROSS JOIN salesops.dim_channel c
WHERE r.region_code='EMEA' AND p.product_sku='SKU-1042' AND c.channel_code='web';

-- A CHF rate that is far too old to apply under the staleness bound.
CREATE TEMP TABLE r_store_chf AS
{store("CHF", chf_rates)};

CREATE TEMP TABLE r_attach_1 AS
{attach_fx};

DO $do$
DECLARE n INTEGER; r NUMERIC;
BEGIN
    SELECT rate_to_usd INTO r FROM salesops.exchange_rates
    WHERE currency='EUR' AND rate_date=DATE '{D_THU}';

    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-THU';
    PERFORM pg_temp.check('fx-attach', 'exact-date match used when published (Thu -> 1.1)',
                          r = 1.1, format('%s', r));

    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-FRI';
    PERFORM pg_temp.check('fx-attach', 'Friday uses Friday (1.2)', r = 1.2, format('%s', r));

    -- The rule the whole design turns on.
    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-SAT';
    PERFORM pg_temp.check('fx-attach', 'Saturday carries forward Friday (1.2), not Monday',
                          r = 1.2, format('%s', r));

    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-SUN';
    PERFORM pg_temp.check('fx-attach', 'Sunday carries forward Friday (1.2)', r = 1.2, format('%s', r));

    -- Never reach FORWARD: that would apply a rate that did not exist yet.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_id IN ('FX-SAT','FX-SUN') AND exchange_rate_to_usd = 1.3;
    PERFORM pg_temp.check('fx-attach', 'no order ever uses a future rate', n = 0, format('%s did', n));

    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-MON';
    PERFORM pg_temp.check('fx-attach', 'Monday uses its own rate (1.3)', r = 1.3, format('%s', r));

    -- Beyond the staleness bound the order stays visibly pending.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_id='FX-STALE' AND exchange_rate_to_usd IS NULL;
    PERFORM pg_temp.check('fx-attach', 'a rate older than the staleness bound is NOT applied', n = 1);

    -- Missing FX must never become zero.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_id='FX-STALE' AND (gross_amount_usd IS NOT NULL OR net_amount_usd IS NOT NULL);
    PERFORM pg_temp.check('fx-attach', 'unconverted order has NULL USD, not 0', n = 0);
END;
$do$;


-- =============================================================================
-- 6. Generated USD columns recompute from the attached rate
-- =============================================================================
DO $do$
DECLARE g NUMERIC; nt NUMERIC; n INTEGER;
BEGIN
    -- FX-SAT: 1 x 100.00 EUR at the carried-forward 1.2 -> 120.00 USD
    SELECT gross_amount_usd, net_amount_usd INTO g, nt
    FROM salesops.fact_orders WHERE order_id='FX-SAT';
    PERFORM pg_temp.check('fx-generated', 'gross_amount_usd = 100 x 1.2 = 120',
                          g = 120.0, format('%s', g));
    PERFORM pg_temp.check('fx-generated', 'net_amount_usd = 120 (no refund)', nt = 120.0, format('%s', nt));

    -- Every converted row must satisfy the identity, across the whole warehouse.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE exchange_rate_to_usd IS NOT NULL
      AND gross_amount_usd IS DISTINCT FROM round(quantity * unit_price * exchange_rate_to_usd, 4);
    PERFORM pg_temp.check('fx-generated', 'gross USD equals qty x price x rate for every row',
                          n = 0, format('%s mismatches', n));

    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE exchange_rate_to_usd IS NULL AND gross_amount_usd IS NOT NULL;
    PERFORM pg_temp.check('fx-generated', 'no USD amount exists without a rate', n = 0, format('%s rows', n));
END;
$do$;


-- =============================================================================
-- 7. Re-attaching is a no-op, and never rewrites an applied rate
-- =============================================================================
CREATE TEMP TABLE r_attach_2 AS
{attach_fx};

DO $do$
DECLARE n INTEGER; r NUMERIC;
BEGIN
    SELECT orders_attached INTO n FROM r_attach_2;
    PERFORM pg_temp.check('fx-idempotency', 're-attaching attaches 0 further orders',
                          n = 0, format('got %s', n));

    SELECT exchange_rate_to_usd INTO r FROM salesops.fact_orders WHERE order_id='FX-SAT';
    PERFORM pg_temp.check('fx-idempotency', 'an applied rate is not rewritten', r = 1.2, format('%s', r));
END;
$do$;


-- =============================================================================
-- 8. FX provider unavailable: nothing is fabricated, nothing is lost
--
-- The workflow's HTTP node throws on failure and the run stops, so the database
-- never sees an empty payload. This asserts the property that matters if it
-- ever did: an empty response inserts nothing and disturbs nothing.
-- =============================================================================
-- Column deliberately NOT named `n`: plpgsql resolves an unqualified name to a
-- local variable before a column, so `SELECT n FROM ...` inside a DO block with
-- a variable `n` is ambiguous and errors.
CREATE TEMP TABLE rates_before_outage AS
SELECT count(*) AS rate_count FROM salesops.exchange_rates;

CREATE TEMP TABLE r_store_empty AS
{store("EUR", {})};

DO $do$
DECLARE n INTEGER; before_n INTEGER;
BEGIN
    SELECT rates_received INTO n FROM r_store_empty;
    PERFORM pg_temp.check('fx-outage', 'an empty payload reports 0 received', n = 0, format('got %s', n));

    SELECT rates_inserted INTO n FROM r_store_empty;
    PERFORM pg_temp.check('fx-outage', 'an empty payload inserts nothing', n = 0, format('got %s', n));

    SELECT rate_count INTO before_n FROM rates_before_outage;
    SELECT count(*) INTO n FROM salesops.exchange_rates;
    PERFORM pg_temp.check('fx-outage', 'existing rates survive an empty response',
                          n = before_n, format('%s vs %s', n, before_n));

    SELECT count(*) INTO n FROM salesops.fact_orders WHERE order_id='FX-STALE'
      AND exchange_rate_to_usd IS NOT NULL;
    PERFORM pg_temp.check('fx-outage', 'an unconvertible order is never marked converted', n = 0);
END;
$do$;


-- =============================================================================
-- 9. KPI rebuild correctness
-- =============================================================================
CREATE TEMP TABLE r_kpi_1 AS SELECT * FROM salesops.refresh_kpi_daily();

-- No variable may be named `k` in this block: the queries below alias
-- salesops.kpi_daily AS k, and plpgsql resolves an unqualified name to a
-- declared variable before a table alias.
DO $do$
DECLARE n INTEGER;
BEGIN
    SELECT dates_written INTO n FROM r_kpi_1;
    PERFORM pg_temp.check('kpi', 'rebuild wrote rows', n > 0, format('%s rows', n));

    -- One row per date that has orders - no more, no fewer.
    SELECT count(*) INTO n FROM (
        SELECT DISTINCT order_date FROM salesops.fact_orders
    ) d;
    PERFORM pg_temp.check('kpi', 'one KPI row per distinct sales date',
                          n = (SELECT count(*)::int FROM salesops.kpi_daily), format('%s dates', n));

    SELECT count(*) INTO n FROM salesops.kpi_daily k
    WHERE NOT EXISTS (SELECT 1 FROM salesops.fact_orders f WHERE f.order_date = k.calendar_date);
    PERFORM pg_temp.check('kpi', 'no KPI row exists for a date with no orders', n = 0, format('%s rows', n));

    -- Recompute every aggregate independently and compare.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    JOIN (
        SELECT order_date,
               count(*)::int                      AS orders_count,
               count(DISTINCT customer_id)::int   AS customers_count,
               sum(quantity)::int                 AS units_sold,
               sum(gross_amount_usd)              AS gross_revenue_usd,
               sum(refund_amount_usd)             AS refund_amount_usd,
               sum(net_amount_usd)                AS net_revenue_usd,
               count(*) FILTER (WHERE exchange_rate_to_usd IS NULL)::int AS pending
        FROM salesops.fact_orders GROUP BY order_date
    ) e ON e.order_date = k.calendar_date
    WHERE k.orders_count      IS DISTINCT FROM e.orders_count
       OR k.customers_count   IS DISTINCT FROM e.customers_count
       OR k.units_sold        IS DISTINCT FROM e.units_sold
       OR k.gross_revenue_usd IS DISTINCT FROM e.gross_revenue_usd
       OR k.refund_amount_usd IS DISTINCT FROM e.refund_amount_usd
       OR k.net_revenue_usd   IS DISTINCT FROM e.net_revenue_usd
       OR k.orders_pending_fx IS DISTINCT FROM e.pending;
    PERFORM pg_temp.check('kpi', 'every aggregate matches an independent recomputation',
                          n = 0, format('%s mismatched dates', n));

    -- new_customers: a customer counts on the date of their first order only.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    JOIN (
        SELECT first_order_date AS d, count(*)::int AS c
        FROM (SELECT customer_id, min(order_date) AS first_order_date
              FROM salesops.fact_orders GROUP BY customer_id) fs
        GROUP BY first_order_date
    ) e ON e.d = k.calendar_date
    WHERE k.new_customers IS DISTINCT FROM e.c;
    PERFORM pg_temp.check('kpi', 'new_customers matches first-order-date recomputation',
                          n = 0, format('%s mismatched dates', n));

    -- Every customer is counted as new exactly once across the whole series.
    SELECT sum(new_customers)::int INTO n FROM salesops.kpi_daily;
    PERFORM pg_temp.check('kpi', 'total new_customers equals distinct customers in fact_orders',
        n = (SELECT count(DISTINCT customer_id)::int FROM salesops.fact_orders), format('%s', n));

    -- AOV divides by converted orders, not all orders.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    JOIN (
        SELECT order_date,
               round(sum(net_amount_usd) /
                     NULLIF(count(*) FILTER (WHERE exchange_rate_to_usd IS NOT NULL), 0), 4) AS aov
        FROM salesops.fact_orders GROUP BY order_date
    ) e ON e.order_date = k.calendar_date
    WHERE k.average_order_value_usd IS DISTINCT FROM e.aov;
    PERFORM pg_temp.check('kpi', 'AOV = net USD / converted orders', n = 0, format('%s mismatched', n));

    -- refund_rate uses safe division.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    JOIN (
        SELECT order_date,
               round(sum(refund_amount_usd) / NULLIF(sum(gross_amount_usd), 0), 6) AS rr
        FROM salesops.fact_orders GROUP BY order_date
    ) e ON e.order_date = k.calendar_date
    WHERE k.refund_rate IS DISTINCT FROM e.rr;
    PERFORM pg_temp.check('kpi', 'refund_rate = refunds / gross, safely divided',
                          n = 0, format('%s mismatched', n));

    -- Completeness columns agree with each other and with the facts.
    SELECT count(*) INTO n FROM salesops.kpi_daily
    WHERE is_complete <> (orders_pending_fx = 0);
    PERFORM pg_temp.check('kpi', 'is_complete agrees with orders_pending_fx', n = 0, format('%s rows', n));

    SELECT count(*) INTO n FROM salesops.kpi_daily
    WHERE fx_completeness_pct IS DISTINCT FROM
          round(100.0 * (orders_count - orders_pending_fx) / NULLIF(orders_count, 0), 3);
    PERFORM pg_temp.check('kpi', 'fx_completeness_pct is computed correctly', n = 0, format('%s rows', n));

    SELECT count(*) INTO n FROM salesops.kpi_daily
    WHERE is_complete AND fx_completeness_pct <> 100;
    PERFORM pg_temp.check('kpi', 'is_complete implies 100% completeness', n = 0, format('%s rows', n));
END;
$do$;


-- =============================================================================
-- 10. Moving averages
-- =============================================================================
DO $do$
DECLARE n INTEGER;
BEGIN
    -- Recompute both windows independently, over calendar days.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    JOIN (
        SELECT calendar_date,
               round(avg(net_revenue_usd) OVER (
                   ORDER BY calendar_date
                   RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW), 4) AS ma7,
               round(avg(net_revenue_usd) OVER (
                   ORDER BY calendar_date
                   RANGE BETWEEN INTERVAL '27 days' PRECEDING AND CURRENT ROW), 4) AS ma28
        FROM salesops.kpi_daily
    ) e ON e.calendar_date = k.calendar_date
    WHERE k.rolling_7d_net_revenue_usd  IS DISTINCT FROM e.ma7
       OR k.rolling_28d_net_revenue_usd IS DISTINCT FROM e.ma28;
    PERFORM pg_temp.check('kpi-ma', 'moving averages match an independent recomputation',
                          n = 0, format('%s mismatched dates', n));

    -- The earliest date has no history, so both windows equal that day itself.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    WHERE k.calendar_date = (SELECT min(calendar_date) FROM salesops.kpi_daily)
      AND (k.rolling_7d_net_revenue_usd  IS DISTINCT FROM round(k.net_revenue_usd, 4)
        OR k.rolling_28d_net_revenue_usd IS DISTINCT FROM round(k.net_revenue_usd, 4));
    PERFORM pg_temp.check('kpi-ma', 'first date averages over itself, not fabricated history', n = 0);

    -- No future contamination: a trailing mean can never exceed the max of the
    -- values at or before it.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    WHERE k.rolling_7d_net_revenue_usd > (
        SELECT max(k2.net_revenue_usd) FROM salesops.kpi_daily k2
        WHERE k2.calendar_date <= k.calendar_date
    ) + 0.0001;
    PERFORM pg_temp.check('kpi-ma', 'no future date contaminates a trailing window',
                          n = 0, format('%s rows', n));

    -- A trailing mean is bounded by the min/max of its own window.
    SELECT count(*) INTO n FROM salesops.kpi_daily k
    WHERE k.rolling_7d_net_revenue_usd IS NOT NULL AND NOT (
        k.rolling_7d_net_revenue_usd BETWEEN
            (SELECT min(k2.net_revenue_usd) FROM salesops.kpi_daily k2
              WHERE k2.calendar_date BETWEEN k.calendar_date - 6 AND k.calendar_date) - 0.0001
        AND (SELECT max(k2.net_revenue_usd) FROM salesops.kpi_daily k2
              WHERE k2.calendar_date BETWEEN k.calendar_date - 6 AND k.calendar_date) + 0.0001);
    PERFORM pg_temp.check('kpi-ma', '7d mean lies within its own window bounds', n = 0, format('%s rows', n));
END;
$do$;


-- =============================================================================
-- 11. KPI rebuild is idempotent
-- =============================================================================
CREATE TEMP TABLE kpi_snapshot AS
SELECT date_key, calendar_date, orders_count, customers_count, new_customers, units_sold,
       gross_revenue_usd, refund_amount_usd, net_revenue_usd, average_order_value_usd,
       refund_rate, orders_pending_fx, fx_completeness_pct, is_complete,
       rolling_7d_net_revenue_usd, rolling_28d_net_revenue_usd
FROM salesops.kpi_daily;

CREATE TEMP TABLE r_kpi_2 AS SELECT * FROM salesops.refresh_kpi_daily();

DO $do$
DECLARE n INTEGER; a INTEGER; b INTEGER;
BEGIN
    SELECT dates_written INTO a FROM r_kpi_1;
    SELECT dates_written INTO b FROM r_kpi_2;
    PERFORM pg_temp.check('kpi-idempotency', 'second rebuild writes the same number of rows',
                          a = b, format('%s vs %s', a, b));

    -- Byte-for-byte identical, both directions.
    SELECT count(*) INTO n FROM (
        (SELECT * FROM kpi_snapshot
         EXCEPT
         SELECT date_key, calendar_date, orders_count, customers_count, new_customers, units_sold,
                gross_revenue_usd, refund_amount_usd, net_revenue_usd, average_order_value_usd,
                refund_rate, orders_pending_fx, fx_completeness_pct, is_complete,
                rolling_7d_net_revenue_usd, rolling_28d_net_revenue_usd
         FROM salesops.kpi_daily)
        UNION ALL
        (SELECT date_key, calendar_date, orders_count, customers_count, new_customers, units_sold,
                gross_revenue_usd, refund_amount_usd, net_revenue_usd, average_order_value_usd,
                refund_rate, orders_pending_fx, fx_completeness_pct, is_complete,
                rolling_7d_net_revenue_usd, rolling_28d_net_revenue_usd
         FROM salesops.kpi_daily
         EXCEPT
         SELECT * FROM kpi_snapshot)
    ) diff;
    PERFORM pg_temp.check('kpi-idempotency', 'rebuild is byte-for-byte identical',
                          n = 0, format('%s differing rows', n));

    SELECT count(*) INTO n FROM salesops.kpi_daily k1
    JOIN salesops.kpi_daily k2 ON k1.date_key = k2.date_key AND k1.calendar_date <> k2.calendar_date;
    PERFORM pg_temp.check('kpi-idempotency', 'no duplicate KPI rows', n = 0);
END;
$do$;


-- =============================================================================
-- 12. Zero-denominator and incomplete-FX safety
-- =============================================================================
DO $do$
DECLARE n INTEGER; k RECORD;
BEGIN
    -- Make one date fully unconvertible and prove the KPI row stays honest.
    UPDATE salesops.fact_orders SET exchange_rate_to_usd = NULL
    WHERE order_date = DATE '{D_MON}';

    PERFORM salesops.refresh_kpi_daily();

    SELECT * INTO k FROM salesops.kpi_daily WHERE calendar_date = DATE '{D_MON}';

    PERFORM pg_temp.check('kpi-safety', 'a fully unconverted date still gets a KPI row',
                          k.calendar_date IS NOT NULL);
    PERFORM pg_temp.check('kpi-safety', 'orders_pending_fx equals orders_count',
                          k.orders_pending_fx = k.orders_count,
                          format('%s of %s', k.orders_pending_fx, k.orders_count));
    PERFORM pg_temp.check('kpi-safety', 'fx_completeness_pct is 0', k.fx_completeness_pct = 0,
                          format('%s', k.fx_completeness_pct));
    PERFORM pg_temp.check('kpi-safety', 'is_complete is false', k.is_complete = FALSE);

    -- The property the whole design protects: missing FX is NULL, never zero.
    PERFORM pg_temp.check('kpi-safety', 'gross_revenue_usd is NULL, not 0',
                          k.gross_revenue_usd IS NULL, format('%s', k.gross_revenue_usd));
    PERFORM pg_temp.check('kpi-safety', 'net_revenue_usd is NULL, not 0',
                          k.net_revenue_usd IS NULL, format('%s', k.net_revenue_usd));
    PERFORM pg_temp.check('kpi-safety', 'AOV is NULL rather than a division error',
                          k.average_order_value_usd IS NULL, format('%s', k.average_order_value_usd));
    PERFORM pg_temp.check('kpi-safety', 'refund_rate is NULL rather than a division error',
                          k.refund_rate IS NULL, format('%s', k.refund_rate));

    -- Volume metrics are unaffected by an FX gap - they never needed a rate.
    PERFORM pg_temp.check('kpi-safety', 'order and unit counts survive an FX gap',
                          k.orders_count > 0 AND k.units_sold > 0);
END;
$do$;


-- =============================================================================
-- 13. The shared run ledger does not corrupt Orders Ingestion's window
--
-- Stage 4 put the FX sync and the KPI refresh into ingestion_runs alongside
-- order ingestion. Orders Ingestion derives its next window from
-- max(window_to) over its own successful runs - so without a source filter it
-- would happily adopt a window belonging to another pipeline entirely.
--
-- Planted here: a kpi-refresh run claiming a wildly future window. The order
-- ingestion window must ignore it completely.
-- =============================================================================
INSERT INTO salesops.ingestion_runs
    (batch_id, source, window_from, window_to, status, records_received,
     records_accepted, records_rejected, records_duplicate, finished_at)
VALUES
    (gen_random_uuid(), 'kpi-refresh', DATE '2027-06-01', DATE '2027-06-30',
     'success', 0, 0, 0, 0, now()),
    (gen_random_uuid(), 'frankfurter', DATE '2027-01-01', DATE '2027-01-31',
     'success', 0, 0, 0, 0, now());

-- Run the real statement. Its top level is INSERT ... RETURNING, which cannot
-- be wrapped in CREATE TABLE AS, so the row it created is read back by the
-- execution id it was given.
{open_ingest_run};

DO $do$
DECLARE w_to DATE; w_from DATE; own_last DATE;
BEGIN
    SELECT window_to, window_from INTO w_to, w_from
    FROM salesops.ingestion_runs WHERE n8n_execution_id = 'test-exec';

    SELECT max(window_to) INTO own_last
    FROM salesops.ingestion_runs
    WHERE source = 'mock-sales-api' AND status IN ('success', 'partial')
      AND COALESCE(n8n_execution_id, '') <> 'test-exec';

    PERFORM pg_temp.check('run-ledger', 'ingestion window ignores other pipelines'' windows',
                          w_to <= CURRENT_DATE, format('window_to = %s', w_to));

    PERFORM pg_temp.check('run-ledger', 'ingestion window derives from its OWN last run',
                          w_from = own_last - 1, format('from %s, own last_to %s', w_from, own_last));

    -- The planted 2027 windows must not have leaked in.
    PERFORM pg_temp.check('run-ledger', 'a 2027 window from another source is not adopted',
                          w_from < DATE '2027-01-01', format('window_from = %s', w_from));
END;
$do$;


-- =============================================================================
-- Report
-- =============================================================================
\\echo ''
\\echo '=============== STAGE 4 FX + KPI TEST RESULTS ==============='

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
        RAISE EXCEPTION 'STAGE 4 TESTS FAILED: % check(s) -> %', failed, names;
    END IF;
    RAISE NOTICE 'All Stage 4 FX and KPI checks passed.';
END;
$do$;

ROLLBACK;
"""


def main() -> int:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "salesops", "-d", "salesops", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        cwd=REPO_ROOT,
        input=build_script(),
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
