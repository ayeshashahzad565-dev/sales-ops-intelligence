-- =============================================================================
-- V005  Materialised daily KPI layer
--
-- Stage 4 produces deterministic analytical inputs.
-- Stage 5 owns statistical anomaly detection.
-- Stage 7 owns LLM reasoning.
--
-- Nothing in this file scores, thresholds or judges anything. It computes what
-- happened, per day, and records how much of it could be expressed in USD.
--
-- Why a table and not just the Stage 2 views: the Stage 5 detector needs a
-- stable, cheap, repeatedly-scannable series, and it needs two things the views
-- do not provide - new_customers, and trailing moving averages. Recomputing
-- window functions over fact_orders on every detector pass would be both slower
-- and non-reproducible between runs.
--
-- Idempotent and transactional, like every migration here.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- kpi_daily
--
-- One row per calendar date that actually has orders. Dates with no orders are
-- absent rather than zero-filled: "no trading" and "traded nothing" are
-- different facts, and inventing zero rows would corrupt the moving averages
-- and hand the Stage 5 detector a fake baseline.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS salesops.kpi_daily (
    date_key                    INTEGER       PRIMARY KEY,
    calendar_date               DATE          NOT NULL UNIQUE,

    -- --- volume -------------------------------------------------------------
    orders_count                INTEGER       NOT NULL,
    customers_count             INTEGER       NOT NULL,
    new_customers               INTEGER       NOT NULL,
    units_sold                  INTEGER       NOT NULL,

    -- --- money ---------------------------------------------------------------
    -- Nullable on purpose. A day whose orders have no exchange rate has no
    -- USD revenue - not zero revenue. Coalescing to 0 here would turn missing
    -- data into a revenue collapse, which is exactly the signal Stage 5 hunts.
    gross_revenue_usd           NUMERIC(18,4),
    refund_amount_usd           NUMERIC(18,4),
    net_revenue_usd             NUMERIC(18,4),
    average_order_value_usd     NUMERIC(18,4),
    refund_rate                 NUMERIC(9,6),

    -- --- FX completeness -----------------------------------------------------
    -- Carried alongside every money figure so a consumer can tell an incomplete
    -- day from a bad one without going back to fact_orders.
    orders_pending_fx           INTEGER       NOT NULL,
    fx_completeness_pct         NUMERIC(6,3)  NOT NULL,
    is_complete                 BOOLEAN       NOT NULL,

    -- --- trend ---------------------------------------------------------------
    rolling_7d_net_revenue_usd  NUMERIC(18,4),
    rolling_28d_net_revenue_usd NUMERIC(18,4),

    refreshed_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT kpi_daily_date_fk
        FOREIGN KEY (calendar_date) REFERENCES salesops.dim_date (calendar_date),

    CONSTRAINT kpi_daily_key_matches_date
        CHECK (date_key = EXTRACT(YEAR FROM calendar_date) * 10000
                        + EXTRACT(MONTH FROM calendar_date) * 100
                        + EXTRACT(DAY FROM calendar_date)),

    -- A row exists because orders exist.
    CONSTRAINT kpi_daily_has_orders          CHECK (orders_count > 0),
    CONSTRAINT kpi_daily_customers_sane      CHECK (customers_count BETWEEN 1 AND orders_count),
    CONSTRAINT kpi_daily_new_customers_sane  CHECK (new_customers BETWEEN 0 AND customers_count),
    -- Every order carries at least one unit.
    CONSTRAINT kpi_daily_units_sane          CHECK (units_sold >= orders_count),

    CONSTRAINT kpi_daily_pending_fx_sane     CHECK (orders_pending_fx BETWEEN 0 AND orders_count),
    CONSTRAINT kpi_daily_completeness_range  CHECK (fx_completeness_pct BETWEEN 0 AND 100),

    -- is_complete is derived, so it cannot disagree with the count it derives
    -- from. A consumer trusting one and not the other still gets the same answer.
    CONSTRAINT kpi_daily_complete_agrees
        CHECK (is_complete = (orders_pending_fx = 0)),
    CONSTRAINT kpi_daily_completeness_agrees
        CHECK ((fx_completeness_pct = 100) = (orders_pending_fx = 0)),

    -- No USD figure may exist on a day where nothing converted.
    CONSTRAINT kpi_daily_no_money_without_fx
        CHECK (orders_pending_fx < orders_count OR gross_revenue_usd IS NULL),

    CONSTRAINT kpi_daily_refund_rate_range
        CHECK (refund_rate IS NULL OR refund_rate BETWEEN 0 AND 1)
);

