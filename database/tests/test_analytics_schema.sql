-- =============================================================================
-- Schema validation suite
--
-- Pure SQL on purpose. The thing under test is a PostgreSQL schema, so testing
-- it with psql needs no ORM, no driver, no second language and no dependency
-- that can drift from what the database actually enforces.
--
-- Run:
--   docker compose exec -T postgres \
--     psql -U salesops -d salesops -v ON_ERROR_STOP=1 -f - < database/tests/test_analytics_schema.sql
--
-- Structure:
--   * everything runs inside one transaction that ROLLBACKs, so the sample rows
--     inserted below never persist - the suite is safe against a populated
--     production-shaped database;
--   * each check appends a row to a temp table rather than aborting, so one
--     failure does not hide the next twelve;
--   * "this must be rejected" cases use a nested BEGIN ... EXCEPTION block,
--     which gives plpgsql an implicit savepoint - without it the first expected
--     violation would poison the whole transaction;
--   * the final block RAISEs if anything failed, so psql exits non-zero and CI
--     notices.
-- =============================================================================

\set ON_ERROR_STOP on
\timing off

BEGIN;

CREATE TEMP TABLE test_results (
    id      SERIAL PRIMARY KEY,
    section TEXT,
    name    TEXT,
    passed  BOOLEAN,
    detail  TEXT
) ON COMMIT DROP;

CREATE OR REPLACE FUNCTION pg_temp.check(
    p_section TEXT,
    p_name    TEXT,
    p_passed  BOOLEAN,
    p_detail  TEXT DEFAULT ''
) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO test_results (section, name, passed, detail)
    VALUES (p_section, p_name, COALESCE(p_passed, FALSE), p_detail);
END;
$$;


-- =============================================================================
-- The decision fixture
--
-- The behavioural sections - the Stage 8, 9 and 10 guard triggers - all need a
-- Stage 6 decision row to build on, because those triggers READ the live
-- decision and compare it with what the caller claims. A synthetic decision
-- passed in memory could not exercise them.
--
-- Historically each of those sections looked for a suitable decision and, if it
-- found none, recorded a PASS labelled "skipped". On a populated warehouse that
-- was invisible. On a FRESH CLONE - which is where this suite is run first, and
-- where its verdict carries the most weight - it meant 32 checks covering
-- authorisation, exactly-once execution and reconciliation silently did not run
-- while the suite reported green.
--
-- So the suite now seeds what it needs. It seeds only what is MISSING, so on a
-- real warehouse these checks still run against genuine Stage 6 output; and
-- because the whole suite rolls back, nothing it inserts survives.
--
-- Everything below satisfies the real CHECK constraints rather than working
-- around them: severity drives routing drives decision, an escalated severity
-- carries an impact tier, and a revenue delta carries both of its inputs.
-- =============================================================================
CREATE OR REPLACE FUNCTION pg_temp.seed_decision(p_severity TEXT, p_date DATE)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    v_anomaly BIGINT;
BEGIN
    IF EXISTS (
        SELECT 1 FROM salesops.anomaly_decisions
        WHERE severity = p_severity AND routing = 'human_review'
    ) THEN
        RETURN;   -- the warehouse already has one; use the real thing
    END IF;

    INSERT INTO salesops.anomaly_daily (
        calendar_date, detector_version, anomaly_score, is_anomaly,
        revenue_deviation_pct, revenue_robust_z, refund_rate_deviation,
        refund_robust_z, baseline_status, baseline_kind, baseline_size,
        dominant_signal, signal_count, revenue_baseline_median
    )
    VALUES (
        p_date, 'v1.0.0', 8.9, TRUE,
        -65.19, -3.78, 0.33, 39.23,
        'scored', 'day_of_week', 12,
        'refund', CASE WHEN p_severity = 'critical' THEN 3 ELSE 1 END, 13641.74
    )
    ON CONFLICT (calendar_date, detector_version) DO NOTHING
    RETURNING anomaly_id INTO v_anomaly;

    IF v_anomaly IS NULL THEN
        SELECT anomaly_id INTO v_anomaly FROM salesops.anomaly_daily
        WHERE calendar_date = p_date AND detector_version = 'v1.0.0';
    END IF;

    INSERT INTO salesops.anomaly_decisions (
        anomaly_id, calendar_date, decision_version, detector_version,
        baseline_status, anomaly_score, is_anomaly, signal_count,
        dominant_signal, revenue_robust_z, refund_robust_z,
        revenue_deviation_pct, refund_rate_deviation,
        expected_net_revenue_usd, actual_net_revenue_usd,
        revenue_delta_usd, absolute_revenue_delta_usd, revenue_delta_pct,
        business_impact_tier, severity, routing, decision,
        notification_allowed, human_review_required, decision_reason_code
    )
    VALUES (
        v_anomaly, p_date, 'stage6-v1', 'v1.0.0',
        'scored', 8.9, TRUE,
        CASE WHEN p_severity = 'critical' THEN 3 ELSE 1 END,
        'refund', -3.78, 39.23, -65.19, 0.33,
        13641.74, 4748.95, -8892.79, 8892.79, -65.19,
        'material', p_severity, 'human_review', 'action_required',
        FALSE, TRUE,
        CASE WHEN p_severity = 'critical'
             THEN 'CRITICAL_COMBINED_IMPACT' ELSE 'HIGH_REVENUE_IMPACT' END
    )
    ON CONFLICT (anomaly_id, decision_version) DO NOTHING;
END;
$$;

DO $$
BEGIN
    -- Two dates far outside any window the pipeline itself uses, so a seeded
    -- row can never be mistaken for pipeline output while the suite is running.
    PERFORM pg_temp.seed_decision('critical', DATE '2025-01-02');
    PERFORM pg_temp.seed_decision('major',    DATE '2025-01-03');

    PERFORM pg_temp.check('fixture',
        'a critical human_review decision is available to the behaviour checks',
        EXISTS (SELECT 1 FROM salesops.anomaly_decisions
                WHERE severity = 'critical' AND routing = 'human_review'));

    PERFORM pg_temp.check('fixture',
        'a major human_review decision is available to the behaviour checks',
        EXISTS (SELECT 1 FROM salesops.anomaly_decisions
                WHERE severity = 'major' AND routing = 'human_review'));
END;
$$;


-- =============================================================================
-- 1. Structure: every expected relation exists
-- =============================================================================
DO $$
DECLARE
    expected_table TEXT;
    expected_view  TEXT;
    found          BOOLEAN;
BEGIN
    FOREACH expected_table IN ARRAY ARRAY[
        'raw_orders_staging', 'dim_date', 'dim_region', 'dim_product',
        'dim_channel', 'dim_customer', 'exchange_rates', 'fact_orders',
        'schema_migrations'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'salesops'
              AND table_name   = expected_table
              AND table_type   = 'BASE TABLE'
        ) INTO found;
        PERFORM pg_temp.check('structure', format('table %s exists', expected_table), found);
    END LOOP;

    FOREACH expected_view IN ARRAY ARRAY[
        'daily_sales_base', 'regional_sales_base',
        -- Stage 11 presentation layer
        'exec_kpi_daily', 'exec_headline_kpis', 'exec_anomaly_severity_summary',
        'exec_actionable_anomalies', 'exec_anomaly_timeline',
        'exec_notification_status', 'exec_review_status',
        'exec_remediation_status', 'exec_pipeline_health',
        'ops_pipeline_runs', 'ops_attention_items',
        'anomaly_investigation', 'anomaly_investigation_detail',
        'audit_event_stream', 'incident_timeline'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'salesops' AND table_name = expected_view
        ) INTO found;
        PERFORM pg_temp.check('structure', format('view %s exists', expected_view), found);
    END LOOP;
END;
$$;


-- =============================================================================
-- 2. Monetary columns are exact numerics
--
-- The rule this enforces: no money column anywhere in the schema may be a
-- binary floating-point type. Written as a scan over every column whose name
-- looks monetary, so a column added later is covered without editing this test.
-- =============================================================================
DO $$
DECLARE
    offending TEXT;
    bad_count INTEGER;
BEGIN
    SELECT count(*), string_agg(format('%s.%s (%s)', table_name, column_name, data_type), ', ')
      INTO bad_count, offending
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND (column_name ~ '(amount|price|rate_to_usd)' )
      AND data_type IN ('double precision', 'real');

    PERFORM pg_temp.check(
        'money', 'no monetary column uses floating point',
        bad_count = 0,
        COALESCE(offending, 'none')
    );

    SELECT count(*)
      INTO bad_count
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND table_name   = 'fact_orders'
      AND column_name IN (
            'unit_price', 'gross_amount_local', 'refund_amount_local',
            'exchange_rate_to_usd', 'gross_amount_usd', 'refund_amount_usd',
            'net_amount_usd')
      AND data_type = 'numeric';

    PERFORM pg_temp.check(
        'money', 'all 7 fact_orders money columns are NUMERIC',
        bad_count = 7,
        format('%s of 7', bad_count)
    );

    SELECT count(*) INTO bad_count
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'exchange_rates'
      AND column_name = 'rate_to_usd' AND data_type = 'numeric';

    PERFORM pg_temp.check('money', 'exchange_rates.rate_to_usd is NUMERIC', bad_count = 1);
END;
$$;


-- =============================================================================
-- 3. Keys, constraints and indexes are actually declared
-- =============================================================================
DO $$
DECLARE
    n INTEGER;
BEGIN
    -- order_id is the idempotency key, so it must be the primary key.
    SELECT count(*) INTO n
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema    = tc.table_schema
    WHERE tc.table_schema   = 'salesops'
      AND tc.table_name     = 'fact_orders'
      AND tc.constraint_type = 'PRIMARY KEY'
      AND kcu.column_name    = 'order_id';
    PERFORM pg_temp.check('constraints', 'fact_orders PK is order_id', n = 1);

    -- Five dimension foreign keys.
    SELECT count(*) INTO n
    FROM information_schema.table_constraints
    WHERE table_schema = 'salesops' AND table_name = 'fact_orders'
      AND constraint_type = 'FOREIGN KEY';
    PERFORM pg_temp.check('constraints', 'fact_orders has 5 foreign keys', n = 5, format('found %s', n));

    -- The FX de-duplication guarantee.
    SELECT count(*) INTO n
    FROM information_schema.table_constraints
    WHERE table_schema = 'salesops' AND table_name = 'exchange_rates'
      AND constraint_type = 'PRIMARY KEY';
    PERFORM pg_temp.check('constraints', 'exchange_rates has a (rate_date, currency) key', n = 1);

    -- Staging must NOT have foreign keys - invalid rows have to be storable.
    SELECT count(*) INTO n
    FROM information_schema.table_constraints
    WHERE table_schema = 'salesops' AND table_name = 'raw_orders_staging'
      AND constraint_type = 'FOREIGN KEY';
    PERFORM pg_temp.check(
        'constraints', 'raw_orders_staging has no foreign keys (accepts bad data)',
        n = 0, format('found %s', n));

    -- Indexes on the columns the analytical queries filter by.
    SELECT count(*) INTO n
    FROM pg_indexes
    WHERE schemaname = 'salesops' AND tablename = 'fact_orders';
    PERFORM pg_temp.check('constraints', 'fact_orders has >= 6 indexes', n >= 6, format('found %s', n));

    SELECT count(*) INTO n FROM pg_indexes
    WHERE schemaname = 'salesops' AND tablename = 'fact_orders'
      AND indexdef LIKE '%exchange_rate_to_usd IS NULL%';
    PERFORM pg_temp.check('constraints', 'partial index exists for rows awaiting FX', n = 1);
END;
$$;


-- =============================================================================
-- 4. Reference data matches what the Mock API emits
--
-- If these drift apart, Stage 3 ingestion fails on a foreign key lookup. This
-- is the test that catches "someone added a region to the API and not the
-- warehouse".
-- =============================================================================
DO $$
DECLARE
    n INTEGER;
    v TEXT;
