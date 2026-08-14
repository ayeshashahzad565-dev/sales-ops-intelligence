-- =============================================================================
-- V007  Persist the revenue baseline median on Stage 5 results
--
-- This is the smallest backward-compatible change that lets Stage 6 measure
-- business impact without inventing a second statistical methodology.
--
-- Why it is needed
-- ----------------
-- Stage 6 must report expected vs actual net revenue. Stage 5 already computes
-- the expected value - the median of the calendar-aware baseline - but only
-- persisted the DERIVED deviation, not the value itself.
--
-- Recovering it arithmetically from what was stored is not acceptable:
--
--   expected = actual / (1 + revenue_deviation_pct / 100)
--
-- is lossy (revenue_deviation_pct is NUMERIC(14,4), so the reconstruction
-- carries rounding error into every monetary threshold comparison) and
-- undefined at -100%. More importantly it would make Stage 6 depend on an
-- inverted formula rather than on the number Stage 5 actually used, which is
-- precisely the "second competing methodology" the design forbids.
--
-- Why the detector version is NOT bumped
-- --------------------------------------
-- Nothing computed changes. `baseline_median` is already produced by
-- statistics.median() inside the Stage 5 scorer for every signal; this migration
-- only gives the revenue one a column to land in. No score, no verdict, and no
-- deviation moves by a single digit. detector_version describes the ALGORITHM,
-- and the algorithm is byte-for-byte the same - so bumping it would falsely
-- claim results are not comparable with earlier ones.
--
-- Why only revenue
-- ----------------
-- The other three signals are already persisted in exactly the form Stage 6
-- consumes them: aov and orders as percent deviations, refunds as an absolute
-- rate difference. Only the money measure needs an absolute baseline, because
-- only the money measure feeds an absolute-dollar threshold. Adding the other
-- three medians would be symmetrical but unused.
--
-- Backward compatibility
-- ----------------------
-- The column is nullable and rows written before this migration keep NULL until
-- the detector next runs. That state is real and Stage 6 handles it explicitly
-- (BUSINESS_IMPACT_UNAVAILABLE -> human review) rather than assuming zero
-- impact. A missing baseline must never quietly downgrade a decision.
-- =============================================================================

BEGIN;

ALTER TABLE salesops.anomaly_daily
    ADD COLUMN IF NOT EXISTS revenue_baseline_median NUMERIC(18,4);

COMMENT ON COLUMN salesops.anomaly_daily.revenue_baseline_median IS
    'Median net USD revenue of the calendar-aware baseline this date was scored '
    'against - i.e. what the day was EXPECTED to earn. The absolute counterpart of '
    'revenue_deviation_pct, persisted so Stage 6 can measure business impact from '
    'the same number Stage 5 judged against instead of re-deriving one. '
    'NULL on rows written before V007, and on unscored rows.';

-- A scored row's revenue signal always has a baseline, so once the detector has
-- run under V007 this is never NULL for a scored date. It is not a CHECK
-- constraint: rows predating this migration are legitimately NULL, and a
-- constraint would make the migration fail on exactly the databases that need it.
-- The Stage 6 test suite asserts the post-run invariant instead.


INSERT INTO salesops.schema_migrations (version, description)
VALUES ('V007', 'Persist the revenue baseline median on anomaly_daily for Stage 6 impact measurement')
ON CONFLICT (version) DO NOTHING;

COMMIT;
