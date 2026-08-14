-- =============================================================================
-- V001  Analytics schema: staging, dimensions, fact
--
-- Structural DDL only. Reference data lives in V002 so that a schema review is
-- not buried in INSERT statements, and so the two can be re-run independently.
--
-- Idempotent (IF NOT EXISTS throughout) and transactional: the whole file
-- either applies or it does not. PostgreSQL supports transactional DDL, so
-- there is no half-migrated state to clean up after a failure.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS salesops;


-- -----------------------------------------------------------------------------
-- Migration ledger
--
-- Deliberately not a migration *framework*. Each file records that it ran; the
-- runner applies files in name order and idempotency makes re-runs harmless.
-- That is enough to answer "what is applied here?" without adding a dependency.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.schema_migrations (
    version     TEXT        PRIMARY KEY,
    description TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE salesops.schema_migrations IS
    'Which migration files have been applied to this database.';


-- =============================================================================
-- LAYER 1 - STAGING
--
-- Everything the pipeline receives lands here first, exactly as the source sent
-- it. This layer is append-only from the pipeline's point of view and has no
-- foreign keys: a row that fails validation must still be storable, or the
-- dead-letter queue in Stage 10 has nothing to inspect.
-- =============================================================================

CREATE TABLE IF NOT EXISTS salesops.raw_orders_staging (
    ingestion_id      BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Groups every row pulled by one ingestion run, so a bad run is traceable
    -- and reversible as a unit.
    batch_id          UUID        NOT NULL,

    -- Nullable on purpose: a malformed payload may not contain an order id at
    -- all, and that row still has to be captured rather than rejected.
    order_id          TEXT,

    -- The untransformed API response for this record. This is the traceability
    -- anchor: any figure in fact_orders can be traced back to the bytes it came
    -- from, which is what makes the pipeline auditable.
    source_payload    JSONB       NOT NULL,

    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at      TIMESTAMPTZ,

    processing_status TEXT        NOT NULL DEFAULT 'pending',
    error_message     TEXT,

    CONSTRAINT raw_orders_staging_status_valid
        CHECK (processing_status IN ('pending', 'processed', 'failed', 'skipped')),

    -- A failure with no explanation is not a usable dead-letter record.
    CONSTRAINT raw_orders_staging_failure_has_reason
        CHECK (processing_status <> 'failed' OR error_message IS NOT NULL)
);

COMMENT ON TABLE salesops.raw_orders_staging IS
    'Landing zone. Raw API payloads, retained whether or not they validate.';
COMMENT ON COLUMN salesops.raw_orders_staging.batch_id IS
    'One ingestion run. Lets a whole run be traced or reverted together.';
COMMENT ON COLUMN salesops.raw_orders_staging.order_id IS
    'Extracted for convenience only. NULL when the payload is malformed.';
COMMENT ON COLUMN salesops.raw_orders_staging.processing_status IS
    'pending | processed | failed | skipped. skipped = valid but already loaded.';

-- Find the work still to do, and the failures to review. Partial: the vast
-- majority of rows settle at 'processed' and are dead weight in this index.
CREATE INDEX IF NOT EXISTS idx_raw_orders_staging_unresolved
    ON salesops.raw_orders_staging (processing_status, received_at)
    WHERE processing_status IN ('pending', 'failed');

-- Retrieve or roll back a single run.
CREATE INDEX IF NOT EXISTS idx_raw_orders_staging_batch
    ON salesops.raw_orders_staging (batch_id);

-- Trace a curated fact row back to the payloads it came from.
CREATE INDEX IF NOT EXISTS idx_raw_orders_staging_order_id
    ON salesops.raw_orders_staging (order_id)
    WHERE order_id IS NOT NULL;


-- =============================================================================
-- LAYER 2 - DIMENSIONS
--
-- Key strategy, applied consistently:
--
--   Surrogate keys (region, product, channel) where the source value is a
--   low-cardinality *label*. Labels get renamed and re-coded; a surrogate means
--   a rename is a one-row UPDATE instead of a fact-table rewrite. They are also
--   2 bytes instead of a string in every fact row.
--
--   Natural key (customer) where the source already provides a stable, opaque
--   identifier. Adding a surrogate over `CUST-NA-0365` would buy nothing and
--   would force every ingestion insert through an extra lookup.
--
-- Either way the fact table holds a constrained foreign key, never an
-- unvalidated free-text copy of whatever the API happened to send.
-- =============================================================================

-- --- dim_date ----------------------------------------------------------------
-- Pre-computed calendar attributes, so "weekday vs weekend" and "by quarter"
-- are joins rather than repeated date arithmetic scattered across queries.
CREATE TABLE IF NOT EXISTS salesops.dim_date (
    date_key      INTEGER     PRIMARY KEY,          -- YYYYMMDD, e.g. 20260809
    calendar_date DATE        NOT NULL UNIQUE,
    year          SMALLINT    NOT NULL,
    quarter       SMALLINT    NOT NULL,
    month         SMALLINT    NOT NULL,
    month_name    TEXT        NOT NULL,
    week          SMALLINT    NOT NULL,             -- ISO 8601 week number
    day_of_week   SMALLINT    NOT NULL,             -- ISO 8601: 1 = Monday .. 7 = Sunday
    day_name      TEXT        NOT NULL,
    is_weekend    BOOLEAN     NOT NULL,

    CONSTRAINT dim_date_key_matches_date
        CHECK (date_key = EXTRACT(YEAR FROM calendar_date) * 10000
                        + EXTRACT(MONTH FROM calendar_date) * 100
                        + EXTRACT(DAY FROM calendar_date)),
    CONSTRAINT dim_date_quarter_range CHECK (quarter BETWEEN 1 AND 4),
    CONSTRAINT dim_date_month_range   CHECK (month BETWEEN 1 AND 12),
    CONSTRAINT dim_date_dow_range     CHECK (day_of_week BETWEEN 1 AND 7)
);

COMMENT ON TABLE salesops.dim_date IS
    'Calendar dimension. day_of_week is ISO 8601 (1 = Monday).';


-- --- dim_region --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.dim_region (
    region_id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region_code TEXT     NOT NULL UNIQUE,           -- natural key from the source: NA, EMEA, ...
    region_name TEXT     NOT NULL,

    CONSTRAINT dim_region_code_format CHECK (region_code ~ '^[A-Z]{2,10}$')
);

COMMENT ON COLUMN salesops.dim_region.region_code IS
    'Source system value. The lookup key for ingestion; region_id is the join key.';


-- --- dim_product -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.dim_product (
    product_id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_sku  TEXT     NOT NULL UNIQUE,          -- natural key: the API's `product` field
    product_name TEXT     NOT NULL,
    category     TEXT     NOT NULL,

    CONSTRAINT dim_product_sku_format CHECK (product_sku ~ '^SKU-[0-9]{4}$')
);

COMMENT ON COLUMN salesops.dim_product.product_sku IS
    'The API sends this as `product`. Kept so ingestion can resolve SKU -> product_id.';


-- --- dim_channel -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.dim_channel (
    channel_id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_code TEXT     NOT NULL UNIQUE,          -- natural key: web, mobile, partner, field_sales
    channel_name TEXT     NOT NULL,

    CONSTRAINT dim_channel_code_format CHECK (channel_code ~ '^[a-z_]{2,20}$')
);


-- --- dim_customer ------------------------------------------------------------
-- Natural key. Populated by ingestion as a late-arriving dimension: a customer
-- exists in this table because an order referenced them, not because a customer
-- feed was loaded first.
CREATE TABLE IF NOT EXISTS salesops.dim_customer (
    customer_id     TEXT     PRIMARY KEY,

    -- Nullable, and it will stay NULL for now. The orders endpoint carries only
    -- customer_id - there is no customer name in the payload and no customer
    -- endpoint to enrich from. Inventing names here would put fabricated data
    -- into the warehouse to satisfy a column. Left honest and empty instead.
    customer_name   TEXT,

    region_id       SMALLINT NOT NULL,
    first_seen_date DATE     NOT NULL,

    CONSTRAINT dim_customer_region_fk
        FOREIGN KEY (region_id) REFERENCES salesops.dim_region (region_id)
);

COMMENT ON COLUMN salesops.dim_customer.customer_name IS
    'NULL by design: the source orders API exposes no customer name. See database/README.md.';
COMMENT ON COLUMN salesops.dim_customer.first_seen_date IS
    'Earliest order_date seen for this customer. Maintained by ingestion via LEAST().';

-- Supports "customers by region" and keeps the FK cheap to validate.
CREATE INDEX IF NOT EXISTS idx_dim_customer_region
    ON salesops.dim_customer (region_id);


-- =============================================================================
-- LAYER 3 - REFERENCE: EXCHANGE RATES
--
-- Intentionally left EMPTY by this migration. Populating it would mean
-- inventing FX rates, and every USD figure in the warehouse would then be
-- traceable to a number someone made up. Stage 3 fills it from Frankfurter.
-- =============================================================================

CREATE TABLE IF NOT EXISTS salesops.exchange_rates (
    rate_date    DATE          NOT NULL,
    currency     CHAR(3)       NOT NULL,
    rate_to_usd  NUMERIC(18,8) NOT NULL,
    source       TEXT          NOT NULL,
    fetched_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- One rate per currency per day. This is what makes the FX sync workflow
    -- safe to re-run: a repeat fetch collides and can be resolved with
    -- ON CONFLICT rather than silently doubling the table.
    CONSTRAINT exchange_rates_pk PRIMARY KEY (rate_date, currency),

    CONSTRAINT exchange_rates_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT exchange_rates_positive        CHECK (rate_to_usd > 0)
);

COMMENT ON TABLE salesops.exchange_rates IS
    'Daily FX. Empty until Stage 3 loads it from Frankfurter. Never seeded with invented rates.';
COMMENT ON COLUMN salesops.exchange_rates.rate_to_usd IS
    'Multiply a local amount by this to get USD. USD itself must be stored as 1.0 - '
    'Frankfurter does not return a USD->USD pair.';
COMMENT ON COLUMN salesops.exchange_rates.source IS
    'Provenance, e.g. ''frankfurter''. Two sources disagreeing is a real incident; record which was used.';


-- =============================================================================
-- LAYER 4 - FACT
-- =============================================================================

CREATE TABLE IF NOT EXISTS salesops.fact_orders (
    -- The natural key from the source system, and the pipeline's idempotency
    -- key. PRIMARY KEY here is what makes re-running an ingestion safe: a
    -- repeated order collides instead of double-counting revenue.
    order_id             TEXT          PRIMARY KEY,

    order_date           DATE          NOT NULL,

    customer_id          TEXT          NOT NULL,
    region_id            SMALLINT      NOT NULL,
    product_id           SMALLINT      NOT NULL,
    channel_id           SMALLINT      NOT NULL,

    quantity             INTEGER       NOT NULL,
    unit_price           NUMERIC(14,4) NOT NULL,
    currency             CHAR(3)       NOT NULL,

    -- Derived, so it cannot disagree with its inputs.
    gross_amount_local   NUMERIC(18,4)
        GENERATED ALWAYS AS (quantity * unit_price) STORED,

    refund_amount_local  NUMERIC(18,4) NOT NULL DEFAULT 0,

    -- The rate actually applied, copied onto the row rather than joined at read
    -- time. Providers revise historical rates; freezing the rate used means a
    -- report re-run next year reproduces the number it produced today.
    exchange_rate_to_usd NUMERIC(18,8),

    -- All three are NULL until a rate exists, and all three recompute if the
    -- rate is later corrected. This makes "no USD value without a real rate" a
    -- structural property of the table rather than a rule someone must remember.
    gross_amount_usd     NUMERIC(18,4)
        GENERATED ALWAYS AS (quantity * unit_price * exchange_rate_to_usd) STORED,
    refund_amount_usd    NUMERIC(18,4)
        GENERATED ALWAYS AS (refund_amount_local * exchange_rate_to_usd) STORED,
    net_amount_usd       NUMERIC(18,4)
        GENERATED ALWAYS AS ((quantity * unit_price - refund_amount_local) * exchange_rate_to_usd) STORED,

    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fact_orders_date_fk
        FOREIGN KEY (order_date) REFERENCES salesops.dim_date (calendar_date),
    CONSTRAINT fact_orders_customer_fk
        FOREIGN KEY (customer_id) REFERENCES salesops.dim_customer (customer_id),
    CONSTRAINT fact_orders_region_fk
        FOREIGN KEY (region_id) REFERENCES salesops.dim_region (region_id),
    CONSTRAINT fact_orders_product_fk
        FOREIGN KEY (product_id) REFERENCES salesops.dim_product (product_id),
    CONSTRAINT fact_orders_channel_fk
        FOREIGN KEY (channel_id) REFERENCES salesops.dim_channel (channel_id),

    CONSTRAINT fact_orders_quantity_positive  CHECK (quantity > 0),
    CONSTRAINT fact_orders_price_non_negative CHECK (unit_price >= 0),
    CONSTRAINT fact_orders_refund_non_negative CHECK (refund_amount_local >= 0),

    -- You cannot refund more than the line was worth. Expressed over the base
    -- columns rather than gross_amount_local, since a CHECK may not depend on a
    -- generated column.
    CONSTRAINT fact_orders_refund_within_gross
        CHECK (refund_amount_local <= quantity * unit_price),

    CONSTRAINT fact_orders_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT fact_orders_rate_positive
        CHECK (exchange_rate_to_usd IS NULL OR exchange_rate_to_usd > 0)
);

COMMENT ON TABLE salesops.fact_orders IS
    'One row per order line. order_id is the idempotency key; USD columns are '
    'generated and stay NULL until a real exchange rate is attached.';
COMMENT ON COLUMN salesops.fact_orders.order_date IS
    'DATE rather than a date_key, so an analyst can filter without joining dim_date. '
    'Integrity is still enforced by a foreign key onto dim_date.calendar_date.';
COMMENT ON COLUMN salesops.fact_orders.exchange_rate_to_usd IS
    'The rate applied to this row. NULL means USD figures are not yet computable.';
COMMENT ON COLUMN salesops.fact_orders.gross_amount_usd IS
    'GENERATED. Do not INSERT or UPDATE this column - PostgreSQL will reject it.';


-- --- indexes -----------------------------------------------------------------
--
-- Six indexes, each tied to a query the pipeline or the dashboards will
-- actually run. Notably absent: a plain index on `currency`. With five distinct
-- values across the whole table the planner would ignore it in favour of a
-- sequential scan. The one currency access pattern that *is* selective - "which
-- rows still need an FX rate?" - is served by the partial index below.

-- Daily aggregation and date-window ingestion checks.
CREATE INDEX IF NOT EXISTS idx_fact_orders_order_date
    ON salesops.fact_orders (order_date);

-- Per-region daily series: the core anomaly-detection query in Stage 5.
CREATE INDEX IF NOT EXISTS idx_fact_orders_region_date
    ON salesops.fact_orders (region_id, order_date);

-- Product and channel breakdowns, and cheap FK validation.
CREATE INDEX IF NOT EXISTS idx_fact_orders_product_date
    ON salesops.fact_orders (product_id, order_date);
CREATE INDEX IF NOT EXISTS idx_fact_orders_channel_date
    ON salesops.fact_orders (channel_id, order_date);

-- Customer history. High cardinality (~1,000 and growing), so genuinely selective.
CREATE INDEX IF NOT EXISTS idx_fact_orders_customer
    ON salesops.fact_orders (customer_id);

-- The FX backfill worklist: rows whose USD values cannot be computed yet.
-- Partial, so it holds only the outstanding rows and empties itself as the
-- backfill completes.
CREATE INDEX IF NOT EXISTS idx_fact_orders_pending_fx
    ON salesops.fact_orders (currency, order_date)
    WHERE exchange_rate_to_usd IS NULL;


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V001', 'Analytics schema: staging, dimensions, exchange rates, fact table')
ON CONFLICT (version) DO NOTHING;

COMMIT;