BEGIN
    SELECT count(*) INTO n FROM salesops.dim_region;
    PERFORM pg_temp.check('reference', 'dim_region has 4 regions', n = 4, format('found %s', n));

    SELECT string_agg(region_code, ',' ORDER BY region_code) INTO v FROM salesops.dim_region;
    PERFORM pg_temp.check('reference', 'region codes are APAC,EMEA,LATAM,NA',
                          v = 'APAC,EMEA,LATAM,NA', COALESCE(v, 'null'));

    SELECT count(*) INTO n FROM salesops.dim_channel;
    PERFORM pg_temp.check('reference', 'dim_channel has 4 channels', n = 4, format('found %s', n));

    SELECT string_agg(channel_code, ',' ORDER BY channel_code) INTO v FROM salesops.dim_channel;
    PERFORM pg_temp.check('reference', 'channel codes match the API',
                          v = 'field_sales,mobile,partner,web', COALESCE(v, 'null'));

    SELECT count(*) INTO n FROM salesops.dim_product;
    PERFORM pg_temp.check('reference', 'dim_product has 6 SKUs', n = 6, format('found %s', n));

    SELECT string_agg(product_sku, ',' ORDER BY product_sku) INTO v FROM salesops.dim_product;
    PERFORM pg_temp.check('reference', 'product SKUs match the API',
                          v = 'SKU-1042,SKU-2210,SKU-3375,SKU-4180,SKU-5031,SKU-6604',
                          COALESCE(v, 'null'));

    SELECT count(*) INTO n FROM salesops.dim_product WHERE category IS NULL;
    PERFORM pg_temp.check('reference', 'every product has a category', n = 0);

    -- No invented FX, ever.
    --
    -- This began as "exchange_rates must be empty", which was right until the
    -- pipeline started writing USD -> 1.0 identity rows. Emptiness was only ever
    -- a proxy for the real rule, so the rule is now asserted directly: every
    -- rate must come from a declared provenance. 'identity' is USD -> USD, which
    -- is arithmetic rather than a market rate; 'frankfurter' is a real feed.
    -- Anything else means somebody made a number up.
    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE source NOT IN ('identity', 'frankfurter');
    PERFORM pg_temp.check('reference', 'no exchange rate has an invented source',
                          n = 0, format('%s rows with an unrecognised source', n));

    SELECT count(*) INTO n FROM salesops.exchange_rates
    WHERE source = 'identity' AND (currency <> 'USD' OR rate_to_usd <> 1.0);
    PERFORM pg_temp.check('reference', 'identity rates are only USD -> 1.0',
                          n = 0, format('%s bad identity rows', n));
END;
$$;


-- =============================================================================
-- 5. dim_date coverage and correctness
-- =============================================================================
DO $$
DECLARE
    n           INTEGER;
    min_d       DATE;
    max_d       DATE;
    expected    INTEGER;
BEGIN
    SELECT count(*), min(calendar_date), max(calendar_date)
      INTO n, min_d, max_d FROM salesops.dim_date;

    expected := (DATE '2027-12-31' - DATE '2025-01-01') + 1;
    PERFORM pg_temp.check('dim_date', 'row count matches the generated range',
                          n = expected, format('%s rows, expected %s', n, expected));
    PERFORM pg_temp.check('dim_date', 'starts 2025-01-01', min_d = DATE '2025-01-01', min_d::TEXT);
    PERFORM pg_temp.check('dim_date', 'ends 2027-12-31',   max_d = DATE '2027-12-31', max_d::TEXT);

    -- No gaps: a missing day would break the fact_orders foreign key.
    SELECT count(*) INTO n
    FROM generate_series(DATE '2025-01-01', DATE '2027-12-31', INTERVAL '1 day') g
    LEFT JOIN salesops.dim_date d ON d.calendar_date = g::DATE
    WHERE d.calendar_date IS NULL;
    PERFORM pg_temp.check('dim_date', 'no missing days in the range', n = 0, format('%s gaps', n));

    -- Covers the Mock API's current 90-day window with room either side.
    PERFORM pg_temp.check('dim_date', 'covers today +/- 180 days',
        min_d <= CURRENT_DATE - 180 AND max_d >= CURRENT_DATE + 180);

    -- is_weekend agrees with ISO day_of_week.
    SELECT count(*) INTO n FROM salesops.dim_date
    WHERE is_weekend <> (day_of_week >= 6);
    PERFORM pg_temp.check('dim_date', 'is_weekend agrees with day_of_week', n = 0, format('%s wrong', n));

    -- Spot-check a known date: 2026-08-09 was a Sunday.
    SELECT count(*) INTO n FROM salesops.dim_date
    WHERE calendar_date = DATE '2026-08-09'
      AND day_name = 'Sunday' AND day_of_week = 7 AND is_weekend
      AND month_name = 'August' AND quarter = 3 AND year = 2026
      AND date_key = 20260809;
    PERFORM pg_temp.check('dim_date', '2026-08-09 resolves to Sunday, Q3, key 20260809', n = 1);

    -- Locale independence: names came from literal arrays, not to_char().
    SELECT count(*) INTO n FROM salesops.dim_date
    WHERE month_name NOT IN ('January','February','March','April','May','June',
                             'July','August','September','October','November','December');
    PERFORM pg_temp.check('dim_date', 'month names are the expected English literals', n = 0);
END;
$$;


-- =============================================================================
-- 6. Inserts: the happy path, end to end
--
-- Builds a small but complete order graph, exactly as Stage 3 will: resolve
-- dimension ids by natural key, upsert the customer, insert the fact row.
-- =============================================================================
DO $$
DECLARE
    v_region_id  SMALLINT;
    v_product_id SMALLINT;
    v_channel_id SMALLINT;
    v_row        RECORD;
    n            INTEGER;
BEGIN
    -- Sections 6-8 insert orders on 2025-03-17 and then assert on the view rows
    -- for that date, so the date has to be one the pipeline never loads into.
    -- Asserted rather than assumed: if ingested data ever reaches this window
    -- the view arithmetic below would silently start measuring other people's
    -- orders, and the failure would look like a broken view.
    SELECT count(*) INTO n FROM salesops.fact_orders
    WHERE order_date IN (DATE '2025-03-17', DATE '2025-03-18');
    PERFORM pg_temp.check('insert', 'the isolated test window holds no ingested data',
                          n = 0, format('%s real orders found - pick another date', n));

    SELECT region_id  INTO v_region_id  FROM salesops.dim_region  WHERE region_code = 'EMEA';
    SELECT product_id INTO v_product_id FROM salesops.dim_product WHERE product_sku = 'SKU-1042';
    SELECT channel_id INTO v_channel_id FROM salesops.dim_channel WHERE channel_code = 'web';

    PERFORM pg_temp.check('insert', 'dimension lookups by natural key resolve',
        v_region_id IS NOT NULL AND v_product_id IS NOT NULL AND v_channel_id IS NOT NULL);

    INSERT INTO salesops.dim_customer (customer_id, region_id, first_seen_date)
    VALUES ('CUST-EMEA-9001', v_region_id, DATE '2025-03-17');

    -- No exchange rate supplied: this is the normal state after Stage 3 lands a
    -- batch but before FX has been attached.
    INSERT INTO salesops.fact_orders (
        order_id, order_date, customer_id, region_id, product_id, channel_id,
        quantity, unit_price, currency, refund_amount_local
    ) VALUES (
        'TEST-ORD-0001', DATE '2025-03-17', 'CUST-EMEA-9001',
        v_region_id, v_product_id, v_channel_id,
        3, 149.0000, 'EUR', 49.0000
    );

    SELECT * INTO v_row FROM salesops.fact_orders WHERE order_id = 'TEST-ORD-0001';

    PERFORM pg_temp.check('insert', 'valid order inserts', v_row.order_id IS NOT NULL);
    PERFORM pg_temp.check('insert', 'gross_amount_local is generated (3 x 149 = 447)',
        v_row.gross_amount_local = 447.0000, v_row.gross_amount_local::TEXT);
    PERFORM pg_temp.check('insert', 'created_at defaults', v_row.created_at IS NOT NULL);

    -- Decision 5, enforced structurally: no rate means no USD figures at all.
    PERFORM pg_temp.check('insert', 'USD columns are NULL without an exchange rate',
        v_row.gross_amount_usd IS NULL
        AND v_row.refund_amount_usd IS NULL
        AND v_row.net_amount_usd IS NULL);

    -- Attaching a rate later recomputes all three, with no rewrite of the amounts.
    UPDATE salesops.fact_orders
       SET exchange_rate_to_usd = 1.08000000
     WHERE order_id = 'TEST-ORD-0001';

    SELECT * INTO v_row FROM salesops.fact_orders WHERE order_id = 'TEST-ORD-0001';

    PERFORM pg_temp.check('insert', 'gross_amount_usd computes on rate backfill (447 x 1.08 = 482.76)',
        v_row.gross_amount_usd = 482.7600, v_row.gross_amount_usd::TEXT);
    PERFORM pg_temp.check('insert', 'refund_amount_usd computes (49 x 1.08 = 52.92)',
        v_row.refund_amount_usd = 52.9200, v_row.refund_amount_usd::TEXT);
    PERFORM pg_temp.check('insert', 'net_amount_usd computes ((447-49) x 1.08 = 429.84)',
        v_row.net_amount_usd = 429.8400, v_row.net_amount_usd::TEXT);

    -- Staging accepts a payload that would never pass fact validation.
    INSERT INTO salesops.raw_orders_staging (batch_id, order_id, source_payload, processing_status, error_message)
    VALUES (gen_random_uuid(), NULL, '{"garbage": true, "quantity": -5}'::JSONB,
            'failed', 'missing order_id');
    PERFORM pg_temp.check('insert', 'staging accepts a malformed payload for dead-lettering', TRUE);
END;
$$;


-- =============================================================================
-- 7. Rejections: the constraints that protect the model
--
-- Each nested BEGIN ... EXCEPTION is its own savepoint, so an expected failure
-- does not abort the suite.
-- =============================================================================
DO $$
DECLARE
    v_region_id  SMALLINT;
    v_product_id SMALLINT;
    v_channel_id SMALLINT;
