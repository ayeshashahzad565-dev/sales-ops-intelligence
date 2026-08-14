-- ---------------------------------------------------------------------------
-- Runs exactly once, during first-time initialisation of an empty data volume.
-- It does NOT run again on subsequent `docker compose up`. To re-run it you
-- must destroy the volume: `docker compose down -v`.
--
-- Purpose: keep application state out of the analytics database.
--
--   ${POSTGRES_DB} (default: salesops) -> pipeline data: staging, dim/fact,
--                                         KPIs, anomalies, audit log
--   n8n                                -> n8n workflows, credentials, executions
--   metabase                           -> Metabase dashboards, questions, users
--
-- These two names are intentionally hardcoded rather than parameterised: they
-- are not secrets, nothing should need to change them, and hardcoding removes
-- any chance of them drifting out of sync with docker-compose.yml.
--
-- Both are owned by POSTGRES_USER, which is the user running this script.
-- ---------------------------------------------------------------------------

CREATE DATABASE n8n;
COMMENT ON DATABASE n8n IS 'n8n internal state. Not analytics data - do not report on this.';

CREATE DATABASE metabase;
COMMENT ON DATABASE metabase IS 'Metabase application state. Not analytics data - do not report on this.';
