#!/usr/bin/env bash
#
# Cold start: from a fresh clone to a populated, demonstrable stack.
#
#   ./bootstrap.sh
#
# Eight steps, in the only order that works, each of them idempotent - so this
# is also the right thing to run when you are not sure what state the stack is
# in. Re-running costs a couple of minutes and changes nothing that is already
# correct.
#
#   1. check prerequisites          docker, compose, python
#   2. create .env                  generating the two secrets it needs
#   3. start the services           and wait for all five health checks
#   4. apply the migrations         V001..V013, then 277 schema checks
#   5. import the workflows         10 workflows + the database credential
#   6. run the pipeline once        ingestion -> ... -> maintenance, in order
#   7. provision the dashboards     4 dashboards, 31 cards, read-only role
#   8. report                       what was built and where to look
#
# Step 6 is why this script exists. Every workflow is on a daily schedule, so a
# freshly imported stack sits empty until the clock catches up; nobody
# evaluating this project is going to wait until 09:30 tomorrow. Running them in
# dependency order does in three minutes what the schedules do over a day.
#
# Requires bash. On Windows use Git Bash, as the other scripts here do.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'; reset=$'\033[0m'
step=0
say()  { step=$((step+1)); printf '\n%s[%d/8] %s%s\n' "$bold" "$step" "$1" "$reset"; }
info() { printf '      %s%s%s\n' "$dim" "$1" "$reset"; }
ok()   { printf '      %s✓%s %s\n' "$green" "$reset" "$1"; }
die()  { printf '\n%sFATAL:%s %s\n' "$red" "$reset" "$1" >&2; exit 1; }

SKIP_PIPELINE=false
[[ "${1:-}" == "--no-pipeline" ]] && SKIP_PIPELINE=true


# ---------------------------------------------------------------------------
say "Checking prerequisites"
# ---------------------------------------------------------------------------
command -v docker >/dev/null || die "docker is not on PATH."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
docker info >/dev/null 2>&1 || die "The Docker daemon is not running."
PYTHON="$(command -v python || command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python is not on PATH (needed to provision Metabase)."
ok "docker, compose and python are available"


# ---------------------------------------------------------------------------
say "Preparing .env"
# ---------------------------------------------------------------------------
# The two values with no safe default are generated rather than left blank: a
# placeholder password that works is a placeholder password that ships.
if [[ -f .env ]]; then
    ok ".env already exists - leaving it alone"
else
    cp .env.example .env
    "$PYTHON" - <<'PY'
import pathlib, re, secrets
p = pathlib.Path(".env")
s = p.read_text(encoding="utf-8")
for key, value in {
    "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
    "N8N_ENCRYPTION_KEY": secrets.token_hex(32),
    "METABASE_ADMIN_EMAIL": "analyst@salesops.local",
    "METABASE_ADMIN_PASSWORD": secrets.token_urlsafe(18),
    "METABASE_READONLY_DB_PASSWORD": secrets.token_urlsafe(24),
}.items():
    s = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", s)
p.write_text(s, encoding="utf-8")
PY
    ok ".env created with freshly generated secrets"
    info "LLM_API_KEY is left blank - Stage 7 degrades to 'no explanations',"
    info "never to 'no decisions'. Set it in .env for hypotheses."
fi


# ---------------------------------------------------------------------------
say "Starting the services"
# ---------------------------------------------------------------------------
docker compose up -d --build
info "waiting for five health checks (Metabase takes 1-3 minutes)..."
# Metabase rebuilds its application schema on a fresh volume, and on a host with
# little memory to spare it can crash once and succeed on the restart, which
# pushes a first run past ten minutes. The wait is generous so that a slow but
# recovering start is not reported as a failure.
deadline=$(( $(date +%s) + 1200 ))
until [[ "$(docker compose ps --format '{{.Health}}' 2>/dev/null | grep -c healthy)" == "5" ]]; do
    (( $(date +%s) < deadline )) || die "services did not become healthy: $(docker compose ps --format '{{.Service}} {{.Health}}' | tr '\n' ';')"
    sleep 5
done
ok "postgres, mock-api, analytics-service, n8n and metabase are healthy"


# ---------------------------------------------------------------------------
say "Applying database migrations"
# ---------------------------------------------------------------------------
./database/migrate.sh --test >/tmp/salesops-migrate.log 2>&1 \
    || { tail -30 /tmp/salesops-migrate.log; die "migrations or schema tests failed."; }
applied=$(grep -c '^  -> V' /tmp/salesops-migrate.log || true)
checks=$(grep -oE '^ +[0-9]+ \| +[0-9]+ \| +0' /tmp/salesops-migrate.log | head -1 | awk '{print $1}')
ok "${applied} migrations applied; ${checks:-all} schema checks passed"