BEGIN
    SELECT region_id  INTO v_region_id  FROM salesops.dim_region  WHERE region_code = 'EMEA';
    SELECT product_id INTO v_product_id FROM salesops.dim_product WHERE product_sku = 'SKU-1042';
    SELECT channel_id INTO v_channel_id FROM salesops.dim_channel WHERE channel_code = 'web';

    -- Duplicate order_id: THE idempotency guarantee. Without this, a re-run
    -- double-counts revenue.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-0001', DATE '2025-03-18', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'EUR');
        PERFORM pg_temp.check('reject', 'duplicate order_id is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN unique_violation THEN
        PERFORM pg_temp.check('reject', 'duplicate order_id is rejected', TRUE);
    END;

    -- ON CONFLICT is what makes a re-run a no-op rather than an error.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-0001', DATE '2025-03-18', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'EUR')
        ON CONFLICT (order_id) DO NOTHING;
        PERFORM pg_temp.check('reject', 'ON CONFLICT DO NOTHING makes re-ingestion safe', TRUE);
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('reject', 'ON CONFLICT DO NOTHING makes re-ingestion safe',
                              FALSE, SQLERRM);
    END;

    -- Unknown region: source values cannot enter the fact table unvalidated.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-BADFK', DATE '2025-03-18', 'CUST-EMEA-9001',
                999, v_product_id, v_channel_id, 1, 10.0000, 'EUR');
        PERFORM pg_temp.check('reject', 'unknown region_id is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN foreign_key_violation THEN
        PERFORM pg_temp.check('reject', 'unknown region_id is rejected', TRUE);
    END;

    -- Unknown customer.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-BADCUST', DATE '2025-03-18', 'CUST-NOPE-0000',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'EUR');
        PERFORM pg_temp.check('reject', 'unknown customer_id is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN foreign_key_violation THEN
        PERFORM pg_temp.check('reject', 'unknown customer_id is rejected', TRUE);
    END;

    -- A date outside dim_date. This is why dim_date is generated wide.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-BADDATE', DATE '2099-01-01', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'EUR');
        PERFORM pg_temp.check('reject', 'order_date outside dim_date is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN foreign_key_violation THEN
        PERFORM pg_temp.check('reject', 'order_date outside dim_date is rejected', TRUE);
    END;

    -- Non-positive quantity.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-BADQTY', DATE '2025-03-18', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 0, 10.0000, 'EUR');
        PERFORM pg_temp.check('reject', 'quantity of 0 is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('reject', 'quantity of 0 is rejected', TRUE);
    END;

    -- Refunding more than the line was worth.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency, refund_amount_local)
        VALUES ('TEST-ORD-BADREFUND', DATE '2025-03-18', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'EUR', 25.0000);
        PERFORM pg_temp.check('reject', 'refund above gross is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('reject', 'refund above gross is rejected', TRUE);
    END;

    -- Malformed currency code.
    BEGIN
        INSERT INTO salesops.fact_orders (
            order_id, order_date, customer_id, region_id, product_id, channel_id,
            quantity, unit_price, currency)
        VALUES ('TEST-ORD-BADCCY', DATE '2025-03-18', 'CUST-EMEA-9001',
                v_region_id, v_product_id, v_channel_id, 1, 10.0000, 'eu');
        PERFORM pg_temp.check('reject', 'malformed currency code is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('reject', 'malformed currency code is rejected', TRUE);
    END;

    -- Generated columns cannot be written to. This is the guarantee that USD
    -- figures can never be hand-set to something inconsistent with the rate.
    BEGIN
        EXECUTE $q$
            INSERT INTO salesops.fact_orders (
                order_id, order_date, customer_id, region_id, product_id, channel_id,
                quantity, unit_price, currency, gross_amount_usd)
            VALUES ('TEST-ORD-GEN', DATE '2025-03-18', 'CUST-EMEA-9001',
                    1, 1, 1, 1, 10.0000, 'EUR', 999.0000)
        $q$;
        PERFORM pg_temp.check('reject', 'writing to a generated USD column is rejected',
                              FALSE, 'insert succeeded');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('reject', 'writing to a generated USD column is rejected', TRUE);
    END;

    -- Duplicate (rate_date, currency) in exchange_rates.
    BEGIN
        INSERT INTO salesops.exchange_rates (rate_date, currency, rate_to_usd, source)
        VALUES (DATE '2025-03-18', 'EUR', 1.08, 'test'),
               (DATE '2025-03-18', 'EUR', 1.09, 'test');
        PERFORM pg_temp.check('reject', 'duplicate (rate_date, currency) is rejected',
                              FALSE, 'insert succeeded');
    EXCEPTION WHEN unique_violation THEN
        PERFORM pg_temp.check('reject', 'duplicate (rate_date, currency) is rejected', TRUE);
    END;

    -- A failed staging row must carry a reason.
    BEGIN
        INSERT INTO salesops.raw_orders_staging (batch_id, source_payload, processing_status)
        VALUES (gen_random_uuid(), '{}'::JSONB, 'failed');
        PERFORM pg_temp.check('reject', 'failed staging row without a reason is rejected',
                              FALSE, 'insert succeeded');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('reject', 'failed staging row without a reason is rejected', TRUE);
    END;

    -- An unknown processing_status.
    BEGIN
        INSERT INTO salesops.raw_orders_staging (batch_id, source_payload, processing_status)
        VALUES (gen_random_uuid(), '{}'::JSONB, 'banana');
        PERFORM pg_temp.check('reject', 'invalid processing_status is rejected', FALSE, 'insert succeeded');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('reject', 'invalid processing_status is rejected', TRUE);
    END;
END;
$$;


-- =============================================================================
-- 8. The views compile and compute correctly
--
-- Inserts a second EMEA order on the same day, one with FX and one without, so
-- the completeness flag has something real to report.
-- =============================================================================
DO $$
DECLARE
    v_region_id  SMALLINT;
    v_product_id SMALLINT;
    v_channel_id SMALLINT;
    v_daily      RECORD;
    v_regional   RECORD;
BEGIN
    SELECT region_id  INTO v_region_id  FROM salesops.dim_region  WHERE region_code = 'EMEA';
    SELECT product_id INTO v_product_id FROM salesops.dim_product WHERE product_sku = 'SKU-2210';
    SELECT channel_id INTO v_channel_id FROM salesops.dim_channel WHERE channel_code = 'partner';

    -- Same day as TEST-ORD-0001, no FX rate attached.
    INSERT INTO salesops.fact_orders (
        order_id, order_date, customer_id, region_id, product_id, channel_id,
        quantity, unit_price, currency, refund_amount_local)
    VALUES ('TEST-ORD-0002', DATE '2025-03-17', 'CUST-EMEA-9001',
            v_region_id, v_product_id, v_channel_id, 2, 50.0000, 'EUR', 0);

    SELECT * INTO v_daily FROM salesops.daily_sales_base WHERE order_date = DATE '2025-03-17';

    PERFORM pg_temp.check('views', 'daily_sales_base returns a row', v_daily.order_date IS NOT NULL);
    PERFORM pg_temp.check('views', 'daily_sales_base counts both orders',
        v_daily.orders_count = 2, format('%s', v_daily.orders_count));
    PERFORM pg_temp.check('views', 'daily_sales_base sums units (3 + 2 = 5)',
        v_daily.units_sold = 5, format('%s', v_daily.units_sold));
    PERFORM pg_temp.check('views', 'daily_sales_base joins calendar attributes',
        v_daily.day_name = 'Monday' AND v_daily.is_weekend = FALSE,
        COALESCE(v_daily.day_name, 'null'));

    -- Only TEST-ORD-0001 has a rate, so revenue reflects that row alone and the
    -- completeness flag reports the other.
    PERFORM pg_temp.check('views', 'gross_revenue_usd counts only rows with FX (482.76)',
        v_daily.gross_revenue_usd = 482.76, format('%s', v_daily.gross_revenue_usd));
    PERFORM pg_temp.check('views', 'orders_pending_fx flags the incomplete row',
        v_daily.orders_pending_fx = 1, format('%s', v_daily.orders_pending_fx));

    -- AOV divides by ALL orders, so an incomplete day understates it. That is
    -- exactly why orders_pending_fx has to be read alongside it.
    PERFORM pg_temp.check('views', 'average_order_value_usd = 482.76 / 2 = 241.38',
        v_daily.average_order_value_usd = 241.38, format('%s', v_daily.average_order_value_usd));

    -- refund_rate = 52.92 / 482.76 = 0.1096
    PERFORM pg_temp.check('views', 'refund_rate = 0.1096',
        v_daily.refund_rate = 0.1096, format('%s', v_daily.refund_rate));

    SELECT * INTO v_regional FROM salesops.regional_sales_base
    WHERE order_date = DATE '2025-03-17' AND region_code = 'EMEA';

    PERFORM pg_temp.check('views', 'regional_sales_base returns the EMEA row',
        v_regional.region_code = 'EMEA');
    PERFORM pg_temp.check('views', 'regional_sales_base carries region_name',
        v_regional.region_name = 'Europe, Middle East & Africa', COALESCE(v_regional.region_name, 'null'));
    PERFORM pg_temp.check('views', 'regional totals match the daily totals for a single-region day',
        v_regional.orders_count = v_daily.orders_count
        AND v_regional.gross_revenue_usd IS NOT DISTINCT FROM v_daily.gross_revenue_usd);
    PERFORM pg_temp.check('views', 'regional_sales_base counts distinct customers',
        v_regional.customers_count = 1, format('%s', v_regional.customers_count));
END;
$$;


-- =============================================================================
-- 9. The decision layer (V007, V008)
--
-- Structural only. The behaviour of the decision rules is Stage 6's own suite
-- (n8n/tests/test_stage6_decisions.py); what belongs here is the shape of the
-- schema those rules depend on - and, above all, that the layer offers a model
-- nowhere to write.
-- =============================================================================
DO $$
DECLARE
    expected TEXT;
    found    BOOLEAN;
    n        INTEGER;
    detail   TEXT;
BEGIN
    FOREACH expected IN ARRAY ARRAY[
        'anomaly_decisions', 'anomaly_decision_reasons',
        'decision_thresholds', 'decision_reason_codes'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'salesops' AND table_name = expected
              AND table_type = 'BASE TABLE'
        ) INTO found;
        PERFORM pg_temp.check('decision-layer', format('table %s exists', expected), found);
    END LOOP;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.views
        WHERE table_schema = 'salesops' AND table_name = 'anomaly_decision_audit'
    ) INTO found;
    PERFORM pg_temp.check('decision-layer', 'view anomaly_decision_audit exists', found);

    -- V007: the Stage 5 extension Stage 6 measures impact from.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'anomaly_daily'
      AND column_name = 'revenue_baseline_median' AND data_type = 'numeric';
    PERFORM pg_temp.check('decision-layer',
        'anomaly_daily.revenue_baseline_median exists and is NUMERIC', n = 1);

    -- Money in the decision layer obeys the same rule as everywhere else.
    SELECT count(*), string_agg(format('%s.%s (%s)', table_name, column_name, data_type), ', ')
      INTO n, detail
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND table_name IN ('anomaly_decisions', 'decision_thresholds')
      AND (column_name ~ '(revenue|value)')
      AND data_type IN ('double precision', 'real');
    PERFORM pg_temp.check('decision-layer', 'no decision money column uses floating point',
                          n = 0, COALESCE(detail, 'none'));

    -- The engine and its guards.
    FOREACH expected IN ARRAY ARRAY[
        'decide_anomalies', 'decision_threshold', 'guard_decision_thresholds'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM pg_proc p
            JOIN pg_namespace ns ON ns.oid = p.pronamespace
            WHERE ns.nspname = 'salesops' AND p.proname = expected
        ) INTO found;
        PERFORM pg_temp.check('decision-layer', format('function %s() exists', expected), found);
    END LOOP;

    -- Section 21: Stage 6 must work with the LLM completely absent, which starts
    -- with there being no column for one to write to. Scanned rather than listed,
    -- so a column added later is covered without editing this test.
    SELECT count(*), string_agg(format('%s.%s', table_name, column_name), ', ')
      INTO n, detail
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND table_name IN ('anomaly_daily', 'anomaly_decisions', 'anomaly_decision_reasons')
      AND column_name ~ '(llm|prompt|embedding|completion|explanation|narrative|hypothesis)';
    PERFORM pg_temp.check('decision-layer',
        'no statistical or decision table has a column for model output',
        n = 0, COALESCE(detail, 'none'));

    -- Thresholds are data, not constants compiled into a function body.
    SELECT count(*) INTO n
    FROM salesops.decision_thresholds WHERE decision_version = 'stage6-v1';
    PERFORM pg_temp.check('decision-layer', 'stage6-v1 thresholds are seeded as reference data',
                          n = 9, format('%s threshold(s)', n));

    SELECT count(*) INTO n FROM salesops.decision_reason_codes;
    PERFORM pg_temp.check('decision-layer', 'the reason-code vocabulary is seeded',
                          n >= 12, format('%s code(s)', n));

    -- Both migrations registered themselves.
    SELECT count(*) INTO n
    FROM salesops.schema_migrations WHERE version IN ('V007', 'V008');
    PERFORM pg_temp.check('decision-layer', 'V007 and V008 are registered', n = 2,
                          format('%s of 2', n));
END;
$$;


-- =============================================================================
-- 10. The hypothesis layer (V009)
--
-- Structural only; Stage 7's behaviour is tested in analytics-service/tests.
-- What belongs here is the shape of the one table in this warehouse whose
-- contents come from a language model - and the guarantee that the model's
-- reach stops at explanation.
-- =============================================================================
DO $$
DECLARE
    found  BOOLEAN;
    n      INTEGER;
    detail TEXT;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'salesops' AND table_name = 'anomaly_hypotheses'
          AND table_type = 'BASE TABLE'
    ) INTO found;
    PERFORM pg_temp.check('hypothesis-layer', 'table anomaly_hypotheses exists', found);

    SELECT EXISTS (
        SELECT 1 FROM information_schema.views
        WHERE table_schema = 'salesops' AND table_name = 'anomaly_hypothesis_audit'
    ) INTO found;
    PERFORM pg_temp.check('hypothesis-layer', 'view anomaly_hypothesis_audit exists', found);

    -- The guard that makes "the model cannot change severity" a property of the
    -- database rather than a promise made by the caller.
    SELECT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_guard_hypothesis_snapshot' AND NOT tgisinternal
    ) INTO found;
    PERFORM pg_temp.check('hypothesis-layer', 'the Stage 6 snapshot guard trigger is installed',
                          found);

    -- Structured output, not stored prose. The four list columns must be JSONB.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'anomaly_hypotheses'
      AND column_name IN ('supporting_evidence', 'alternative_hypotheses',
                          'missing_evidence', 'recommended_checks')
      AND data_type = 'jsonb';
    PERFORM pg_temp.check('hypothesis-layer', 'the four evidence lists are JSONB, not prose',
                          n = 4, format('%s of 4', n));

    -- Provenance is mandatory: an unattributable hypothesis is not auditable.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'anomaly_hypotheses'
      AND column_name IN ('model_provider', 'model_name', 'prompt_version',
                          'evidence_digest', 'generated_at')
      AND is_nullable = 'NO';
    PERFORM pg_temp.check('hypothesis-layer', 'model, prompt version and digest are all NOT NULL',
                          n = 5, format('%s of 5', n));

    -- Nowhere for a credential to be stored, by accident or otherwise.
    -- 'token' on its own is deliberately not matched: prompt_tokens and
    -- completion_tokens are usage COUNTS, and a pattern that flags them trains
    -- the next reader to loosen this check rather than trust it.
    SELECT count(*), string_agg(format('%s.%s', table_name, column_name), ', ')
      INTO n, detail
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND column_name ~* '(api_key|apikey|secret|access_token|auth_token|bearer|credential|password)';
    PERFORM pg_temp.check('hypothesis-layer', 'no table anywhere has a column for a secret',
                          n = 0, COALESCE(detail, 'none'));

    -- Stage 6 remains the only writer of its own verdict: the hypothesis table
    -- carries a snapshot, and the enum columns are constrained to match.
    SELECT count(*) INTO n
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE t.relname = 'anomaly_hypotheses'
      AND c.conname IN ('anomaly_hypotheses_severity_valid',
                        'anomaly_hypotheses_routing_valid',
                        'anomaly_hypotheses_decision_valid',
                        'anomaly_hypotheses_confidence_valid');
    PERFORM pg_temp.check('hypothesis-layer', 'severity, routing, decision and confidence are closed vocabularies',
                          n = 4, format('%s of 4', n));

    -- One official analysis per generation identity.
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'anomaly_hypotheses'
          AND c.conname = 'anomaly_hypotheses_unique_generation'
          AND c.contype = 'u'
    ) INTO found;
    PERFORM pg_temp.check('hypothesis-layer',
                          'one hypothesis per (anomaly, decision version, prompt version, model)',
                          found);

    SELECT count(*) INTO n
    FROM salesops.schema_migrations WHERE version = 'V009';
    PERFORM pg_temp.check('hypothesis-layer', 'V009 is registered', n = 1);
END;
$$;

-- The guard has to actually refuse. Asserting the trigger exists is not the
-- same as asserting it works, and this is the constraint the whole stage rests on.
DO $$
DECLARE
    v_decision RECORD;
BEGIN
    SELECT decision_id, anomaly_id, calendar_date, decision_version, severity, routing, decision
    INTO v_decision
    FROM salesops.anomaly_decisions
    WHERE decision = 'action_required'
    ORDER BY decision_id
    LIMIT 1;

    IF NOT FOUND THEN
        -- Unreachable: the fixture seeds an actionable decision when the
        -- warehouse has none. A FAIL here means the seeding broke, and that is
        -- worth knowing loudly - the alternative is 32 checks quietly not running.
        PERFORM pg_temp.check('hypothesis-layer', 'snapshot guard rejects a restated verdict',
                              FALSE, 'no actionable decision - the fixture failed to seed one');
        RETURN;
    END IF;

    BEGIN
        INSERT INTO salesops.anomaly_hypotheses (
            anomaly_id, decision_id, calendar_date, decision_version,
            -- The lie: a severity that disagrees with the decision referenced.
            --
            -- Derived from the decision rather than hardcoded. A fixed 'minor'
            -- here is only a lie when the decision picked above is not itself
            -- minor, and decisions are seeded in date order, so whether this
            -- check tested anything depended on which severity happened to land
            -- on the earliest actionable date. When it did not, the guard
            -- correctly accepted a truthful row and the check read as a failure.
            severity, routing, decision,
            summary, confidence, primary_hypothesis,
            supporting_evidence, alternative_hypotheses, missing_evidence, recommended_checks,
            model_provider, model_name, prompt_version, evidence_digest
        ) VALUES (
            v_decision.anomaly_id, v_decision.decision_id, v_decision.calendar_date,
            v_decision.decision_version,
            CASE WHEN v_decision.severity = 'minor' THEN 'critical' ELSE 'minor' END,
            CASE WHEN v_decision.routing = 'auto_notify' THEN 'human_review' ELSE 'auto_notify' END,
            v_decision.decision,
            'schema test', 'low', 'schema test',
            '[{"metric":"x"}]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
            'schema-test', 'schema-test-model', 'stage7-prompt-v0', 'digest'
        );
        PERFORM pg_temp.check('hypothesis-layer', 'snapshot guard rejects a restated verdict',
                              FALSE, 'the insert was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('hypothesis-layer', 'snapshot guard rejects a restated verdict',
                              SQLERRM LIKE '%may not restate%', SQLERRM);
    END;

    -- ...and a truthful snapshot must still be accepted, or the guard would be
    -- rejecting everything and passing the test above for the wrong reason.
    BEGIN
        INSERT INTO salesops.anomaly_hypotheses (
            anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision,
            summary, confidence, primary_hypothesis,
            supporting_evidence, alternative_hypotheses, missing_evidence, recommended_checks,
            model_provider, model_name, prompt_version, evidence_digest
        ) VALUES (
            v_decision.anomaly_id, v_decision.decision_id, v_decision.calendar_date,
            v_decision.decision_version,
            v_decision.severity, v_decision.routing, v_decision.decision,
            'schema test', 'low', 'schema test',
            '[{"metric":"x"}]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
            'schema-test', 'schema-test-model', 'stage7-prompt-v0', 'digest'
        );
        PERFORM pg_temp.check('hypothesis-layer', 'snapshot guard accepts a truthful verdict', TRUE);
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('hypothesis-layer', 'snapshot guard accepts a truthful verdict',
                              FALSE, SQLERRM);
    END;

    -- An empty explanation is not an explanation.
    BEGIN
        INSERT INTO salesops.anomaly_hypotheses (
            anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision,
            summary, confidence, primary_hypothesis,
            supporting_evidence, alternative_hypotheses, missing_evidence, recommended_checks,
            model_provider, model_name, prompt_version, evidence_digest
        ) VALUES (
            v_decision.anomaly_id, v_decision.decision_id, v_decision.calendar_date,
            v_decision.decision_version,
            v_decision.severity, v_decision.routing, v_decision.decision,
            'schema test', 'low', '   ',
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
            'schema-test', 'schema-test-model', 'stage7-prompt-v1', 'digest'
        );
        PERFORM pg_temp.check('hypothesis-layer', 'an empty hypothesis is rejected',
                              FALSE, 'the insert was ACCEPTED');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('hypothesis-layer', 'an empty hypothesis is rejected', TRUE);
    END;
END;
$$;


-- =============================================================================
-- 11. The delivery layer (V010)
--
-- Structural only; Stage 8's behaviour lives in analytics-service/tests. What
-- belongs here is the shape of the tables and the two guarantees that make the
-- layer trustworthy: eligibility is a constraint, and the review state machine
-- is not a text column with opinions.
-- =============================================================================
DO $$
DECLARE
    expected TEXT;
    found    BOOLEAN;
    n        INTEGER;
BEGIN
    FOREACH expected IN ARRAY ARRAY[
        'notifications', 'notification_attempts', 'review_queue', 'review_events'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'salesops' AND table_name = expected
              AND table_type = 'BASE TABLE'
        ) INTO found;
        PERFORM pg_temp.check('delivery-layer', format('table %s exists', expected), found);
    END LOOP;

    FOREACH expected IN ARRAY ARRAY['notification_audit', 'review_queue_audit'] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'salesops' AND table_name = expected
        ) INTO found;
        PERFORM pg_temp.check('delivery-layer', format('view %s exists', expected), found);
    END LOOP;

    FOREACH expected IN ARRAY ARRAY[
        'guard_stage8_snapshot', 'guard_review_transition', 'record_review_created'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
            WHERE ns.nspname = 'salesops' AND p.proname = expected
        ) INTO found;
        PERFORM pg_temp.check('delivery-layer', format('function %s() exists', expected), found);
    END LOOP;

    -- Eligibility as a constraint rather than a convention: these are what make
    -- "a notification only exists for auto_notify" a property of the database.
    SELECT count(*) INTO n
    FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.conname IN ('notifications_only_for_eligible_decisions',
                        'review_queue_only_for_eligible_decisions');
    PERFORM pg_temp.check('delivery-layer', 'eligibility is enforced by CHECK on both tables',
                          n = 2, format('%s of 2', n));

    -- Idempotency keys.
    SELECT count(*) INTO n
    FROM pg_constraint c
    WHERE c.contype = 'u'
      AND c.conname IN ('notifications_unique_delivery', 'review_queue_unique_item');
    PERFORM pg_temp.check('delivery-layer', 'both idempotency keys are unique constraints',
                          n = 2, format('%s of 2', n));

    -- No secret has anywhere to live, in a table or a view.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND table_name IN ('notifications', 'notification_attempts', 'review_queue',
                         'review_events', 'notification_audit', 'review_queue_audit')
      AND column_name ~* '(api_key|apikey|secret|access_token|auth_token|bearer|credential|password|webhook_url)';
    PERFORM pg_temp.check('delivery-layer', 'no delivery table or view has a column for a secret',
                          n = 0);

    -- The audit view must not re-expose the payload it was built to summarise.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'notification_audit'
      AND column_name = 'payload';
    PERFORM pg_temp.check('delivery-layer', 'the notification audit view omits the payload',
                          n = 0);

    SELECT count(*) INTO n FROM salesops.schema_migrations WHERE version = 'V010';
    PERFORM pg_temp.check('delivery-layer', 'V010 is registered', n = 1);
