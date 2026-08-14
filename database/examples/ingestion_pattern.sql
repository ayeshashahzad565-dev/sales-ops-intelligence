-- =============================================================================
-- The Stage 3 ingestion pattern, written out as SQL
--
-- This is not a migration and not a test. It is the executable specification of
-- what the n8n ingestion workflow will do, so that the workflow can be built
-- against something already proven to work rather than invented from scratch.
--
-- It runs inside a transaction that ROLLBACKs, so it can be run against a
-- populated database without consequence:
--
--   docker compose exec -T postgres \
--     psql -U salesops -d salesops -v ON_ERROR_STOP=1 -f /database/examples/ingestion_pattern.sql
--
-- The flow being demonstrated:
--
--   Mock API  ->  raw_orders_staging  ->  validate  ->  dimensions  ->  fact_orders
--                        (JSONB)                          (upsert)      (idempotent)
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

\echo ''
\echo '### STEP 1 - land the raw API payloads, untouched'
\echo ''

-- n8n does exactly this: one row per order from GET /orders, the whole object
-- stored as JSONB, tagged with the run's batch_id. Nothing is validated yet -
-- landing must not be able to fail on bad data.
CREATE TEMP TABLE demo_batch AS SELECT gen_random_uuid() AS batch_id;

INSERT INTO salesops.raw_orders_staging (batch_id, order_id, source_payload)
SELECT
    (SELECT batch_id FROM demo_batch),
    payload ->> 'order_id',
    payload
FROM (
    VALUES
    ('{"order_id":"DEMO-0001","order_date":"2026-08-03","region":"NA","product":"SKU-1042",
       "channel":"web","customer_id":"CUST-NA-0365","quantity":2,"unit_price":149.00,
       "currency":"USD","refund_amount":0.00}'::JSONB),
    ('{"order_id":"DEMO-0002","order_date":"2026-08-03","region":"EMEA","product":"SKU-3375",
       "channel":"partner","customer_id":"CUST-EMEA-0112","quantity":1,"unit_price":249.00,
       "currency":"EUR","refund_amount":249.00}'::JSONB),
    ('{"order_id":"DEMO-0003","order_date":"2026-08-03","region":"APAC","product":"SKU-5031",
       "channel":"mobile","customer_id":"CUST-APAC-0044","quantity":12,"unit_price":4200,
       "currency":"JPY","refund_amount":0}'::JSONB),
    -- A deliberately broken payload: negative quantity. It lands like any other
    -- row; validation rejects it in step 2 and it becomes a dead-letter record.
    ('{"order_id":"DEMO-BAD","order_date":"2026-08-03","region":"NA","product":"SKU-1042",
       "channel":"web","customer_id":"CUST-NA-0365","quantity":-4,"unit_price":10.00,
       "currency":"USD","refund_amount":0.00}'::JSONB)
) AS raw(payload);

SELECT count(*) AS payloads_landed, processing_status
FROM salesops.raw_orders_staging
WHERE batch_id = (SELECT batch_id FROM demo_batch)
GROUP BY processing_status;


\echo ''
\echo '### STEP 2 - reject what cannot be trusted, with a reason'
\echo ''

-- Validation happens in SQL against the staging layer, not in flight. A row
-- that fails keeps its payload and gains an explanation, which is what makes
-- the dead-letter queue in Stage 10 actionable rather than just a bin.
UPDATE salesops.raw_orders_staging AS s
SET processing_status = 'failed',
    error_message     = 'quantity must be greater than zero',
    processed_at      = now()
WHERE s.batch_id = (SELECT batch_id FROM demo_batch)
  AND (s.source_payload ->> 'quantity')::NUMERIC <= 0;

SELECT order_id, processing_status, error_message
FROM salesops.raw_orders_staging
WHERE batch_id = (SELECT batch_id FROM demo_batch)
  AND processing_status = 'failed';


\echo ''
\echo '### STEP 3 - upsert customers (a late-arriving dimension)'
\echo ''

-- Customers are discovered from the order stream; there is no customer feed.
-- LEAST() on conflict means first_seen_date only ever moves earlier, so a
-- backfill of older orders corrects it instead of corrupting it.
INSERT INTO salesops.dim_customer (customer_id, region_id, first_seen_date)
SELECT DISTINCT
    s.source_payload ->> 'customer_id',
    r.region_id,
    (s.source_payload ->> 'order_date')::DATE
FROM salesops.raw_orders_staging AS s
JOIN salesops.dim_region        AS r ON r.region_code = s.source_payload ->> 'region'
WHERE s.batch_id = (SELECT batch_id FROM demo_batch)
  AND s.processing_status = 'pending'
ON CONFLICT (customer_id) DO UPDATE
    SET first_seen_date = LEAST(salesops.dim_customer.first_seen_date, EXCLUDED.first_seen_date);

SELECT customer_id, region_id, first_seen_date, customer_name
FROM salesops.dim_customer ORDER BY customer_id;


\echo ''
\echo '### STEP 4 - resolve dimensions and insert facts, idempotently'
\echo ''

