# n8n orchestration layer

Ten workflows: one that loads orders into the warehouse, one that fetches
exchange rates, one that rebuilds the daily KPI table, one that scores those KPIs
for anomalies, one that grades and routes the anomalies, one that asks a model to
explain the ones that matter, one that delivers the result to whoever needs it,
one that carries out what a human then approved, one that cleans up after all of
them — and one that records it when something breaks.

> **Stage 5 determines statistical anomaly evidence. Stage 6 determines business
> severity and routing. Stage 7 generates explanatory hypotheses. Stage 7 cannot
> modify Stage 6 decisions.** Only one workflow calls a model, it runs last, and
> it writes to exactly one table that contains no severity, routing or decision
> the model chose.

The division of labour is deliberate and holds for the rest of the project:

```
n8n         orchestration - schedule, HTTP, control flow, retries, run bookkeeping
PostgreSQL  deterministic data integrity, transformation, severity and routing
Python      statistical analysis                                 (Stage 5)
LLM         explanation, never a verdict                         (Stage 7)
```

n8n decides *when* and *in what order*. PostgreSQL decides *what is true*. No
business rule lives in an n8n expression, because an expression cannot be tested
without n8n and cannot be reviewed as SQL.

---

## Workflows

| Workflow | Id | Trigger |
|---|---|---|
| **Orders Ingestion** | `salesopsIngest001` | Schedule (hourly) + Manual Run |
| **FX Rate Sync** | `salesopsFxSync001` | Schedule (daily 05:00) + Manual Run |
| **KPI Daily Refresh** | `salesopsKpiRfr001` | Schedule (daily 06:00) + Manual Run |
| **Statistical Anomaly Detection** | `salesopsDetect001` | Schedule (daily 07:00) + Manual Run |
| **Deterministic Anomaly Decision** | `salesopsDecide01` | Schedule (daily 07:30) + Manual Run |
| **LLM Root Cause Analysis** | `salesopsLlmRca01` | Schedule (daily 08:00) + Manual Run |
| **Notification & Review Routing** | `salesopsNotify01` | Schedule (daily 08:30) + Manual Run |
| **Remediation Execution** | `salesopsRemed01` | Schedule (daily 09:00) + Manual Run |
| **Operational Maintenance** | `salesopsMaint01` | Schedule (daily 09:30) + Manual Run |
| **Pipeline Error Handler** | `salesopsErrors001` | Error Trigger |

They are separate workflows, not stages of one, so each is independently
observable and retryable: an FX outage does not stop order ingestion, and a KPI
rebuild can be re-run without re-fetching anything.

### One run ledger, shared

All nine scheduled pipelines write to `salesops.ingestion_runs`, told apart by
`source` (`mock-sales-api`, `frankfurter`, `kpi-refresh`, `anomaly-detector`,
`anomaly-decision`, `llm-root-cause`, `notification-router`,
`remediation-executor`, `operational-maintenance`), plus `ingestion-replay` for
replays. One table answers "did everything run, and did it finish?" — and since
Stage 10, one of those pipelines answers "...and did any of them stop halfway?".

The consequence, which is load-bearing: **any query deriving a window from
`max(window_to)` must filter on its own `source`.** Orders Ingestion was updated
for exactly this reason when Stage 4 started sharing the table — without the
filter it would happily adopt the FX sync's window as its own. There is a test
that plants a foreign 2027 window and proves the ingestion window ignores it.

`status` means the same thing across all nine:

| Status | Meaning |
|---|---|
| `running` | In flight, or crashed before finishing |
| `success` | Completed, output fully trustworthy |
| `partial` | Completed, but the output is **not** fully trustworthy — records were rejected (ingestion), the provider returned nothing while orders waited (FX), some date lacks full FX coverage (KPI), or a KPI row could not be scored (detection) |
| `failed` | The pipeline errored. `error_message` says how |

### Orders Ingestion

```
Schedule - Hourly ┐
                  ├─► Create Batch Context ─► Fetch Orders ─► Validate API Response
Manual Run ───────┘         (Postgres)          (HTTP)            (Code)
                                                                     │
   ┌─────────────────────────────────────────────────────────────────┘
   ▼
Stage Raw Orders ─► Validate Orders ─► Resolve Customers ─► Insert Facts
   (Postgres)         (Postgres)          (Postgres)         (Postgres)
                           │                                     │
                           ▼                                     ▼
                    failed + reason                   Ensure USD Identity Rate
                    (dead letter)                                │
                                                                 ▼
                                                       Attach Available FX
                                                                 │
                                                                 ▼
                                                     Finalize Ingestion Run
```

Ten working nodes, one query each. Every step is a named thing that either
happened or did not, which is what makes a failed execution readable in the n8n
UI without opening a single node.

| Node | Does |
|---|---|
| **Create Batch Context** | Opens the `ingestion_runs` row as `running` and computes this run's date window |
| **Fetch Orders** | `GET http://mock-api:8000/orders?from=&to=` — 3 attempts, 5s apart |
| **Validate API Response** | Shape check on the JSON. Throws on a malformed 200 |
| **Stage Raw Orders** | One set-based `INSERT` of the whole payload array into `raw_orders_staging` |
| **Validate Orders** | Applies the business rules; dead-letters failures with every reason |
| **Resolve Customers** | Upserts `dim_customer` as a late-arriving dimension |
| **Insert Facts** | Resolves dimensions by natural key, inserts `ON CONFLICT DO NOTHING` |
| **Ensure USD Identity Rate** | Writes `USD → 1.0` into `exchange_rates` for the window |
| **Attach Available FX** | Applies whatever rates already exist (exact date). Anything left over is picked up by the FX Rate Sync's carry-forward pass |
| **Finalize Ingestion Run** | Derives `success` vs `partial` from the counts |

### FX Rate Sync

```
Schedule - Daily 05:00 ┐
                       ├─► Open FX Sync Run ─► Currencies To Sync ─► Fetch Rates
Manual Run ────────────┘      (Postgres)          (Postgres)          (HTTP ×N)
                                                                          │
        Store Rates ─► Summarise Fetch ─► Attach FX To Orders ─► Finalize FX Sync Run
         (Postgres)        (Code)              (Postgres)             (Postgres)
```

| Node | Does |
|---|---|
| **Open FX Sync Run** | Opens `ingestion_runs` with `source='frankfurter'`; window = span of unconverted order dates, widened back 7 days |
| **Currencies To Sync** | One row per non-USD currency **derived from `fact_orders`**, not hardcoded |
| **Fetch Rates** | `GET api.frankfurter.dev/v1/{from}..{to}?base=<CCY>&symbols=USD` — 3 attempts, 5s apart |
| **Store Rates** | `jsonb_each` over the response, `ON CONFLICT DO NOTHING` |
| **Summarise Fetch** | Collapses the per-currency results to one item so the attach runs once |
| **Attach FX To Orders** | Carry-forward `LATERAL` lookup (below) |
| **Finalize FX Sync Run** | Closes the run, reports `orders_still_pending` |

`base=<CCY>&symbols=USD` gives `rate_to_usd` **directly** — no inversion
arithmetic on our side. USD is excluded: Frankfurter has no `USD/USD` pair, and
the identity rate is written by the ingestion pipeline.

#### The weekend problem

Frankfurter republishes ECB rates, and the ECB quotes **business days only**.
Over the current 90-day window it returns **64 dates — all 26 missing are
weekends.** 359 non-USD orders in this dataset fall on a weekend.