END;
$$;

-- The guards have to actually refuse. Asserting a trigger exists is not the
-- same as asserting it works.
DO $$
DECLARE
    v_review RECORD;
    v_notify RECORD;
BEGIN
    -- A decision Stage 6 routed to a HUMAN must not accept a notification.
    SELECT decision_id, anomaly_id, calendar_date, decision_version,
           severity, routing, decision, notification_allowed, human_review_required
    INTO v_review
    FROM salesops.anomaly_decisions WHERE routing = 'human_review' LIMIT 1;

    IF NOT FOUND THEN
        PERFORM pg_temp.check('delivery-layer', 'eligibility CHECK refuses an ineligible notification',
                              TRUE, 'skipped: no human_review decision in this database');
    ELSE
        BEGIN
            INSERT INTO salesops.notifications (
                anomaly_id, decision_id, calendar_date, decision_version,
                severity, routing, decision, notification_allowed, human_review_required,
                channel, recipient, subject, payload)
            VALUES (v_review.anomaly_id, v_review.decision_id, v_review.calendar_date,
                    v_review.decision_version, v_review.severity, v_review.routing,
                    v_review.decision, v_review.notification_allowed,
                    v_review.human_review_required,
                    'webhook', 'schema-test@example.invalid', 'schema test', '{}'::jsonb);
            PERFORM pg_temp.check('delivery-layer', 'eligibility CHECK refuses an ineligible notification',
                                  FALSE, 'the insert was ACCEPTED');
        EXCEPTION WHEN check_violation THEN
            PERFORM pg_temp.check('delivery-layer', 'eligibility CHECK refuses an ineligible notification',
                                  TRUE);
        END;

        -- ...and a fabricated snapshot claiming otherwise is refused too.
        BEGIN
            INSERT INTO salesops.notifications (
                anomaly_id, decision_id, calendar_date, decision_version,
                severity, routing, decision, notification_allowed, human_review_required,
                channel, recipient, subject, payload)
            VALUES (v_review.anomaly_id, v_review.decision_id, v_review.calendar_date,
                    v_review.decision_version, 'minor', 'auto_notify', 'action_required',
                    TRUE, FALSE,
                    'webhook', 'schema-test@example.invalid', 'schema test', '{}'::jsonb);
            PERFORM pg_temp.check('delivery-layer', 'snapshot guard refuses a restated verdict',
                                  FALSE, 'the insert was ACCEPTED');
        EXCEPTION WHEN OTHERS THEN
            PERFORM pg_temp.check('delivery-layer', 'snapshot guard refuses a restated verdict',
                                  SQLERRM LIKE '%may not restate%', SQLERRM);
        END;
    END IF;

    -- A truthful review item must be accepted, or the refusals above would pass
    -- for the wrong reason.
    --
    -- The queue row is cleared first rather than looking for an unqueued
    -- decision. Once Stage 8 has run, every human_review decision IS queued, so
    -- searching for a free one made these two checks silently skip in exactly
    -- the state the system normally lives in - which is the state they most
    -- need to run in. The whole suite rolls back, so the deletion is temporary.
    SELECT decision_id, anomaly_id, calendar_date, decision_version,
           severity, routing, decision, notification_allowed, human_review_required
    INTO v_notify
    FROM salesops.anomaly_decisions d
    WHERE d.routing = 'human_review'
    LIMIT 1;

    IF NOT FOUND THEN
        PERFORM pg_temp.check('delivery-layer', 'the review state machine refuses an invalid jump',
                              FALSE, 'no human_review decision - the fixture failed to seed one');
        RETURN;
    END IF;

    DELETE FROM salesops.review_queue
    WHERE anomaly_id = v_notify.anomaly_id
      AND decision_version = v_notify.decision_version;

    INSERT INTO salesops.review_queue (
        anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required)
    VALUES (v_notify.anomaly_id, v_notify.decision_id, v_notify.calendar_date,
            v_notify.decision_version, v_notify.severity, v_notify.routing,
            v_notify.decision, v_notify.notification_allowed, v_notify.human_review_required);

    PERFORM pg_temp.check('delivery-layer', 'a truthful review item is accepted', TRUE);

    -- pending -> resolved is not a transition anyone may make.
    BEGIN
        UPDATE salesops.review_queue
        SET status = 'resolved', resolution = 'confirmed'
        WHERE anomaly_id = v_notify.anomaly_id
          AND decision_version = v_notify.decision_version;
        PERFORM pg_temp.check('delivery-layer', 'the review state machine refuses an invalid jump',
                              FALSE, 'pending -> resolved was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('delivery-layer', 'the review state machine refuses an invalid jump',
                              SQLERRM LIKE '%Invalid review transition%', SQLERRM);
    END;

    -- ...but the legal one is allowed, and records itself.
    UPDATE salesops.review_queue SET status = 'in_review', assigned_to = 'schema-test'
    WHERE anomaly_id = v_notify.anomaly_id AND decision_version = v_notify.decision_version;

    PERFORM pg_temp.check(
        'delivery-layer', 'a valid transition is accepted and recorded',
        EXISTS (SELECT 1 FROM salesops.review_events e
                JOIN salesops.review_queue q ON q.review_id = e.review_id
                WHERE q.anomaly_id = v_notify.anomaly_id
                  AND e.from_status = 'pending' AND e.to_status = 'in_review'));
END;
$$;


-- =============================================================================
-- 12. The remediation layer (V011)
--
-- Structural, plus the three guarantees that make Stage 9 trustworthy:
-- authorisation is a trigger, eligibility is a foreign key, and an executed
-- action has no way back. Stage 9's orchestration lives in
-- analytics-service/tests; what belongs here is what the database refuses.
-- =============================================================================
DO $$
DECLARE
    expected TEXT;
    found    BOOLEAN;
    n        INTEGER;
