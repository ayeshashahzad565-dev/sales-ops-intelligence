-- =============================================================================
-- V002  Reference data for the conformed dimensions
--
-- Separated from V001 so structural review is not buried in INSERTs, and so
-- reference data can be corrected and re-applied without touching DDL.
--
-- Every statement is idempotent:
--   * the labelled dimensions UPSERT on their natural key, so a renamed product
--     keeps its surrogate id and every existing fact row stays correct - which
--     is the entire reason those dimensions have surrogate keys;
--   * dim_date does nothing on conflict, since a calendar day cannot change.
--
-- Deliberately NOT seeded here: exchange_rates (Stage 3 loads real rates from
-- Frankfurter), dim_customer (a late-arriving dimension, discovered from the
-- order stream), and fact_orders (arrives only through the pipeline).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- dim_region
-- Codes match the `region` field emitted by the Mock Sales/Orders API.
-- -----------------------------------------------------------------------------
INSERT INTO salesops.dim_region (region_code, region_name) VALUES
    ('NA',    'North America'),
    ('EMEA',  'Europe, Middle East & Africa'),
    ('APAC',  'Asia Pacific'),
    ('LATAM', 'Latin America')
ON CONFLICT (region_code) DO UPDATE
    SET region_name = EXCLUDED.region_name;


-- -----------------------------------------------------------------------------
-- dim_product
-- SKUs match the `product` field emitted by the API. `category` is a grouping
-- the source does not provide - it exists in the warehouse, not upstream, which
-- is a normal and useful thing for a dimension to add.
-- -----------------------------------------------------------------------------
INSERT INTO salesops.dim_product (product_sku, product_name, category) VALUES
    ('SKU-1042', 'Atlas Core Licence',     'Software'),
    ('SKU-2210', 'Atlas Field Kit',        'Hardware'),
    ('SKU-3375', 'Atlas Enterprise Suite', 'Software'),
    ('SKU-4180', 'Atlas Analytics Add-on', 'Software'),
    ('SKU-5031', 'Atlas Sensor Pack',      'Hardware'),
    ('SKU-6604', 'Atlas Support Plan',     'Services')
ON CONFLICT (product_sku) DO UPDATE
    SET product_name = EXCLUDED.product_name,
        category     = EXCLUDED.category;


-- -----------------------------------------------------------------------------
-- dim_channel
-- Codes match the `channel` field emitted by the API; names are presentation
-- labels for dashboards.
-- -----------------------------------------------------------------------------
INSERT INTO salesops.dim_channel (channel_code, channel_name) VALUES
    ('web',         'Web'),
    ('mobile',      'Mobile App'),
    ('partner',     'Partner / Reseller'),
    ('field_sales', 'Field Sales')
ON CONFLICT (channel_code) DO UPDATE
    SET channel_name = EXCLUDED.channel_name;


-- -----------------------------------------------------------------------------
-- dim_date  (2025-01-01 .. 2027-12-31)
--
-- Wider than the current dataset on purpose. The Mock API anchors its 90-day
-- history to *today* and generates new orders forward from there, so a fixed
-- narrow window would silently start rejecting inserts on some future date -
-- a foreign-key failure with no obvious cause. Three years is ~1,096 rows.
--
-- Month and day names come from literal arrays rather than to_char(). to_char
-- output depends on the server's lc_time, which would make this migration
-- produce different data on a differently-configured host. Arrays make it
-- deterministic, which a migration has to be.
--
-- day_of_week and week are ISO 8601: Monday = 1, and week 1 is the week
-- containing the first Thursday of the year.
-- -----------------------------------------------------------------------------
INSERT INTO salesops.dim_date (
    date_key, calendar_date, year, quarter, month, month_name,
    week, day_of_week, day_name, is_weekend
)
SELECT
    (EXTRACT(YEAR  FROM d) * 10000
   + EXTRACT(MONTH FROM d) * 100
   + EXTRACT(DAY   FROM d))::INTEGER            AS date_key,
    d::DATE                                     AS calendar_date,
    EXTRACT(YEAR    FROM d)::SMALLINT           AS year,
    EXTRACT(QUARTER FROM d)::SMALLINT           AS quarter,
    EXTRACT(MONTH   FROM d)::SMALLINT           AS month,
    (ARRAY[
        'January', 'February', 'March',     'April',   'May',      'June',
        'July',    'August',   'September', 'October', 'November', 'December'
     ])[EXTRACT(MONTH FROM d)::INTEGER]         AS month_name,
    EXTRACT(WEEK  FROM d)::SMALLINT             AS week,
    EXTRACT(ISODOW FROM d)::SMALLINT            AS day_of_week,
    (ARRAY[
        'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
     ])[EXTRACT(ISODOW FROM d)::INTEGER]        AS day_name,
    EXTRACT(ISODOW FROM d) >= 6                 AS is_weekend
FROM generate_series(
        DATE '2025-01-01',
        DATE '2027-12-31',
        INTERVAL '1 day'
     ) AS d
ON CONFLICT (date_key) DO NOTHING;


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V002', 'Reference data: dim_region, dim_product, dim_channel, dim_date')
ON CONFLICT (version) DO NOTHING;

COMMIT;