# ---------------------------------------------------------------------------
say "Importing n8n workflows"
# ---------------------------------------------------------------------------
./n8n/import-workflows.sh --activate >/tmp/salesops-import.log 2>&1 \
    || { tail -20 /tmp/salesops-import.log; die "workflow import failed."; }
ok "10 workflows imported and their schedules activated"


# ---------------------------------------------------------------------------
say "Injecting the demonstration incident"
# ---------------------------------------------------------------------------
# The generator emits ordinary business variation and Stage 5 finds some of it,
# but none of it is severe enough to reach the top of the Stage 6 ladder. The
# incident the dashboards are built around is therefore injected deliberately -
# by rewriting real order rows, never by setting a flag, so detection still has
# to rediscover it.
#
# V008 grades 'critical' only on a severe revenue impact combined with at least
# one severe operational signal, so this is two injections on one date: a price
# collapse and a refund spike.
#
# The date is relative because the order book anchors its 90-day window to today
# unless MOCK_API_HISTORY_END_DATE pins it. A literal would drift out of the
# window. provision.sh reads this same value for the investigation dashboard's
# default, so the panel and the data always agree.
#
# Resolved once and then written to .env, because "ten days ago" is a different
# day tomorrow. The order book keeps the incident where it was injected, so a
# recomputed date would drift off it overnight and every consumer - the
# dashboard default, the test fixtures - would point at an ordinary Tuesday.
INCIDENT_DATE="${SALESOPS_INCIDENT_DATE:-}"
if [[ -z "$INCIDENT_DATE" ]]; then
    INCIDENT_DATE="$(grep -E '^SALESOPS_INCIDENT_DATE=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
fi
if [[ -z "$INCIDENT_DATE" ]]; then
    INCIDENT_DATE="$(date -d '10 days ago' +%F 2>/dev/null || date -v-10d +%F)"
fi
export SALESOPS_INCIDENT_DATE="$INCIDENT_DATE"
mock_api="http://localhost:${MOCK_API_HOST_PORT:-8000}"

# Injections compound - applying the same one twice stacks the effect - so a
# second bootstrap run must not deepen the incident it already created.
if curl -sf "${mock_api}/admin/anomalies" | grep -q "\"${INCIDENT_DATE}\""; then
    info "already present for ${INCIDENT_DATE}; injections compound, so skipping"
else
    inject_anomaly() {
        curl -sf -X POST "${mock_api}/admin/inject-anomaly" \
            -H 'Content-Type: application/json' \
            -d "{\"type\":\"$1\",\"date\":\"${INCIDENT_DATE}\",\"severity\":$2}" >/dev/null
    }
    inject_anomaly revenue_drop 0.55 || die "could not inject the revenue drop"
    inject_anomaly refund_spike 0.60 || die "could not inject the refund spike"
fi

# Persisted so that provision.sh, the Stage 11 fixtures and a bootstrap run
# tomorrow all name the same day.
if grep -qE '^SALESOPS_INCIDENT_DATE=' .env 2>/dev/null; then
    sed -i.bak "s|^SALESOPS_INCIDENT_DATE=.*|SALESOPS_INCIDENT_DATE=${INCIDENT_DATE}|" .env
    rm -f .env.bak
else
    printf '\n# The date bootstrap.sh injected the demonstration incident into.\n# Written here so it survives the calendar; delete it to pick a new day.\nSALESOPS_INCIDENT_DATE=%s\n' \
        "$INCIDENT_DATE" >> .env
fi
ok "critical incident staged for ${INCIDENT_DATE}"


# ---------------------------------------------------------------------------
say "Running the pipeline once"
# ---------------------------------------------------------------------------
# In dependency order. Each stage reads what the one before it wrote, so this is
# the schedule compressed into one pass.
#
# N8N_RUNNERS_BROKER_PORT moves the one-shot execution's task broker off 5679,
# which the running instance already holds.
if [[ "$SKIP_PIPELINE" == true ]]; then
    info "skipped (--no-pipeline)"