BEGIN
    FOREACH expected IN ARRAY ARRAY[
        'remediation_action_types', 'remediation_action_eligibility',
        'remediation_actions', 'remediation_attempts', 'remediation_events'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'salesops' AND table_name = expected
              AND table_type = 'BASE TABLE'
        ) INTO found;
        PERFORM pg_temp.check('remediation-layer', format('table %s exists', expected), found);
    END LOOP;

    FOREACH expected IN ARRAY ARRAY[
        'remediation_audit', 'remediation_pending_execution'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'salesops' AND table_name = expected
        ) INTO found;
        PERFORM pg_temp.check('remediation-layer', format('view %s exists', expected), found);
    END LOOP;

    -- Eligibility is a foreign key into reference data, not a CASE expression.
    -- If this constraint disappears, an ineligible action becomes possible and
    -- nothing else in the schema would notice.
    PERFORM pg_temp.check(
        'remediation-layer', 'severity/action eligibility is a foreign key',
        EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'remediation_actions_eligible_fk' AND contype = 'f'));

    PERFORM pg_temp.check(
        'remediation-layer', 'the idempotency key is unique',
        EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'remediation_actions_idempotent' AND contype = 'u'));

    -- Generated, so it cannot be supplied by a caller and cannot drift from the
    -- identity it claims to describe.
    PERFORM pg_temp.check(
        'remediation-layer', 'the idempotency key is generated, not supplied',
        EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'salesops' AND table_name = 'remediation_actions'
                  AND column_name = 'idempotency_key' AND is_generated = 'ALWAYS'));

    -- NO foreign key to any earlier stage. One that blocked would stop a Stage 6
    -- re-decision; one that cascaded would erase an action a human authorised.
    SELECT count(*) INTO n
    FROM pg_constraint c
    JOIN pg_class child  ON child.oid  = c.conrelid
    JOIN pg_class parent ON parent.oid = c.confrelid
    WHERE c.contype = 'f'
      AND child.relname = 'remediation_actions'
      AND parent.relname IN ('anomaly_daily', 'anomaly_decisions',
                             'anomaly_hypotheses', 'review_queue');
    PERFORM pg_temp.check(
        'remediation-layer', 'no foreign key can block a Stage 6 re-decision',
        n = 0, format('%s such constraint(s) found', n));

    -- Every action in the vocabulary is a request for human work.
    SELECT count(*) INTO n
    FROM salesops.remediation_action_types WHERE mutates_external_state;
    PERFORM pg_temp.check(
        'remediation-layer', 'no action type mutates an external system',
        n = 0, format('%s mutating action type(s)', n));

    -- Eligibility only ever mentions the two severities Stage 6 routes to a
    -- human. A 'minor' row here would be a side door around human review.
    SELECT count(*) INTO n
    FROM salesops.remediation_action_eligibility
    WHERE severity NOT IN ('major', 'critical');
    PERFORM pg_temp.check(
        'remediation-layer', 'eligibility never bypasses human review',
        n = 0, format('%s row(s) for a non-review severity', n));

    -- The review approval state, added by V011 without disturbing the four
    -- Stage 8 states.
    PERFORM pg_temp.check(
        'remediation-layer', 'the review queue gained an approval state',
        (SELECT pg_get_constraintdef(oid) FROM pg_constraint
          WHERE conname = 'review_queue_status_valid') LIKE '%approved%');

    FOREACH expected IN ARRAY ARRAY['pending', 'in_review', 'resolved', 'dismissed'] LOOP
        PERFORM pg_temp.check(
            'remediation-layer',
            format('Stage 8 review state %s still permitted', expected),
            (SELECT pg_get_constraintdef(oid) FROM pg_constraint
              WHERE conname = 'review_queue_status_valid') LIKE '%' || expected || '%');
    END LOOP;
END;
$$;


-- -----------------------------------------------------------------------------
-- Behaviour: what the database refuses.
--
-- Built on a live human_review decision, because a synthetic one could not
-- exercise the guard triggers - they read the real Stage 6 row.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    v_critical RECORD;
    v_major    RECORD;
    v_review   BIGINT;
    v_major_review BIGINT;
    v_action   BIGINT;
BEGIN
    SELECT d.* INTO v_critical
    FROM salesops.anomaly_decisions d
    WHERE d.routing = 'human_review' AND d.severity = 'critical'
    ORDER BY d.calendar_date LIMIT 1;

    SELECT d.* INTO v_major
    FROM salesops.anomaly_decisions d
    WHERE d.routing = 'human_review' AND d.severity = 'major'
    ORDER BY d.calendar_date LIMIT 1;

    IF v_critical.decision_id IS NULL OR v_major.decision_id IS NULL THEN
        PERFORM pg_temp.check('remediation-layer', 'remediation behaviour checks',
                              FALSE, 'no critical/major decision - the fixture failed to seed one');
        RETURN;
    END IF;

    -- The suite rolls back, so these deletions never persist. They are needed
    -- because Stage 9 may already have run against this database, and a check
    -- that silently finds nothing to test is worse than no check at all.
    DELETE FROM salesops.remediation_actions
    WHERE review_id IN (SELECT review_id FROM salesops.review_queue
                        WHERE anomaly_id IN (v_critical.anomaly_id, v_major.anomaly_id));
    DELETE FROM salesops.review_queue
    WHERE anomaly_id IN (v_critical.anomaly_id, v_major.anomaly_id);

    INSERT INTO salesops.review_queue (
        anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required)
    VALUES (v_critical.anomaly_id, v_critical.decision_id, v_critical.calendar_date,
            v_critical.decision_version, v_critical.severity, v_critical.routing,
            v_critical.decision, v_critical.notification_allowed,
            v_critical.human_review_required)
    RETURNING review_id INTO v_review;

    -- ---- an unapproved review authorises nothing ---------------------------
    BEGIN
        INSERT INTO salesops.remediation_actions (
            review_id, anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision, notification_allowed, human_review_required,
            decision_reason_code, review_approved_by, review_approved_at,
            review_resolution, action_type, request_payload)
        VALUES (v_review, v_critical.anomaly_id, v_critical.decision_id,
                v_critical.calendar_date, v_critical.decision_version,
                v_critical.severity, v_critical.routing, v_critical.decision,
                v_critical.notification_allowed, v_critical.human_review_required,
                v_critical.decision_reason_code, 'schema-test', now(),
                'confirmed', 'create_investigation', '{}'::jsonb);
        PERFORM pg_temp.check('remediation-layer',
            'a pending review cannot authorise remediation', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'a pending review cannot authorise remediation',
            SQLERRM LIKE '%approved review%', SQLERRM);
    END;

    -- ---- pending -> approved is not a transition anyone may make -----------
    BEGIN
        UPDATE salesops.review_queue
        SET status = 'approved', resolution = 'confirmed', approved_by = 'schema-test'
        WHERE review_id = v_review;
        PERFORM pg_temp.check('remediation-layer',
            'a review cannot jump straight to approved', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'a review cannot jump straight to approved',
            SQLERRM LIKE '%Invalid review transition%', SQLERRM);
    END;

    -- ---- an approval needs a confirming resolution -------------------------
    -- Claimed WITHOUT an assignee on purpose: the trigger falls back to
    -- assigned_to when no approver is named, so leaving one set would make the
    -- "no identifiable actor" check below pass for the wrong reason.
    UPDATE salesops.review_queue SET status = 'in_review' WHERE review_id = v_review;

    BEGIN
        UPDATE salesops.review_queue
        SET status = 'approved', resolution = 'false_positive', approved_by = 'schema-test'
        WHERE review_id = v_review;
        PERFORM pg_temp.check('remediation-layer',
            'a false positive cannot authorise remediation', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'a false positive cannot authorise remediation',
            SQLERRM LIKE '%review_queue_approval_needs_confirmation%', SQLERRM);
    END;

    -- ---- ...and an identifiable actor --------------------------------------
    BEGIN
        UPDATE salesops.review_queue
        SET status = 'approved', resolution = 'confirmed'
        WHERE review_id = v_review;
        PERFORM pg_temp.check('remediation-layer',
            'an approval requires an identifiable actor', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'an approval requires an identifiable actor', TRUE, SQLERRM);
    END;

    -- ---- the legal approval, which records itself --------------------------
    UPDATE salesops.review_queue
    SET status = 'approved', resolution = 'confirmed', approved_by = 'schema-test'
    WHERE review_id = v_review;

    PERFORM pg_temp.check(
        'remediation-layer', 'in_review -> approved is accepted and recorded',
        EXISTS (SELECT 1 FROM salesops.review_events
                 WHERE review_id = v_review
                   AND from_status = 'in_review' AND to_status = 'approved'));

    -- ---- an ineligible action is a foreign key violation -------------------
    -- The review is critical, so refund review IS permitted here; the major
    -- review below is where the refusal is exercised.
    INSERT INTO salesops.remediation_actions (
        review_id, anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required,
        decision_reason_code, review_approved_by, review_approved_at,
        review_resolution, action_type, request_payload)
    SELECT v_review, r.anomaly_id, r.decision_id, r.calendar_date, r.decision_version,
           r.severity, r.routing, r.decision, r.notification_allowed,
           r.human_review_required, v_critical.decision_reason_code,
           r.approved_by, r.approved_at, r.resolution,
           'request_refund_review', '{}'::jsonb
    FROM salesops.review_queue r WHERE r.review_id = v_review
    RETURNING remediation_id INTO v_action;

    PERFORM pg_temp.check('remediation-layer',
        'an approved review can authorise an eligible action', v_action IS NOT NULL);

    PERFORM pg_temp.check('remediation-layer',
        'a new action starts unexecuted',
        (SELECT status FROM salesops.remediation_actions WHERE remediation_id = v_action)
        = 'proposed');

    PERFORM pg_temp.check('remediation-layer',
        'creating an action records an opening event',
        EXISTS (SELECT 1 FROM salesops.remediation_events
                 WHERE remediation_id = v_action AND from_status IS NULL
                   AND to_status = 'proposed'));

    -- ---- a duplicate is refused by the idempotency key ---------------------
    BEGIN
        INSERT INTO salesops.remediation_actions (
            review_id, anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision, notification_allowed, human_review_required,
            decision_reason_code, review_approved_by, review_approved_at,
            review_resolution, action_type, request_payload)
        SELECT v_review, r.anomaly_id, r.decision_id, r.calendar_date, r.decision_version,
               r.severity, r.routing, r.decision, r.notification_allowed,
               r.human_review_required, v_critical.decision_reason_code,
               r.approved_by, r.approved_at, r.resolution,
               'request_refund_review', '{}'::jsonb
        FROM salesops.review_queue r WHERE r.review_id = v_review;
        PERFORM pg_temp.check('remediation-layer',
            'a duplicate action is refused', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN unique_violation THEN
        PERFORM pg_temp.check('remediation-layer', 'a duplicate action is refused', TRUE);
    END;

    -- ---- proposed cannot execute -------------------------------------------
    BEGIN
        UPDATE salesops.remediation_actions SET status = 'executing'
        WHERE remediation_id = v_action;
        PERFORM pg_temp.check('remediation-layer',
            'an unauthorised action cannot execute', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer', 'an unauthorised action cannot execute',
            SQLERRM LIKE '%Invalid remediation transition%', SQLERRM);
    END;

    -- ---- authorisation needs an actor --------------------------------------
    BEGIN
        UPDATE salesops.remediation_actions SET status = 'approved'
        WHERE remediation_id = v_action;
        PERFORM pg_temp.check('remediation-layer',
            'authorisation requires an identifiable actor', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'authorisation requires an identifiable actor', TRUE, SQLERRM);
    END;

    -- ---- the full authorised path ------------------------------------------
    UPDATE salesops.remediation_actions
    SET status = 'approved', authorized_by = 'schema-test'
    WHERE remediation_id = v_action;

    UPDATE salesops.remediation_actions SET status = 'executing'
    WHERE remediation_id = v_action;

    UPDATE salesops.remediation_actions
    SET status = 'executed', executed_by = 'schema-test', attempt_count = 1
    WHERE remediation_id = v_action;

    PERFORM pg_temp.check('remediation-layer',
        'execution stamps its own timestamp',
        (SELECT executed_at IS NOT NULL FROM salesops.remediation_actions
          WHERE remediation_id = v_action));

    PERFORM pg_temp.check('remediation-layer',
        'every transition is audited',
        (SELECT count(*) FROM salesops.remediation_events
          WHERE remediation_id = v_action) = 4);

    -- ---- executed is the end of the line -----------------------------------
    BEGIN
        UPDATE salesops.remediation_actions SET status = 'executing'
        WHERE remediation_id = v_action;
        PERFORM pg_temp.check('remediation-layer',
            'an executed action cannot execute again', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer', 'an executed action cannot execute again',
            SQLERRM LIKE '%Invalid remediation transition%', SQLERRM);
    END;

    -- ---- the authorisation snapshot is immutable ---------------------------
    BEGIN
        UPDATE salesops.remediation_actions SET severity = 'major'
        WHERE remediation_id = v_action;
        PERFORM pg_temp.check('remediation-layer',
            'the authorisation snapshot cannot be rewritten', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'the authorisation snapshot cannot be rewritten',
            SQLERRM LIKE '%immutable%', SQLERRM);
    END;

    -- ---- eligibility, exercised on a major review --------------------------
    INSERT INTO salesops.review_queue (
        anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required)
    VALUES (v_major.anomaly_id, v_major.decision_id, v_major.calendar_date,
            v_major.decision_version, v_major.severity, v_major.routing,
            v_major.decision, v_major.notification_allowed,
            v_major.human_review_required)
    RETURNING review_id INTO v_major_review;

    UPDATE salesops.review_queue SET status = 'in_review', assigned_to = 'schema-test'
    WHERE review_id = v_major_review;
    UPDATE salesops.review_queue
    SET status = 'approved', resolution = 'confirmed', approved_by = 'schema-test'
    WHERE review_id = v_major_review;

    BEGIN
        INSERT INTO salesops.remediation_actions (
            review_id, anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision, notification_allowed, human_review_required,
            decision_reason_code, review_approved_by, review_approved_at,
            review_resolution, action_type, request_payload)
        SELECT v_major_review, r.anomaly_id, r.decision_id, r.calendar_date,
               r.decision_version, r.severity, r.routing, r.decision,
               r.notification_allowed, r.human_review_required,
               v_major.decision_reason_code, r.approved_by, r.approved_at, r.resolution,
               'request_refund_review', '{}'::jsonb
        FROM salesops.review_queue r WHERE r.review_id = v_major_review;
        PERFORM pg_temp.check('remediation-layer',
            'refund review is refused for a major anomaly', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN foreign_key_violation THEN
        PERFORM pg_temp.check('remediation-layer',
            'refund review is refused for a major anomaly', TRUE);
    END;

    -- ---- a fabricated snapshot is refused ----------------------------------
    -- The one attack the eligibility FK alone would not stop: claim a severity
    -- the review does not carry, so an ineligible action looks eligible.
    BEGIN
        INSERT INTO salesops.remediation_actions (
            review_id, anomaly_id, decision_id, calendar_date, decision_version,
            severity, routing, decision, notification_allowed, human_review_required,
            decision_reason_code, review_approved_by, review_approved_at,
            review_resolution, action_type, request_payload)
        SELECT v_major_review, r.anomaly_id, r.decision_id, r.calendar_date,
               r.decision_version, 'critical', r.routing, r.decision,
               r.notification_allowed, r.human_review_required,
               v_major.decision_reason_code, r.approved_by, r.approved_at, r.resolution,
               'request_refund_review', '{}'::jsonb
        FROM salesops.review_queue r WHERE r.review_id = v_major_review;
        PERFORM pg_temp.check('remediation-layer',
            'a fabricated severity snapshot is refused', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('remediation-layer',
            'a fabricated severity snapshot is refused',
            SQLERRM LIKE '%does not match review%', SQLERRM);
    END;

    PERFORM pg_temp.check(
        'remediation-layer', 'the audit view reports no external side effect',
        (SELECT NOT had_external_side_effect FROM salesops.remediation_audit
          WHERE remediation_id = v_action));
END;
$$;


-- =============================================================================
-- 13. The operational reliability layer (V012)
--
-- Structural, plus the guarantees that make unattended recovery safe: the audit
-- log cannot be rewritten, retention cannot reach a pending or failed row, and
-- a crashed execution has no path back to 'executing' except through a human.
-- Stage 10's orchestration lives in analytics-service/tests.
-- =============================================================================
DO $$
DECLARE
    expected TEXT;
    found    BOOLEAN;
    n        INTEGER;
BEGIN
    FOREACH expected IN ARRAY ARRAY[
        'operational_config', 'operational_events', 'ingestion_replays'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'salesops' AND table_name = expected
              AND table_type = 'BASE TABLE'
        ) INTO found;
        PERFORM pg_temp.check('operational-layer', format('table %s exists', expected), found);
    END LOOP;

    FOREACH expected IN ARRAY ARRAY[
        'operational_health', 'operational_health_summary', 'operational_retry_queue',
        'review_ageing', 'stale_notifications', 'staging_retention_report',
        'ingestion_replay_candidates'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'salesops' AND table_name = expected
        ) INTO found;
        PERFORM pg_temp.check('operational-layer', format('view %s exists', expected), found);
    END LOOP;

    FOREACH expected IN ARRAY ARRAY[
        'operational_setting', 'recover_stale_runs', 'recover_stale_remediation',
        'reconcile_remediation', 'stage_replay_batch', 'load_staged_batch',
        'replay_failed_batch', 'purge_staging'
    ] LOOP
        SELECT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
            WHERE ns.nspname = 'salesops' AND p.proname = expected
        ) INTO found;
        PERFORM pg_temp.check('operational-layer', format('function %s exists', expected), found);
    END LOOP;

    -- Every threshold this stage compares against is a row, not a constant.
    SELECT count(*) INTO n FROM salesops.operational_config;
    PERFORM pg_temp.check('operational-layer', 'operational thresholds are data',
                          n >= 10, format('%s setting(s)', n));

    -- The remediation vocabulary gained execution_unknown without losing anything.
    PERFORM pg_temp.check(
        'operational-layer', 'remediation gained an execution_unknown state',
        (SELECT pg_get_constraintdef(oid) FROM pg_constraint
          WHERE conname = 'remediation_actions_status_valid') LIKE '%execution_unknown%');

    FOREACH expected IN ARRAY ARRAY[
        'proposed', 'approved', 'executing', 'executed', 'rejected', 'failed', 'cancelled'
    ] LOOP
        PERFORM pg_temp.check(
            'operational-layer',
            format('Stage 9 remediation state %s still permitted', expected),
            (SELECT pg_get_constraintdef(oid) FROM pg_constraint
              WHERE conname = 'remediation_actions_status_valid') LIKE '%' || expected || '%');
    END LOOP;

    PERFORM pg_temp.check(
        'operational-layer', 'an attempt may record an unknown outcome',
        (SELECT pg_get_constraintdef(oid) FROM pg_constraint
          WHERE conname = 'remediation_attempts_outcome_valid') LIKE '%unknown%');

    -- The replay mapping cannot point at itself, and cannot record two replays
    -- of the same row at the same attempt number.
    PERFORM pg_temp.check(
        'operational-layer', 'a replay cannot be its own origin',
        EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'ingestion_replays_not_self' AND contype = 'c'));
    PERFORM pg_temp.check(
        'operational-layer', 'replay attempts are unique per original row',
        EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'ingestion_replays_unique_attempt' AND contype = 'u'));

    -- Indexes the operational queries actually need.
    PERFORM pg_temp.check(
        'operational-layer', 'runs are indexed by source and recency',
        EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'salesops'
                 AND indexname = 'idx_ingestion_runs_source_recent'));
    PERFORM pg_temp.check(
        'operational-layer', 'operational events are indexed by entity',
        EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'salesops'
                 AND indexname = 'idx_operational_events_entity'));