COMMENT ON TABLE salesops.kpi_daily IS
    'Deterministic daily KPIs, rebuilt wholesale by salesops.refresh_kpi_daily(). '
    'The canonical input for the Stage 5 detector. Contains no thresholds or scores.';
COMMENT ON COLUMN salesops.kpi_daily.new_customers IS
    'Customers whose earliest order_date anywhere in fact_orders is this date. '
    'Recomputed from fact_orders, not read from dim_customer.first_seen_date, so a '
    'full rebuild does not depend on an incrementally-maintained value.';
COMMENT ON COLUMN salesops.kpi_daily.average_order_value_usd IS
    'net_revenue_usd / orders that actually converted to USD. Divides by the '
    'converted subset, not by orders_count, so an incomplete day is not understated. '
    'NOTE: daily_sales_base.average_order_value_usd uses gross / all orders - the two '
    'are deliberately different measures. See database/README.md.';
COMMENT ON COLUMN salesops.kpi_daily.fx_completeness_pct IS
    'Share of the day''s orders carrying an exchange rate, 0-100. Below 100 means '
    'every money column on this row is understated.';
COMMENT ON COLUMN salesops.kpi_daily.rolling_7d_net_revenue_usd IS
    'Mean daily net USD revenue over a trailing 7 CALENDAR-day window ending on this '
    'row (RANGE, not ROWS), so a gap in the series shortens the window rather than '
    'silently reaching further back. Never includes future dates.';


