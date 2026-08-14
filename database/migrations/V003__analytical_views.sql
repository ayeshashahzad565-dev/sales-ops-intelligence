-- =============================================================================
-- V003  Analytical base views
--
-- Two views, no more. They exist so that "daily revenue" has exactly one
-- definition in this project - the Stage 5 detector, the Metabase dashboards
-- and any ad-hoc query all read the same numbers, because they read the same
-- SQL. Adding speculative views now would just create definitions nothing uses.
--
-- Deliberately NOT here: moving averages, z-scores, thresholds, anomaly flags.
-- Those are Stage 5, and they belong with the detector that owns their
-- parameters, not baked into a base view.
--
-- Everything is plain aggregate SQL: same inputs, same outputs, no window
-- functions over unstable orderings, no dependence on the clock.
--
-- ---------------------------------------------------------------------------
-- A NOTE ON CURRENCY, which matters for reading these correctly
--
-- Revenue is reported in USD, because summing across five currencies is
-- otherwise meaningless. USD columns in fact_orders are NULL until an exchange
-- rate is attached, and SUM() silently skips NULLs - so a day where only some
-- rows have rates would report a real-looking but incomplete total.
--
-- Both views therefore expose `orders_pending_fx`. Any row where that is
-- greater than zero has understated revenue. It is not a warning tucked in a
-- comment; it is a column, so it shows up in the dashboard.
--
-- Until Stage 3 loads FX, every revenue figure here will be NULL and
-- orders_pending_fx will equal orders_count. That is correct behaviour, not a
-- broken view.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- daily_sales_base
-- One row per calendar day on which orders exist, company-wide.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.daily_sales_base AS
SELECT
    f.order_date,
    d.year,
    d.quarter,
    d.month,
    d.month_name,
    d.week,
    d.day_name,
    d.is_weekend,

    COUNT(*)                                     AS orders_count,
    COUNT(DISTINCT f.customer_id)                AS customers_count,
    SUM(f.quantity)                              AS units_sold,

    ROUND(SUM(f.gross_amount_usd), 2)            AS gross_revenue_usd,
    ROUND(SUM(f.refund_amount_usd), 2)           AS refund_amount_usd,
    ROUND(SUM(f.net_amount_usd), 2)              AS net_revenue_usd,

    -- Average order value. NULLIF guards the impossible-but-cheap case of a
    -- group with no rows, and keeps the expression total.
    ROUND(SUM(f.gross_amount_usd) / NULLIF(COUNT(*), 0), 2)
                                                 AS average_order_value_usd,

    -- Refunds as a share of gross. NULLIF avoids a divide-by-zero on a day that
    -- somehow grossed nothing.
    ROUND(
        SUM(f.refund_amount_usd) / NULLIF(SUM(f.gross_amount_usd), 0),
        4
    )                                            AS refund_rate,

    -- Completeness flag. > 0 means the revenue figures above are understated.
    COUNT(*) FILTER (WHERE f.exchange_rate_to_usd IS NULL)
                                                 AS orders_pending_fx
FROM salesops.fact_orders AS f
JOIN salesops.dim_date    AS d ON d.calendar_date = f.order_date
GROUP BY
    f.order_date, d.year, d.quarter, d.month, d.month_name,
    d.week, d.day_name, d.is_weekend;

COMMENT ON VIEW salesops.daily_sales_base IS
    'Company-wide daily KPIs in USD. Check orders_pending_fx: any value above 0 '
    'means revenue is understated because those rows have no exchange rate yet.';


-- -----------------------------------------------------------------------------
-- regional_sales_base
-- One row per calendar day per region. This is the grain the Stage 5 detector
-- consumes: four separate series, so a collapse in one region is not diluted
-- by the other three.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW salesops.regional_sales_base AS
SELECT
    f.order_date,
    d.year,
    d.quarter,
    d.month,
    d.week,
    d.day_name,
    d.is_weekend,

    r.region_id,
    r.region_code,
    r.region_name,

    COUNT(*)                                     AS orders_count,
    COUNT(DISTINCT f.customer_id)                AS customers_count,
    SUM(f.quantity)                              AS units_sold,

    ROUND(SUM(f.gross_amount_usd), 2)            AS gross_revenue_usd,
    ROUND(SUM(f.refund_amount_usd), 2)           AS refund_amount_usd,
    ROUND(SUM(f.net_amount_usd), 2)              AS net_revenue_usd,

    ROUND(SUM(f.gross_amount_usd) / NULLIF(COUNT(*), 0), 2)
                                                 AS average_order_value_usd,
    ROUND(
        SUM(f.refund_amount_usd) / NULLIF(SUM(f.gross_amount_usd), 0),
        4
    )                                            AS refund_rate,

    COUNT(*) FILTER (WHERE f.exchange_rate_to_usd IS NULL)
                                                 AS orders_pending_fx
FROM salesops.fact_orders AS f
JOIN salesops.dim_date    AS d ON d.calendar_date = f.order_date
JOIN salesops.dim_region  AS r ON r.region_id     = f.region_id
GROUP BY
    f.order_date, d.year, d.quarter, d.month, d.week, d.day_name, d.is_weekend,
    r.region_id, r.region_code, r.region_name;

COMMENT ON VIEW salesops.regional_sales_base IS
    'Daily KPIs per region in USD. The grain the Stage 5 anomaly detector reads. '
    'Check orders_pending_fx before trusting revenue.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V003', 'Analytical base views: daily_sales_base, regional_sales_base')
ON CONFLICT (version) DO NOTHING;

COMMIT;