END;
$$;


-- -----------------------------------------------------------------------------
-- Behaviour: what recovery refuses to do.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    v_event  BIGINT;
    v_run    BIGINT;
    v_before INTEGER;
    v_after  INTEGER;
    v_id     BIGINT;
BEGIN
    -- ---- the audit log is append-only ---------------------------------------
    INSERT INTO salesops.operational_events
        (event_type, entity_type, entity_id, actor, reason_code)
    VALUES ('maintenance_run', 'maintenance', 'schema-test', 'schema-test', 'SELF_TEST')
    RETURNING event_id INTO v_event;

    BEGIN
        UPDATE salesops.operational_events SET reason_code = 'REWRITTEN'
        WHERE event_id = v_event;
        PERFORM pg_temp.check('operational-layer', 'an operational event cannot be updated',
                              FALSE, 'the UPDATE was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer', 'an operational event cannot be updated',
                              SQLERRM LIKE '%append-only%', SQLERRM);
    END;

    BEGIN
        DELETE FROM salesops.operational_events WHERE event_id = v_event;
        PERFORM pg_temp.check('operational-layer', 'an operational event cannot be deleted',
                              FALSE, 'the DELETE was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer', 'an operational event cannot be deleted',
                              SQLERRM LIKE '%append-only%', SQLERRM);
    END;

    -- ---- an unknown threshold raises rather than defaulting ------------------
    BEGIN
        PERFORM salesops.operational_setting('definitely_not_a_setting');
        PERFORM pg_temp.check('operational-layer',
            'an unknown threshold raises rather than defaulting', FALSE, 'it returned a value');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer',
            'an unknown threshold raises rather than defaulting',
            SQLERRM LIKE '%No operational setting%', SQLERRM);
    END;

    -- ---- stale run recovery --------------------------------------------------
    INSERT INTO salesops.ingestion_runs
        (batch_id, source, window_from, window_to, status, started_at)
    VALUES (gen_random_uuid(), 'schema-test-stale', CURRENT_DATE, CURRENT_DATE,
            'running', now() - INTERVAL '500 minutes')
    RETURNING run_id INTO v_run;

    PERFORM salesops.recover_stale_runs('schema-test', FALSE);

    PERFORM pg_temp.check(
        'operational-layer', 'a stale run is closed with a machine-readable reason',
        (SELECT status = 'failed' AND error_message LIKE 'STALE_RUN_TIMEOUT:%'
           FROM salesops.ingestion_runs WHERE run_id = v_run));

    PERFORM pg_temp.check(
        'operational-layer', 'stale recovery writes exactly one event',
        (SELECT count(*) FROM salesops.operational_events
          WHERE entity_type = 'ingestion_run' AND entity_id = v_run::text) = 1);

    -- ...and a second pass finds nothing to do.
    PERFORM salesops.recover_stale_runs('schema-test', FALSE);
    PERFORM pg_temp.check(
        'operational-layer', 'stale recovery is idempotent',
        (SELECT count(*) FROM salesops.operational_events
          WHERE entity_type = 'ingestion_run' AND entity_id = v_run::text) = 1);

    -- ---- a fresh run survives ------------------------------------------------
    INSERT INTO salesops.ingestion_runs
        (batch_id, source, window_from, window_to, status, started_at)
    VALUES (gen_random_uuid(), 'schema-test-fresh', CURRENT_DATE, CURRENT_DATE,
            'running', now())
    RETURNING run_id INTO v_id;

    PERFORM salesops.recover_stale_runs('schema-test', FALSE);
    PERFORM pg_temp.check(
        'operational-layer', 'a fresh run is left alone',
        (SELECT status FROM salesops.ingestion_runs WHERE run_id = v_id) = 'running');

    -- ---- retention protects unfinished work and the dead-letter trail --------
    INSERT INTO salesops.raw_orders_staging
        (batch_id, order_id, source_payload, processing_status, received_at, error_message)
    VALUES
        (gen_random_uuid(), 'SCHEMA-TEST-OLD-PENDING', '{}'::jsonb, 'pending',
         now() - INTERVAL '500 days', NULL),
        (gen_random_uuid(), 'SCHEMA-TEST-OLD-FAILED', '{}'::jsonb, 'failed',
         now() - INTERVAL '500 days', 'schema test'),
        (gen_random_uuid(), 'SCHEMA-TEST-OLD-DONE', '{}'::jsonb, 'processed',
         now() - INTERVAL '500 days', NULL);

    PERFORM salesops.purge_staging(FALSE, 'schema-test');

    PERFORM pg_temp.check(
        'operational-layer', 'retention never deletes a pending row',
        EXISTS (SELECT 1 FROM salesops.raw_orders_staging
                 WHERE order_id = 'SCHEMA-TEST-OLD-PENDING'));
    PERFORM pg_temp.check(
        'operational-layer', 'retention never deletes a failed row',
        EXISTS (SELECT 1 FROM salesops.raw_orders_staging
                 WHERE order_id = 'SCHEMA-TEST-OLD-FAILED'));
    PERFORM pg_temp.check(
        'operational-layer', 'retention deletes an old settled row',
        NOT EXISTS (SELECT 1 FROM salesops.raw_orders_staging
                     WHERE order_id = 'SCHEMA-TEST-OLD-DONE'));

    -- ...and a second sweep has nothing left to do.
    SELECT rows_deleted INTO v_after FROM salesops.purge_staging(FALSE, 'schema-test');
    PERFORM pg_temp.check('operational-layer', 'retention is idempotent', v_after = 0,
                          format('second sweep deleted %s row(s)', v_after));

    -- ---- a dry run deletes nothing -------------------------------------------
    INSERT INTO salesops.raw_orders_staging
        (batch_id, order_id, source_payload, processing_status, received_at)
    VALUES (gen_random_uuid(), 'SCHEMA-TEST-DRY', '{}'::jsonb, 'processed',
            now() - INTERVAL '500 days');

    PERFORM salesops.purge_staging(TRUE, 'schema-test');
    PERFORM pg_temp.check(
        'operational-layer', 'a retention dry run deletes nothing',
        EXISTS (SELECT 1 FROM salesops.raw_orders_staging WHERE order_id = 'SCHEMA-TEST-DRY'));
END;
$$;


-- -----------------------------------------------------------------------------
-- Behaviour: the execution_unknown state machine.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    v_action BIGINT;
    v_review BIGINT;
    v_dec    RECORD;
