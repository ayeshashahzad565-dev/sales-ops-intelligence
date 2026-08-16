# Architecture

How the eleven stages fit together, what each one is allowed to do, and why
the ordering is the whole design.

[← back to the README](../README.md)

---

## What is built

**Stage 0 — local environment.** A reproducible development environment,
startable from a fresh clone with one command: five services on one Docker
network, with health checks that make their state obvious.

**Stage 1 — the data source.** The Mock Sales/Orders API serves a seeded 90-day
synthetic order history and lets an operator append fresh orders and inject
controlled anomalies. [Full documentation →](../mock-api/README.md)

**Stage 2 — the warehouse schema.** A star schema with a staging layer in the
`salesops` schema: dimensions seeded, `fact_orders` keyed on `order_id` for
idempotent loads, `NUMERIC` money throughout, and two base views.
[Full documentation →](../database/README.md)

**Stage 3 — the ingestion pipeline.** An n8n workflow that pulls a date window
from the Mock API, lands every payload in staging, validates in SQL,
dead-letters what fails, resolves dimensions, and loads `fact_orders`
idempotently — with a run ledger and an error-handling workflow.
[Full documentation →](../n8n/README.md)

**Stage 4 — FX and the KPI layer.** Real exchange rates from Frankfurter, with a
bounded carry-forward for the days the market is closed, and a materialised
`kpi_daily` table rebuilt atomically. All 3,880 orders now carry a rate and all
90 KPI dates report 100% FX completeness.

**Stage 5 — statistical anomaly detection.** A Python service that finds unusual
days in `kpi_daily` using robust, calendar-aware statistics: median/MAD baselines
built from prior observations of the *same weekday*, four weighted signals, and a
deterministic score. No ML, no randomness, no LLM.
[Full documentation →](../analytics-service/README.md)

