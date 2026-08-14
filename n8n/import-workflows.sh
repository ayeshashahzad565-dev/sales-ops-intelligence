#!/usr/bin/env bash
#
# Imports the salesops workflows and their PostgreSQL credential into n8n.
#
# Safe to re-run: workflows carry fixed ids, so an import updates the existing
# workflow rather than creating a duplicate.
#
# The credential is assembled INSIDE the n8n container from the environment
# variables Compose already gave it. The password is never written to a file on
# the host, never passed as a command-line argument, and never enters the repo.
# n8n encrypts it on import using N8N_ENCRYPTION_KEY.
#
# Usage:
#   ./n8n/import-workflows.sh             import
#   ./n8n/import-workflows.sh --activate  import, then activate all schedules

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$script_dir")"

ERROR_ID="salesopsErrors001"
INGESTION_ID="salesopsIngest001"
FX_ID="salesopsFxSync001"
KPI_ID="salesopsKpiRfr001"
DETECT_ID="salesopsDetect001"
DECIDE_ID="salesopsDecide01"
RCA_ID="salesopsLlmRca01"
NOTIFY_ID="salesopsNotify01"
REMED_ID="salesopsRemed01"
MAINT_ID="salesopsMaint01"

activate=false
[[ "${1:-}" == "--activate" ]] && activate=true

echo "Creating the PostgreSQL credential inside the n8n container..."
docker compose exec -T n8n sh -c '
set -e
umask 077
cred=/tmp/salesops-credential.json
cat > "$cred" <<JSON
[
  {
    "id": "salesopsPgCred01",
    "name": "Salesops Analytics DB",
    "type": "postgres",
    "data": {
      "host": "postgres",
      "port": 5432,
      "database": "salesops",
      "user": "$DB_POSTGRESDB_USER",
      "password": "$DB_POSTGRESDB_PASSWORD",
      "ssl": "disable",
      "allowUnauthorizedCerts": false,
      "maxConnections": 20
    }
  }
]
JSON
n8n import:credentials --input="$cred"
rm -f "$cred"
'

# The error handler goes first: every other workflow references it by id in
# settings.errorWorkflow.
#
# MSYS_NO_PATHCONV stops Git Bash on Windows from rewriting the container path
# /workflows/... into a host path. Other shells ignore the variable.
echo "Importing workflows..."
for wf in pipeline-error-handler orders-ingestion fx-rate-sync kpi-daily-refresh \
          statistical-anomaly-detection deterministic-anomaly-decision \
          llm-root-cause-analysis notification-and-review-routing \
          remediation-execution operational-maintenance; do
    echo "  -> $wf"
    MSYS_NO_PATHCONV=1 docker compose exec -T n8n \
        n8n import:workflow --input="/workflows/${wf}.json" >/dev/null
done

# The error handler must be ACTIVE to be callable. n8n silently declines to run
# an inactive error workflow - the failing workflow still fails, but nothing is
# ever recorded, and the only clue is a line in the n8n log. An Error Trigger
# has no schedule of its own, so "active" here just means "available to invoke".
echo "Activating the error handler (required for it to be invocable)..."
MSYS_NO_PATHCONV=1 docker compose exec -T n8n \
    n8n update:workflow --id="$ERROR_ID" --active=true >/dev/null

if [[ "$activate" == true ]]; then
    echo "Activating schedules..."
    for id in "$INGESTION_ID" "$FX_ID" "$KPI_ID" "$DETECT_ID" "$DECIDE_ID" "$RCA_ID" "$NOTIFY_ID" "$REMED_ID" "$MAINT_ID"; do
        MSYS_NO_PATHCONV=1 docker compose exec -T n8n \
            n8n update:workflow --id="$id" --active=true >/dev/null
    done
fi

echo "Restarting n8n so activation changes take effect..."
docker compose restart n8n >/dev/null

cat <<EOF

Done. Workflow ids:
  Pipeline Error Handler  $ERROR_ID   (always active)
  Orders Ingestion        $INGESTION_ID   hourly
  FX Rate Sync            $FX_ID   daily 05:00
  KPI Daily Refresh       $KPI_ID   daily 06:00
  Anomaly Detection       $DETECT_ID   daily 07:00
  Anomaly Decision        $DECIDE_ID   daily 07:30
  LLM Root Cause          $RCA_ID   daily 08:00
  Notification & Review   $NOTIFY_ID   daily 08:30
  Remediation Execution   $REMED_ID   daily 09:00
  Operational Maintenance $MAINT_ID   daily 09:30

Run one now (the alternate broker port avoids colliding with the running
instance's task broker on 5679):

  docker compose exec -T -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id=$FX_ID
  docker compose exec -T -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id=$KPI_ID
EOF