BEGIN
    SELECT d.* INTO v_dec
    FROM salesops.anomaly_decisions d
    WHERE d.routing = 'human_review' AND d.severity = 'critical'
    ORDER BY d.calendar_date LIMIT 1;

    IF v_dec.decision_id IS NULL THEN
        PERFORM pg_temp.check('operational-layer', 'execution_unknown behaviour',
                              FALSE, 'no critical decision - the fixture failed to seed one');
        RETURN;
    END IF;

    DELETE FROM salesops.remediation_actions
    WHERE review_id IN (SELECT review_id FROM salesops.review_queue
                        WHERE anomaly_id = v_dec.anomaly_id);
    DELETE FROM salesops.review_queue WHERE anomaly_id = v_dec.anomaly_id;

    INSERT INTO salesops.review_queue (
        anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required)
    VALUES (v_dec.anomaly_id, v_dec.decision_id, v_dec.calendar_date,
            v_dec.decision_version, v_dec.severity, v_dec.routing, v_dec.decision,
            v_dec.notification_allowed, v_dec.human_review_required)
    RETURNING review_id INTO v_review;

    UPDATE salesops.review_queue SET status = 'in_review' WHERE review_id = v_review;
    UPDATE salesops.review_queue
    SET status = 'approved', resolution = 'confirmed', approved_by = 'schema-test'
    WHERE review_id = v_review;

    INSERT INTO salesops.remediation_actions (
        review_id, anomaly_id, decision_id, calendar_date, decision_version,
        severity, routing, decision, notification_allowed, human_review_required,
        decision_reason_code, review_approved_by, review_approved_at,
        review_resolution, action_type, request_payload)
    SELECT v_review, r.anomaly_id, r.decision_id, r.calendar_date, r.decision_version,
           r.severity, r.routing, r.decision, r.notification_allowed,
           r.human_review_required, v_dec.decision_reason_code,
           r.approved_by, r.approved_at, r.resolution,
           'create_investigation', '{}'::jsonb
    FROM salesops.review_queue r WHERE r.review_id = v_review
    RETURNING remediation_id INTO v_action;

    UPDATE salesops.remediation_actions
    SET status = 'approved', authorized_by = 'schema-test' WHERE remediation_id = v_action;
    UPDATE salesops.remediation_actions
    SET status = 'executing' WHERE remediation_id = v_action;

    -- A fresh execution is not stale.
    PERFORM salesops.recover_stale_remediation('schema-test', FALSE);
    PERFORM pg_temp.check(
        'operational-layer', 'a fresh execution is not recovered',
        (SELECT status FROM salesops.remediation_actions
          WHERE remediation_id = v_action) = 'executing');

    -- With the timeout at zero it is.
    UPDATE salesops.operational_config SET config_value = 0
    WHERE config_key = 'stale_remediation_timeout_minutes';

    PERFORM salesops.recover_stale_remediation('schema-test', FALSE);

    PERFORM pg_temp.check(
        'operational-layer', 'a stale execution becomes execution_unknown',
        (SELECT status FROM salesops.remediation_actions
          WHERE remediation_id = v_action) = 'execution_unknown');

    PERFORM pg_temp.check(
        'operational-layer', 'the unknown attempt is recorded as unknown',
        EXISTS (SELECT 1 FROM salesops.remediation_attempts
                 WHERE remediation_id = v_action AND outcome = 'unknown'
                   AND NOT external_side_effect));

    PERFORM pg_temp.check(
        'operational-layer', 'an unknown execution leaves the work set',
        NOT EXISTS (SELECT 1 FROM salesops.remediation_pending_execution
                     WHERE remediation_id = v_action));

    -- The point of the whole design: no path back to executing.
    BEGIN
        UPDATE salesops.remediation_actions SET status = 'executing'
        WHERE remediation_id = v_action;
        PERFORM pg_temp.check('operational-layer',
            'an unknown execution cannot re-execute without reconciliation',
            FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer',
            'an unknown execution cannot re-execute without reconciliation',
            SQLERRM LIKE '%Invalid remediation transition%', SQLERRM);
    END;

    -- Reconciliation requires evidence...
    BEGIN
        PERFORM salesops.reconcile_remediation(v_action, 'confirmed_executed', 'schema-test', '');
        PERFORM pg_temp.check('operational-layer',
            'reconciliation requires a statement of evidence', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer',
            'reconciliation requires a statement of evidence', TRUE, SQLERRM);
    END;

    -- ...and only accepts the two defined outcomes.
    BEGIN
        PERFORM salesops.reconcile_remediation(v_action, 'probably_fine', 'schema-test', 'hunch');
        PERFORM pg_temp.check('operational-layer',
            'reconciliation refuses an undefined outcome', FALSE, 'it was ACCEPTED');
    EXCEPTION WHEN OTHERS THEN
        PERFORM pg_temp.check('operational-layer',
            'reconciliation refuses an undefined outcome', TRUE, SQLERRM);
    END;

    PERFORM salesops.reconcile_remediation(
        v_action, 'confirmed_not_executed', 'schema-test', 'no provider record');

    PERFORM pg_temp.check(
        'operational-layer', 'reconciling as not executed returns it to the retry path',
        (SELECT status = 'failed' AND last_error LIKE 'RECONCILED_NOT_EXECUTED:%'
           FROM salesops.remediation_actions WHERE remediation_id = v_action));

    PERFORM pg_temp.check(
        'operational-layer', 'reconciliation is audited',
        EXISTS (SELECT 1 FROM salesops.operational_events
                 WHERE entity_type = 'remediation_action' AND entity_id = v_action::text
                   AND reason_code = 'RECONCILED_NOT_EXECUTED'));
END;
$$;


-- -----------------------------------------------------------------------------
-- Behaviour: health is deterministic and explains itself.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n
    FROM salesops.operational_health
    WHERE status NOT IN ('healthy', 'warning', 'degraded', 'failed');
    PERFORM pg_temp.check('operational-layer', 'health statuses are a closed vocabulary',
                          n = 0, format('%s row(s) with an unexpected status', n));

    SELECT count(*) INTO n
    FROM salesops.operational_health
    WHERE reason_code IS NULL OR reason_code !~ '^[A-Z][A-Z0-9_]*$';
    PERFORM pg_temp.check('operational-layer', 'every component has a machine-readable reason',
                          n = 0, format('%s row(s) without one', n));

    SELECT count(*) INTO n
    FROM salesops.operational_health WHERE status = 'healthy' AND reason_code <> 'OK';
    PERFORM pg_temp.check('operational-layer', 'a healthy component reports OK', n = 0);

    -- The health view must never read what Stage 7 said. Reading whether Stage 7
    -- RAN is fine; reading its output would let a model influence whether the
    -- pipeline reports itself healthy.
    PERFORM pg_temp.check(
        'operational-layer', 'health reads no model output',
        pg_get_viewdef('salesops.operational_health'::regclass, TRUE)
            NOT LIKE '%anomaly_hypotheses%');

    -- Ageing labels and anomaly severities are deliberately different words, so
    -- that neither can be mistaken for the other in a query.
    SELECT count(*) INTO n
    FROM salesops.review_ageing
    WHERE ageing_bucket IN ('none', 'minor', 'major', 'critical');
    PERFORM pg_temp.check('operational-layer',
        'ageing labels cannot be confused with anomaly severity', n = 0);

    SELECT count(*) INTO n
    FROM salesops.review_ageing WHERE review_status NOT IN ('pending', 'in_review');
    PERFORM pg_temp.check('operational-layer', 'ageing only describes open reviews', n = 0);

    -- The retry queue answers the same question whatever failed.
    SELECT count(*) INTO n
    FROM salesops.operational_retry_queue
    WHERE entity_type NOT IN ('ingestion_run', 'notification', 'remediation_action',
                              'staging_batch')
       OR disposition IS NULL;
    PERFORM pg_temp.check('operational-layer', 'the retry queue is uniformly shaped', n = 0);

    -- An unknown execution is never retry-eligible: it needs a person.
    SELECT count(*) INTO n
    FROM salesops.operational_retry_queue
    WHERE disposition = 'AWAITING_RECONCILIATION' AND retry_eligible;
    PERFORM pg_temp.check('operational-layer',
        'an unknown execution is never automatically retryable', n = 0);
END;
$$;


-- =============================================================================
-- 14. Presentation layer (Stage 11)
--
-- Stage 11 adds no behaviour, so almost nothing here checks what it does. These
-- check what it must not do: mix a language model's output with an audited
-- number, borrow one stage's vocabulary for another's, recompute a figure some
-- other stage already owns, or hand a reporting login a write path.
-- =============================================================================
DO $$
DECLARE
    n         INTEGER;
    txt       TEXT;
    ok        BOOLEAN;
    v         TEXT;