-- Source labels ('NA', 'SKU-1042', 'web') are resolved to surrogate keys by
-- JOIN. An unknown label produces no row rather than a silently invented
-- dimension member - the count mismatch is then visible and investigable.
--
-- ON CONFLICT (order_id) DO NOTHING is the idempotency guarantee: re-running
-- this whole block changes nothing and double-counts nothing.
--
-- exchange_rate_to_usd is not set here. There is no rate yet, and the USD
-- columns are generated, so they stay NULL until step 5.
INSERT INTO salesops.fact_orders (
    order_id, order_date, customer_id, region_id, product_id, channel_id,
    quantity, unit_price, currency, refund_amount_local
)
SELECT
    s.source_payload ->> 'order_id',
    (s.source_payload ->> 'order_date')::DATE,
    s.source_payload ->> 'customer_id',
    r.region_id,
    p.product_id,
    c.channel_id,
    (s.source_payload ->> 'quantity')::INTEGER,
    (s.source_payload ->> 'unit_price')::NUMERIC(14,4),
    s.source_payload ->> 'currency',
    (s.source_payload ->> 'refund_amount')::NUMERIC(18,4)
FROM salesops.raw_orders_staging AS s
JOIN salesops.dim_region  AS r ON r.region_code   = s.source_payload ->> 'region'
JOIN salesops.dim_product AS p ON p.product_sku   = s.source_payload ->> 'product'
JOIN salesops.dim_channel AS c ON c.channel_code  = s.source_payload ->> 'channel'
WHERE s.batch_id = (SELECT batch_id FROM demo_batch)
  AND s.processing_status = 'pending'
ON CONFLICT (order_id) DO NOTHING;

UPDATE salesops.raw_orders_staging
SET processing_status = 'processed', processed_at = now()
WHERE batch_id = (SELECT batch_id FROM demo_batch)
  AND processing_status = 'pending';

SELECT order_id, order_date, currency, quantity, unit_price,
       gross_amount_local, refund_amount_local,
       gross_amount_usd AS usd_before_fx
FROM salesops.fact_orders ORDER BY order_id;

\echo '-- Re-running the same insert must be a no-op:'
INSERT INTO salesops.fact_orders (
    order_id, order_date, customer_id, region_id, product_id, channel_id,
    quantity, unit_price, currency, refund_amount_local)
SELECT 'DEMO-0001', DATE '2026-08-03', 'CUST-NA-0365', r.region_id, p.product_id, c.channel_id,
       999, 1.0000, 'USD', 0
FROM salesops.dim_region r, salesops.dim_product p, salesops.dim_channel c
WHERE r.region_code = 'NA' AND p.product_sku = 'SKU-1042' AND c.channel_code = 'web'
ON CONFLICT (order_id) DO NOTHING;

SELECT count(*) AS fact_rows_after_rerun, sum(quantity) AS total_units
FROM salesops.fact_orders;


\echo ''
\echo '### STEP 5 - attach real exchange rates'
\echo ''

-- Stage 3 loads these from Frankfurter. The values below are placeholders for
-- this walkthrough only and are rolled back with everything else.
--
-- Note USD -> 1.0: Frankfurter never returns a USD/USD pair, so the workflow
-- must insert it explicitly or every USD order stays unconverted forever.
INSERT INTO salesops.exchange_rates (rate_date, currency, rate_to_usd, source) VALUES
    (DATE '2026-08-03', 'USD', 1.00000000, 'demo'),
    (DATE '2026-08-03', 'EUR', 1.08000000, 'demo'),
    (DATE '2026-08-03', 'JPY', 0.00640000, 'demo')
ON CONFLICT (rate_date, currency) DO UPDATE
    SET rate_to_usd = EXCLUDED.rate_to_usd,
        source      = EXCLUDED.source,
        fetched_at  = now();

-- Setting the rate is all it takes: gross/refund/net USD are generated columns
-- and recompute themselves. There is no second pass writing money values, so
-- they cannot drift out of step with the rate.
UPDATE salesops.fact_orders AS f
SET exchange_rate_to_usd = x.rate_to_usd
FROM salesops.exchange_rates AS x
WHERE x.rate_date = f.order_date
  AND x.currency  = f.currency
  AND f.exchange_rate_to_usd IS NULL;

SELECT order_id, currency, gross_amount_local, exchange_rate_to_usd,
       gross_amount_usd, refund_amount_usd, net_amount_usd
FROM salesops.fact_orders ORDER BY order_id;


\echo ''
\echo '### STEP 6 - the analytical views, now that FX exists'
\echo ''

SELECT order_date, day_name, is_weekend, orders_count, units_sold,
       gross_revenue_usd, refund_amount_usd, net_revenue_usd,
       average_order_value_usd, refund_rate, orders_pending_fx
FROM salesops.daily_sales_base
ORDER BY order_date;

SELECT order_date, region_code, orders_count, units_sold,
       gross_revenue_usd, average_order_value_usd, refund_rate, orders_pending_fx
FROM salesops.regional_sales_base
ORDER BY order_date, region_code;


\echo ''
\echo '### STEP 7 - traceability: any figure back to the bytes it came from'
\echo ''

SELECT f.order_id, f.gross_amount_usd, s.batch_id, s.received_at, s.source_payload
FROM salesops.fact_orders          AS f
JOIN salesops.raw_orders_staging   AS s ON s.order_id = f.order_id
WHERE f.order_id = 'DEMO-0002';


\echo ''
\echo '### Rolling back - this walkthrough leaves no data behind.'
ROLLBACK;