else
    run_workflow() {
        local id="$1" label="$2"
        printf '      %-26s' "$label"
        if MSYS_NO_PATHCONV=1 docker compose exec -T -e N8N_RUNNERS_BROKER_PORT=5690 n8n \
               n8n execute --id="$id" >>/tmp/salesops-pipeline.log 2>&1; then
            printf '%s✓%s\n' "$green" "$reset"
        else
            # A stage that fails is recorded as a failed run and the pipeline
            # carries on: that is the designed behaviour, not a bootstrap error.
            # Stage 7 fails by design when LLM_API_KEY is unset.
            printf '%s- see /tmp/salesops-pipeline.log%s\n' "$dim" "$reset"
        fi
    }
    : >/tmp/salesops-pipeline.log
    run_workflow salesopsIngest001 "orders ingestion"
    run_workflow salesopsFxSync001 "FX rates"
    run_workflow salesopsKpiRfr001 "KPI refresh"
    run_workflow salesopsDetect001 "anomaly detection"
    run_workflow salesopsDecide01  "anomaly decision"
    run_workflow salesopsLlmRca01  "LLM root cause"

    # Stage 7 works through candidates in date order, and free LLM tiers meter
    # tokens per minute rather than per day - a 90-day backlog can exhaust the
    # quota before it reaches the incident this demo is built around. Ask for
    # that one by name. It is a no-op when the scheduled run already covered it,
    # and it runs before Stage 8 because the review queue snapshots whether a
    # hypothesis existed at the moment it queued the review.
    printf '      %-26s' "incident hypothesis"
    if curl -sf -X POST "http://localhost:${ANALYTICS_API_HOST_PORT:-8001}/anomalies/analyze" \
           -H 'Content-Type: application/json' \
           -d "{\"decision_version\":\"stage6-v1\",\"regenerate\":false,\"dates\":[\"${INCIDENT_DATE}\"]}" \
           --max-time 300 >>/tmp/salesops-pipeline.log 2>&1; then
        printf '%s✓%s\n' "$green" "$reset"
    else
        # No key, no quota, or no model. Stage 6 has already decided and Stage 8
        # will still escalate; the incident simply arrives without a hypothesis.
        printf '%s- no hypothesis for %s%s\n' "$dim" "$INCIDENT_DATE" "$reset"
    fi

    # The same quota problem reaches the notified anomalies. A notification
    # carries a hypothesis only if one exists by the time Stage 8 runs, and the
    # minor days are last in date order, so on a free tier they never get one.
    # Asked for by name here for the same reason as the incident above.
    minor_pending="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-salesops}" \
        -d "${POSTGRES_DB:-salesops}" -qtA -c "
        SELECT d.calendar_date FROM salesops.anomaly_decisions d
        LEFT JOIN salesops.anomaly_hypotheses h USING (anomaly_id)
        WHERE d.is_anomaly AND d.severity = 'minor' AND d.routing = 'auto_notify'
          AND h.hypothesis_id IS NULL
        ORDER BY d.anomaly_score DESC LIMIT 1;" 2>/dev/null | tr -d '\r' || true)"
    if [[ -n "$minor_pending" ]]; then
        printf '      %-26s' "notified hypothesis"
        if curl -sf -X POST "http://localhost:${ANALYTICS_API_HOST_PORT:-8001}/anomalies/analyze" \
               -H 'Content-Type: application/json' \
               -d "{\"decision_version\":\"stage6-v1\",\"regenerate\":false,\"dates\":[\"${minor_pending}\"]}" \
               --max-time 300 >>/tmp/salesops-pipeline.log 2>&1; then
            printf '%s✓%s\n' "$green" "$reset"
        else
            printf '%s- none for %s%s\n' "$dim" "$minor_pending" "$reset"
        fi
    fi

    run_workflow salesopsNotify01  "notification and review"
    run_workflow salesopsRemed01   "remediation execution"
    run_workflow salesopsMaint01   "operational maintenance"
    ok "pipeline executed in dependency order"

    # The critical incident is injected, so its date is chosen. The other three
    # are not: which ordinary days rise to major, which fall to minor, and which
    # stay unremarkable are all properties of the generated series. The
    # warehouse-backed suites need one of each to assert against, and literals
    # would be wrong the first time anyone rebuilt the volumes - so they are read
    # back here and recorded beside the injected date.
    read_back() {
        docker compose exec -T postgres psql -U "${POSTGRES_USER:-salesops}" \
            -d "${POSTGRES_DB:-salesops}" -qtA -c "$1" 2>/dev/null | tr -d '\r' || true
    }
    record() {
        local key="$1" value="$2" note="$3"
        [[ -n "$value" ]] || return 0
        if grep -qE "^${key}=" .env 2>/dev/null; then
            sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
        else
            printf '\n# %s\n%s=%s\n' "$note" "$key" "$value" >> .env
        fi
        printf '      %-26s %s\n' "${key#SALESOPS_}" "$value"
    }

    record SALESOPS_MAJOR_DATE "$(read_back "
        SELECT calendar_date FROM salesops.anomaly_decisions
        WHERE is_anomaly AND severity = 'major'
          AND decision_reason_code = 'HIGH_REVENUE_IMPACT'
        ORDER BY anomaly_score DESC, calendar_date LIMIT 1;")" \
        "The highest-scoring day graded major - a rung below the injected critical."

    record SALESOPS_MINOR_DATE "$(read_back "
        SELECT calendar_date FROM salesops.anomaly_decisions
        WHERE is_anomaly AND severity = 'minor' AND routing = 'auto_notify'
        ORDER BY anomaly_score DESC, calendar_date LIMIT 1;")" \
        "A real anomaly too small to be worth a person - the only kind that is notified."

    # The control day, and every clause here is load-bearing. A Sunday, because
    # the baseline is per weekday and "normal" has to be normal against the
    # right comparison set. Not an anomaly, obviously. Scored against a
    # day-of-week baseline rather than a fallback. And ABOVE its own weekday
    # median while sitting well under the trailing seven-day mean - which is the
    # whole argument for calendar awareness: a blind moving average calls this
    # day a collapse, and the detector correctly does not.
    record SALESOPS_NORMAL_DATE "$(read_back "
        SELECT calendar_date FROM salesops.anomaly_daily
        WHERE NOT is_anomaly AND baseline_kind = 'day_of_week'
          AND EXTRACT(DOW FROM calendar_date) = 0
          AND revenue_deviation_pct > 0
        ORDER BY calendar_date DESC LIMIT 1;")" \
        "An ordinary Sunday: under the trailing mean, above its own weekday median."