Writing those missing days into `exchange_rates` would mean storing rows the
provider never published. So the table holds **only what was published**, and
the gap is bridged at attachment time:

```sql
CROSS JOIN LATERAL (
    SELECT x.rate_to_usd FROM salesops.exchange_rates x
    WHERE x.currency   = f.currency
      AND x.rate_date <= f.order_date                        -- never reach forward
      AND x.rate_date >= f.order_date - INTERVAL '7 days'    -- staleness bound
    ORDER BY x.rate_date DESC LIMIT 1
) AS r
```

A Saturday order takes Friday's rate. That is standard practice for a closed
market, it is a documented join rule rather than invented data, and it is
bounded — beyond 7 days an order stays visibly pending instead of silently
inheriting a stale rate. Verified live: Sunday 2026-08-09 orders carry the rate
published on Friday 2026-08-07.

Only rows with **no** rate are ever touched, so an applied rate is never
rewritten and the whole workflow is safe to re-run.

#### If Frankfurter is unavailable

The HTTP node retries 3 times and then throws, which stops the run. Nothing is
fabricated, nothing already stored is disturbed, no order is marked converted,
and the error handler records the failure and marks the run `failed`. The KPI
layer keeps reporting the true `fx_completeness_pct` — an outage shows up as
incomplete coverage, never as a revenue collapse.

### KPI Daily Refresh

```
Schedule - Daily 06:00 ┐
                       ├─► Open KPI Refresh Run ─► Rebuild KPI Daily ─► Finalize KPI Refresh Run
Manual Run ────────────┘       (Postgres)            (Postgres)              (Postgres)
```

`Rebuild KPI Daily` is a single statement: `SELECT * FROM salesops.refresh_kpi_daily()`.

The `DELETE` and `INSERT` live inside the function because they must be atomic —
a function body runs in the caller's transaction, so a failure rolls back the
`DELETE` too and the previous contents survive. Two n8n nodes would leave a
window in which `kpi_daily` was empty.

The rebuild is wholesale rather than incremental: the table is one row per
trading day, so a full rebuild costs less than the bookkeeping an incremental
strategy would need, and it cannot drift from `fact_orders`. Re-running it
produces a byte-for-byte identical table, which the tests assert both ways with
`EXCEPT`.

It finishes `partial` when any date still lacks full FX coverage — the run
succeeded, the *data* is incomplete, and conflating those would either hide the
gap or cry wolf.

Runs an hour after the FX sync so the day's rates are already attached. Ordering
is a convenience, not a correctness requirement: a rebuild run before FX simply
reports lower `fx_completeness_pct`.

### Statistical Anomaly Detection

```
Schedule - Daily 07:00 ┐
                       ├─► Open Detection Run ─► Run Detection ─► Validate Detection Result
Manual Run ────────────┘      (Postgres)          (HTTP)                (Code)
                                                                           │
                                                    Finalize Detection Run ┘
                                                          (Postgres)
```

| Node | Does |
|---|---|
| **Open Detection Run** | Opens `ingestion_runs` with `source='anomaly-detector'` |
| **Run Detection** | `POST http://analytics-service:8000/detect` - 3 attempts, 5s apart, 120s timeout |
| **Validate Detection Result** | Shape check; throws if results were dropped |
| **Finalize Detection Run** | Derives status from what was actually processed |

The detector is an HTTP service rather than a script because the n8n container
has no Python runtime. It is a **separate workflow** from ingestion so an FX or
detector outage cannot stop orders loading, and so a detection re-run costs
nothing upstream.

Runs at 07:00, an hour after the KPI refresh, so the day's KPIs are rebuilt
first: **FX 05:00 -> KPI 06:00 -> detection 07:00**.

Count semantics on the run ledger:

| Field | Meaning |
|---|---|
| `records_received` | KPI dates evaluated |
| `records_accepted` | Results written - every evaluated date gets a row, scored or explicitly unscored |
| `records_rejected` | Dates whose KPI row lacked FX coverage. A real data gap, and the only thing that makes the run `partial` |
| `records_duplicate` | Always 0 - results are upserted in place, so there is no "already present" category |

**Dates skipped for insufficient history are not rejections.** Early in a series
there is genuinely nothing to compare against; treating that as a fault would
leave every run permanently `partial` and train the reader to ignore the status.

Success is derived from what the detector processed, never from the workflow
having reached its final node - a run that evaluated nothing is reported
`partial`, not `success`.

Statistical methodology, baseline design and the scoring formula are documented
in [analytics-service/README.md](../analytics-service/README.md).

### Deterministic Anomaly Decision

```
Schedule - Daily 07:30 ┐                        ┌─ true ─► Decide Anomalies ─► Finalize Decision Run
                       ├─► Open Decision Run ─► IF                (Postgres)         (Postgres)
Manual Run ────────────┘      (Postgres)      Stage 5 Ready?
                                                └─ false ─► Abort - Stage 5 Not Ready
                                                                   (Postgres)
```

| Node | Does |
|---|---|
| **Open Decision Run** | Opens `ingestion_runs` with `source='anomaly-decision'`, and reports Stage 5 readiness in the same statement |
| **Stage 5 Ready?** | Routes on that flag — no decision is made against a detection pass that is still in flight or that failed |
| **Decide Anomalies** | `SELECT * FROM salesops.decide_anomalies('stage6-v1')` |
| **Finalize Decision Run** | Derives status from the decisions actually persisted |
| **Abort - Stage 5 Not Ready** | Closes the run as `failed` with the reason, and decides nothing |

Runs at 07:30, after detection: **FX 05:00 → KPI 06:00 → detection 07:00 →
decision 07:30**. It is attached to Stage 5's output, never to ingestion.

**No HTTP service, no Python.** Stage 5 needs a real numerics runtime and a unit
test suite; Stage 6 is business rules over columns that already exist in one
database. Expressing them as SQL keeps them next to the data and inspectable
from `psql`, and avoids a second service whose only job is comparing numbers to
constants. Every rule lives in `decide_anomalies()` rather than in this node,
because the decision upsert and the reason-code rebuild must be atomic — split
across two nodes, a failure between them would leave decisions carrying the
previous run's reason codes, an audit trail contradicting the verdict it
explains.

**The readiness gate.** "Ready" means Stage 5 evidence exists *and* its most
recent recorded run did not fail or hang. A detector run still `running` is
explicitly not ready. A detector that has never run through n8n is allowed,
because it is also reachable over HTTP and by CLI, and refusing there would
block a fresh environment on bookkeeping rather than on the evidence. When the
gate fails, the run is recorded `failed` — deciding anyway would publish
severities derived from a half-written table, with exactly the confidence of a
good run.

Count semantics on the run ledger:

| Field | Meaning |
|---|---|
| `records_received` | Stage 5 evidence rows evaluated |
| `records_accepted` | Decisions written — equal by construction, every evidence row gets one |
| `records_rejected` / `records_duplicate` | Always 0 — decisions are upserted in place |

| Status | When |
|---|---|
| `success` | Every decision rests on measured evidence |
| `partial` | At least one flagged date had no baseline revenue, so its impact could not be measured. Those dates escalate rather than being assumed harmless, but the run says its output rests on incomplete evidence |
| `failed` | The engine returned but wrote nothing over a populated evidence table, or Stage 5 was not ready |

**Dates with insufficient history do not make a run `partial`.** A young
warehouse having no baseline yet is expected, not degraded — same reasoning as
the detection workflow.