**Stage 6 — the deterministic decision layer.** SQL rules that turn statistical
evidence into business severity: expected-vs-actual revenue in dollars, three
severity levels, routing, notification permission and human-review requirement —
all decided before any language model is involved, and none of it writable by
one. [Full documentation →](../database/README.md#the-decision-layer--stage-6)

**Stage 7 — LLM root-cause hypotheses.** The first stage permitted to call a
language model, and the only one that could be switched off without the pipeline
losing a decision. It receives Stage 6's verdict as settled context and produces
a structured, evidence-grounded explanation: a primary hypothesis, the metrics
supporting it, alternatives, what evidence is missing, and what a human should
check next. It explains; it does not judge.
[Full documentation →](../analytics-service/README.md#stage-7--root-cause-hypotheses)

**Stage 8 — delivery and human review.** Minor anomalies are notified
automatically; major and critical ones are queued for a person, with a state
machine enforced in the database and a full audit trail. Notifications separate
what was **observed** from what is **hypothesised** from what is **not
confirmed**, so nobody mistakes a model's guess for a finding. It delivers; it
takes no action.
[Full documentation →](../analytics-service/README.md#stage-8--delivery-and-human-review)

**Stage 9 — human-approved remediation.** The only stage that executes anything,
and the one with the least discretion. A reviewer approves an anomaly and names
a response; somebody authorises that specific response; something executes it.
Three separate acts, three separate audit events. The database refuses an action
without an approved review, an action type the severity does not permit, and any
attempt to execute one twice.
[Full documentation →](../analytics-service/README.md#stage-9--human-approved-remediation)

**Stage 10 — operational reliability.** Not a pipeline stage: the parts that let
the other nine run unattended. Runs abandoned mid-flight are closed with a
machine-readable reason, dead-letter batches can be replayed without ever making
the original failure look like it never happened, settled staging data ages out
while `pending` and `failed` rows never do, and a single view reports pipeline
health with the numbers behind every verdict. It recovers; it does not re-run.
[Full documentation →](../analytics-service/README.md#stage-10--operational-reliability)

**Stage 11 — the dashboard layer.** Four read-only Metabase dashboards over
fifteen views, adding no behaviour and no arithmetic. Every panel carries the
*layer* its content came from, and exactly one of the eight layers is
model-generated — enforced by a `CHECK` constraint, not a convention. The
executive page says whether a hypothesis exists; only the investigation page
says what one claims, beneath the evidence it is trying to explain and behind a
warning. Metabase connects as a role with `SELECT` and no `EXECUTE`, and the
provisioning script proves that before it does anything else.
[Full documentation →](../metabase/README.md)

> **Stage 6 decides. Stage 7 explains. Stage 8 delivers. Stage 9 executes what a
> human approved. Stage 10 keeps the whole machine recoverable. Stage 11 shows
> all of it without letting any layer impersonate another.** No stage after
> 6 can modify a Stage 6 decision, nothing executes that a person did not
> authorise by name, and nothing in Stage 10 repeats work it merely found stuck.
>
> The ordering is the architecture: *the LLM proposes, deterministic rules
> decide, a human approves, and only then does anything run.* The deciding half
> was finished, and tested, before the model was ever called.


---

## The services

| Service | Image | Host URL | Role |
|---|---|---|---|
| **postgres** | `postgres:17-alpine` | `localhost:5432` | System of record. Hosts three databases (below). |
| **mock-api** | built from `./mock-api` | http://localhost:8000/docs | Simulated upstream order-management REST API. |
| **n8n** | `docker.n8n.io/n8nio/n8n` | http://localhost:5678 | Workflow orchestrator. |
| **metabase** | `metabase/metabase` | http://localhost:3000 | BI / dashboard layer. |
| **analytics-service** | built from `./analytics-service` | http://localhost:8001/docs | Stage 5 detection, Stage 7 hypotheses, Stage 8 delivery and review, Stage 9 remediation. |

#### Why three databases on one Postgres

`database/init/00-create-service-databases.sql` creates two additional
databases on first startup:

| Database | Contains |
|---|---|
| `salesops` | Analytics data — staging, dimensions, facts, KPIs, anomalies, audit log |
| `n8n` | n8n's own state: workflows, credentials, execution history |
| `metabase` | Metabase's own state: dashboards, questions, users |

n8n defaults to SQLite and Metabase to H2; both warn against it and both hide
their state in opaque files. Giving each its own database on the *same*
container costs nothing extra to run, makes state inspectable and backupable
with standard Postgres tooling, and keeps a hard boundary between application
state and analytics data. When Metabase is later pointed at `salesops`, that is
unambiguously a read-only reporting connection.


---

## Database architecture

Inside the `salesops` database, the `salesops` schema holds a star schema with a
staging layer in front of it. [Full documentation →](../database/README.md)

```
Mock API ──► raw_orders_staging ──► validate ──► fact_orders ──► base views
             (JSONB, no FKs,         in SQL      (PK order_id,   (daily / regional)
              accepts anything)        │          NUMERIC money)
                                       ▼
                              failed + error_message
                                (dead letter, replayable)
```

| | |
|---|---|
| **Staging** | `raw_orders_staging` — raw JSONB payloads, no foreign keys, so invalid rows are still storable |
| **Dimensions** | `dim_date`, `dim_region`, `dim_product`, `dim_channel`, `dim_customer` |
| **Reference** | `exchange_rates` — real Frankfurter rates plus the `USD → 1.0` identity |
| **KPI** | `kpi_daily` — one row per trading day, rebuilt wholesale; Stage 5's input |
| **Detection** | `anomaly_daily` — statistical evidence per date, keyed by detector version |
| **Decision** | `anomaly_decisions` + reason codes — deterministic severity, routing and flags |
| **Hypotheses** | `anomaly_hypotheses` — LLM explanations; the only model-written table |
| **Delivery** | `notifications` + attempts, `review_queue` + events — who was told, who must look |
| **Remediation** | `remediation_actions` + attempts + events — what a human authorised, and whether it ran |
| **Operations** | `operational_events` (append-only), `operational_config`, `ingestion_replays` — what recovery did, and why |
| **Observability** | `ingestion_runs` (shared run ledger), `pipeline_errors` (caught failures) |
| **Fact** | `fact_orders` — one row per order line, five foreign keys, `order_id` as primary key |
| **Views** | `daily_sales_base`, `regional_sales_base` |

Four decisions worth knowing before reading the SQL:

- **`order_id` is the primary key of `fact_orders`**, which makes the whole
  pipeline safe to re-run: `ON CONFLICT (order_id) DO NOTHING` means a repeated
  window double-counts nothing.
- **All money is `NUMERIC`**, never floating point. The schema test fails if a
  float column ever appears.
- **USD amounts are generated columns**, computed from the local amount and
  `exchange_rate_to_usd`. No real rate means `NULL` — structurally, not by
  convention — and attaching a rate later backfills them automatically.
- **Source labels never become free text in the fact table.** `'EMEA'` resolves
  to a `region_id` foreign key; an unrecognised region produces no fact row.
- **The LLM has exactly one writable table**, and no column in it for a severity,
  a routing value or a decision. A trigger rejects any hypothesis whose recorded
  Stage 6 verdict disagrees with the decision it explains.
- **Remediation cannot exist without a human approval.** Which action is
  permitted at which severity is a foreign key into reference data, so an
  ineligible action is an integrity error rather than a missed check — and
  `executed` has no outgoing transition, so nothing runs twice.
- **Recovery cannot rewrite history.** `operational_events` refuses `UPDATE` and
  `DELETE` by trigger, a replay never modifies the row it is replaying, and
  retention can never reach a `pending` or `failed` staging row.

```powershell
.\database\migrate.ps1 -Test        # apply migrations, then run 277 schema checks
```

```bash
./database/migrate.sh --test
```


---

## The pipeline

```
Mock API ──► Orders Ingestion ──► raw_orders_staging ──► fact_orders ──► views
  hourly        (12 nodes)          (JSONB, batch_id)    (idempotent)      │
                     │                      │                             │
                     │                      └─► failed + error_message    │
                     │                             (dead letter)          │
Frankfurter ─► FX Rate Sync ────────────────────────────► attaches rates ─┤
  daily 05:00   (9 nodes, carry-forward)                                  │
                                                                          ▼
               KPI Daily Refresh ──────────────────────────────────► kpi_daily
                 daily 06:00 (atomic full rebuild)                        │
                                                                          ▼
               Statistical Anomaly Detection ──► analytics-service ──► anomaly_daily
                 daily 07:00                     (robust, calendar-aware)      │
                                                                               ▼
               Deterministic Anomaly Decision ──────────────────► anomaly_decisions
                 daily 07:30 (SQL rules only)                    + decision reasons
                     │                                                    │
                     │                                    severity · routing · may
                     │                                    we notify · human needed
                     │                                                    │
                     │                                    actionable only ▼
               LLM Root Cause Analysis ──────────────────────► anomaly_hypotheses
                 daily 08:00 (the ONLY LLM call)          explanation, never a verdict
                     │                                                    │
                     │                                     minor ─┐  ┌─ major/critical
                     │                                            ▼  ▼
               Notification & Review Routing ──────────► notifications · review_queue
                 daily 08:30 (delivers, never acts)      "someone was told" · "someone must look"
                     │                                                    │
                     │                                    a human approves ▼ and names an action
               Remediation Execution ──────────────────────► remediation_actions
                 daily 09:00 (runs only what          proposed → approved → executed,
                 a person authorised)                 once, with every step attributed
                     │
                     ▼
               Operational Maintenance ─────────────► operational_events · health
                 daily 09:30 (recovers what           stale runs closed, dead letters
                 got stuck; re-runs nothing)          replayable, staging aged out
                     │
                     ▼
              ingestion_runs                    Pipeline Error Handler
         (shared ledger, keyed by source)   (Error Trigger → pipeline_errors
                                             → marks the run failed)

  ─────────────────────────────────────────────────────────────────────────
              Metabase ◄── salesops_readonly ◄── V013 presentation views
                (4 dashboards, SELECT only, no EXECUTE on anything volatile)
```

**n8n orchestrates. PostgreSQL enforces integrity. Raw payloads are preserved,
so every transformed record stays traceable.** No business rule lives in an n8n
expression — validation, dimension resolution and idempotency are all SQL, which
can be reviewed and tested without n8n running.

```bash
./n8n/import-workflows.sh --activate       # credential + 10 workflows + schedules
python n8n/tests/test_ingestion_sql.py     # 34 checks - ingestion
python n8n/tests/test_stage4_fx_kpi.py     # 66 checks - FX and KPI
python n8n/tests/test_stage6_decisions.py  # 79 checks - decisions
cd analytics-service && python -m pytest   # 555 checks - detection through the dashboards
```

Idempotency is the load-bearing property: `fact_orders` is keyed on `order_id`
and inserts use `ON CONFLICT DO NOTHING`, so overlapping windows and re-runs
cannot double-count. Full detail in [n8n/README.md](../n8n/README.md).


---

## The dashboards

```bash
./metabase/provision.sh        # build or update, idempotent
```

Then open <http://localhost:3000>.

| Dashboard | What it answers |
|---|---|
| **Executive Overview** | Revenue, orders, AOV, refund rate against baseline; anomalies by severity; what is actionable; who is waiting on whom; pipeline health |
| **Anomaly Investigation** | One incident, layer by layer, in reading order. Defaults to the injected incident |
| **Operational Health** | Per-pipeline runs, stale and overdue items, replays, unknown executions |
| **Audit Trail** | Every recorded transition: who, when, from what state to what, under which version |

Four dashboards, 31 cards, fifteen views and one reference table, and **zero new
arithmetic**. Every number was already stored by the stage that owns it.
[Full documentation →](../metabase/README.md)

#### Following the injected incident

`bootstrap.sh` injects one incident - a price collapse and a refund spike on the
same day - and it runs the whole length of the platform. The investigation
dashboard shows it as ten steps:

```
 1 orders               58 orders, 111 units                       observed fact
 2 kpi                  net revenue 2,243.43 USD, AOV 38.68 USD    observed fact
 3 anomaly              score 14.478, dominant revenue, 3 signals  statistical
 4 decision             critical / human_review /                  deterministic
                        CRITICAL_COMBINED_IMPACT
 5 hypothesis           unverified, stated confidence medium       ▲ LLM
 6 notification         not notified (routing = human_review)      human review
 7 review               approved            — dana@finance         human review
 8 remediation          request_refund_review — priya@revops       approved
 9 execution            executed, reference local-record-1         completed
10 operational outcome  0 events recorded against this action      operational
```

Step 5 is the only line a language model wrote, and step 4 happened before it.

The figures are from one run. The generator anchors its ninety days to today, so
your dates and totals will differ; `bootstrap.sh` records the day it injected
into as `SALESOPS_INCIDENT_DATE` and the dashboard defaults to it.


---

## Deterministic and probabilistic layers

The platform contains exactly one probabilistic component, and it is downstream
of every decision.

| | Deterministic | Probabilistic |
|---|---|---|
| **What** | ingestion, FX, KPIs, robust z-scores, severity, routing, reason codes, eligibility, health, ageing, retention | one LLM call per actionable anomaly |
| **Reproducible** | yes — same inputs, same outputs, forever | no |
| **Can change a verdict** | it *is* the verdict | never |
| **Audited** | every threshold is a row in a table | prompt version, model, digest, tokens and latency are stored; the claim is not verified |
| **Runs** | always | only when Stage 6 already said `action_required` |

Stage 5 says a day was unusual. **Stage 6 decides whether it matters**, from
thresholds in `decision_thresholds`, and writes a reason code for each. Only
then is the model called — and it is handed the decision as *input*, with no
path to write to `anomaly_decisions`.

The ordering is the architecture. The deciding half was finished, and tested,
before the model was ever called.

**On the dashboards** this is a column, not a colour. Every presentation view
carries its layer; the layer table has a `CHECK` constraint making
`is_model_generated` true for exactly one of the eight; `llm_verified` is `false`
on every row that has one, because nothing here verifies a hypothesis.


---

## Human in the loop

Four separate gates, deliberately not collapsible into one call:

1. **Stage 6 decides whether a person is needed** — `human_review_required` is a
   stored column, and `auto_notify` versus `human_review` is a routing decision
   made by SQL before anyone is told anything.
2. **A reviewer claims and resolves.** `resolved` means *reviewed and closed
   without remediation*, and the code refuses to read it as consent — approving
   is a different transition with a different word.
3. **Approving the review** confirms the anomaly and proposes an action. The
   action is `proposed`. Nothing has run.
4. **Authorising the action** is a second call, by a second name, answering a
   different question: not "is this real?" but "is this the response?". Only
   then can it execute, once, via a conditional `UPDATE` that no race can
   double.

Three different mechanisms enforce it, on purpose — authorisation is a trigger,
eligibility is a foreign key `(policy_version, severity, action_type)` into
reference data, and exactly-once execution is a conditional `UPDATE`. One
function doing all three would be one function to get wrong.

Stage 10 never overrides any of it. A crashed execution becomes
`execution_unknown` — a state with no path back to `executing` — because
re-running might do the work twice and failing it might claim it never happened.
Reconciliation is a person's decision.