-- =============================================================================
-- refresh_kpi_daily()
--
-- A full deterministic rebuild. Same inputs, same output, every time.
--
-- This is a function rather than SQL in an n8n node for one concrete reason:
-- the rebuild is a DELETE followed by an INSERT, and those must be atomic.
-- A function body runs inside the caller's transaction, so a failure anywhere
-- rolls back the DELETE too - the table is never left empty or half-written.
-- Splitting it across two n8n nodes would put a window between them where
-- kpi_daily does not exist.
--
-- It also means the tests exercise exactly the SQL that production runs.
-- =============================================================================
CREATE OR REPLACE FUNCTION salesops.refresh_kpi_daily()
RETURNS TABLE (
    dates_written    INTEGER,
    dates_complete   INTEGER,
    dates_incomplete INTEGER,
    earliest_date    DATE,
    latest_date      DATE
)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Wholesale replace. The table is one row per trading day - a few hundred
    -- rows - so a rebuild costs less than the bookkeeping an incremental
    -- strategy would need, and it cannot drift from fact_orders.
    DELETE FROM salesops.kpi_daily;

    INSERT INTO salesops.kpi_daily (
        date_key, calendar_date,
        orders_count, customers_count, new_customers, units_sold,
        gross_revenue_usd, refund_amount_usd, net_revenue_usd,
        average_order_value_usd, refund_rate,
        orders_pending_fx, fx_completeness_pct, is_complete,
        rolling_7d_net_revenue_usd, rolling_28d_net_revenue_usd
    )
    WITH
    -- A customer's first appearance anywhere in the fact table.
    first_seen AS (
        SELECT customer_id, min(order_date) AS first_order_date
        FROM salesops.fact_orders
        GROUP BY customer_id
    ),
    new_by_date AS (
        SELECT first_order_date AS order_date, count(*)::INTEGER AS new_customers
        FROM first_seen
        GROUP BY first_order_date
    ),
    daily AS (
        SELECT
            f.order_date,
            count(*)::INTEGER                       AS orders_count,
            count(DISTINCT f.customer_id)::INTEGER  AS customers_count,
            sum(f.quantity)::INTEGER                AS units_sold,

            -- SUM skips NULLs, so these total only the orders that converted.
            -- orders_pending_fx below is what tells you whether that is all of them.
            sum(f.gross_amount_usd)                 AS gross_revenue_usd,
            sum(f.refund_amount_usd)                AS refund_amount_usd,
            sum(f.net_amount_usd)                   AS net_revenue_usd,

            count(*) FILTER (WHERE f.exchange_rate_to_usd IS NULL)::INTEGER
                                                    AS orders_pending_fx,
            count(*) FILTER (WHERE f.exchange_rate_to_usd IS NOT NULL)::INTEGER
                                                    AS orders_converted
        FROM salesops.fact_orders f
        GROUP BY f.order_date
    ),
    measured AS (
        SELECT
            d.order_date,
            d.orders_count,
            d.customers_count,
            COALESCE(n.new_customers, 0)            AS new_customers,
            d.units_sold,
            d.gross_revenue_usd,
            d.refund_amount_usd,
            d.net_revenue_usd,

            -- Divided by the converted subset. NULLIF keeps a fully-unconverted
            -- day at NULL instead of raising or reporting a fictitious zero.
            round(d.net_revenue_usd / NULLIF(d.orders_converted, 0), 4)
                                                    AS average_order_value_usd,
            round(d.refund_amount_usd / NULLIF(d.gross_revenue_usd, 0), 6)
                                                    AS refund_rate,

            d.orders_pending_fx,
            round(100.0 * d.orders_converted / NULLIF(d.orders_count, 0), 3)
                                                    AS fx_completeness_pct,
            (d.orders_pending_fx = 0)               AS is_complete
        FROM daily d
        LEFT JOIN new_by_date n ON n.order_date = d.order_date
    )
    SELECT
        dd.date_key,
        m.order_date,
        m.orders_count,
        m.customers_count,
        m.new_customers,
        m.units_sold,
        m.gross_revenue_usd,
        m.refund_amount_usd,
        m.net_revenue_usd,
        m.average_order_value_usd,
        m.refund_rate,
        m.orders_pending_fx,
        m.fx_completeness_pct,
        m.is_complete,

        -- Trailing calendar windows. RANGE with an interval, not ROWS: ROWS
        -- would count rows, so a day with no orders would let the window reach
        -- an eighth or twenty-ninth calendar day back. Both windows end at the
        -- CURRENT ROW, so no future date can ever contribute.
        --
        -- The first days of the series average over fewer observations rather
        -- than being NULL - a shorter honest window beats fabricated history.
        round(avg(m.net_revenue_usd) OVER w7,  4)  AS rolling_7d_net_revenue_usd,
        round(avg(m.net_revenue_usd) OVER w28, 4)  AS rolling_28d_net_revenue_usd
    FROM measured m
    JOIN salesops.dim_date dd ON dd.calendar_date = m.order_date
    WINDOW
        w7  AS (ORDER BY m.order_date RANGE BETWEEN INTERVAL '6 days'  PRECEDING AND CURRENT ROW),
        w28 AS (ORDER BY m.order_date RANGE BETWEEN INTERVAL '27 days' PRECEDING AND CURRENT ROW);

    RETURN QUERY
    SELECT
        count(*)::INTEGER,
        count(*) FILTER (WHERE k.is_complete)::INTEGER,
        count(*) FILTER (WHERE NOT k.is_complete)::INTEGER,
        min(k.calendar_date),
        max(k.calendar_date)
    FROM salesops.kpi_daily k;
END;
$$;

COMMENT ON FUNCTION salesops.refresh_kpi_daily() IS
    'Full deterministic rebuild of kpi_daily. Atomic: DELETE and INSERT share the '
    'caller''s transaction, so a failure leaves the previous contents intact.';


-- -----------------------------------------------------------------------------
-- ingestion_runs now carries more than order ingestion
--
-- Stage 4 adds two more scheduled pipelines. Rather than stand up a second run
-- ledger, they share this one and are told apart by `source`. One table still
-- answers "did everything run, and did it finish?".
--
-- The consequence, which matters: any query asking "what did WE last load?"
-- must filter on source. The Orders Ingestion window calculation was updated
-- for exactly this reason.
-- -----------------------------------------------------------------------------
COMMENT ON TABLE salesops.ingestion_runs IS
    'One row per scheduled pipeline execution, written as ''running'' up front so a '
    'crashed run is visible. Shared by all pipelines; `source` says which: '
    '''mock-sales-api'' (order ingestion), ''frankfurter'' (FX sync), '
    '''kpi-refresh'' (KPI rebuild). Always filter by source when reading windows.';

COMMENT ON COLUMN salesops.ingestion_runs.source IS
    'Which pipeline produced this run. Queries that derive a next window from '
    'max(window_to) MUST scope to their own source.';


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V005', 'Materialised daily KPI layer: kpi_daily table and refresh_kpi_daily()')
ON CONFLICT (version) DO NOTHING;

COMMIT;
