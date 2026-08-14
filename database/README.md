# The `salesops` analytics schema

The warehouse the pipeline writes into and the dashboards read out of. A
star schema with a staging layer in front of it, living in the `salesops` schema
of the `salesops` database.

The `n8n` and `metabase` databases sit on the same PostgreSQL server but are
entirely separate — they hold application state, not analytics data, and nothing
here references them.

> **Status: Stage 10.** Schema, reference data, base views, ingestion, real FX
> from Frankfurter, a materialised daily KPI layer, statistical detection
> results, the deterministic decision layer, LLM hypotheses, the delivery and
> human-review layer, the remediation layer, and the operational reliability
> layer. All 3,880 orders carry an
> exchange rate and all 90 KPI dates report 100% FX completeness.
>
> **Stage 6 decides. Stage 7 explains. Stage 8 delivers. Stage 9 executes what a
> human approved. Stage 10 keeps the whole machine recoverable.** No stage after
> 6 can change a Stage 6 decision, nothing executes that a person did not
> authorise by name, and nothing in Stage 10 repeats work it merely found stuck.
>
> The transactional and KPI layers still score, threshold and judge nothing —
> severity lives in [the decision layer](#the-decision-layer--stage-6), and the
> single model-written table is [the hypothesis layer](#the-hypothesis-layer--stage-7).

---

## Layers

```
                 ┌──────────────────────────────────────┐
   Mock API  ──► │  raw_orders_staging                  │   land everything,
                 │  batch_id · source_payload JSONB     │   validate nothing
                 └──────────────────┬───────────────────┘
                                    │  validate in SQL
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
             processing_status              processing_status
               = 'processed'                  = 'failed'
                    │                       (dead letter, replayable)
                    ▼
                 ┌──────────────────────────────────────┐
                 │  fact_orders                         │
                 │  PK order_id  ·  NUMERIC money       │
                 └──────────────────┬───────────────────┘
                                    │  5 foreign keys
        ┌──────────┬────────────────┼────────────────┬──────────────┐
        ▼          ▼                ▼                ▼              ▼
    dim_date   dim_region      dim_product      dim_channel    dim_customer
                    ▲                                                │
                    └────────────────────────────────────────────────┘

    exchange_rates ──► supplies fact_orders.exchange_rate_to_usd
                       (frankfurter + USD identity; carry-forward on non-trading days)

    daily_sales_base · regional_sales_base   ──► views over fact_orders
    kpi_daily                                ──► rebuilt wholesale, Stage 5's input
```

| Layer | Tables | Rule |
|---|---|---|
| **Staging** | `raw_orders_staging` | Accepts anything. No foreign keys, no validation. |
| **Dimensions** | `dim_date`, `dim_region`, `dim_product`, `dim_channel`, `dim_customer` | Conformed, constrained, small. |
| **Reference** | `exchange_rates` | Externally sourced: Frankfurter, plus the USD identity. |
| **KPI** | `kpi_daily` | One row per trading day. Rebuilt wholesale. Stage 5's input. |
| **Fact** | `fact_orders` | One row per order line. Every dimension reference is a foreign key. |
| **Semantic** | `daily_sales_base`, `regional_sales_base` | One definition of "daily revenue" for the whole project. |

### Why staging and fact are separate

`raw_orders_staging` has **no foreign keys and almost no constraints** — that is
the point. A payload with a negative quantity or an unknown region has to be
*storable*, or there is nothing for the dead-letter queue to inspect and nothing
to replay after a fix. Stage 10 is what does the replaying, and it depends
entirely on this: a failed row keeps its payload and its rejection reason
forever, which is why retention never deletes one. Validation happens on the way *out* of
staging, in SQL, where the rejection reason can be recorded next to the payload
that caused it.

`fact_orders` is the opposite: every column is constrained, every dimension
reference is a foreign key. Nothing reaches it without passing validation.

---

## Key strategy

| Dimension | Key | Why |
|---|---|---|
| `dim_region`, `dim_product`, `dim_channel` | **surrogate** `SMALLINT` identity, natural code kept `UNIQUE` | These are low-cardinality *labels*. Labels get renamed and re-coded; a surrogate makes a rename a one-row `UPDATE` instead of a fact-table rewrite. Also 2 bytes per fact row instead of a string. |
| `dim_customer` | **natural** `customer_id TEXT` | The source already supplies a stable, opaque identifier (`CUST-NA-0365`). A surrogate on top would add a lookup to every ingest and buy nothing. |
| `dim_date` | `date_key` (`YYYYMMDD`) PK, `calendar_date` `UNIQUE` | Conventional, and `calendar_date` is what `fact_orders` actually joins on. |

`fact_orders.order_date` is a real `DATE`, not a `date_key`, with a foreign key
onto `dim_date.calendar_date`. Analysts can filter by date without joining
`dim_date` at all, and integrity is still enforced.

Either way, **source values never enter the fact table as unvalidated free
text**. `'EMEA'` becomes `region_id`; an unrecognised region produces no fact row
rather than a new uncontrolled string.

---

## Idempotency

`order_id` is the primary key of `fact_orders`. That single choice is what makes
the whole pipeline safe to re-run:

```sql
INSERT INTO salesops.fact_orders (...) VALUES (...)
ON CONFLICT (order_id) DO NOTHING;
```

Re-ingesting a window that was already loaded changes nothing and double-counts
no revenue. A retry after a partial failure is safe. A backfill overlapping
existing data is safe.

`exchange_rates` has the same property via its `(rate_date, currency)` primary
key, so re-running the FX sync cannot produce two rates for one day.

`dim_customer` upserts with `LEAST()` on `first_seen_date`, so loading older
orders later *corrects* the value rather than corrupting it.

`raw_orders_staging` is deliberately **not** deduplicated — it is a log of what
arrived. The same order appearing in two batches is a fact worth keeping.

---

## Money

Every monetary column is `NUMERIC`. No `float`, no `double precision`, anywhere.
Binary floating point cannot represent `0.1` exactly; summing 4,000 order lines
in it produces a total that is wrong in a way that is hard to see and impossible
to defend. The schema test asserts this and fails if a float column ever appears.

| Column | Type |
|---|---|
| `unit_price` | `NUMERIC(14,4)` |
| `gross_amount_local`, `refund_amount_local` | `NUMERIC(18,4)` |
| `exchange_rate_to_usd` | `NUMERIC(18,8)` — JPY is ~0.0064, so 8dp matters |
| `gross/refund/net_amount_usd` | `NUMERIC(18,4)` |

### USD values are generated, not written

`gross_amount_local`, `gross_amount_usd`, `refund_amount_usd` and
`net_amount_usd` are `GENERATED ALWAYS AS ... STORED`. They are computed by the
database from `quantity`, `unit_price`, `refund_amount_local` and
`exchange_rate_to_usd`.

Three consequences, all deliberate:

1. **A USD figure cannot exist without a real exchange rate.** No rate means
   `NULL`, structurally. It is not a rule someone has to remember to follow.
2. **Attaching a rate later backfills the USD values automatically.** Ingestion
   sets `exchange_rate_to_usd`; the three USD columns recompute themselves. No
   second pass writing money values, so they cannot drift out of step.
3. **They cannot be written by hand.** PostgreSQL rejects any `INSERT`/`UPDATE`
   that targets them — so Stage 3's SQL must not list them, and no one can
   quietly set a USD amount inconsistent with its rate.

### Why the rate is copied onto the fact row

`fact_orders.exchange_rate_to_usd` duplicates a value that also lives in
`exchange_rates`. That is intentional. FX providers revise historical rates.
Freezing the rate actually applied means a report re-run next year reproduces
the number it produced today — a joined rate would silently change history.

---

## Exchange rates

Two provenances, and only two. The schema test enforces it: every row must be
`identity` or `frankfurter`, and identity rows must be `USD` at exactly `1.0`.

| Source | What it is |
|---|---|
| `identity` | `USD → 1.0`. Arithmetic, not a market rate. Written by the ingestion pipeline, because Frankfurter never returns a `USD/USD` pair and without it every USD order would sit unconverted forever. |
| `frankfurter` | Real published rates, fetched by the FX Rate Sync workflow. |

Nothing else is ever written. Seeding plausible-looking numbers would make every
USD figure in the warehouse traceable to something someone made up, and the
fabrication would be invisible once aggregated into a dashboard.

### The weekend problem, and how it is solved without inventing anything

Frankfurter republishes ECB reference rates, and the ECB quotes on **business
days only**. Over the current 90-day sales window it returns **64 dates — every
one of the 26 missing days is a weekend**. Orders, meanwhile, arrive every day:
359 non-USD orders in the current dataset fall on a weekend.

Filling those gaps in `exchange_rates` would mean writing rows the provider
never published. So the table stores **only what was actually published**, and
the carry-forward happens at *attachment* time:

> An order takes the most recent rate published **on or before** its order date,
> within a **7-day staleness bound**.

A Saturday order uses Friday's rate. That is standard practice — the FX market
is closed, and the last published rate is the applicable one. It is a documented
join rule, not invented data, and it is bounded: a two-day weekend is normal, a
three-week gap means the feed is broken and those orders stay visibly pending
rather than silently inheriting a stale rate.

The rate actually applied is then frozen onto `fact_orders.exchange_rate_to_usd`,
so a report re-run next year reproduces today's number even if the provider
revises its history.

Result: **3,880 of 3,880 orders converted, 100% FX completeness on all 90 dates.**

**One thing Stage 3 must not forget:** Frankfurter returns no `USD → USD` pair.
The workflow has to insert `('USD', 1.0)` explicitly, or every USD order stays
unconverted forever.

---

## `kpi_daily` — the analytical layer

One row per calendar date **that has orders**. Dates with no orders are absent
rather than zero-filled: "no trading" and "traded nothing" are different facts,
and inventing zero rows would corrupt the moving averages and hand the Stage 5
detector a fake baseline.

Rebuilt wholesale by `salesops.refresh_kpi_daily()`.

### Columns

| Column | Definition |
|---|---|
| `date_key`, `calendar_date` | `YYYYMMDD` and the date. FK onto `dim_date`. |
| `orders_count` | Orders on the date. |
| `customers_count` | Distinct customers ordering on the date. |
| `new_customers` | Customers whose **earliest order date anywhere in `fact_orders`** is this date. Recomputed from facts, not read from `dim_customer.first_seen_date`, so a rebuild never depends on an incrementally-maintained value. |
| `units_sold` | Sum of quantity. |
| `gross_revenue_usd` | `SUM(gross_amount_usd)` |
| `refund_amount_usd` | `SUM(refund_amount_usd)` |
| `net_revenue_usd` | `SUM(net_amount_usd)` |
| `average_order_value_usd` | `net_revenue_usd / orders that converted to USD` |
| `refund_rate` | `refund_amount_usd / gross_revenue_usd`, safely divided |
| `orders_pending_fx` | Orders with no exchange rate. |
| `fx_completeness_pct` | Share of the day's orders carrying a rate, 0–100. |
| `is_complete` | `orders_pending_fx = 0` |
| `rolling_7d_net_revenue_usd` | Trailing 7-calendar-day **mean** of `net_revenue_usd` |
| `rolling_28d_net_revenue_usd` | Trailing 28-calendar-day **mean** |

> **Two different AOVs exist in this project, deliberately.**
> `kpi_daily.average_order_value_usd` divides **net** revenue by the **converted
> subset**, so an incomplete day is not understated.
> `daily_sales_base.average_order_value_usd` divides **gross** by **all** orders.
> They answer different questions and will not match. The KPI table's definition
> is the one Stage 5 consumes.

### Missing FX is `NULL`, never zero

Every money column is nullable. A day whose orders have no exchange rate has
**no USD revenue — not zero revenue**. Coalescing to `0` would turn missing data
into a revenue collapse, which is precisely the signal Stage 5 exists to hunt;
the detector would fire on an FX outage and call it a business event.

`orders_pending_fx` and `fx_completeness_pct` sit on every row so a consumer can
tell an incomplete day from a bad one without going back to `fact_orders`. Two
CHECK constraints keep the three completeness columns from ever disagreeing.

### Moving-average semantics

```sql
avg(net_revenue_usd) OVER (
    ORDER BY calendar_date
    RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW)
```

- **`RANGE`, not `ROWS`.** `ROWS` counts rows, so a date with no orders would let
  a "7-day" window silently reach back to an eighth calendar day. `RANGE` with an
  interval is calendar-accurate and gap-safe.
- **Ends at `CURRENT ROW`**, so no future date can ever contribute.
- **The first 6 / 27 days average over fewer observations** rather than being
  `NULL`. A shorter honest window beats fabricated history, and it means Stage 5
  gets a usable series from day one.
- `AVG` skips `NULL`s, so a window containing an unconverted day averages the
  days that did convert. `is_complete` is how you know.

### Why a table and not just the views

The Stage 5 detector needs a stable, cheap, repeatedly-scannable series, and it
needs two things the views do not provide: `new_customers`, and trailing moving
averages. Recomputing window functions over `fact_orders` on every detector pass
would be slower and — because the underlying facts keep growing — not
reproducible between runs.

### Why the rebuild is a function

`refresh_kpi_daily()` does a `DELETE` then an `INSERT`, and those must be atomic.
A function body runs inside the caller's transaction, so a failure rolls back the
`DELETE` too and the previous contents survive. Split across two n8n nodes there
would be a window in which `kpi_daily` was empty, and any reader in that window
would see a warehouse with no history.

Rebuilding wholesale rather than incrementally is deliberate: the table is one
row per trading day, so a full rebuild costs less than the bookkeeping an
incremental strategy would need — and it cannot drift from `fact_orders`.

---

## The views

Two, and only two. They exist so "daily revenue" has exactly one definition in
this project — the Stage 5 detector, the Metabase dashboards and any ad-hoc
query all read the same numbers because they read the same SQL.

| View | Grain | For |
|---|---|---|
| `daily_sales_base` | one row per day | executive KPI trend |
| `regional_sales_base` | one row per day per region | the Stage 5 detector — four separate series, so one region collapsing is not diluted by the other three |

Both expose revenue, orders, units, AOV, refunds and refund rate in USD.

**Read `orders_pending_fx` alongside the revenue.** `SUM()` skips `NULL`s, so a
day where only some rows have exchange rates reports a real-looking but
incomplete total. That column is how you tell. Any value above zero means
revenue is understated.

Not in these views, on purpose: moving averages, z-scores, thresholds, anomaly
flags. Those belong with the Stage 5 detector that owns their parameters, not
baked into a base view.

---

## The decision layer — Stage 6

```text
Stage 4  what happened          kpi_daily
Stage 5  how unusual it was     anomaly_daily          <- Python, robust statistics
Stage 6  whether it matters     anomaly_decisions      <- SQL, deterministic rules
Stage 7  why it might have happened                    <- LLM, later
```

Stage 5 deliberately produces no severity. Stage 6 supplies it, in SQL, before a
language model is anywhere near the pipeline. Stage 7 will read a decision that
has already been made; it cannot create, upgrade, downgrade or veto one. There
is no column in these tables an LLM is permitted to write, and the schema
validation suite scans for one so that stays true.

### Tables

| Relation | Holds |
|---|---|
| `anomaly_decisions` | one decision per `(anomaly_id, decision_version)`: evidence snapshot, business impact, severity, routing, flags |
| `anomaly_decision_reasons` | every reason code that applied, normalised |
| `decision_reason_codes` | the closed reason vocabulary, with descriptions |
| `decision_thresholds` | every constant the rules compare against, keyed by version |
| `anomaly_decision_audit` | a view: one readable row answering "why is this critical?" |

`salesops.decide_anomalies('stage6-v1')` is the engine. Idempotent — the same
evidence and version produce identical severity, routing, flags and reason codes,
and re-running upserts in place.

### The severity model

Two axes, kept apart on purpose:

**Money** — how far net revenue moved from what this weekday normally earns.
Both an absolute *and* a relative gate must pass, so a large dollar move on an
exceptionally large day is not automatically material:

| tier | absolute | relative |
|---|---|---|
| `trivial` | < $1,000 | — |
| `limited` | ≥ $1,000 | — |
| `material` | ≥ $4,000 | ≥ 20% |
| `severe` | ≥ $9,000 | ≥ 40% |

Calibrated against the live series (median trading day $11,146; weekday
$11–13k, weekend $5.7–6.4k), so `material` is roughly a third of a typical
weekday and `severe` is close to a whole day gained or lost.

**Corroboration** — independent operational damage, each test one-sided,
because refunds falling and order value rising are not incidents:

- refund rate up ≥ 10 percentage points (baselines sit near 0.02–0.05)
- average order value down ≥ 30%
- order volume down ≥ 30%
- or 3 of Stage 5's 4 signals independently significant

The ladder:

```text
critical   severe money WITH corroboration
           OR (severe or material money) WITH two operational failures
major      severe or material money
minor      flagged by Stage 5, but the money did not reach material
none       not flagged, or not scorable
```

Money gates every escalation, so a statistically spectacular move on a small day
stays `minor`. Corroboration gates `critical` specifically, so `critical` never
means "biggest z-score this week".

**The live data demonstrates the distinction rather than asserting it.**
2026-08-09 carries the highest anomaly score in the series (12.94 — a Sunday at
3.3× its baseline) and comes out `major`: one measure, moving upward, nothing
operational behind it. 2026-08-05 scores lower (8.93) and comes out `critical`,
because its revenue shortfall arrives with the refund rate up 33 points and
average order value down 64%. Ranked by score alone, the wrong one is at the top
of the queue.

### Routing is a constraint, not a convention

```text
none      → no_action     decision = no_action
minor     → auto_notify   notification_allowed = true
major     → human_review  human_review_required = true
critical  → human_review  human_review_required = true
```

`notification_allowed` means *automated* notification is permitted, which only
`minor` gets. Major and critical go to a person, who decides what is
communicated — a critical event is not silently emailed.

Both flags are the routing restated, and CHECK constraints enforce that they
cannot disagree with it, that an unscored or unflagged date cannot carry a
severity, and that nothing unscored can be notified. Trying to switch off
`human_review_required` on a critical decision is rejected by the database, not
by a convention someone might forget.

### Expected revenue comes from Stage 5, not a second estimate

V007 added `anomaly_daily.revenue_baseline_median` — the median of the
calendar-aware baseline the detector already computed. Stage 6 measures impact
against that exact number rather than re-deriving one, so the two layers cannot
develop contradictory ideas of "expected". Recovering it arithmetically from the
stored percentage would be lossy and undefined at −100%; more importantly it
would be a second methodology.

When it is missing, impact is `unknown` and the decision escalates to human
review with `BUSINESS_IMPACT_UNAVAILABLE`. An unmeasured impact is not a small
one.

### Versioning

Every decision carries `decision_version`, and thresholds are rows keyed by it.
A trigger refuses to change or delete a threshold once decisions reference that
version — so historical decisions stay reproducible from stored configuration,
and changing a number is forced to be a visible, versioned event. Two versions
coexist over the same evidence, which is how a threshold change gets diffed
against its predecessor instead of erasing it.

---

## The hypothesis layer — Stage 7

```text
Stage 6  whether it matters     anomaly_decisions      deterministic SQL
Stage 7  why it might have happened                    anomaly_hypotheses  <- an LLM
```

`anomaly_hypotheses` is the only table in this warehouse whose contents come from
a language model, and everything about its design follows from that.

The model may write prose and structured lists: a summary, one primary
hypothesis, supporting evidence, alternatives, missing evidence, recommended
checks. It may not write `severity`, `routing` or `decision`. Those three columns
exist here as a **snapshot** of the Stage 6 verdict the hypothesis was written
about — copied out of `anomaly_decisions` by the service, never supplied by the
model, and verified against the live decision by a trigger before any row is
accepted.

### Why the guard is a trigger and not a foreign key

A composite FK onto `(decision_id, severity, routing, decision)` is the obvious
move and is wrong in both available flavours:

| | Consequence |
|---|---|
| Without `ON UPDATE CASCADE` | A Stage 6 re-run that changed a severity would be **blocked** by the existence of a hypothesis. Stage 7 output could stop Stage 6, inverting the architecture |
| With `ON UPDATE CASCADE` | The snapshot would be silently rewritten underneath prose that still describes the old verdict — a row quietly contradicting itself |

A trigger fires only on writes to `anomaly_hypotheses`. It cannot block Stage 6
and it cannot rewrite history. If a re-decision later moves a severity, the drift
becomes **visible** through `anomaly_hypothesis_audit.decision_current` rather
than being hidden by either mechanism.

There is deliberately no `CHECK (decision = 'action_required')` either, for the
same reason: Stage 7 only analyses actionable decisions and its test suite proves
it, but a constraint here would let an existing hypothesis block a later
downgrade.

### Identity and versioning

```text
UNIQUE (anomaly_id, decision_version, prompt_version, model_name)
```

with `ON CONFLICT DO NOTHING` — the opposite of Stages 5 and 6, which upsert. A
detection and a decision are derived opinions that must track their inputs; a
hypothesis is a generated artefact with provenance, and overwriting one silently
replaces reasoning a human may already have read and acted on. Change the prompt
or the model and a new row appears **beside** the old one.

Provenance is mandatory and `NOT NULL`: `model_provider`, `model_name`,
`prompt_version`, and `evidence_digest` — a SHA-256 of exactly what the model was
shown, so two differing answers can be proven to have had identical or different
inputs. No table anywhere in this schema has a column for an API key, and the
validation suite scans for one.

`salesops.anomaly_hypothesis_audit` puts the hypothesis beside the decision it
explains, with the drift flag.

---

## The delivery layer — Stage 8

```text
Stage 6  whether it matters      anomaly_decisions    DECIDES
Stage 7  why it might have happened  anomaly_hypotheses   EXPLAINS
Stage 8  who needs to know        notifications        DELIVERS
                                  review_queue
```

Four tables and two views: `notifications` and `notification_attempts` for
automated delivery, `review_queue` and `review_events` for human review, with
`notification_audit` and `review_queue_audit` over the top.

### Eligibility is a constraint, not a convention

Nothing in Stage 8 computes severity or re-derives who should be told. Each row
carries a **snapshot** of the Stage 6 verdict that routed it, and a CHECK
requires that snapshot to be an eligible one:

```sql
CONSTRAINT notifications_only_for_eligible_decisions
    CHECK (notification_allowed AND routing = 'auto_notify'
           AND decision = 'action_required')

CONSTRAINT review_queue_only_for_eligible_decisions
    CHECK (human_review_required AND routing = 'human_review'
           AND decision = 'action_required')
```

These CHECKs are safe here in a way they would not have been in V009. The
snapshot columns are Stage 8's own immutable copy — no later process updates
them — so a constraint on them can never block a Stage 6 re-decision. A guard
trigger then verifies the snapshot against the live decision at insert time, so
a caller cannot fabricate an eligible-looking snapshot for an anomaly Stage 6
routed to `no_action`.

Between them: **sending a notification for a no_action anomaly is not a bug code
review has to catch. It is a write the database refuses.**

### The review state machine

Declared once, enforced by trigger, so there is no "set status to whatever".

```text
pending    -> in_review     a reviewer claims it
pending    -> dismissed     triaged away without claiming
in_review   -> resolved     reviewed and closed out
in_review   -> dismissed    reviewed and judged not worth pursuing
in_review   -> pending      claim released, back to the queue
resolved / dismissed        terminal
```

Releasing a claim is deliberate: without it, an item claimed by someone who then
becomes unavailable is stuck in `in_review` forever, and the only remedy is a
manual UPDATE that bypasses every rule here.

Terminal states require a resolution — `confirmed`, `false_positive`,
`expected_business_variation` or `requires_follow_up` — and **cannot be edited
afterwards**. Rewriting a resolution after the fact is the one thing an audit
trail exists to prevent. Every transition appends to `review_events` with actor
and timestamp; the queue row holds current state, the event log holds how it got
there.

A resolution is **not a new severity** and triggers nothing. Stage 8 ends at the
recorded outcome.

### What a reviewer cannot do

| | |
|---|---|
| Record status, resolution, notes | ✅ |
| Change severity, routing, decision, either flag | ❌ refused by the snapshot guard |
| Reopen a terminal item | ❌ refused by the state machine |
| Rewrite a resolution | ❌ refused by the state machine |

### Delivery state and retries

```text
pending    created, never attempted
sent       delivered
failed     retryable, and the budget is not spent - a later run retries
abandoned  permanent failure, or the retry budget is exhausted
```

`notification_attempts` records **every** attempt with its outcome classified as
`success`, `retryable_failure` or `permanent_failure`. That classification is
what bounds retries: a timeout is worth another attempt, a 401 never will be.

`sent_at` tracks the *current* status and is cleared whenever the status moves
away from `sent` — a row that is no longer delivered must not claim a delivery
time. Nothing is lost, because the attempts table keeps the successful attempt.

### Review notes are untrusted

Written by a person, read by a person. Length-bounded by CHECK, stored verbatim
rather than interpreted, never rendered into a notification payload, and never
concatenated into anything executed or sent to a model. A test plants a canary
string in a note and asserts it never appears in any outbound payload.

### No secrets, anywhere

No table or view in this schema has a column for an API key, a webhook URL, a
bearer token or a password — the validation suite scans for one. The
`notification_audit` view deliberately omits the payload it summarises.

---

## Migrations

```text
database/
├── init/            runs ONCE, on first container start (creates n8n + metabase DBs)
├── migrations/      versioned, idempotent, applied by migrate.ps1 / migrate.sh
├── tests/           schema validation suite
└── examples/        runnable walkthrough of the Stage 3 ingestion pattern
```

`init/` is **not** a migration system and stays that way. It is Postgres's
one-shot bootstrap hook, it only fires on an empty data volume, and it knows
nothing about the analytics schema.

| Migration | Contents |
|---|---|
| `V001__analytics_schema.sql` | Schema, tables, constraints, indexes |
| `V002__reference_data.sql` | `dim_region`, `dim_product`, `dim_channel`, `dim_date` |
| `V003__analytical_views.sql` | The two base views |
| `V004__ingestion_observability.sql` | `ingestion_runs`, `pipeline_errors`, safe-cast helpers |
| `V005__kpi_daily.sql` | `kpi_daily` and `refresh_kpi_daily()` |
| `V006`–`V007` | `anomaly_daily`, the stored baseline median |
| `V008__decision_layer.sql` | Thresholds, severity, routing, reason codes |
| `V009__anomaly_hypotheses.sql` | Stage 7 output, snapshot-guarded |
| `V010__notification_and_review.sql` | Notifications, review queue, review events |
| `V011__remediation.sql` | Action types, eligibility, actions, attempts, events |
| `V012__operational_reliability.sql` | Recovery, replay, retention, health |
| `V013__presentation_layer.sql` | Presentation views, layer vocabulary, read-only role |

Structural DDL and reference data are separate files so a schema review is not
buried in `INSERT` statements, and so reference data can be corrected and
re-applied without touching DDL.

Every migration is **wrapped in a transaction** (PostgreSQL has transactional
DDL, so a failure leaves nothing half-applied) and **idempotent**
(`IF NOT EXISTS`, `ON CONFLICT`). Re-running is the normal way to bring an
existing database up to date. `salesops.schema_migrations` records what ran.

This is deliberately not Flyway, Liquibase or Alembic. Numbered idempotent SQL
plus a ledger table answers "what is applied here?" without adding a dependency,
and every file stays readable by an analyst who does not know the tool.

```powershell
.\database\migrate.ps1              # apply
.\database\migrate.ps1 -Test        # apply, then validate
```

```bash
./database/migrate.sh               # apply
./database/migrate.sh --test        # apply, then validate
```

`./database` is mounted read-only into the Postgres container at `/database`, so
`psql -f` reads the files directly. Nothing is piped through the host shell,
which keeps encoding and line endings out of the picture on Windows.

---

## Tests

```bash
docker compose exec -T postgres \
  psql -U salesops -d salesops -v ON_ERROR_STOP=1 -f /database/tests/test_analytics_schema.sql
```

213 checks: structure, `NUMERIC`-only money, keys and indexes, reference data
matching the Mock API, `dim_date` coverage and correctness, working inserts,
generated-column behaviour, every constraint rejection, view arithmetic, the
decision layer's shape, and the hypothesis layer's guarantees — including a scan
proving no table has a column for a secret, and a live test that the snapshot
guard **rejects a restated verdict while still accepting a truthful one**.

That pairing matters: asserting only the rejection would pass just as well if the
guard rejected everything.

It also proves the Stage 8 guarantees rather than asserting them: that the
eligibility CHECK refuses a notification for a `human_review` decision, that the
snapshot guard refuses a restated verdict, and that the review state machine
refuses `pending -> resolved` while accepting `pending -> in_review`.

The Stage 9 section does the same for remediation, and pairs every refusal with
the corresponding acceptance: a pending review authorises nothing but an
approved one does; a false positive cannot approve but `confirmed` can; refund
review is refused at `major` and accepted at `critical`; an executed action
cannot execute again while the full authorised path runs through cleanly. It
also checks that no foreign key from `remediation_actions` reaches a table an
earlier stage rebuilds — the one mistake that would make Stage 9 able to block
Stage 6.

Those last two clear the queue row first rather than hunting for an unqueued
decision. Searching for a free one made them silently skip once Stage 8 had run —
in exactly the state the system normally lives in, which is the state they most
need to run in.

Behaviour is tested separately — the decision rules in
`n8n/tests/test_stage6_decisions.py` (79 checks), and Stages 7, 8 and 9 in
`analytics-service/tests/` (380 checks, all against fake providers - nothing is
sent to anybody and nothing is remediated).

The pipeline layers have their own suites, run from the repo root:

```bash
python n8n/tests/test_ingestion_sql.py    # 34 checks - Stage 3 ingestion SQL
python n8n/tests/test_stage4_fx_kpi.py    # 66 checks - Stage 4 FX and KPI
```

Written in plain SQL because the thing under test *is* a PostgreSQL schema —
testing it with `psql` needs no ORM, no driver and no dependency that can drift
from what the database actually enforces.

The whole suite runs inside a transaction that `ROLLBACK`s, so it is safe
against a populated database and leaves nothing behind. Each check records a
result rather than aborting, so one failure does not hide the next twelve; the
final block raises if anything failed, so the process exits non-zero.

---

## Ready for Stage 3

```text
Mock API  GET /orders?from=&to=
    │
    ▼
n8n ingestion workflow
    │   one row per order, whole payload as JSONB, tagged with batch_id
    ▼
raw_orders_staging                       nothing can fail here
    │
    │   validate in SQL
    ├──────────────► processing_status = 'failed' + error_message   (dead letter)
    ▼
dim_customer upsert  →  resolve region/product/channel by natural key
    │
    ▼
fact_orders   INSERT ... ON CONFLICT (order_id) DO NOTHING          idempotent
    │
    ▼
exchange_rates (Frankfurter)  →  UPDATE fact_orders.exchange_rate_to_usd
    │                             USD columns compute themselves
    ▼
daily_sales_base · regional_sales_base
```

`database/examples/ingestion_pattern.sql` runs this whole flow as working SQL —
including a deliberately broken payload being dead-lettered, a duplicate insert
proving idempotency, and FX backfill recomputing the USD columns. It rolls back,
so it is safe to run at any time:

```bash
docker compose exec -T postgres \
  psql -U salesops -d salesops -v ON_ERROR_STOP=1 -f /database/examples/ingestion_pattern.sql
```

---

## The remediation layer — Stage 9

```text
Stage 6  whether it matters          anomaly_decisions      DECIDES
Stage 7  why it might have happened  anomaly_hypotheses     EXPLAINS
Stage 8  who needs to know           notifications          DELIVERS
                                     review_queue
Stage 9  what a human authorised     remediation_actions    EXECUTES
```

Five tables and two views. Three of the tables are the action itself
(`remediation_actions`, `remediation_attempts`, `remediation_events`); two are
reference data (`remediation_action_types`, `remediation_action_eligibility`),
and putting the policy in tables rather than in a function body is what lets an
operator answer "what may we do about a major anomaly?" with a `SELECT`.

### Three gates, in three different mechanisms

| Gate | Question | Enforced by |
|---|---|---|
| Authorisation | Did a human approve this? | `guard_remediation_authorization()`, at INSERT |
| Eligibility | May this action be taken at this severity? | a composite **foreign key** |
| Execution | Has it already run? | the state machine, and a conditional UPDATE |

Using three different mechanisms is deliberate. A single trigger doing all three
would be one function to get wrong; a foreign key cannot be forgotten, a CHECK
cannot be raced, and a conditional UPDATE cannot be beaten by two callers
arriving at once.

### `resolved` is not approval

V011 adds exactly one review state and changes nothing else about Stage 8:

```text
in_review -> approved    confirmed, and remediation is authorised
in_review -> resolved    reviewed and closed WITHOUT remediation
in_review -> dismissed   not worth pursuing
```

Before it, one `resolved` state had to mean both "confirmed, do something" and
"confirmed, do nothing" — and reading either as consent would be guessing at
what a person meant. Every Stage 8 transition still works unchanged, every
existing row is untouched, and there is no backfill and no default that
reinterprets history.

Approval also requires a confirming resolution:

```sql
CONSTRAINT review_queue_approval_needs_confirmation
    CHECK (status <> 'approved'
           OR resolution IN ('confirmed', 'requires_follow_up'))
```

You cannot authorise action on something you have just called a false positive.

### Eligibility is a foreign key

```sql
CONSTRAINT remediation_actions_eligible_fk
    FOREIGN KEY (policy_version, severity, action_type)
    REFERENCES salesops.remediation_action_eligibility
               (policy_version, severity, action_type)
```

| severity | permitted actions |
|---|---|
| `critical` | investigation, operations review, **refund review** |
| `major` | investigation, operations review |
| `minor`, `none` | *(no rows — see below)* |

`minor` is absent, and not by a separate rule. Stage 6 routes it to
`auto_notify`, so no review item is created, so there is nothing to approve. A
CHECK on the eligibility table itself refuses any row for a severity Stage 6
does not route to a human, so the table cannot drift into contradicting V008's
routing contract.

### No foreign key to any earlier stage

`review_id`, `anomaly_id`, `decision_id` and `hypothesis_id` are plain `BIGINT`
columns, validated once by the guard trigger at INSERT. The V009 header sets out
the reasoning in full; the short version is that a foreign key here would have
to choose between two bad outcomes — without `CASCADE` it can block a Stage 6
re-decision or a Stage 4 KPI rebuild, and with `CASCADE` it silently erases an
action a human authorised.

The guard trigger fires on INSERT only, for the same reason. Stage 6 may
re-decide and Stage 7 may regenerate; neither may retroactively invalidate what
somebody approved. `remediation_audit.authorization_current` reports the drift
instead, which is the honest way to surface something that has already happened.

### The state machine

```text
proposed  -> approved  -> executing -> executed     (terminal)
proposed  -> rejected | cancelled                   (terminal)
approved  -> cancelled                              (terminal)
executing -> failed    -> executing                 (retry, bounded at 3)
failed    -> cancelled                              (terminal)
```

`executed` has no outgoing transition. `failed` is a resting state that permits
a bounded explicit retry — and once the budget is spent the trigger refuses,
so a permanently broken action stops being retried rather than being attempted
by every scheduled run forever.

Every transition appends to `remediation_events` from inside the trigger, so a
transition that happened without an audit event is not reachable. The whole
authorisation snapshot is immutable on UPDATE: 21 columns compared in one
`IS DISTINCT FROM`, refused as a group.

### Idempotency

```sql
idempotency_key TEXT GENERATED ALWAYS AS (
    review_id::text || ':' || action_type || ':' || decision_version
) STORED
```

Generated and stored rather than living only in an `ON CONFLICT` clause, so it
is visible in a `SELECT` and cannot be supplied by a caller.
`decision_version` is part of it because a re-decided anomaly is a *different*
authorisation, not the same one again.

### Nothing here mutates a business system

```sql
CONSTRAINT remediation_action_types_are_review_requests
    CHECK (NOT mutates_external_state)
```

All three actions are requests for human work. Adding one that changes state
outside this warehouse would require a deliberate migration that removes this
constraint — not a quiet `INSERT`. `remediation_attempts.external_side_effect`
records the provider's own statement on every attempt, and the audit view
reports it, so a reader never has to infer whether anything real happened.

---

## The operational reliability layer — Stage 10

```text
Stage 9   what a human authorised     remediation_actions    EXECUTES
Stage 10  what got stuck, and why     operational_events     RECOVERS
```

Three tables, seven views, eight functions. None of it is a pipeline stage; all
of it exists because every earlier stage writes a state it can be interrupted in.

### Recovery is not re-execution

```text
RECOVERY      moves a stuck record into an honest, final-or-actionable state.
              It never repeats work.
RE-EXECUTION  repeats work. It is always somebody's explicit decision.
```

Closing a run abandoned at `running` does not re-run it. Moving a crashed
remediation out of `executing` does not call a provider. Replay is the only
operation in the layer that repeats anything.

### Thresholds are data

`operational_config`, one row per threshold, exactly as `decision_thresholds`
holds the Stage 6 numbers. An operator asking "how old is stale?" runs one
`SELECT`. `salesops.operational_setting(key)` **raises** on an unknown key rather
than returning a plausible default — a typo in a threshold name must not quietly
disable a safety check.

### The audit log is append-only, enforced

```sql
CREATE TRIGGER trg_operational_events_append_only
    BEFORE UPDATE OR DELETE ON salesops.operational_events
    FOR EACH ROW EXECUTE FUNCTION salesops.guard_operational_events_append_only();
```

Both operations raise. The failure mode this guards against is specific: an
automated process tidying away the evidence of what it did.

### `execution_unknown`

The one genuinely hard problem in the layer. A remediation action enters
`executing`, the provider is called, the process dies. Nothing here can know
whether the call landed, and both automatic answers are wrong — re-executing
might do the thing twice, failing it might claim something did not happen when it
did.

So V012 adds one state and four transitions to the Stage 9 machine:

```text
executing → execution_unknown → executed | failed | cancelled
```

There is deliberately **no** `execution_unknown → executing`. Confirming an
execution did not happen returns the action to `failed`, where the ordinary
bounded retry applies — so a retry is always a human's decision. The state is
also absent from `remediation_pending_execution`, so nothing picks it up
automatically. The recorded attempt gets outcome `unknown`: an attempt was made,
and what it achieved is not known.

### Replay never rewrites a failure

`ingestion_replays` maps each replayed row to the one it came from. The original
staging row is never modified, so both facts stay true in separate places:

```sql
raw_orders_staging.processing_status = 'failed'      -- the first attempt failed
ingestion_replays.outcome            = 'succeeded'   -- a replay of it worked
```

The replay run uses `source = 'ingestion-replay'` so it can never move the Stage 3
ingestion window, and `ON CONFLICT (order_id) DO NOTHING` makes it idempotent
against `fact_orders`. Bounded by `max_replay_attempts`.

### Retention protects evidence

| status | eligible? |
|---|---|
| `processed`, `skipped` | yes, once older than `staging_retention_days` |
| `pending` | never — unfinished work |
| `failed` | never — the dead-letter trail and the replay source |

Plus a predicate excluding any row involved in a replay, which the foreign key
would refuse anyway; saying so in the `DELETE` makes the rule findable without
reading the constraint list. `purge_staging()` is **dry run by default**.

### Health explains itself

`operational_health` reports `status`, `reason_code`, `observed_value`,
`threshold_value` and `measure` per component, so a verdict can be recomputed by
hand from the same inputs. `operational_health_summary` takes the **worst**
component — a pipeline is not "mostly healthy".

It reads whether Stage 7 *ran*, never what it *said*, and a schema check asserts
the view definition does not reference `anomaly_hypotheses`.

### Three vocabularies, deliberately different words

| | Values |
|---|---|
| anomaly severity (Stage 6) | `none` `minor` `major` `critical` |
| operational health (Stage 10) | `healthy` `warning` `degraded` `failed` |
| review ageing (Stage 10) | `fresh` `warning` `overdue` `critical_overdue` |

No word appears in two of them except `warning`, and a schema check asserts no
ageing bucket is ever named after a severity.

---

## The presentation layer — Stage 11

Fifteen views, one reference table and one database role. No behaviour, no
arithmetic, no threshold. `V013` is the only migration in the project that a
reviewer can read knowing in advance that nothing it adds can change an outcome.

**The layer vocabulary.** `presentation_layers` names the eight kinds of thing a
dashboard renders — measured fact, statistical signal, deterministic decision,
model hypothesis, human review, approved remediation, completed remediation,
operational event — in the order they must be read. One `CHECK` carries the
whole design:

```sql
CONSTRAINT presentation_layers_model_flag_chk CHECK (
    is_model_generated = (evidence_kind = 'model_generated')
)
```

Exactly one layer is model-generated, and the flag and the kind cannot be
written apart. Every presentation view carries a layer key, so a measurement and
a guess cannot be rendered as the same kind of thing by accident — and a test
can prove the separation rather than an eye having to notice it.

**One day, several versions.** A calendar date does not have one row downstream.
It has one per detector version in `anomaly_daily`, one per decision version in
`anomaly_decisions`, and one per `(prompt version, model)` in
`anomaly_hypotheses` — all three uniqueness constraints say so, and keeping old
generations is the point of storing a version at all. A plain equi-join to any of
them multiplies rows, and the failure mode is not an error: it is a dashboard
quietly reporting two of every anomaly the first time Stage 7 is re-run with a
new prompt. Every join in `V013` is a `LATERAL` taking the newest row, and the
version it took is a column.

*(This was found by the schema suite, not by review. The check that caught it —
`the view drops none of them` — compares the view's row count against
`count(DISTINCT anomaly_id)` and fails on any fan-out.)*

**The reporting role.** `salesops_readonly` gets `USAGE` and `SELECT` and
nothing else. PostgreSQL grants `EXECUTE` on every function to `PUBLIC` by
default, and `purge_staging()` and `replay_failed_batch()` are write operations
reachable from a `SELECT` box, so `PUBLIC` loses it — except on the non-`VOLATILE`
functions, granted by *volatility* rather than by name:

```sql
WHERE n.nspname = 'salesops' AND p.prokind = 'f'
  AND p.provolatile <> 'v'            -- 's' stable, 'i' immutable
  AND p.prorettype <> 'trigger'::regtype
```

Four functions qualify, and they are exactly the configuration readers that
`operational_health` and its neighbours call. This matters more than it looks:
PostgreSQL checks `EXECUTE` against the **calling** role even inside a view whose
table access is checked against its owner, so revoking everything would have
broken those views for the reporting role — and it would have surfaced as a
blank dashboard panel, not as an error.

The role is created `NOLOGIN` with no password. A password in a migration is a
password in version control; `metabase/provision.sh` sets one from the
environment, and then proves the result by asking the role to `DELETE FROM
kpi_daily` and requiring the refusal.

---

## Known limitations

- **`dim_customer.customer_name` will stay `NULL`.** The orders endpoint carries
  only `customer_id`; there is no name in the payload and no customer endpoint to
  enrich from. The column exists because the dimension is incomplete without it,
  and inventing names would put fabricated data in the warehouse. Filling it
  needs a source that does not exist yet.
- **No slowly-changing dimensions.** Renaming a product overwrites the label for
  all history. SCD Type 2 would need effective-dated dimension rows and a
  surrogate on every fact row; nothing in this project asks "what was this
  product called last March?"
- **No partitioning on `fact_orders`.** At a few thousand rows a day it would be
  overhead with no benefit. The `order_date` index is the right tool at this size.
- **The reporting role is bounded by privilege, not by row.** `salesops_readonly`
  has `SELECT` on the whole schema and `EXECUTE` on nothing volatile, so it
  cannot write; it also cannot be restricted to a subset of the data. There is no
  row-level security and no per-viewer scope, because there is no notion of a
  viewer - see the note on asserted actors below.
- **Actors are asserted, not authenticated.** Every `actor`, `approved_by`,
  `authorized_by` and `executed_by` in this schema is a string a caller supplied.
  The audit trail records what was *claimed*, immutably and completely, and that
  is the whole of what it can prove. This is the largest security limitation in
  the project.
- **A remediation action stranded in `executing` needs a human.** The state
  machine has no timeout and there is no reaper: a process killed between the
  claim and the result leaves the row claimed, and recovery is a manual UPDATE.
- **The Stage 9 eligibility policy is versioned but has no migration path.**
  Editing `stage9-v1` in place would silently re-interpret rows already
  authorised under it. Adding `stage9-v2` is the intended route, and nothing yet
  exists to move actions between versions.
- **`salesops.load_staged_batch()` duplicates the Stage 3 workflow's validation
  rules.** Replay has to run exactly the validation the original run ran, and
  that logic lives inside n8n node parameters where PostgreSQL cannot reach it.
  Both implementations are tested independently against the same documented
  Stage 2 rules, so a drift fails one of the two suites — but the right
  long-term fix is for the Stage 3 workflow to call this function.
- **The dead-letter trail grows without bound.** Retention never deletes a
  failed staging row, which is the only default that cannot lose evidence and
  also the one that never reclaims space. Archival needs a deliberate decision.
- **The presentation views show the newest version of each thing.** A date can
  carry several detector versions, decision versions and hypothesis generations -
  every uniqueness constraint says so - and the Stage 11 views take the latest of
  each so a dashboard cannot count one anomaly twice. Older versions remain
  stored, and remain visible in `audit_event_stream`; they are simply not what
  the headline panels count. Which version a panel used is a column on it.