The severity model, thresholds and routing rules are documented in
[database/README.md](../database/README.md#the-decision-layer--stage-6).
No LLM is involved, reachable, or required: Stage 7 will read what this
produces, and cannot alter it.

### LLM Root Cause Analysis

```
Schedule - Daily 08:00 ┐                        ┌─ true ─► Analyze Anomalies ─► Finalize Analysis Run
                       ├─► Open Analysis Run ─► IF              (HTTP)              (Postgres)
Manual Run ────────────┘      (Postgres)      Stage 6 Ready?        │
                                                │                   └─ error ─► Fail Analysis Run
                                                └─ false ─► Abort - Stage 6 Not Ready
```

| Node | Does |
|---|---|
| **Open Analysis Run** | Opens `ingestion_runs` with `source='llm-root-cause'`, and reports Stage 6 readiness in the same statement |
| **Stage 6 Ready?** | Routes on that flag — nothing is explained against a decision pass still in flight |
| **Analyze Anomalies** | `POST http://analytics-service:8000/anomalies/analyze` — 2 attempts, 10s apart, 300s timeout |
| **Finalize Analysis Run** | Derives status from the service's own counts; records model and prompt version |
| **Fail Analysis Run** | Closes the run as `failed` when the service call itself fails |
| **Abort - Stage 6 Not Ready** | Closes the run as `failed` without calling the provider |

Runs at 08:00, last in the chain: **FX 05:00 → KPI 06:00 → detection 07:00 →
decision 07:30 → explanation 08:00**. Attached to Stage 6's output, never to
ingestion.

**No API key is in this workflow.** The service reads `LLM_API_KEY` from its own
environment, so no credential enters the workflow JSON, the n8n database, or an
execution history that gets exported when someone shares a workflow.

**`regenerate` is hardcoded false.** A nightly job that silently rewrote
yesterday's reasoning would destroy the audit trail it exists to create.
Regeneration is a deliberate, explicit call.

**Retry is safe because analysis is idempotent.** A second attempt re-analyses
only what did not persist, so a transient network failure costs one retry rather
than a duplicate bill. The long timeout is because this is *one model call per
actionable anomaly*, not one call.

#### Why this workflow closes its own ledger entry

`Analyze Anomalies` uses `onError: continueErrorOutput` and routes failures to
`Fail Analysis Run` rather than throwing. The Pipeline Error Handler still covers
unexpected failures, but it is invoked by n8n and does not fire in every
execution mode — a CLI run, for instance. A run left at `running` forever is
indistinguishable from one still in flight, so the workflow owns the outcome
itself and the result is the same however it was started.

This was found by running the workflow with no API key configured: the service
returned 500, the HTTP node threw, and the ledger row sat at `running`. Now it
reads:

```
status        | failed
error_message | Stage 7 analysis could not run: 500 - LLM_API_KEY is not set...
                No hypotheses were written; Stage 6 decisions are unchanged.
```

| Status | When |
|---|---|
| `success` | Everything eligible was analysed — **or nothing was eligible**. A quiet week is not a fault |
| `partial` | Some analyses failed and some succeeded |
| `failed` | Every attempt failed, the service was unreachable, or Stage 6 was not ready |

Count semantics: `records_received` = anomalies processed, `records_accepted` =
hypotheses written, `records_rejected` = analyses that failed, `records_duplicate`
= already analysed.

Whatever the outcome, **no Stage 6 decision changes.** The anomalies stay
critical, stay routed to a human, stay carrying their reason codes — they are
simply unexplained. The provider abstraction, prompt design, validation rules and
failure semantics are documented in
[analytics-service/README.md](../analytics-service/README.md#stage-7--root-cause-hypotheses).

### Notification & Review Routing

```
Schedule - Daily 08:30 ┐                       ┌─ true ─► Route Anomalies ─► Finalize Routing Run
                       ├─► Open Routing Run ─► IF             (HTTP)             (Postgres)
Manual Run ────────────┘      (Postgres)     Stage 6 Ready?       │
                                               │                  └─ error ─► Fail Routing Run
                                               └─ false ─► Abort - Stage 6 Not Ready
```

| Node | Does |
|---|---|
| **Open Routing Run** | Opens `ingestion_runs` with `source='notification-router'`, and reports Stage 6 readiness |
| **Stage 6 Ready?** | Routes on that flag — nothing is delivered from a decision pass still in flight |
| **Route Anomalies** | `POST http://analytics-service:8000/notifications/process` — 2 attempts, 10s apart, 180s timeout |
| **Finalize Routing Run** | Derives status from the service's counts; also reports the open review backlog |
| **Fail Routing Run** | Closes the run as `failed` when the service call itself fails |
| **Abort - Stage 6 Not Ready** | Closes the run as `failed` without delivering anything |

Runs at 08:30, last in the chain: **FX 05:00 → KPI 06:00 → detection 07:00 →
decision 07:30 → explanation 08:00 → delivery 08:30**.

**No severity logic in any node.** The service reads Stage 6's `routing`,
`notification_allowed` and `human_review_required` columns directly, so this
workflow decides nothing — it orders the work and records what happened.

**No webhook URL or recipient list here.** The service reads them from its own
environment. A webhook URL is a bearer token in URL form: anyone holding a Slack
or Teams one can post as the integration, so it never enters the workflow JSON,
the execution history, or the n8n database.

**`resend` is hardcoded false.** A rerun must not tell somebody the same thing
twice. Retry is safe precisely because routing is idempotent — a second attempt
delivers only what did not persist.

**Readiness deliberately excludes Stage 7.** A failed LLM analysis must never
stop an escalation reaching a human; the review item is created either way and
says the analysis was unavailable. Nor does it depend on there being any
actionable anomalies — zero is a healthy week.

| Status | When |
|---|---|
| `success` | Everything eligible was delivered or queued — or nothing was eligible |
| `partial` | Some deliveries failed while other work succeeded |
| `failed` | Every delivery failed and nothing was queued, the service was unreachable, or Stage 6 was not ready |

Count semantics: `records_received` = eligible anomalies, `records_accepted` =
notifications sent + reviews created, `records_rejected` = deliveries that
failed, `records_duplicate` = already delivered or queued. The four balance by
construction — every eligible anomaly gets exactly one outcome.

Whatever the outcome, **no Stage 6 decision changes** and **no business action is
taken**. Stage 8 ends at a row saying someone was told, or a row saying someone
must look. The eligibility rules, review state machine and retry semantics are
documented in
[database/README.md](../database/README.md#the-delivery-layer--stage-8).

### Remediation Execution

```
Schedule - Daily 09:00 ┐                        ┌─ true ─► Execute Approved ─► Finalize Run
                       ├─► Open Remediation ──► IF            Actions (HTTP)      (Postgres)
Manual Run ────────────┘       Run (Postgres)  Approved            │
                                               Work Waiting?       └─ error ─► Fail Run
                                                    │
                                                    └─ false ─► Close - Nothing Authorized
```

| Node | Does |
|---|---|
| **Open Remediation Run** | Opens `ingestion_runs` with `source='remediation-executor'`, and counts the authorised work waiting |
| **Approved Work Waiting?** | Routes on that count — with nothing authorised, the service is not called at all |
| **Execute Approved Actions** | `POST http://analytics-service:8000/remediation/execute-approved` — 2 attempts, 10s apart, 180s timeout |
| **Finalize Remediation Run** | Derives status from the service's counts; also reports how many actions are still waiting on a human |
| **Fail Remediation Run** | Closes the run as `failed` when the service call itself fails |
| **Close - Nothing Authorized** | Closes the run as `success` without calling anything |

Runs at 09:00, last in the chain: **FX 05:00 → KPI 06:00 → detection 07:00 →
decision 07:30 → explanation 08:00 → delivery 08:30 → execution 09:00**.

**This workflow authorises nothing.** Its work set is
`salesops.remediation_pending_execution`, a view containing only actions a human
moved to `approved`, with the retry budget already applied. It never sees an
action still `proposed`. A test enumerates every Postgres node in the file and
asserts that the only table any of them writes to is `ingestion_runs` — so the
workflow physically cannot advance an action's state, only ask the service to.

**`actor` is `stage9-workflow`, never a person.** The workflow executes; it does
not approve. That distinction is the whole stage, and it is worth being visible
in the audit trail rather than only in the documentation.

**No credentials here.** The recording provider needs none, and a real provider
would read its own from the service environment — never from this JSON, its
execution history, or the n8n database.

**Nothing upstream is a precondition.** Unlike Stage 8, this workflow does not
check whether Stage 6 ran. Remediation carries out an approval a human gave,
possibly days ago; making it wait on an upstream pipeline would mean a failed FX
sync could silently block an authorised investigation.

| Status | When |
|---|---|
| `success` | Everything authorised executed — or nothing was authorised, which is a good week |
| `partial` | Some executed and some did not |
| `failed` | Every execution failed, or the service was unreachable |

Count semantics: `records_received` = authorised actions found waiting,
`records_accepted` = executed, `records_rejected` = failed this run,
`records_duplicate` = claimed by another caller between the read and the write.
The four balance by construction.

Whatever the outcome, **no Stage 6 decision changes, no approval is created, and
no business action is taken** — the actions in the vocabulary are all requests
for human work, and the development provider records them without contacting
anything. The approval contract, action vocabulary and state machine are
documented in
[analytics-service/README.md](../analytics-service/README.md#stage-9--human-approved-remediation).

### Operational Maintenance

```
Schedule 09:30 ┐                    Recover      Recover       Collect       Staging      Retry Stale
               ├─► Open Maintenance ─► Stale ──► Stale ─────► Operational ─► Retention ─► Notifications ─► Finalize
Manual Run ────┘        Run            Runs      Remediation   Signals                      (HTTP)          Run
```

| Node | Does |
|---|---|
| **Open Maintenance Run** | Opens `ingestion_runs` with `source='operational-maintenance'`, and snapshots the world before anything is recovered |
| **Recover Stale Runs** | Closes runs abandoned at `running` past the timeout |
| **Recover Stale Remediation** | Moves crashed executions to `execution_unknown`. **Calls no provider** |
| **Collect Operational Signals** | Review ageing and replay candidates. Read-only |
| **Staging Retention** | Deletes settled staging rows past the retention period |
| **Retry Stale Notifications** | Asks Stage 8 to route again for stale dates only |
| **Finalize Maintenance Run** | Derives status from how many branches failed |

Runs at 09:30, after everything else has run **and had a chance to fail**:
**FX 05:00 → KPI 06:00 → detection 07:00 → decision 07:30 → explanation 08:00 →
delivery 08:30 → execution 09:00 → maintenance 09:30**.

**The five branches are independent.** Every one carries
`onError: continueRegularOutput`, so a failure is recorded and the next branch
still runs. They are unrelated operations — a retention sweep failing says
nothing about whether a stale run can be closed — and a maintenance workflow that
abandoned the rest of its work on the first error would be a reliability feature
that reduces reliability.

**It recovers; it does not re-run.** Closing a stale run does not repeat it.
Moving a crashed remediation out of `executing` does not call a provider. Replay
and purging both default to off in the scheduled path, because they are the two
operations that repeat work and delete data respectively.

**It writes only to operational tables.** A test enumerates every Postgres node
and asserts the set of tables written is a subset of `ingestion_runs`,
`raw_orders_staging`, `operational_events`, `remediation_actions` and
`remediation_attempts` — so the workflow physically cannot change a decision, a
hypothesis, a notification or an authorisation.

**It can find its own crashes.** The maintenance run uses the same ledger as
every other pipeline, deliberately: a maintenance run that died is exactly the
kind of stuck `running` row this workflow exists to find.

| Status | When |
|---|---|
| `success` | Every branch completed — including the common case of nothing to recover |
| `partial` | Some branches failed and others did real work |
| `failed` | Every branch failed |

Count semantics: `records_received` = branches attempted, `records_accepted` =
branches that completed, `records_rejected` = branches that failed,
`records_duplicate` = 0. A branch has one outcome; there is no third state.

### Pipeline Error Handler

```
Pipeline Failure ─► Log Pipeline Error ─► Fail Ingestion Run
 (Error Trigger)      (Postgres)              (Postgres)
```

Records the failure in `salesops.pipeline_errors`, then flips the matching
`ingestion_runs` row from `running` to `failed` — matched on
`n8n_execution_id`, which is exactly why that column exists.

Stage 3 stops at *persisting* the failure. Stage 10 reads this table rather than
replacing it: the operational health view and the retry queue are built on
`ingestion_runs` and `pipeline_errors`, and the maintenance workflow closes the
runs this handler could not reach. Escalation to a person is still absent - see
the Stage 10 limitations.

> **This workflow must be ACTIVE to work.** n8n silently declines to invoke an
> inactive error workflow: the failing workflow still fails, nothing is
> recorded, and the only clue is one line in the n8n log. An Error Trigger has
> no schedule of its own, so "active" here just means "available to invoke".
> `import-workflows.sh` activates it for you.

---

## Date-window strategy

Every run requests a window, never "everything":

| Situation | Window |
|---|---|
| **Cold start** (no successful run) | `today - 180 days` → `today` |
| **Every run after** | `last successful window_to - 1 day` → `today` |

Two things make this work.

**The one-day overlap is intentional.** A run at 14:00 sees only the orders
placed by 14:00; the rest of that day arrives later. Re-requesting the previous
day's window is how those are picked up. Without the overlap, every day would
lose whatever was ordered after the last run of the day.

**Overlap is free because re-ingestion is a no-op.** `fact_orders` is keyed on
`order_id` and inserts use `ON CONFLICT DO NOTHING`, so re-reading a window
already loaded produces zero new rows and a `records_duplicate` count. Observed:

```
run 1   window 2026-02-11 → 2026-08-10   received 3880   accepted 3880   duplicate 0
run 2   window 2026-08-09 → 2026-08-10   received   44   accepted    0   duplicate 44
run 3   window 2026-08-09 → 2026-08-10   received   44   accepted    0   duplicate 44
```

`fact_orders` held at 3,880 rows throughout.

The cold-start backfill is 180 days rather than 90 so a first run cannot miss
the start of the Mock API's history, which is anchored to the day its data
volume was created and drifts as time passes. Requesting more days than exist is
harmless — the API returns what it has.

---

## Idempotency: why `DO NOTHING` and not `DO UPDATE`

**A placed order is an immutable historical event.** Re-reading a window must
never rewrite what was already recorded.

`ON CONFLICT (order_id) DO UPDATE` would silently overwrite a stored order every
time the source served it again — and because the source is the only witness,
nothing would show that the number had changed. That destroys the audit trail
the staging layer exists to provide.

`DO NOTHING` makes the first observation authoritative. If an order genuinely
needs correcting, that is a deliberate amendment with its own record, not a side
effect of a routine hourly job.

The staging layer keeps the receipts either way: every arrival of an order is
logged in `raw_orders_staging` with its batch, so a source that started sending
different values for an existing `order_id` is visible in the history even
though the fact row did not move.

`raw_orders_staging` is deliberately **not** deduplicated — it is a log of what
arrived, so the same order appearing in three windows is three rows. After the
three runs above it held 3,968 rows against 3,880 facts.

---

## Validation rules

Applied in SQL, against the staged payload, after landing and before the fact
table. A record failing **any** rule is dead-lettered.

| Rule | Message |
|---|---|
| `order_id` present | `order_id is missing or empty` |
| `order_date` parses | `order_date is missing or not a valid date` |
| `order_date` in `dim_date` | `order_date … is outside dim_date` |
| `customer_id` present | `customer_id is missing or empty` |
| region known | `unknown region: ANTARCTICA` |
| product known | `unknown product: SKU-9999` |
| channel known | `unknown channel: telepathy` |
| `quantity` numeric and > 0 | `quantity must be greater than zero` |
| `unit_price` numeric and ≥ 0 | `unit_price must not be negative` |
| `refund_amount` numeric and ≥ 0 | `refund_amount must not be negative` |
| `refund_amount` ≤ gross | `refund_amount exceeds gross amount` |
| currency is a 3-letter ISO code | `currency is not a 3-letter ISO code: US` |
| currency is supported | `unsupported currency: XXX` |

Three properties worth knowing:

- **All reasons are reported, not just the first.** A record that is three kinds
  of broken says so once, rather than making you fix and re-run three times.
- **Casts fail soft.** `quantity: "lots"` becomes `NULL` via
  `salesops.try_to_numeric`, not a raised exception that would abort the whole
  batch this step exists to isolate.
- **Currency case is normalised, not rejected.** `usd` becomes `USD`. ISO 4217
  codes are case-insensitive and the same `upper()` runs on insert, so the
  warehouse never holds two spellings of one currency. `US` is still rejected.

### Unknown reference values are rejected, never created

If the API starts sending a region, product or channel the warehouse has never
heard of, the record is dead-lettered and the dimension is left alone.

Auto-creating the member would be the convenient choice and the wrong one: it
turns a reference-data change nobody agreed to into silent warehouse growth, and
the first anyone hears about it is a dashboard with a category they cannot
explain. Rejecting makes the drift visible while the payload is still on disk to
inspect.

---

## Staging: the traceability boundary

Everything received is written to `raw_orders_staging` before anything is
interpreted — whole payload, as JSONB, tagged with the run's `batch_id`.

The table has **no foreign keys and almost no constraints**, on purpose. A
payload with a negative quantity or an invented region has to be *storable*, or
there is nothing to dead-letter and nothing to replay after a fix.

Status transitions:

| Status | Meaning |
|---|---|
| `pending` | Landed, not yet processed |
| `failed` | Rejected by validation. `error_message` says why; payload intact |
| `processed` | Became a new row in `fact_orders` |
| `skipped` | Valid, but that `order_id` was already loaded |

Any figure in the warehouse traces back to the bytes it came from:

```sql
SELECT f.order_id, f.gross_amount_local, s.batch_id, s.received_at, s.source_payload
FROM salesops.fact_orders        f
JOIN salesops.raw_orders_staging s ON s.order_id = f.order_id
WHERE f.order_id = 'ORD-2026-000101';
```

---

## Retries and failure handling

**Fetch Orders** retries 3 times, 5 seconds apart, with a 30-second timeout.
Non-2xx responses throw — the node is not configured to tolerate them.

**A failed request stops the workflow.** It does not fall through to an empty
ingestion that would close the run as a clean success over zero rows.

**Run state is written before the work, not after.** `Create Batch Context`
inserts `status = 'running'` up front, so a crash leaves visible evidence:

```sql
-- Runs that started and never finished
SELECT run_id, batch_id, started_at, window_from, window_to
FROM salesops.ingestion_runs
WHERE status = 'running' AND started_at < now() - INTERVAL '1 hour';
```

**Success is derived, never asserted.** `Finalize Ingestion Run` computes
`partial` when anything was rejected, so a green run cannot hide a dead-letter
pile. `ingestion_runs` also carries a CHECK requiring
`received = accepted + rejected + duplicate` on any finished run — a counting
bug becomes a loud constraint violation instead of a plausible-looking number.

---

## Running it

### First-time setup

```bash
./database/migrate.sh          # V004 adds ingestion_runs + pipeline_errors
./n8n/import-workflows.sh      # credential + both workflows, error handler activated
```

The PostgreSQL credential is assembled **inside** the n8n container from the
environment variables Compose already gave it. The password is never written to
a file on the host, never passed as a command-line argument, and never enters
the repo. n8n encrypts it on import with `N8N_ENCRYPTION_KEY`.

Re-running the import is safe: the workflows carry fixed ids, so it updates
rather than duplicating.

### Run one now

```bash
BROKER="-e N8N_RUNNERS_BROKER_PORT=5690"
docker compose exec -T $BROKER n8n n8n execute --id=salesopsIngest001   # orders
docker compose exec -T $BROKER n8n n8n execute --id=salesopsFxSync001   # FX
docker compose exec -T $BROKER n8n n8n execute --id=salesopsKpiRfr001   # KPIs
```

`N8N_RUNNERS_BROKER_PORT` is needed because n8n 2.x's CLI starts its own task
broker, which collides with the running instance's on port 5679. Any free port
works.

Or open <http://localhost:5678>, open **Orders Ingestion**, and click **Execute
workflow** — which runs the same path through the **Manual Run** trigger.

### Enable the schedule

```bash
./n8n/import-workflows.sh --activate
```

Activation is stored in the database, so n8n has to be restarted to pick it up —
the script does that.

### Verify

```sql
-- What has run, and how it went
SELECT run_id, status, window_from, window_to,
       records_received, records_accepted, records_rejected, records_duplicate,
       round(extract(epoch FROM (finished_at - started_at))::numeric, 1) AS secs
FROM salesops.ingestion_runs ORDER BY run_id DESC LIMIT 10;

-- Where the records went
SELECT processing_status, count(*) FROM salesops.raw_orders_staging GROUP BY 1;

-- Anything dead-lettered, and why
SELECT order_id, error_message, source_payload
FROM salesops.raw_orders_staging WHERE processing_status = 'failed';

-- Failures caught by the error handler
SELECT occurred_at, workflow_name, node_name, left(error_message, 80)
FROM salesops.pipeline_errors ORDER BY occurred_at DESC;
```

### Tests

```bash
python n8n/tests/test_ingestion_sql.py     # 34 checks - ingestion
python n8n/tests/test_stage4_fx_kpi.py     # 66 checks - FX and KPI
python n8n/tests/test_stage6_decisions.py  # 79 checks - decisions
cd analytics-service && python -m pytest   # 555 checks - detection through the dashboards
```

**Decisions — 79 checks.** Same harness: the workflow's own SQL, extracted from
`deterministic-anomaly-decision.json`, run inside a transaction that rolls back.
Synthetic fixtures live in March 2025, far from the ingested 2026 series, and
the isolation is asserted rather than assumed.

Most of the suite would pass if the engine merely ran. Two fixture *pairs* would
not, and they are the point:

| Pair | Held constant | Varied | Required outcome |
|---|---|---|---|
| `2025-03-07` vs `2025-03-10` | anomaly score, robust-z, percent deviation | absolute dollars | `major` vs `minor` |
| `2025-03-07` vs `2025-03-18` | dollars, score | operational damage | `major` vs `critical` |

Between them they pin down the two claims Stage 6 actually makes: severity is
not a re-labelling of the z-score, and `critical` means "money moved *and*
something broke". A third fixture moves all three operational measures past
their thresholds in the *favourable* direction, so a magnitude-based rule would
read three severe failures where a direction-aware one reads none.

Also covered: the override attempts the database must refuse (switching off
`human_review_required`, re-routing a critical decision to `no_action`, enabling
notification on an unscored date, inserting a reason code outside the
vocabulary), idempotency across three runs, threshold immutability, a second
decision version reaching a different conclusion without disturbing the first,
and both live dates the specification names.

**Ingestion — 34 checks.** The Mock API only emits valid orders, so the rejection paths are
driven with controlled payloads — but through **the real SQL**, extracted from
`orders-ingestion.json` at run time rather than copied. Edit a node's query and
the test runs the edited query; a hand-copied fixture would drift the first time
either changed. Everything runs in a transaction that rolls back.

Covers: every validation rule, multi-reason messages, payload preservation,
unknown region/product/channel not creating dimension members, no customer name
fabricated, currency normalisation, re-ingestion accepting zero, and existing
fact rows not being overwritten.

---

## Editing the workflows

The JSON files here are the committed source of truth. The round trip is:

```bash
# edit in the n8n UI at http://localhost:5678, then export back
docker compose exec -T n8n n8n export:workflow \
    --id=salesopsIngest001 --pretty --output=/workflows/orders-ingestion.json
```

Then commit the diff. Importing and exporting use the same `/workflows` mount,
so nothing has to be copied in or out of the container.

---

## Known limitations

- **FX is USD-only.** `Ensure USD Identity Rate` writes `USD → 1.0`, which is an
  identity rather than a market rate, so it is not invented data. The other four
  currencies stay `NULL` until the Frankfurter sync exists, and both analytical
  views expose `orders_pending_fx` so the gap is visible rather than assumed
  away. Today: 1,585 of 3,880 orders have USD amounts.
- **No dead-letter replay.** Failures are recorded and inspectable; re-driving
  them after a fix is Stage 10, alongside the retention policy.
- **One batch per run, no chunking.** A 180-day cold start is a single ~800 KB
  request and one set-based insert, which completes in about 19 seconds. If the
  source grew to millions of orders this would need windowing.
- **The error handler cannot mark a run failed if the run row was never
  created.** A failure inside `Create Batch Context` itself is recorded in
  `pipeline_errors` but has no `ingestion_runs` row to update. The `UPDATE`
  affecting zero rows is a valid outcome, not an error.
- **CLI `execute` does not fire error workflows.** Error workflows run for
  production executions only, so failure handling must be tested with the
  schedule active, not via `n8n execute`.


---

## Stage 4 verification queries

```sql
-- Did every pipeline run, and how did each finish?
SELECT source, status, window_from, window_to,
       records_received, records_accepted, records_rejected, records_duplicate
FROM salesops.ingestion_runs ORDER BY run_id DESC LIMIT 10;

-- FX coverage, by provenance
SELECT source, currency, count(*), min(rate_date), max(rate_date)
FROM salesops.exchange_rates GROUP BY 1, 2 ORDER BY 1, 2;

-- Anything still unconvertible?
SELECT currency, count(*) FROM salesops.fact_orders
WHERE exchange_rate_to_usd IS NULL GROUP BY 1;

-- The KPI series
SELECT calendar_date, orders_count, new_customers, net_revenue_usd,
       average_order_value_usd, refund_rate, fx_completeness_pct, is_complete,
       rolling_7d_net_revenue_usd, rolling_28d_net_revenue_usd
FROM salesops.kpi_daily ORDER BY calendar_date DESC LIMIT 10;

-- Any day whose money columns are understated
SELECT calendar_date, orders_pending_fx, fx_completeness_pct
FROM salesops.kpi_daily WHERE NOT is_complete ORDER BY calendar_date;
```

## Stage 6 verification queries

```sql
-- What needs attention, worst first, with the reasoning attached
SELECT calendar_date, day_name, severity, routing, business_impact_tier,
       round(expected_net_revenue_usd) AS expected,
       round(actual_net_revenue_usd)   AS actual,
       round(revenue_delta_usd)        AS delta,
       all_reasons
FROM salesops.anomaly_decision_audit
WHERE severity <> 'none' AND decision_version = 'stage6-v1'
ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1 ELSE 2 END,
         abs(revenue_delta_usd) DESC;

-- The queue a human actually works
SELECT calendar_date, severity, round(revenue_delta_usd) AS delta, all_reasons
FROM salesops.anomaly_decision_audit
WHERE human_review_required ORDER BY calendar_date DESC;

-- Why was this one classified that way? (no LLM required)
SELECT * FROM salesops.anomaly_decision_audit WHERE calendar_date = '2026-08-05';

-- Which rules are actually firing?
SELECT r.reason_code, c.description, count(*)
FROM salesops.anomaly_decision_reasons r
JOIN salesops.decision_reason_codes c USING (reason_code)
GROUP BY 1, 2 ORDER BY 3 DESC;

-- What are the thresholds, and why?
SELECT threshold_key, threshold_value, unit, description
FROM salesops.decision_thresholds WHERE decision_version = 'stage6-v1'
ORDER BY threshold_key;
```

## Stage 4 limitations

- **The FX sync always fetches all non-USD currencies over the derived window**,
  even when nothing is pending. Four cheap requests a day, and `ON CONFLICT DO
  NOTHING` makes repeats free - simpler than tracking which dates are already
  covered, and it self-heals if a rate was ever missed.
- **Carry-forward is bounded at 7 days.** Long enough for any weekend or holiday
  closure, short enough that a broken feed leaves orders visibly pending. A
  market closed longer than a week would need the bound revisited.
- **Rates are never revised.** `ON CONFLICT DO NOTHING` keeps the first value
  fetched, so a provider correction is ignored. Deliberate - the rate applied is
  the rate kept - but a genuinely wrong early rate needs a manual amendment.
- **If `fact_orders` held no non-USD orders**, `Currencies To Sync` would return
  zero rows and the run would stop before finalising, leaving it at `running`.
  Cannot occur while the Mock API emits five currencies; not worth branching for.
- **The KPI rebuild is full-table.** Fine at one row per trading day; a
  multi-year warehouse would want incremental refresh with a watermark.
- **`kpi_daily` is daily and company-wide.** Per-region KPIs live in
  `regional_sales_base`; if Stage 5 needs a materialised regional series, that is
  a second table.

## Stage 7 verification queries

```sql
-- What has been explained, and how confident the model was about its explanation
SELECT calendar_date, decision_severity, confidence, primary_hypothesis,
       model_name, prompt_version
FROM salesops.anomaly_hypothesis_audit
ORDER BY calendar_date DESC;

-- Why is this critical, and what might have caused it? (one row, no LLM needed
-- to read it back)
SELECT * FROM salesops.anomaly_hypothesis_audit WHERE calendar_date = '2026-08-05';

-- Has Stage 6 re-decided since any analysis was written?
SELECT calendar_date, analysed_severity, decision_severity
FROM salesops.anomaly_hypothesis_audit WHERE NOT decision_current;

-- Which metrics does the model actually reason from?
SELECT e->>'metric' AS metric, count(*)
FROM salesops.anomaly_hypotheses h,
     jsonb_array_elements(h.supporting_evidence) e
GROUP BY 1 ORDER BY 2 DESC;

-- What does it consistently say it is missing? (the gap analysis writes itself)
SELECT m #>> '{}' AS missing_evidence, count(*)
FROM salesops.anomaly_hypotheses h,
     jsonb_array_elements(h.missing_evidence) m
GROUP BY 1 ORDER BY 2 DESC;

-- Actionable anomalies with no explanation yet
SELECT d.calendar_date, d.severity
FROM salesops.anomaly_decisions d
WHERE d.decision = 'action_required'
  AND NOT EXISTS (SELECT 1 FROM salesops.anomaly_hypotheses h
                  WHERE h.decision_id = d.decision_id)
ORDER BY d.calendar_date;

-- Cost and latency of the last run
SELECT calendar_date, model_name, prompt_tokens, completion_tokens, latency_ms
FROM salesops.anomaly_hypotheses ORDER BY generated_at DESC LIMIT 10;
```

## Stage 8 verification queries

```sql
-- The review queue a human actually works, worst first
SELECT calendar_date, queued_severity, status, assigned_to,
       round(revenue_delta_usd) AS delta, hypothesis_status, hypothesis_confidence
FROM salesops.review_queue_audit
WHERE status IN ('pending', 'in_review')
ORDER BY CASE queued_severity WHEN 'critical' THEN 0 ELSE 1 END, created_at;

-- What was delivered, to whom, and did it work?
SELECT calendar_date, notified_severity, recipient, channel, status,
       attempt_count, sent_at, last_error
FROM salesops.notification_audit ORDER BY calendar_date;

-- Anything undelivered that should have been?
SELECT calendar_date, recipient, status, attempt_count, last_error
FROM salesops.notification_audit WHERE status <> 'sent';

-- The full delivery history for one anomaly
SELECT a.attempt_number, a.outcome, a.status_code, a.error_message, a.attempted_at
FROM salesops.notification_attempts a
JOIN salesops.notifications n USING (notification_id)
WHERE n.calendar_date = '2026-06-01' ORDER BY a.attempt_number;

-- How did this review reach its current state?
SELECT from_status, to_status, actor, resolution, occurred_at
FROM salesops.review_events e
JOIN salesops.review_queue q USING (review_id)
WHERE q.calendar_date = '2026-08-05' ORDER BY occurred_at;

-- Has Stage 6 re-decided since anything was delivered or queued?
SELECT calendar_date, notified_severity, decision_severity FROM salesops.notification_audit
WHERE NOT decision_current
UNION ALL
SELECT calendar_date, queued_severity, decision_severity FROM salesops.review_queue_audit
WHERE NOT decision_current;

-- Review throughput
SELECT resolution, count(*), round(avg(seconds_to_resolution)) AS avg_seconds
FROM salesops.review_queue_audit WHERE resolution IS NOT NULL GROUP BY 1;
```

## Runbook — investigating a failure

Start here, and work down only as far as you need to.

### 1. What is wrong?

```sql
SELECT overall_status, unhealthy FROM salesops.operational_health_summary;
```

`unhealthy` is a list of `component:REASON_CODE`. If it is empty and the status
is `healthy`, the pipeline is fine and the problem is somewhere else.

```sql
-- The detail behind every unhealthy component, with the numbers
SELECT component, status, reason_code, observed_value, threshold_value, measure,
       last_status, last_run_at, detail
FROM salesops.operational_health
WHERE status <> 'healthy'
ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END;
```

Every verdict is recomputable from `observed_value` against `threshold_value`.
If you disagree with a status, one of those two numbers is what to argue with.

### 2. What failed, and what happens to it?

```sql
SELECT entity_type, entity_id, subsystem, disposition, retry_eligible,
       next_retry_at, attempt_count, max_attempts, left(failure_reason, 100)
FROM salesops.operational_retry_queue
WHERE NOT terminal OR retry_eligible
ORDER BY latest_failure_at DESC;
```

`disposition` is the answer:

| | |
|---|---|
| `SELF_HEALING_NEXT_RUN` | nothing to do — the ingestion window self-corrects |
| `RETRY_VIA_STAGE8_ROUTING` | nothing to do — the next routing run retries it |
| `RETRY_VIA_STAGE9_WORKFLOW` | nothing to do — the next remediation run retries it |
| `REPLAYABLE` | replay it (below) |
| `AWAITING_RECONCILIATION` | a human must decide (below) |
| `RETRY_BUDGET_SPENT` / `ABANDONED` | investigate; nothing further is automatic |

### 3. What did recovery already do?

```sql
SELECT occurred_at, event_type, entity_type, entity_id, from_state, to_state,
       actor, reason_code, detail
FROM salesops.operational_events
ORDER BY occurred_at DESC LIMIT 20;
```

Append-only. If something changed and there is no event here, Stage 10 did not
do it.

### 4. Replaying a dead-letter batch

```sql
SELECT original_batch_id, failed_rows, rows_eligible, max_attempts_used,
       replay_eligible, sample_errors
FROM salesops.ingestion_replay_candidates ORDER BY first_failure_at;
```

Read `sample_errors` first. If it says `unknown region` and the region genuinely
does not exist, replay will fail again — validation is deterministic, and the fix
is the reference data, not another attempt.

```bash
curl -X POST http://localhost:8001/operations/replay \
     -H "Content-Type: application/json" \
     -d '{"batch_id":"<original_batch_id>","actor":"you@example.com"}'
```

```sql
-- Per-row outcome, with the original failure still intact beside it
SELECT r.attempt_number, r.outcome, r.original_error, s.processing_status
FROM salesops.ingestion_replays r
JOIN salesops.raw_orders_staging s ON s.ingestion_id = r.replay_ingestion_id
WHERE r.original_batch_id = '<original_batch_id>' ORDER BY r.attempt_number;
```

The original rows stay `failed` forever. That is deliberate: "the first attempt
failed" and "a replay succeeded" are both true, and they are recorded separately
so both can stay true.

### 5. Reconciling an unknown execution

```sql
SELECT remediation_id, calendar_date, action_type, attempt_count,
       review_approved_by, authorized_by, last_error
FROM salesops.remediation_actions WHERE status = 'execution_unknown';
```

The process died around a provider call and nothing in the database knows whether
it landed. Check the provider side, then say what you found:

```bash
curl -X POST http://localhost:8001/remediation/<id>/reconcile \
     -H "Content-Type: application/json" \
     -d '{"outcome":"confirmed_not_executed","actor":"you@example.com",
          "evidence":"No request recorded on the provider side."}'
```

`confirmed_executed` closes it. `confirmed_not_executed` returns it to `failed`,
where Stage 9's ordinary bounded retry applies — so the retry is your decision,
not recovery's. There is no path back to `executing` without going through this.

### 6. A run stuck at `running`

It will be closed automatically at 09:30. To do it now:

```bash
curl -X POST http://localhost:8001/operations/recover/runs \
     -H "Content-Type: application/json" -d '{"actor":"you@example.com"}'
```

This closes the ledger entry. **It does not re-run the work** — the ingestion
window self-heals on the next run, Stages 5-8 are idempotent, and Stage 9 needs a
human. If the work does need repeating, trigger that pipeline explicitly.

### 7. Reviews nobody has looked at

```sql
SELECT review_id, calendar_date, anomaly_severity, review_status, assigned_to,
       age_hours, ageing_bucket, escalation_eligible
FROM salesops.review_ageing
WHERE ageing_bucket <> 'fresh' ORDER BY age_hours DESC;
```

`ageing_bucket` is **operational ageing, not anomaly severity**. Nothing resolves,
dismisses or approves an item because it got old — "nobody has looked at this" is
not a decision, and Stage 10 does not make it on anyone's behalf.

### 8. Retention

```sql
SELECT disposition, processing_status, rows, oldest, newest
FROM salesops.staging_retention_report ORDER BY disposition;
```

Always readable before anything is deleted. `pending` and `failed` rows are never
eligible, and neither is any row involved in a replay.

```bash
curl -X POST http://localhost:8001/operations/staging/purge \
     -H "Content-Type: application/json" -d '{"dry_run":true}'
```

## Stage 10 limitations

- **Recovery infers a crash from elapsed time.** A genuinely slow provider call
  exceeding `stale_remediation_timeout_minutes` would be moved to
  `execution_unknown` while still in flight. The timeout is set well beyond any
  HTTP timeout, and the consequence is a reconciliation rather than a double
  execution — which is the right way round for that trade.
- **Nothing escalates.** An overdue review is labelled and reported; nobody is
  told. Stage 8 delivers anomalies, not operational conditions, and putting
  pipeline noise in the same channel as revenue findings would degrade both.
- **Retention never deletes a failed row**, so the dead-letter trail grows
  without bound. Archival is a deliberate decision nobody has made yet.
- **The maintenance branches are sequential.** Each continues on error, so one
  failure never stops the rest, but a slow branch delays the others.
- **`actor` is asserted, not authenticated** — `stage10-recovery` for automated
  work, and whatever a caller types for anything manual.

## Stage 9 verification queries

```sql
-- The whole chain for every remediation, in one row each
SELECT calendar_date, authorized_severity, action_type, status,
       review_approved_by, authorized_by, executed_by,
       attempt_count, had_external_side_effect, authorization_current
FROM salesops.remediation_audit ORDER BY remediation_id;

-- Authorised and waiting to run, worst first
SELECT * FROM salesops.remediation_pending_execution;

-- Proposed but nobody has authorised them. Whose desk is this on?
SELECT remediation_id, calendar_date, severity, action_type, review_approved_by,
       created_at
FROM salesops.remediation_actions WHERE status = 'proposed' ORDER BY created_at;

-- Exactly-once, checked rather than assumed: every executed action must show 1
SELECT remediation_id, status, successful_attempts, attempts_recorded
FROM salesops.remediation_audit WHERE status = 'executed';

-- Full history of one action, including who did what
SELECT from_status, to_status, actor, reason, occurred_at
FROM salesops.remediation_events WHERE remediation_id = 1 ORDER BY occurred_at;

-- Did anything ever claim an external side effect? (It must not.)
SELECT count(*) FROM salesops.remediation_attempts WHERE external_side_effect;

-- Has Stage 6 re-decided since something was authorised?
SELECT calendar_date, authorized_severity, decision_severity, status
FROM salesops.remediation_audit WHERE NOT authorization_current;

-- Approval-to-execution latency
SELECT action_type, count(*),
       round(avg(seconds_approval_to_execution)) AS avg_seconds
FROM salesops.remediation_audit WHERE executed_at IS NOT NULL GROUP BY 1;

-- Reviews approved for remediation, and what came of them
SELECT q.calendar_date, q.severity, q.approved_by, q.approved_at,
       a.action_type, a.status
FROM salesops.review_queue q
LEFT JOIN salesops.remediation_actions a ON a.review_id = q.review_id
WHERE q.status = 'approved' ORDER BY q.approved_at;
```

## Stage 9 limitations

- **`actor` is asserted, not authenticated.** The stage rests entirely on who
  approved something, and nothing verifies who that was. It is the largest gap
  in Stage 9, and no amount of state-machine rigour makes up for it.
- **The provider contacts nothing.** There is no ticketing system in this
  project, so the only provider records the request and returns. What a real
  integration would do about partial success or a ticket closed on the far side
  is unexplored, because there is nothing here to explore it against.
- **An action stranded in `executing` needs a human.** A raising provider is
  caught and recorded, but a process killed mid-call leaves the row claimed with
  nothing watching it. No reaper, no timeout.
- **Retries stop at three, quietly.** The action leaves the work set — visible
  in `remediation_pending_execution` and `ingestion_runs` — but nothing alerts.
- **Nothing chases an unauthorised proposal.** An action nobody authorises sits
  in `proposed` indefinitely. The run ledger reports the backlog on every
  execution; it does not escalate it.

## Stage 8 limitations

- **A re-decision does not re-notify.** The idempotency key is
  `(anomaly, decision_version, channel, recipient)`, so if Stage 6 re-runs and
  changes a severity within the same version, the original notification stands
  and the audit view reports `decision_current = false`. Telling people "that
  thing we sent you is now worse" is a real feature, and it is not this one.
- **No delivery-window or digest logic.** Eleven anomalies produce four
  notifications, one per minor anomaly per recipient. A noisier business would
  want batching, quiet hours and a daily digest before this reached anyone's
  phone.
- **The review queue has no UI and no authentication.** It is a REST API on the
  Docker network. Anyone who can reach the service can claim and resolve items,
  and `actor` is whatever the caller says it is. That is a real limitation, and
  a shared secret in an environment variable would look like authentication
  without being any.
- **Retries are bounded to three attempts across runs**, then the notification is
  `abandoned` and nothing further is tried automatically. Nothing alerts on that
  yet — it is visible in `ingestion_runs` and in the audit view.
- **No escalation on an unclaimed review.** A critical item nobody claims sits in
  `pending` indefinitely. Ageing and escalation belong with the alerting layer.

## Stage 7 limitations

- **The workflow fails closed, and that is the whole point.** No key, unreachable
  service, provider outage — the run is recorded `failed` and the anomalies stay
  exactly as Stage 6 left them. Unexplained is a recoverable state; a wrong
  explanation attached to a real decision is not.
- **Cost scales with actionable anomalies.** 11 calls a day against the current
  dataset. A looser Stage 6 threshold changes that arithmetic; the service's
  `limit` parameter is the guard, and it logs what it drops.
- **Retry granularity is the whole batch, not the anomaly.** A retry re-runs the
  request, and idempotency means only unanalysed anomalies cost anything — but a
  single persistently-failing anomaly is retried alongside the rest.
- **No alerting on a `partial` run.** Stage 10 now *surfaces* it - the health
  view reports `LAST_RUN_PARTIAL` and the retry queue lists what failed - but
  nothing tells a person. Reading the health view is still something somebody
  has to choose to do.

## Stage 6 limitations

- **The engine re-decides every evidence row on every run.** Correct and cheap at
  one row per trading day, and it is what keeps decisions in step with a Stage 5
  re-run or a backfill. A multi-year warehouse would want to scope the pass to
  changed evidence.
- **Thresholds are absolute dollars, tuned to this business's scale.** They are
  reference data and versioned, so changing them is a visible act — but a
  business ten times the size needs different numbers, not a different model.
  Expressing them as a fraction of trailing median revenue would auto-scale, at
  the cost of a threshold that moves on its own.
- **Impact is measured on net revenue only.** Margin, unit economics and customer
  lifetime value are not in `kpi_daily`, so a day that held revenue by discounting
  heavily reads as ordinary.
- **Severity is per-day and company-wide.** A single region collapsing while three
  stay healthy is diluted before Stage 6 ever sees it — inherited from
  `kpi_daily`, not introduced here.
- **`decided_at` moves on every run**, so it means "last evaluated", not "last
  changed". Idempotency is asserted over the decision columns, which is the
  property that matters; a separate `first_decided_at` would be needed to answer
  "when did this first become critical?"
- **The decision is not an alert.** Nothing is delivered anywhere. Stage 6 grants
  permission; a later stage acts on it.