BEGIN
    -- --- the layer vocabulary --------------------------------------------
    SELECT count(*) INTO n FROM salesops.presentation_layers;
    PERFORM pg_temp.check('presentation', 'the layer vocabulary is seeded', n = 8,
                          format('%s row(s)', n));

    SELECT count(*) INTO n FROM salesops.presentation_layers
    WHERE is_model_generated;
    PERFORM pg_temp.check('presentation',
        'exactly one layer is model-generated', n = 1, format('%s', n));

    SELECT count(*) INTO n FROM salesops.presentation_layers
    WHERE layer_key = 'model_hypothesis' AND is_model_generated;
    PERFORM pg_temp.check('presentation',
        'and it is the hypothesis layer', n = 1);

    -- The ranks are the reading order. A gap or a duplicate would make the
    -- drill-down render in an order nobody chose.
    SELECT count(*) INTO n FROM (
        SELECT layer_rank FROM salesops.presentation_layers
        EXCEPT SELECT generate_series(1, 8)
    ) s;
    PERFORM pg_temp.check('presentation',
        'layer ranks are 1..8 with no gaps', n = 0);

    -- A row claiming to be measured evidence while flagged as model output is
    -- rejected by CHECK rather than by review.
    BEGIN
        INSERT INTO salesops.presentation_layers
            (layer_key, layer_rank, layer_label, evidence_kind,
             is_model_generated, produced_by_stage, source_relations, description)
        VALUES ('x', 99, 'x', 'measured', TRUE, 'x', ARRAY['x'], 'x');
        PERFORM pg_temp.check('presentation',
            'a measured layer cannot claim to be model-generated', FALSE,
            'the insert was accepted');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('presentation',
            'a measured layer cannot claim to be model-generated', TRUE);
    END;

    BEGIN
        INSERT INTO salesops.presentation_layers
            (layer_key, layer_rank, layer_label, evidence_kind,
             is_model_generated, produced_by_stage, source_relations, description)
        VALUES ('y', 98, 'y', 'model_generated', FALSE, 'y', ARRAY['y'], 'y');
        PERFORM pg_temp.check('presentation',
            'nor can model output hide behind is_model_generated = false', FALSE,
            'the insert was accepted');
    EXCEPTION WHEN check_violation THEN
        PERFORM pg_temp.check('presentation',
            'nor can model output hide behind is_model_generated = false', TRUE);
    END;

    -- --- model output stays where it was put ------------------------------
    -- No executive view may expose a column the model wrote. Checked against
    -- the catalogue rather than by reading SQL, so an alias is caught too.
    SELECT count(*), string_agg(table_name || '.' || column_name, ', ')
      INTO n, txt
    FROM information_schema.columns
    WHERE table_schema = 'salesops'
      AND left(table_name, 5) = 'exec_'
      AND column_name IN ('llm_summary', 'llm_primary_hypothesis',
                          'llm_supporting_evidence', 'llm_alternative_hypotheses',
                          'llm_recommended_checks', 'llm_missing_evidence',
                          'summary', 'primary_hypothesis');
    PERFORM pg_temp.check('presentation',
        'no executive view exposes model prose', n = 0, COALESCE(txt, 'none'));

    -- The investigation view may - but every one of them is llm_-prefixed, and
    -- the prefix is what a renderer keys off.
    SELECT count(*) INTO n
    FROM information_schema.columns
    WHERE table_schema = 'salesops' AND table_name = 'anomaly_investigation'
      AND column_name IN ('llm_summary', 'llm_primary_hypothesis',
                          'llm_confidence', 'llm_model_name', 'llm_verified',
                          'llm_missing_evidence');
    PERFORM pg_temp.check('presentation',
        'the investigation view carries the hypothesis under an llm_ prefix', n = 6,
        format('%s of 6', n));

    -- Nothing in this system verifies a hypothesis, and the column says so on
    -- every row rather than in a footnote.
    SELECT count(*) INTO n FROM salesops.anomaly_investigation
    WHERE llm_verified IS DISTINCT FROM FALSE;
    PERFORM pg_temp.check('presentation',
        'no hypothesis is ever reported as verified', n = 0);

    -- Every line of the drill-down carries its own layer flag, so a renderer
    -- cannot lose the distinction between a measurement and a guess.
    SELECT count(*) INTO n FROM salesops.anomaly_investigation_detail
    WHERE is_model_generated <> (layer_key = 'model_hypothesis');
    PERFORM pg_temp.check('presentation',
        'every drill-down line is flagged correctly', n = 0);

    -- The health view must not become describable by a language model.
    PERFORM pg_temp.check('presentation', 'health reads no model output',
        pg_get_viewdef('salesops.exec_pipeline_health'::regclass, TRUE)
            NOT LIKE '%anomaly_hypotheses%');

    -- --- vocabularies stay separate ---------------------------------------
    SELECT count(*) INTO n FROM salesops.exec_pipeline_health
    WHERE health_status IN ('none', 'minor', 'major', 'critical');
    PERFORM pg_temp.check('presentation',
        'pipeline health never borrows an anomaly severity', n = 0);

    SELECT count(*) INTO n FROM salesops.exec_pipeline_health
    WHERE health_status NOT IN ('healthy', 'warning', 'degraded', 'failed');
    PERFORM pg_temp.check('presentation',
        'pipeline health keeps Stage 10''s closed vocabulary', n = 0);

    -- Ageing belongs to reviews. A failed run does not have one, and inventing
    -- one would put a review vocabulary on a pipeline object.
    SELECT count(*) INTO n FROM salesops.ops_attention_items
    WHERE ageing_bucket IS NOT NULL AND entity_type <> 'review';
    PERFORM pg_temp.check('presentation',
        'only reviews carry an ageing bucket', n = 0);

    SELECT count(*) INTO n FROM salesops.ops_attention_items
    WHERE ageing_bucket IN ('none', 'minor', 'major', 'critical');
    PERFORM pg_temp.check('presentation',
        'ageing buckets are still not severities', n = 0);

    -- --- nothing is recomputed --------------------------------------------
    -- The baseline comparison shown on the dashboard must equal the deviation
    -- Stage 5 stored. Any drift means a second definition of the same number.
    SELECT count(*) INTO n
    FROM salesops.exec_kpi_daily e
    JOIN salesops.anomaly_daily a ON a.calendar_date = e.calendar_date
    WHERE e.revenue_vs_baseline_pct IS NOT NULL
      AND a.revenue_deviation_pct IS NOT NULL
      AND abs(e.revenue_vs_baseline_pct - a.revenue_deviation_pct) > 0.02;
    PERFORM pg_temp.check('presentation',
        'the dashboard reproduces Stage 5''s own deviation', n = 0,
        format('%s day(s) disagree', n));

    -- 'Actionable' is Stage 6's stored decision, not a severity filter applied
    -- by the presentation layer.
    SELECT count(*) INTO n FROM salesops.exec_actionable_anomalies
    WHERE decision <> 'action_required';
    PERFORM pg_temp.check('presentation',
        'actionable means Stage 6 said action_required', n = 0);

    -- count(DISTINCT anomaly_id): a date can carry several decision versions,
    -- and the view deliberately shows the newest one only. Comparing against
    -- count(*) would fail the moment anything is re-decided.
    SELECT (SELECT count(*) FROM salesops.exec_actionable_anomalies) =
           (SELECT count(DISTINCT anomaly_id) FROM salesops.anomaly_decisions
             WHERE decision = 'action_required')
      INTO ok;
    PERFORM pg_temp.check('presentation',
        'and the view drops none of them', ok);

    -- One row per anomaly, whatever the version history says.
    SELECT count(*) INTO n FROM (
        SELECT anomaly_id FROM salesops.exec_actionable_anomalies
        GROUP BY anomaly_id HAVING count(*) > 1
    ) s;
    PERFORM pg_temp.check('presentation',
        'no anomaly is listed twice', n = 0);

    SELECT count(*) INTO n FROM (
        SELECT calendar_date FROM salesops.anomaly_investigation
        GROUP BY calendar_date HAVING count(*) > 1
    ) s;
    PERFORM pg_temp.check('presentation',
        'the investigation view is one row per date', n = 0);

    -- Severity counts must reconcile with the decision table exactly.
    SELECT count(*) INTO n
    FROM salesops.exec_anomaly_severity_summary s
    WHERE s.anomaly_count <> (
        SELECT count(*) FROM salesops.anomaly_decisions d WHERE d.severity = s.severity
    );
    PERFORM pg_temp.check('presentation',
        'severity counts reconcile with anomaly_decisions', n = 0);

    -- Every severity is present even at zero, so an empty bar is
    -- distinguishable from a missing one.
    SELECT count(*) INTO n FROM salesops.exec_anomaly_severity_summary;
    PERFORM pg_temp.check('presentation',
        'every severity appears in the summary', n = 4, format('%s', n));

    -- --- the incident chain -----------------------------------------------
    -- The chain's LENGTH is a property of the view, so it is asserted from the
    -- view definition rather than from rows: on an empty warehouse there are no
    -- incidents, and a count of steps over zero incidents is zero. Asserting
    -- ten here would fail on a fresh clone, which is exactly where this suite's
    -- verdict matters most.
    --
    -- That the ten steps are the RIGHT ten, in the right order, and that
    -- 2026-08-05 travels all of them, is asserted in
    -- analytics-service/tests/test_presentation_views.py - which has a fixture
    -- that builds the chain through the real endpoints rather than assuming it.
    SELECT count(*) INTO n FROM (
        SELECT DISTINCT step_rank FROM salesops.incident_timeline
    ) s;
    PERFORM pg_temp.check('presentation',
        'the incident chain never yields a partial set of steps',
        n IN (0, 10), format('%s distinct step(s)', n));

    -- A step that did not happen must say why. Missing data and a deliberate
    -- stop look identical otherwise.
    SELECT count(*) INTO n FROM salesops.incident_timeline
    WHERE NOT reached AND COALESCE(summary, '') = '';
    PERFORM pg_temp.check('presentation',
        'a step that did not happen still explains itself', n = 0);

    -- Every step's layer resolves. An unknown key renders with no label.
    SELECT count(*) INTO n FROM salesops.incident_timeline
    WHERE layer_label IS NULL;
    PERFORM pg_temp.check('presentation',
        'every chain step resolves to a known layer', n = 0);

    -- --- auditability ------------------------------------------------------
    -- Which streams the union DEFINES is a property of the view; which of them
    -- currently has rows is a property of the data. Only the first belongs in a
    -- schema suite.
    PERFORM pg_temp.check('presentation',
        'the audit view unions all six streams',
        (SELECT count(*) FROM regexp_matches(
            pg_get_viewdef('salesops.audit_event_stream'::regclass, TRUE),
            '''(decision|hypothesis|notification|review|remediation|operational)''::text',
            'g')) >= 6);

    SELECT count(DISTINCT stream) INTO n FROM salesops.audit_event_stream;
    PERFORM pg_temp.check('presentation',
        'every stream that has rows is one of the six', n <= 6, format('%s', n));

    SELECT count(*) INTO n FROM salesops.audit_event_stream
    WHERE actor IS NULL OR actor = '' OR occurred_at IS NULL;
    PERFORM pg_temp.check('presentation',
        'every audit row has an actor and a time', n = 0);

    -- The union must be complete: an audit trail that quietly dropped rows
    -- would make the system look tidier than it is.
    SELECT count(*) INTO n FROM salesops.audit_event_stream WHERE stream = 'review';
    PERFORM pg_temp.check('presentation', 'the review stream loses nothing',
        n = (SELECT count(*) FROM salesops.review_events));

    SELECT count(*) INTO n FROM salesops.audit_event_stream WHERE stream = 'remediation';
    PERFORM pg_temp.check('presentation', 'the remediation stream loses nothing',
        n = (SELECT count(*) FROM salesops.remediation_events));

    SELECT count(*) INTO n FROM salesops.audit_event_stream WHERE stream = 'operational';
    PERFORM pg_temp.check('presentation', 'the operational stream loses nothing',
        n = (SELECT count(*) FROM salesops.operational_events));

    SELECT count(*) INTO n FROM salesops.audit_event_stream WHERE stream = 'notification';
    PERFORM pg_temp.check('presentation', 'the notification stream loses nothing',
        n = (SELECT count(*) FROM salesops.notification_attempts));

    -- Stage 6 and Stage 7 are machines. Naming a person as the actor of a
    -- threshold comparison would be a fabricated attribution.
    SELECT count(*) INTO n FROM salesops.audit_event_stream
    WHERE stream IN ('decision', 'hypothesis') AND actor LIKE '%@%';
    PERFORM pg_temp.check('presentation',
        'machine events are not attributed to a person', n = 0);

    -- A recovery happens under no policy version, and the column says nothing
    -- rather than borrowing a value from elsewhere.
    SELECT count(*) INTO n FROM salesops.audit_event_stream
    WHERE stream = 'operational' AND version_info IS NOT NULL;
    PERFORM pg_temp.check('presentation',
        'operational events claim no version they do not have', n = 0);

    -- --- the reporting role ------------------------------------------------
    PERFORM pg_temp.check('presentation', 'the reporting role exists',
        EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salesops_readonly'));

    SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls INTO ok
    FROM pg_roles WHERE rolname = 'salesops_readonly';
    PERFORM pg_temp.check('presentation',
        'the reporting role holds no elevated attribute', NOT ok);

    SELECT count(*), string_agg(DISTINCT privilege_type, ', ') INTO n, txt
    FROM information_schema.table_privileges
    WHERE grantee = 'salesops_readonly' AND privilege_type <> 'SELECT';
    PERFORM pg_temp.check('presentation',
        'the reporting role holds SELECT and nothing else', n = 0,
        COALESCE(txt, 'none'));

    SELECT count(*) INTO n
    FROM information_schema.table_privileges
    WHERE grantee = 'salesops_readonly' AND privilege_type = 'SELECT';
    PERFORM pg_temp.check('presentation',
        'the reporting role can actually read', n > 0, format('%s relation(s)', n));

    -- The important one. PostgreSQL grants EXECUTE to PUBLIC by default, and a
    -- VOLATILE function is a write reachable from a SELECT box.
    SELECT count(*), string_agg(p.proname, ', ') INTO n, txt
    FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
    WHERE ns.nspname = 'salesops' AND p.provolatile = 'v'
      AND has_function_privilege('salesops_readonly', p.oid, 'EXECUTE');
    PERFORM pg_temp.check('presentation',
        'the reporting role cannot execute anything that writes', n = 0,
        COALESCE(txt, 'none'));

    -- ...but the four configuration readers must stay reachable, or the health
    -- views fail for the reporting role as a blank panel rather than an error.
    SELECT count(*) INTO n
    FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
    WHERE ns.nspname = 'salesops' AND p.provolatile <> 'v'
      AND p.prorettype <> 'trigger'::regtype
      AND NOT has_function_privilege('salesops_readonly', p.oid, 'EXECUTE');
    PERFORM pg_temp.check('presentation',
        'and can still read a configured threshold', n = 0);

    SELECT count(*) INTO n
    FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
    WHERE ns.nspname = 'salesops'
      AND has_function_privilege('public', p.oid, 'EXECUTE');
    PERFORM pg_temp.check('presentation',
        'PUBLIC executes nothing in this schema', n = 0, format('%s', n));

    -- --- nothing upstream moved -------------------------------------------
    -- The severities of 2026-08-05 and 2026-08-09 are pipeline OUTPUT, not
    -- schema, and a fresh clone has neither. They are asserted in
    -- analytics-service/tests/test_presentation_views.py, by date, against the
    -- live warehouse. What belongs here is the rule they are an instance of:
    -- a decision's severity and its routing can never disagree.
    SELECT count(*) INTO n FROM salesops.anomaly_decisions
    WHERE (severity IN ('critical', 'major')) <> (routing = 'human_review');
    PERFORM pg_temp.check('presentation',
        'no decision escalates without routing to a human', n = 0);

    -- Stage 11 is a read layer. It must not have registered a migration that
    -- touched the fact tables.
    FOREACH v IN ARRAY ARRAY['exec_kpi_daily', 'anomaly_investigation',
                             'incident_timeline', 'audit_event_stream',
                             'ops_pipeline_runs'] LOOP
        SELECT count(*) INTO n FROM information_schema.views
        WHERE table_schema = 'salesops' AND table_name = v;
        PERFORM pg_temp.check('presentation',
            format('%s is a view, not a materialised copy', v), n = 1);
    END LOOP;
END;
$$;


-- =============================================================================
-- Report
-- =============================================================================
\echo ''
\echo '================= SCHEMA VALIDATION RESULTS ================='

SELECT
    CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END AS result,
    section,
    name,
    NULLIF(detail, '') AS detail
FROM test_results
ORDER BY id;

SELECT
    count(*)                             AS total,
    count(*) FILTER (WHERE passed)       AS passed,
    count(*) FILTER (WHERE NOT passed)   AS failed
FROM test_results;

DO $$
DECLARE
    failed INTEGER;
    names  TEXT;
BEGIN
    SELECT count(*), string_agg(name, '; ')
      INTO failed, names
    FROM test_results WHERE NOT passed;

    IF failed > 0 THEN
        RAISE EXCEPTION 'SCHEMA VALIDATION FAILED: % check(s) failed -> %', failed, names;
    END IF;

    RAISE NOTICE 'All schema validation checks passed.';
END;
$$;

-- Nothing this suite inserted is kept.
ROLLBACK;