fi


# ---------------------------------------------------------------------------
say "Provisioning the dashboards"
# ---------------------------------------------------------------------------
./metabase/provision.sh >/tmp/salesops-metabase.log 2>&1 \
    || { tail -20 /tmp/salesops-metabase.log; die "dashboard provisioning failed."; }
ok "4 dashboards and 31 cards, served through a read-only role"


# ---------------------------------------------------------------------------
say "Ready"
# ---------------------------------------------------------------------------
docker compose exec -T postgres psql -U "${POSTGRES_USER:-salesops}" \
    -d "${POSTGRES_DB:-salesops}" -q -c "
SELECT
  (SELECT count(*) FROM salesops.fact_orders)                            AS orders,
  (SELECT count(*) FROM salesops.kpi_daily)                              AS kpi_days,
  (SELECT count(*) FROM salesops.anomaly_daily WHERE is_anomaly)         AS anomalies,
  (SELECT count(*) FROM salesops.anomaly_decisions
    WHERE decision = 'action_required')                                  AS actionable,
  (SELECT count(*) FROM salesops.anomaly_hypotheses)                     AS hypotheses,
  (SELECT count(*) FROM salesops.review_queue)                           AS reviews;" || true

cat <<EOF

  ${bold}Metabase${reset}   http://localhost:3000    the four dashboards
  ${bold}n8n${reset}        http://localhost:5678    the ten workflows
  ${bold}Analytics${reset}  http://localhost:8001/docs
  ${bold}Mock API${reset}   http://localhost:8000/docs

  Credentials for Metabase are in .env (METABASE_ADMIN_EMAIL / _PASSWORD).

  ${bold}The review queue is deliberately still pending.${reset}
  Nothing was approved, because approving is a human act and this script is not
  a human. That is the whole architecture in one line: the pipeline detected,
  graded, explained and escalated on its own, and then stopped.

  ${bold}To finish the chain yourself${reset} - two calls, two named people, and the
  audit trail will record both:

    R=\$(curl -s "http://localhost:8001/reviews?severity=critical" \\
        | ${PYTHON##*/} -c "import sys,json; print(json.load(sys.stdin)['reviews'][0]['review_id'])")

    curl -s -X POST http://localhost:8001/reviews/\$R/claim \\
      -H 'Content-Type: application/json' -d '{"actor":"you@example.com"}'

    curl -s -X POST http://localhost:8001/reviews/\$R/approve \\
      -H 'Content-Type: application/json' \\
      -d '{"actor":"you@example.com","action_type":"request_refund_review",
           "resolution":"confirmed","notes":"Confirmed the refund spike."}'

  That confirms the anomaly and PROPOSES an action. It does not run it. Running
  it is a separate decision, by design, and normally a different person - so the
  record can show a reviewer who confirmed a finding and then rejected the
  response somebody proposed for it:

    curl -s -X POST http://localhost:8001/remediation/<id>/approve \\
      -H 'Content-Type: application/json' -d '{"actor":"someone.else@example.com"}'
    curl -s -X POST http://localhost:8001/remediation/<id>/execute \\
      -H 'Content-Type: application/json' -d '{"actor":"someone.else@example.com"}'

  Then open ${bold}Anomaly Investigation${reset}. It defaults to ${bold}${INCIDENT_DATE}${reset} and now traces
  that incident from the orders through to the action you authorised - with both
  names on it, and the language model's contribution clearly marked as the only
  unverified line in the chain.

EOF
