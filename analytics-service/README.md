# Analytics service — detection, hypotheses, delivery, remediation, recovery

Five stages live here, deliberately unequal in authority.

```
Stage 4  deterministic KPI generation
Stage 5  statistical anomaly detection      <- /detect
Stage 6  deterministic severity and routing    (SQL, in the database)
Stage 7  LLM root-cause hypotheses          <- /anomalies/analyze
Stage 8  delivery and human review          <- /notifications/*, /reviews/*
Stage 9  human-approved remediation         <- /remediation/*
Stage 10 recovery, replay and health        <- /operations/*
```

**Stage 6 decides. Stage 7 explains. Stage 8 delivers. Stage 9 executes what a
human approved. Stage 10 keeps the whole machine recoverable.** No stage after 6
can change a Stage 6 decision, nothing runs that a person did not authorise by
name, and nothing in Stage 10 repeats work it merely found stuck.

Stage 6 has no endpoint at all — it is deterministic SQL, and it runs in the
database. That asymmetry is the architecture rather than an oversight: the layer
with the final say is the one with the least machinery in front of it.

The detector half of this service contains **no LLM call, no natural language,
and no business severity**. It answers *how unusual was this day, and on which
measures* — not how serious that is, what caused it, or what anyone should do.

The Stage 7 half is the only part of this project permitted to call a language
model, and it is the only part that could be switched off without the pipeline
losing a decision. It explains; it does not judge.

- **Stage 5 →** [Method](#method) · [Persistence](#persistence-and-idempotency)
- **Stage 7 →** [Root-cause hypotheses](#stage-7--root-cause-hypotheses)
- **Stage 8 →** [Delivery and review](#stage-8--delivery-and-human-review)
- **Stage 9 →** [Human-approved remediation](#stage-9--human-approved-remediation)
- **Stage 10 →** [Recovery and health](#stage-10--operational-reliability)

---

## The problem it has to solve

Weekday revenue in this dataset runs ~11–13k; weekend revenue ~5.7–6.4k. **Every
Sunday is roughly 45% of the trailing 7-day mean, by construction, every week.**

A detector comparing days against an undifferentiated recent average would rank
"it is Sunday" as its strongest and most frequent finding, fire ~104 times a
year, and be switched off within a fortnight.

The live case that makes this concrete — one run, and the shape of the argument
rather than the exact figures. The generator anchors its ninety days to today,
so your dates and totals will differ; `bootstrap.sh` records the days it used as
`SALESOPS_INCIDENT_DATE` and `SALESOPS_NORMAL_DATE`, and the control day is
chosen precisely because it has the property this table illustrates:

| | injected incident (Wed) | control day (Sun) |
|---|---|---|
| vs trailing 7-day mean | 46% | **44.5%** |
| vs its **own weekday** median | **−65%** | **+30%** |
| AOV | −64% | −3.9% |
| refund rate | +0.335 | −0.023 |
| orders | −2.8% | +31.6% |
| **verdict** | **anomaly, score 8.93** | **normal, score 2.21** |

Against a moving average the two look nearly identical. Against their own
weekday they could not be more different.

---

## Method

### 1. Baseline — what a day is compared against

Prior observations of **the same weekday**, most recent first.

| Tier | Requires | Window |
|---|---|---|
| `day_of_week` (preferred) | ≥ 6 prior same-weekday observations | latest 8 |
| `day_type` (fallback) | ≥ 10 prior same weekday-or-weekend days | latest 20 |
| `insufficient_history` | neither | — nothing is scored |

Nothing about Sunday is hard-coded. The rule is "same weekday"; the data
supplies the consequences. The `day_type` fallback accumulates about five times
faster, so a young warehouse gets a coarser baseline instead of nothing —
weekday multipliers span 0.92–1.12 and weekend 0.41–0.48, so each class is
internally consistent enough to compare within.

Two invariants:

1. **Strictly earlier dates only.** A baseline containing the day under judgement
   pulls the median toward it and hides the deviation being looked for. Nothing
   can see the future.
2. **Complete FX coverage only.** An understated revenue figure in the baseline
   would depress the median and make a genuine drop look ordinary.

A *past* anomaly still sits in the baseline of later days — unavoidable without
knowing in advance which days were anomalous. That is why the estimators are
robust ones.

### 2. Robust statistics — how unusual

```
median      = median(baseline)
MAD         = median(|x_i − median|)
scale       = max(1.4826 × MAD, 0.05 × |median|)
robust_z    = (observation − median) / scale
```

`1.4826 = 1 / Φ⁻¹(0.75)` converts MAD into a standard-deviation equivalent for
normal data, so a robust z is directly comparable to an ordinary z — which is
what lets the conventional **|z| > 3.5** outlier threshold (Iglewicz & Hoaglin,
1993) keep its usual meaning.

**Why not mean and standard deviation.** Both have a breakdown point of 0: one
extreme value drags the mean toward itself *and* inflates the standard
deviation, shifting the baseline toward the anomaly and widening the band around
it. One past anomaly makes the next one harder to see. The median and MAD have a
breakdown point of 50% — up to half the baseline can be corrupted before either
moves materially.

**The dispersion floor** (`0.05 × |median|`) exists because of a real case in
this data. Saturday order counts run `22, 23, 23, 23, 24, 23, 24` — the MAD is
exactly 0, the mean-absolute-deviation fallback yields a scale of 0.54, and an
ordinary 30-order Saturday lands at **z = 13**. That number is arithmetically
correct and practically meaningless: it reflects a dispersion estimated from
seven near-identical integers. Refusing to believe any series varies by less
than 5% of its own median cuts that to z = 6.1. The floor only binds on series
that are already implausibly smooth; for revenue (median ~12,000, scale ~2,400)
it is nowhere near active.

Fallback chain when MAD is 0: mean absolute deviation × 1.2533, then the floor.
`robust_z` is `NULL` only when the baseline is entirely zero — the one genuinely
undefined case.

### 3. Signals — four, weighted

| Signal | Source column | Weight | Deviation reported as |
|---|---|---|---|
| revenue | `net_revenue_usd` | **1.0** | percent |
| aov | `average_order_value_usd` | 0.5 | percent |
| refund | `refund_rate` | 0.5 | **absolute** |
| orders | `orders_count` | 0.5 | percent |

Refunds use an absolute difference because baseline refund rates sit near 0.02,
where a percentage change is numerically unstable: 0.02 → 0.35 is *+1,650%*, a
figure driven by the denominator rather than the size of the move.

Together the four separate **fewer orders** from **smaller orders** from
**refund-driven** decline — 2026-08-05 has revenue and AOV collapsing while
order count holds steady, which is a price/refund event, not lost demand.

### 4. Score

```
excess_s       = clamp(|z_s| − 1.0, 0, 10.0 − 1.0)
contribution_s = weight_s × excess_s
anomaly_score  = Σ contribution_s
is_anomaly     = anomaly_score ≥ 2.5
```

**The noise floor of 1.0 is load-bearing.** The first version of this detector
summed raw `|z|` and flagged a day in a synthetic series containing no injected
event at all. For a well-behaved metric `E[|z|] ≈ 0.8`, so four signals produce a
score near 2.0 on a completely unremarkable day — the score had no meaningful
zero and the threshold sat barely above the noise. Subtracting a floor means a
signal contributes only what exceeds ordinary variation, an ordinary day scores
0, and the number reads as *total evidence beyond normal fluctuation*.

**The threshold is derived, not chosen:**

```
2.5 = weight(revenue) × (3.5 − 1.0)
```

so revenue alone, at the textbook outlier level, is exactly break-even.
Consequences:

- A large isolated revenue move is still flagged, unaccompanied.
- The three supporting signals can flag a day without revenue (3.75 at |z| = 3.5
  each) — a day where order value, refunds and volume all move sharply while
  revenue does not is genuinely strange.
- **Corroborated moderate moves accumulate.** On 2026-08-05 revenue sits at
  z = −3.78, and its contribution alone (2.78) would clear the bar — but AOV
  (z = −4.29) and refunds (z = 39.2, capped) take the total to 8.93. A
  per-signal threshold detector would have caught less, and ranked it lower.

Absolute values throughout: a collapse and a spike are both worth a human look.
Direction is preserved in the signed `robust_z` and `deviation` columns.

The cap (|z| ≤ 10) keeps one pathological signal from swamping the sum and keeps
scores comparable between days.

### 5. What is *not* scored

| `baseline_status` | Meaning |
|---|---|
| `scored` | Compared against a real baseline |
| `insufficient_history` | Too few prior comparable observations to judge |
| `incomplete_kpi` | The day's KPI row lacked FX coverage |

Both non-verdicts are **recorded, not dropped**: `is_anomaly = false` with
`anomaly_score = NULL`. Absence of evidence is not evidence of normality, and a
consumer must be able to tell "we looked and it was fine" from "we could not
look". A database CHECK enforces that an unscored row can never be an anomaly.

Scoring an FX-incomplete day would measure a data gap as a revenue collapse —
exactly the false positive that destroys trust in a detector.

---

## Layout

```text
analytics-service/
├── analytics/
│   ├── statistics.py    robust estimators - median, MAD, robust z    (pure)
│   ├── baseline.py      which prior days a day is compared against   (pure)
│   ├── detector.py      signal scoring and combination               (pure)
│   ├── models.py        value types passed between them
│   ├── repository.py    the only module that touches PostgreSQL
│   ├── runner.py        sequencing and run counts
│   ├── api.py           HTTP surface for n8n
│   ├── config.py        environment-derived settings
│   └── cli.py           command-line entrypoint
└── tests/
```

The three `(pure)` modules have no I/O, so every formula is unit-tested against
hand-worked values rather than against a previous run of itself.

### No numpy, pandas or scipy

The series is ~90 observations and every operation is a sort or a mean over a
handful of values. Standard-library arithmetic keeps each formula visible in the
code that computes it and makes the unit tests exact, with no dependency on a
library's floating-point implementation. At millions of rows numpy would earn its
place; at this size it would only hide the mathematics.

Money arrives from PostgreSQL as `Decimal` and is converted to `float` at the
repository boundary. `NUMERIC` remains authoritative for every monetary value in
the warehouse and nothing here writes money back — what this service computes are
dimensionless statistics, where float is the right type.

---

## Persistence and idempotency

Results are keyed `(calendar_date, detector_version)` and **upserted**.

That is deliberately the opposite of `fact_orders`, which uses
`ON CONFLICT DO NOTHING`. A fact is an immutable observation of something that
happened; a detection is a derived opinion about it. If the KPI inputs change —
a backfill, an FX correction — the detection *should* change with them. Freezing
it would leave a verdict describing data that no longer exists.

`detector_version` (`v1.0.0`) makes algorithm changes traceable: bump it and both
versions' results coexist on the same dates, so a change can be diffed against
its predecessor instead of silently replacing it.

### The one column Stage 6 asked for

Migration V007 added `anomaly_daily.revenue_baseline_median` — the median of the
revenue baseline, which the scorer already computed for every signal but only
persisted in derived form as `revenue_deviation_pct`.

Stage 6 needs the absolute value to measure business impact in dollars.
Reconstructing it as `actual / (1 + pct/100)` would be lossy (the percentage is
`NUMERIC(14,4)`, so rounding error would reach every monetary threshold
comparison), undefined at −100%, and — the real objection — it would make the
decision layer depend on an inverted formula rather than on the number this
stage actually judged against. That is how two layers end up with contradictory
ideas of "expected".

**`detector_version` was not bumped, and that is correct.** Nothing computed
changes: no score, no verdict, no deviation moves by a digit. The version
describes the algorithm, and the algorithm is identical — bumping it would
falsely claim these results are not comparable with earlier ones. Re-running the
detector after V007 reproduced the same 90 rows, the same 11 anomalies, and the
same scores.

Only the revenue baseline is stored. The other three signals are already
persisted in exactly the form Stage 6 consumes them — AOV and orders as percent
deviations, refunds as an absolute rate difference — because only the money
measure feeds an absolute-dollar threshold.

### Full vs incremental

**Full is the default and the recommendation.** Baselines look backwards, so a
late-arriving order changes not only its own date's KPI row but the baseline of
every later date. An incremental run that only touched new dates would leave
earlier verdicts describing inputs that have since changed. At one row per
trading day a full recomputation takes ~4 seconds, so correctness is free.

`incremental` exists for when that trade-off flips — a long history where only
the newest date is genuinely new. It still reads the whole series to build
baselines; it only narrows what gets written.

---

# Stage 7 — root-cause hypotheses

`POST /anomalies/analyze` asks a language model to explain the anomalies Stage 6
has already judged actionable, and writes the result to
`salesops.anomaly_hypotheses`.

## The boundary, and why there are three of them

Stage 6 is authoritative for `severity`, `routing`, `decision`,
`notification_allowed` and `human_review_required`. The model reads them as
settled context and cannot write them. That is enforced three times over, and the
redundancy is deliberate — the interesting failure is not the model getting a
cause wrong, it is the model being quietly promoted to decision-maker.

| Layer | Mechanism | What it stops |
|---|---|---|
| **Prompt** | The verdict is stated as settled fact; no question invites a severity opinion | The model volunteering a grading |
| **Schema** | `extra="forbid"`, and no severity/routing/decision field exists | A returned `severity: "minor"` becoming a value someone reads |
| **Database** | A trigger checks the stored snapshot against the live decision | A service bug landing a contradictory row |

A model returning `severity: "minor"` beside an otherwise excellent analysis is
the most dangerous output this system can receive: it is valid JSON, it reads as
helpful, and a consumer that trusted it would have let the model overrule the
deterministic layer. It fails validation instead.

Stage 7 also has no code path that writes to `anomaly_decisions`. The absence is
the safety property, and it is asserted against the live table for six different
provider outcomes — success, timeout, exception, malformed JSON, schema
violation, empty response.

## Provider abstraction

```
analytics/llm/
    models.py     evidence package in, validated hypothesis out
    prompts.py    how evidence becomes a prompt, and the prompt version
    provider.py   the interface, one real implementation, one fake
    service.py    eligibility, orchestration, per-anomaly failure isolation
```

One real provider, speaking the OpenAI chat-completions dialect — which Groq,
OpenAI, Together, OpenRouter and most local runtimes all serve. There are not
three provider classes because the difference between these services is a base
URL and a model name, and a class hierarchy over that would be structure
inventing its own justification. `LLMProvider` is the seam if a genuinely
different protocol is ever needed.

Nothing in `llm/` talks to PostgreSQL. Evidence arrives as a value object and the
hypothesis leaves as one; the SQL lives in `repository.py` with every other query
in the service. Prompts and validation stay testable without a database, and the
SQL stays reviewable without reading prompt text.

## Configuration

Environment only. No key is ever hard-coded, persisted, logged, or placed in a
workflow.

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq`, `openai`, or anything with `LLM_BASE_URL` set |
| `LLM_MODEL` | provider default | Model line-ups move quickly — check the provider's current list |
| `LLM_API_KEY` | *(none)* | Required. A default secret is a hardcoded secret |
| `LLM_BASE_URL` | from provider | Only for a provider not listed above |
| `LLM_TEMPERATURE` | `0` | Same evidence, closely consistent output |
| `LLM_TIMEOUT_SECONDS` | `60` | |
| `LLM_JSON_MODE` | `schema` | See below |

`GET /llm` reports the resolved configuration and returns **no key, not even a
masked prefix** — a prefix is still a fact about a credential. `configured:
false` is enough to tell a missing key from a wrong one.

Stage 5 works with every one of these unset. The pipeline degrades to "no
explanations", never to "no detections" and never to "no decisions".

### Structured output

`schema` asks the provider to enforce the response JSON Schema, falling back once
to `json_object` if the model rejects that `response_format` — support varies by
model and by the week. That fallback is capability negotiation at the transport
layer, **not** repairing bad output: the response is validated identically either
way, and which mode was actually used is persisted in `json_mode`.

The live default exercises the fallback. Groq's `llama-3.3-70b-versatile` returns
`This model does not support response format json_schema`, so every stored
hypothesis carries `json_mode = object` — valid JSON guaranteed by the provider,
shape guaranteed by us. That is why the prompt states the output contract in full
rather than relying on the transport; see below.

## Evidence

Compact and labelled. The warehouse holds 90 days of orders; sending all of it
would cost more, read worse, and let the model anchor on whatever it noticed.

| Section | Source | Contents |
|---|---|---|
| Decision | `anomaly_decisions` | severity, routing, decision, version, reason codes |
| KPI | `kpi_daily` | actual/expected revenue, delta, AOV, refund rate, orders, customers, rolling means |
| Statistics | `anomaly_decisions` | anomaly score, four robust z-scores, deviations, signal count, baseline status |
| History | `kpi_daily` | 6 prior same-weekday days, 5 immediately preceding days |
| Absent | — | an explicit list of what this warehouse does not contain |

Every item carries a metric name, a unit, a source table and a date:

```
refund_rate = 35.72% (refunds as a share of gross revenue) [source: kpi_daily, 2026-08-05]
refund_rate_deviation = +33.55 percentage points (absolute change versus the
                        same-weekday baseline) [source: anomaly_decisions, 2026-08-05]
```

not `0.357222` and `0.335483`. A model given bare floats guesses at scale, and a
reviewer auditing the analysis afterwards has to guess twice. Refund deviation is
labelled *percentage points* specifically because baseline refund rates sit near
0.02, where a percentage change reads as thousands of percent for an ordinary
move.

**No future data.** History queries are bounded `calendar_date < target`, and the
package re-asserts it before every prompt. This is the one leak that would not
announce itself: a model shown what happened next explains the anomaly perfectly,
using information nobody had at the time, and nothing raises.

## Prompting

`PROMPT_VERSION = "stage7-prompt-v2"`, persisted with every result. A material
change bumps it and produces a **new row beside the old reasoning** rather than
overwriting it.

> **Why v2 exists — the bug the mock provider could not catch.**
> v1 ended with "reply with a single JSON object matching the required schema"
> and never showed a schema. That is fine only for a provider enforcing one at
> the transport layer. On the first live run, Groq rejected `json_schema`, the
> request correctly fell back to `json_object`, and the model returned
> well-formed JSON of a *different shape*: no `summary`, and `supporting_evidence`
> as a list of strings rather than objects.
>
> Validation rejected both responses, nothing was persisted, and the Stage 6
> decisions were untouched — the safety machinery worked exactly as designed. The
> prompt was the thing at fault, for never saying what to produce. v2 states the
> full contract: every field, its type, which arrays hold objects rather than
> strings, and an explicit instruction not to return severity or routing.
>
> The mock provider is handed a payload that is correct by construction, so no
> offline test could have found this. Five tests now assert the contract is
> present, including one that checks the contract contains no example *content* —
> a worked example invites the model to copy it.

Three things the prompt is careful about:

**Stage 6 is context, never a question.** The verdict is presented as settled and
the model is asked to explain it. There is no "is this critical?", no "should this
be escalated?", and a test scans the instructions for interrogatives that could
be mistaken for one.

**Absent data is named.** The warehouse has no campaigns, no payment logs, no
support tickets. A model not told this reaches for them anyway, because those are
the causes that explain revenue anomalies in its training data. Listing what is
missing converts a tempting invention into an acknowledged gap — and
`missing_evidence` gives it somewhere legitimate to go. The model may *propose* a
payment-gateway problem; it may never assert one as observed.

**Data is data.** All evidence sits inside a delimited block that the system
prompt defines as untrusted, with an instruction to ignore any directive found
inside it. The delimiters are neutralised within the block, so a value containing
`</evidence>` cannot close it early and have the text after it read as
instructions. The current synthetic source contains almost no free text; the
boundary is built now because the alternative is discovering it from the outside,
later.

## Output

```json
{
  "summary": "...",
  "confidence": "low | medium | high",
  "primary_hypothesis": "...",
  "supporting_evidence":    [{"metric": "...", "observation": "...", "relevance": "..."}],
  "alternative_hypotheses": [{"hypothesis": "...", "why_plausible": "...",
                              "what_would_confirm": "..."}],
  "missing_evidence":   ["..."],
  "recommended_checks": ["..."]
}
```

**Confidence is about the explanation, not the anomaly.** Stage 5 established
that the day was unusual and Stage 6 established that it matters; neither is the
model's to revisit. `confidence` says how strongly the available evidence
supports *its proposed cause*. Most explanations built from aggregate KPIs alone
are honestly `medium` or `low`, and the prompt says so.

### Validation

LLM output is untrusted input. A failed validation is a Stage 7 failure, **never
a repair** — filling in a missing field means inventing content the model did not
produce, which is the exact fabrication this stage exists to prevent.

Rejected: invalid JSON · missing required fields · empty summary or hypothesis ·
no supporting evidence · a confidence value outside `low|medium|high` · any
unknown field, at any nesting depth · **any metric cited as support that was not
in the evidence package**.

That last one is the only mechanical defence against a fabricated fact. A model
writing `payment_gateway_error_rate = 14%` under supporting evidence has produced
something formally indistinguishable from a real observation. Requiring the
metric name to come from the package makes that specific fabrication impossible.

Its limits are worth stating plainly: it cannot police prose. A speculative
sentence in `primary_hypothesis` is discouraged by the prompt and by the
hedging vocabulary, not enforced by code.

## Idempotency and provenance

Identity is `(anomaly_id, decision_version, prompt_version, model_name)`, with
`ON CONFLICT DO NOTHING` — the opposite of Stages 5 and 6, which upsert.

A detection and a decision are derived opinions that must track their inputs. A
hypothesis is a generated artefact with provenance; overwriting one silently
replaces reasoning a human may already have read and acted on. Change the prompt
or the model and a **new row appears beside the old one**. Replacing an existing
analysis requires `regenerate: true`, which the scheduled workflow never sets.

Eligibility is evaluated in SQL, so a second run costs nothing: it builds no
evidence packages and makes no provider call.

Persisted with every hypothesis: `model_provider`, `model_name`,
`prompt_version`, `evidence_digest` (SHA-256 of exactly what the model was
shown), `json_mode`, and — when the provider supplies them — `request_id`,
token counts and latency. Two answers that differ can be shown to have come from
identical or different inputs.

## Failure

An LLM failure must never corrupt a deterministic decision, so failures are
isolated per anomaly: one timeout affects that anomaly and nothing else.

| Outcome | Run status |
|---|---|
| All eligible analysed | `success` |
| Nothing eligible | `success` — a quiet week is not a fault |
| Some failed | `partial` |
| Every attempt failed | `failed` |

In every case the Stage 6 decision is unchanged: the anomaly is still critical,
still routed to a human, still carrying its reason codes. It is simply
unexplained. Failure reasons are recorded per date in `ingestion_runs`, and are
truncated and credential-free by construction.

## Cost control

Only `decision = 'action_required'` records are ever sent to a model — 11 of 90
days in the live dataset. Normal days, unscorable days and `no_action` decisions
are never analysed: it would spend money to explain that nothing happened.
Evidence windows are small, re-runs are free, and `limit` caps a run — logging
what it dropped, because a silent cap reads as "we covered everything".

---

# Stage 8 — delivery and human review

`POST /notifications/process` turns Stage 6 decisions into delivered
notifications and queued review items. `/reviews/*` is the queue.

## Routing is read, never re-derived

```
minor     -> auto_notify   -> notification delivered
major     -> human_review  -> review item queued
critical  -> human_review  -> review item queued
none      -> no_action     -> nothing at all
```

Nothing in this package computes severity or decides who should be told. The two
eligibility questions are single predicates over columns Stage 6 owns:

```sql
needs_notification = notification_allowed AND routing = 'auto_notify'
                     AND decision = 'action_required'
needs_review       = human_review_required AND routing = 'human_review'
                     AND decision = 'action_required'
```

and the database refuses a row whose snapshot does not satisfy them. Sending a
notification for a `no_action` anomaly is a write the database rejects, not a bug
review has to catch.

`notification_allowed` means **automated notification is permitted**. It does not
mean the anomaly may be automatically acted upon — major and critical go to a
person precisely because something might need doing.

## Provider abstraction

```
analytics/notifications/
    models.py     what gets delivered, and what an attempt returned
    provider.py   the interface, one real implementation, one fake
    service.py    eligibility, rendering, delivery, retry, the review queue
```

The real provider POSTs JSON to a configured webhook, which Slack, Teams,
Discord, PagerDuty, Zapier, an internal relay and most mail gateways all accept.
A webhook over an SMTP client is deliberate: it can be exercised end to end
locally without a mail server, a messaging workspace, or anyone's inbox being
used as a test fixture.

| Variable | Default | Notes |
|---|---|---|
| `NOTIFICATION_PROVIDER` | `webhook` | |
| `NOTIFICATION_WEBHOOK_URL` | *(none)* | Required. **Treated as a credential** |
| `NOTIFICATION_TIMEOUT_SECONDS` | `15` | |
| `NOTIFICATION_FROM` | `salesops-pipeline` | |
| `NOTIFICATION_RECIPIENTS` | *(none)* | Comma-separated; part of the idempotency key |

`GET /notifications` reports the webhook **host** and never the path — webhook
secrets live in the path, and an ops endpoint is a poor place to hand one out.

For local work, point it at the service's own in-memory sink:

```
NOTIFICATION_WEBHOOK_URL=http://analytics-service:8000/dev/notification-sink
```

`/dev/notification-sink` is capped at 50 entries, never persisted, and exists
because validating delivery against a real channel means either an integration
nobody trusts or a genuine one everybody mutes. It is not for a real deployment.

## What a notification says

Three labelled blocks, because a reader scanning between other work must not come
away believing a cause has been established.

```
OBSERVED:
  2026-08-05 was graded CRITICAL by the deterministic rules (routing: human_review).
  Net revenue $4,748.95 against an expected $13,641.63 - $8,892.68 below baseline (-65.2%).
  Average order value $93.12, refund rate 35.72%, 51 orders.
  Anomaly score 8.93 (3 of 4 signals individually significant).
  Reason codes: CRITICAL_COMBINED_IMPACT, SEVERE_REFUND_SPIKE, ...

HYPOTHESIS:
  Most plausible explanation: consistent with a refund-related operational issue.
  Confidence in this explanation: medium.

NOT CONFIRMED:
  The explanation above is a hypothesis, not a finding. It has not
  been verified against any system outside this warehouse.
    - Refund reason codes are not stored.

This anomaly requires human review before any action is taken.
```

**A reader who stops after the first block has read only true things.** A test
asserts the OBSERVED block contains no speculative vocabulary at all.

## Missing Stage 7 output

A Stage 7 failure never blocks routing. The review item is created either way,
carrying the deterministic evidence — which is what justified the escalation —
and the notification says plainly:

```
AI analysis unavailable for this anomaly. No explanation has been generated;
the observed evidence above stands on its own.
```

`hypothesis_status = 'unavailable'` is a first-class state, not an error.

## Idempotency and retries

Identity is `(anomaly, decision_version, channel, recipient)` for notifications
and `(anomaly, decision_version)` for reviews. A rerun delivers nothing new and
makes no provider call — the check happens before the message is sent, not after.

| Status | Meaning |
|---|---|
| `pending` | created, never attempted |
| `sent` | delivered — a rerun will not resend |
| `failed` | retryable, budget not spent — a later run retries |
| `abandoned` | permanent failure, or three attempts spent |

Every attempt is recorded with its outcome classified as `success`,
`retryable_failure` (timeout, connection failure, 429, 5xx) or
`permanent_failure` (400, 401, 403, 404, 422). Retrying a timeout is free and
often works; retrying a 401 three times only delays the moment somebody notices.

Re-sending an already-delivered notification requires `resend: true`, which the
scheduled workflow never sets.

## The review queue

```
pending -> in_review -> resolved | dismissed
pending -> dismissed
in_review -> pending          (release a claim)
resolved / dismissed          terminal
```

Enforced by a database trigger, so a transition is refused rather than silently
accepted, and every move appends to `review_events` with actor and timestamp.
Terminal resolutions and notes cannot be edited afterwards.

A reviewer records `status`, `resolution` and `review_notes`. They cannot touch
severity, routing, decision or either flag — the snapshot guard refuses it. A
resolution of `false_positive` does not make a critical anomaly minor; it records
what a person concluded, and nothing fires because of it.

Review notes are treated as untrusted input: length-bounded, stored verbatim
rather than interpreted, never rendered into a notification payload, and never
concatenated into anything executed or sent to a model.

## Stage 8 does not act

No refunds, no order changes, no CRM updates, no tickets, no remediation, no
approval. The last thing that happens to an anomaly here is a row saying someone
was told, or a row saying someone must look. A test asserts that a full routing
run and a complete review lifecycle leave `fact_orders`, `kpi_daily`,
`anomaly_daily` and `dim_customer` byte-identical.

## Authentication

There is none — on these endpoints or any other in this service. It listens on
the Docker network and is not published beyond the development host port. That is
a real limitation, stated rather than papered over with a shared secret in an
environment variable, which would look like authentication without being any.

---

# Stage 9 — human-approved remediation

The only stage that executes anything, and the one with the least discretion.

## Three acts, not one

```
POST /reviews/{id}/approve      in_review -> approved, and one action proposed
POST /remediation/{id}/approve  proposed  -> approved   (this action is the response)
POST /remediation/{id}/execute  approved  -> executing -> executed
```

Approving the **review** answers *is this anomaly real, and does it warrant a
response?* Authorising the **action** answers *is this the response?* Those are
different judgements. A system that collapses them cannot record a reviewer who
confirmed an anomaly and then rejected the action proposed for it — which is a
thing reviewers do constantly.

Executing is the third act, and it is mechanical: it runs what somebody already
authorised. No endpoint combines it with either of the first two.

## `resolved` is not approval

Stage 8 shipped with one closing state for a reviewed anomaly. It could not
distinguish *confirmed, and something should be done* from *confirmed, and
nothing should be done*, and reading either as consent would be guessing at what
a person meant. V011 adds one state and changes nothing else:

```
in_review -> approved    confirmed, and remediation is authorised
in_review -> resolved    reviewed and closed WITHOUT remediation
in_review -> dismissed   not worth pursuing
```

Approval also requires a **confirming** resolution. You cannot authorise action
on something you have just called a false positive, and
`expected_business_variation` says the movement was normal — normal movements do
not need remediating. Both are refused by a CHECK constraint rather than by
convention.

## The action vocabulary

| Action | Permitted at | What it asks for |
|---|---|---|
| `create_investigation` | major, critical | Establish the cause and record what is found |
| `request_operations_review` | major, critical | Review fulfilment, order handling and systems for this date |
| `request_refund_review` | **critical only** | Re-examine the refunds issued on this date |

All three are **requests for human work**. None issues a refund, cancels an
order, contacts a customer, changes a price or touches inventory. That is the
scope rather than a shortcoming: this project has no downstream system that
could safely perform any of those, and building a fake ERP to execute against
would produce a convincing demonstration of something that does not exist.

Refund review is held back to `critical` because it asks people to re-open
settled financial transactions — the most disruptive thing in the vocabulary.
Stage 6 has already published which days it considers worth that, and reusing
its answer beats forming a second opinion here.

`minor` never appears in the table at all, and not by a separate rule: Stage 6
routes minor to `auto_notify`, so no review item is ever created for one, so
there is nothing to approve. If a minor anomaly ever needs remediating, the
honest fix is to change Stage 6's routing — not to open a door into Stage 9 that
never passes a person.

## Eligibility is a foreign key

```sql
CONSTRAINT remediation_actions_eligible_fk
    FOREIGN KEY (policy_version, severity, action_type)
    REFERENCES salesops.remediation_action_eligibility
               (policy_version, severity, action_type)
```

Asking for a refund review on a `major` anomaly is not a check somebody has to
remember to write. There is no row to reference, so the insert fails.

That alone would leave one gap — claim a severity the review does not carry, and
an ineligible action looks eligible — so a guard trigger compares the whole
authorisation snapshot against the live review *before* the foreign key is ever
consulted. Both are tested by trying exactly that.

## The state machine

```
proposed  -> approved  -> executing -> executed     (terminal)
proposed  -> rejected                               (terminal)
proposed  -> cancelled                              (terminal)
approved  -> cancelled                              (terminal)
executing -> failed    -> executing                 (retry, bounded at 3)
failed    -> cancelled                              (terminal)
```

`executed` has **no outgoing transition**. An action that has run cannot run
again, however it is asked — through the service, through the batch endpoint, or
by writing to the table directly.

`failed` is a resting state rather than a dead end: an explicit retry may move
it back to `executing` while the attempt budget lasts. Once spent, the trigger
refuses and the action drops quietly out of the work set instead of raising on
every scheduled run for the rest of the system's life.

Cancellation is not permitted from `executing` or `executed`. An action already
handed to a provider cannot be un-handed by changing a row, and recording
otherwise would put a lie in the audit trail.

## Executed exactly once

Entering `executing` is a conditional UPDATE:

```sql
UPDATE salesops.remediation_actions
SET status = 'executing', executed_by = %(actor)s
WHERE remediation_id = %(id)s
  AND status IN ('approved', 'failed')
  AND attempt_count < 3
RETURNING ...
```

Two concurrent callers race here; exactly one wins; the loser finds no row and
does nothing. No lock is held across the provider call, because a lock held
across a network call is a lock held for however long the network feels like
taking.

The idempotency key is generated and stored rather than living only in an
`ON CONFLICT` clause:

```
idempotency_key = review_id : action_type : decision_version
```

`decision_version` is part of it because a re-decided anomaly is a *different*
authorisation, not the same one again.

## The provider

```
analytics/remediation/
    models.py     the action vocabulary, and what a request carries
    provider.py   the interface, and one development implementation
    service.py    approval, authorisation, execution, retry, the audit trail
```

There is one provider, `RecordingRemediationProvider`, and it is honest about
what it is. It records the request, returns a deterministic result, and contacts
nothing. Every attempt it writes carries `external_side_effect = false`, the
audit view reports that column rather than leaving a reader to infer it, and the
reference it returns is `local-record-N` — deliberately not shaped like
`TICKET-4821`, which would invite somebody to go looking for it.

Stage 8 ships a real provider because a webhook is a real destination. Stage 9
has no equivalent, so rather than a mock wearing a convincing name, the boundary
itself is the deliverable: one method, a validated request in, a classified
result out. A real provider — a Jira client, a ServiceNow client, an internal
case API — implements that method and changes nothing else. The state machine,
the retry budget, the idempotency key and the audit trail all sit outside it,
which is where they belong: they are properties of the system, not of whichever
ticketing product an organisation happens to run.

## What the provider is told

Three blocks: what is being requested, what was observed, and who authorised it.

```json
{
  "action": {
    "action_type": "request_refund_review",
    "request": "Re-examine the refunds issued on this date and confirm each was legitimate.",
    "note": "This action is a request for human investigation or review. Executing it
             records the request and notifies nobody's systems: it issues no refund,
             changes no order, contacts no customer and moves no money."
  },
  "observed": { "calendar_date": "2026-08-05", "severity": "critical", "...": "..." },
  "authorization": {
    "review_id": 5040,
    "approved_by": "alex@revenue-ops",
    "approved_at": "2026-08-13T16:31:02+01:00",
    "policy": "Authorised by a human through the Stage 8 review queue."
  }
}
```

**No Stage 7 content is included.** A hypothesis is a guess, and putting one in
front of the person asked to investigate would anchor the investigation on it.
The hypothesis id, model and prompt version are recorded on the row for
provenance — so an auditor can see what the approver was shown — and a test
asserts that none of it reaches the payload.

## What Stage 9 cannot do

It never writes to `anomaly_decisions`, `anomaly_hypotheses` or `notifications`,
and the only Stage 8 write it makes is the one review transition a human
explicitly asked for. It computes no severity. It reads no model output — the
remediation package does not import `analytics.llm` at all, and a test enforces
that structurally rather than trusting the paths somebody thought to write.

A test drives one anomaly through every outcome the stage can produce —
executed, failed, retried to exhaustion, rejected, cancelled — and asserts that
`fact_orders`, `kpi_daily`, `anomaly_daily`, `dim_customer`, every Stage 6
verdict and every Stage 7 hypothesis are byte-identical afterwards.

## Authentication

Still none, and `actor` is whatever the caller says it is. The whole stage rests
on the identity of the person who approved something, and that identity is
asserted rather than authenticated. It is the largest gap in Stage 9, stated
plainly rather than papered over with a shared secret in an environment
variable — which would look like authentication without being any.

---

# Stage 10 — operational reliability

Not a pipeline stage. The parts that let the other nine run unattended.

## Recovery is not re-execution

Every distinction in this stage comes off this one:

```
RECOVERY      moves a stuck record into an honest, final-or-actionable state.
              It never repeats work.
RE-EXECUTION  repeats work. It is always somebody's explicit decision.
```

Closing a run abandoned at `running` does not re-run it. Moving a crashed
remediation out of `executing` does not call a provider. Replay is the only
operation here that deliberately repeats anything, it is explicit, bounded, and
idempotent against `fact_orders`.

## Three vocabularies that are not each other

| | Values | Answers |
|---|---|---|
| **Anomaly severity** (Stage 6) | `none` `minor` `major` `critical` | How serious was this day? |
| **Operational health** (Stage 10) | `healthy` `warning` `degraded` `failed` | Is the pipeline working? |
| **Review ageing** (Stage 10) | `fresh` `warning` `overdue` `critical_overdue` | How long has this waited? |

A critical anomaly reviewed within the hour is a healthy pipeline. A minor one
unclaimed for a week is not. The words are deliberately different so the two can
never be confused in a query, and a schema check asserts that no ageing bucket
is ever named after a severity.

## What could get permanently stuck, and what now happens

| Stuck state | Cause | Recovery | Repeats work? |
|---|---|---|---|
| `ingestion_runs.status = 'running'` | process died after the run opened | closed as `failed` with `STALE_RUN_TIMEOUT` | no |
| `remediation_actions.status = 'executing'` | process died around a provider call | moved to `execution_unknown` | **no — see below** |
| `notifications` not `sent` | routing stopped running | reported stale; Stage 8 retries it | no (Stage 8 owns delivery) |
| `raw_orders_staging.processing_status = 'failed'` | validation rejected the payload | explicit, bounded replay | yes, by request |
| settled staging rows | nothing ever deleted them | retention sweep | n/a |
| open `review_queue` items | nobody looked | **ageing labels only** | never |

## `execution_unknown`

The sharpest problem in the stage, and the one worth reading.

A remediation action enters `executing`, the provider is called, and the process
dies. Nothing in this database can know whether that call landed. Both automatic
answers are wrong:

- **re-execute** — might do the thing twice
- **fail it** — might claim something did not happen when it did

So recovery produces neither. It records an attempt with outcome `unknown`
(an attempt *was* made; what it achieved is not known), moves the action to
`execution_unknown`, and stops.

```
executing → execution_unknown → executed   (a human confirmed it happened)
                              → failed     (a human confirmed it did not)
                              → cancelled
```

There is deliberately **no transition back to `executing`**. Confirming an
execution did not happen returns the action to `failed`, where Stage 9's ordinary
bounded retry applies — so a retry is always something a person chose, never
something recovery did. `execution_unknown` is also absent from
`remediation_pending_execution`, so the Stage 9 workflow cannot pick it up.

Reconciliation requires an actor **and** a statement of evidence. Unattributed
or unexplained, a reconciliation is a guess with a timestamp.

```bash
curl -X POST http://localhost:8001/remediation/468/reconcile \
     -H "Content-Type: application/json" \
     -d '{"outcome":"confirmed_not_executed","actor":"dana@finance",
          "evidence":"Provider record shows no request was received."}'
```

## Replay

The rule replay is built around: **it must never make a failure look like it
never happened.**

The original staging rows are never modified. Their payloads are copied verbatim
into a new batch under a new run, and the mapping is recorded, so both facts stay
true in separate places:

```
did the first attempt fail?   raw_orders_staging.processing_status  -> 'failed'
did a replay of it succeed?   ingestion_replays.outcome             -> 'succeeded'
```

The replay run uses `source = 'ingestion-replay'`, not `'mock-sales-api'`. Stage 3
computes its next window from the newest successful `mock-sales-api` run; a replay
landing in that source would move the window forward and silently skip a day of
real orders.

```bash
curl -X POST http://localhost:8001/operations/replay \
     -H "Content-Type: application/json" \
     -d '{"batch_id":"11111111-...","actor":"ops@example.invalid"}'
```

The request takes a batch id **and nothing else**. No payload, no override, no
correction field: a replay endpoint that accepted order data would be an
unauthenticated write path into `fact_orders` wearing a recovery label.

Outcomes per row are recorded individually:

| Outcome | Meaning |
|---|---|
| `succeeded` | valid now, and loaded |
| `duplicate` | the order was already in `fact_orders`; nothing was written |
| `failed_again` | still invalid. Validation is deterministic |

Bounded at `max_replay_attempts` (3). A row that has failed validation three
times is failing for a reason replay cannot fix.

## Retention

Deliberately conservative. Only rows that have **settled successfully** are ever
eligible:

```
processed / skipped   eligible once older than staging_retention_days (90)
pending               never — unfinished work
failed                never — the dead-letter trail, and the replay source
```

Keeping every failed row forever is stricter than a retention policy needs to be.
It is also the only default that cannot lose evidence; if the volume ever becomes
a problem that deserves a deliberate archival decision, not a smaller number in a
config table.

Dry run by default, in the API and in SQL. A cleanup whose safe mode has to be
asked for is one that will eventually be called without arguments.

## Notification retry

Stage 8 already had bounded retry, an explicit `abandoned` state and full attempt
history. What it had no notion of was **time**: a notification at `failed` with
attempts left is retried by the next routing run, and if the routing schedule
itself stops, nothing notices.

So Stage 10 adds detection, not a second delivery path. Retrying means asking
Stage 8 to route again, restricted to the dates that have a stale notification —
its own idempotency key, its own retry classification, its own attempt
accounting. `resend` is never set, so a notification already `sent` cannot be
touched, and the Stage 8 invariant that `sent_at` reflects the *current* status
is preserved untouched.

## Health

One row per pipeline and per operational condition, each with the numbers behind
it:

```
component  status  reason_code  observed_value  threshold_value  measure
```

A status a caller cannot recompute from the same inputs is a status nobody argues
with when it is wrong. The overall status is the **worst** individual one — a
pipeline is not "mostly healthy".

**No LLM output is read anywhere in it.** The view reads whether Stage 7 *ran*,
never what it *said*, and a schema check asserts it does not reference
`anomaly_hypotheses`. A health signal a language model could influence would be
one nobody could trust during the incident that mattered.

## The retry queue

Every failed operational record in one shape, whatever produced it, because the
question at 3am is the same regardless of which subsystem failed:

| `disposition` | What to do |
|---|---|
| `SELF_HEALING_NEXT_RUN` | nothing — the ingestion window self-corrects |
| `RETRY_VIA_STAGE8_ROUTING` | nothing — the next routing run retries it |
| `RETRY_VIA_STAGE9_WORKFLOW` | nothing — the next remediation run retries it |
| `REPLAYABLE` | `POST /operations/replay` |
| `AWAITING_RECONCILIATION` | a human must reconcile it |
| `RETRY_BUDGET_SPENT` / `ABANDONED` | investigate; nothing further is automatic |

## Configuration

Thresholds live in `salesops.operational_config`, one row each, exactly as the
Stage 6 thresholds live in `decision_thresholds`. An operator asking "how old is
stale?" runs one `SELECT`, and a change is a visible row rather than an edited
constant. An unknown key **raises** rather than returning a plausible default — a
typo in a threshold name must not quietly disable a safety check.

| Key | Default | |
|---|---|---|
| `staging_retention_days` | 90 | days a settled staging row is kept |
| `stale_run_timeout_minutes` | 120 | longer than the slowest pipeline |
| `stale_notification_timeout_minutes` | 180 | longer than the gap between routing runs |
| `stale_remediation_timeout_minutes` | 60 | short: the provider call is HTTP-bounded |
| `review_warning_age_hours` | 24 | |
| `review_overdue_age_hours` | 72 | |
| `review_critical_overdue_age_hours` | 168 | one week |
| `max_replay_attempts` | 3 | |
| `retry_backoff_minutes` | 30 | flat, not exponential |

These are **operational defaults, not business requirements**. None was handed
down by a retention policy or an SLA; they are starting points chosen to be safe.

## The audit log

`salesops.operational_events` records everything Stage 10 did *to* the pipeline,
and is **append-only, enforced by trigger**. `UPDATE` and `DELETE` are both
refused. The specific failure mode that guards against is an automated process
tidying away the evidence of what it did.

## Authentication

Still none, and `actor` is still whatever the caller says it is. Recovery
operations are attributed to `stage10-recovery`, which is a convention rather
than an identity. The replay endpoint's narrow input shape is the one place this
is partially mitigated: it accepts a batch id, so a caller who reaches it cannot
inject data — only ask the warehouse to re-read its own.

---

## Running it

The n8n **Statistical Anomaly Detection** workflow calls this service daily at
07:00, after FX (05:00) and the KPI refresh (06:00).

```bash
# HTTP, as n8n calls it
curl -X POST http://localhost:8001/detect \
     -H "Content-Type: application/json" -d '{"mode":"full"}'

curl http://localhost:8001/health
curl http://localhost:8001/detector      # the constants defining this version

# CLI, inside the container
docker compose exec analytics-service python -m analytics.cli detect --mode full
docker compose exec analytics-service python -m analytics.cli show --anomalies-only
```

The **LLM Root Cause Analysis** workflow calls it at 08:00, after the decision
pass at 07:30, and **Notification & Review Routing** at 08:30.

```bash
curl http://localhost:8001/llm           # resolved config; never returns the key

# Every actionable anomaly that has no analysis yet
curl -X POST http://localhost:8001/anomalies/analyze \
     -H "Content-Type: application/json" -d '{}'

# One date, replacing any existing analysis for this prompt version and model
curl -X POST http://localhost:8001/anomalies/analyze \
     -H "Content-Type: application/json" \
     -d '{"dates":["2026-08-05"],"regenerate":true}'
```

```bash
curl http://localhost:8001/notifications      # config; never returns the webhook path

# Route every actionable anomaly: notify the minor ones, queue the rest
curl -X POST http://localhost:8001/notifications/process \
     -H "Content-Type: application/json" -d '{}'

curl "http://localhost:8001/reviews?status=pending"      # the queue
curl http://localhost:8001/reviews/1                     # one item, with history

curl -X POST http://localhost:8001/reviews/1/claim \
     -H "Content-Type: application/json" -d '{"actor":"alex@revenue-ops"}'
curl -X POST http://localhost:8001/reviews/1/resolve \
     -H "Content-Type: application/json" \
     -d '{"resolution":"confirmed","notes":"Verified against order detail."}'

curl http://localhost:8001/dev/notification-sink         # what was delivered locally
```

Interactive docs: <http://localhost:8001/docs>

### Tests

```bash
cd analytics-service
pip install -r requirements-dev.txt
python -m pytest
```

555 tests. The unit tests need nothing running; the integration tests skip
automatically when the warehouse is unreachable. **No test touches a real
provider** — Stages 7, 8 and 9 are all exercised through fakes, so the suite runs
offline, costs nothing, sends nothing to anybody, remediates nothing, and gives
the same answer every time.

Stage 5:

- `test_statistics.py` — estimators against hand-worked values, contamination
  resistance, the dispersion floor, degenerate baselines
- `test_baseline.py` — no future leakage, day-of-week separation, window sizing,
  tier fallback, order independence
- `test_detector.py` — drops, spikes, corroboration, seasonality *not* flagged,
  unscorable observations, reproducibility
- `test_integration.py` — persistence contract, DB constraints, idempotency, and
  behaviour on the live series

Stage 7:

- `test_llm_prompt.py` — the Stage 6 verdict reaches the model intact; no
  interrogative invites a severity opinion; evidence is labelled and sourced;
  values are humanised; no future date can appear; the injection boundary holds
  against a value containing `</evidence>`
- `test_llm_validation.py` — malformed, incomplete and empty responses; every
  Stage 6 field rejected individually; unknown fields at any depth; ungrounded
  metric citations
- `test_llm_service.py` — eligibility against live Stage 6 data, idempotency,
  regeneration, provenance, the snapshot guard, and **no Stage 6 decision
  changing across six different provider outcomes**
- `test_llm_config.py` — environment configuration, blank-is-unset, and that no
  code path can reveal the key

Stage 8:

- `test_notification_content.py` — the three labelled blocks; measured values
  never leave OBSERVED; the hypothesis is always marked unconfirmed; a missing
  analysis says so instead of inventing one; no credential-shaped content
- `test_notification_routing.py` — eligibility read from Stage 6, the eligibility
  CHECK and snapshot guard, idempotency, retry classification and bounds, run
  status, and **no Stage 6 decision changing across six delivery outcomes**
- `test_review_queue.py` — every valid and invalid transition, terminal
  immutability, review notes as untrusted input, and that a reviewer cannot
  re-grade an anomaly

Stage 9:

- `test_remediation_authorization.py` — the boundary. Every state that does *not*
  authorise anything (pending, in_review, dismissed, resolved), every resolution
  that cannot carry an approval, eligibility by severity, a fabricated snapshot,
  a duplicate approval, and a direct INSERT that bypasses the service entirely
- `test_remediation_execution.py` — the state machine, the exactly-once
  guarantee counted against the provider's own call log, the retry budget,
  a provider that raises, and every illegal transition
- `test_remediation_boundaries.py` — Stages 6, 7 and 8 byte-identical after a
  full lifecycle; the warehouse untouched; no LLM import anywhere in the
  package; no secrets, no severity logic and no approval path in the workflow

Stage 10:

- `test_operational_recovery.py` — stale runs and stale executions, the timeout
  boundary in both directions, idempotency, and the property the stage exists
  for: **recovery never calls a provider**
- `test_operational_replay.py` — replay reuses the original payload, never
  modifies the original row, never duplicates an order, is bounded, and cannot
  move the ingestion window; retention protects `pending` and `failed` rows
- `test_operational_health.py` — health is deterministic and explained, reads no
  model output, ageing changes no review state, and the maintenance workflow
  writes only to operational tables

The three most load-bearing tests are the ones that snapshot every severity,
routing, decision, notification flag and review flag in the live database and
assert it is byte-identical afterwards:
`test_no_stage6_decision_changes_whatever_the_model_does` (Stage 7, six provider
outcomes) and `test_no_stage6_decision_changes_whatever_delivery_does` (Stage 8,
six delivery outcomes). A third,
`test_no_stage6_decision_changes_whatever_remediation_does` (Stage 9, driving one
anomaly through executed, failed, exhausted, rejected and cancelled). Two more —
`test_stage8_performs_no_business_action` and `test_stage9_performs_no_business_action`
— do the same for `fact_orders`, `kpi_daily`, `anomaly_daily` and `dim_customer`,
which is how "no business action is taken" stops being a claim in a README.

---

## Known limitations

- **Flag rate is ~16% of scored dates (11 of 70).** Stage 5 is deliberately a
  *statistical* filter, not an alerting layer — the specification puts business
  severity in a later stage, and the score is what that stage will threshold on.
  The separation is visible in the numbers: the injected incident scores several
  times higher than the marginal days around it.

  Stage 6 now does that thresholding, resolving the flags into `critical`,
  `major` and `minor` so that only a handful of the ninety days reach a human —
  in practice around one a fortnight. It also reverses the score ordering where
  the money justifies it, grading a loud but cheap day `minor` and a quieter,
  expensive one `major`. Exact counts depend on the generated series and are not
  quoted here; the ordering property itself is asserted by
  [test_stage6_decisions.py](../n8n/tests/test_stage6_decisions.py). See
  [database/README.md](../database/README.md#the-decision-layer--stage-6).
- **Weekend days are flagged more often than weekdays (25% vs 13%).** This
  reflects the data, not a seasonality artifact: Sunday revenue ranges 1,846 to
  14,713 across the series, genuinely heavy-tailed. Every weekend flag carries a
  signal with |z| > 3 against its own weekday, which the tests enforce.
- **20 of 90 dates are unscorable** for insufficient history — the first ~3 weeks,
  where the day-type fallback also lacks 10 observations. Expected, and explicit.
- **The first same-weekday observations carry disproportionate weight** while a
  window is filling, since a median over 6 values moves more than one over 8.
- **Signals are treated as independent** when summing contributions. Revenue, AOV
  and order count are correlated in reality (revenue ≈ orders × AOV), so a
  genuine revenue event is somewhat double-counted. This is intentional — it is
  what makes corroboration strengthen the score — but it means the score is
  evidence-weighted, not a calibrated probability.
- **No trend or level-shift handling.** A permanent step change in revenue will
  be flagged for as many days as the baseline window takes to absorb it (up to 8
  same-weekday observations). Detecting *change points* rather than *outliers* is
  a different problem.
- **No multi-day event detection.** Each day is scored independently; a slow
  three-week decline where no single day is unusual will not be caught.
- **No per-region detection.** `kpi_daily` is company-wide, so a collapse in one
  region diluted by three healthy ones can pass unnoticed. `regional_sales_base`
  exists for this; a materialised regional series would be the next step.

### Stage 7

- **There is no ground truth to be right about.** The dataset carries no causal
  label, so a hypothesis cannot be scored for correctness — only for evidence
  grounding, logical consistency, honest uncertainty, absence of invented facts,
  and structural validity. Judging the model on whether it guessed a hidden
  "correct" cause would be measuring a coincidence.
- **The grounding check covers citations, not prose.** Every metric in
  `supporting_evidence` must exist in the evidence package. A speculative
  sentence inside `primary_hypothesis` is constrained by the prompt and by the
  required hedging vocabulary, not by code.
- **The prompt is not a security boundary against a determined injection.** It
  is a correctly-built one — delimited untrusted block, explicit instruction to
  ignore embedded directives, unforgeable delimiters — but no instruction-level
  defence is absolute. It matters more here than it looks: the current data is
  synthetic and nearly free-text-free, so this is a boundary built before it is
  needed rather than after.
- **Output is consistent, not deterministic.** Temperature is 0 and the evidence
  digest pins the input, but a provider can still return different text for the
  same request, and providers silently revise models behind a name. This is why
  the model name is part of the generation identity and why `evidence_digest` is
  stored: two differing analyses can be shown to have had identical inputs.
- **One prompt for all severities.** A `minor` anomaly gets the same instructions
  as a `critical` one. Differentiating would mean the prompt varying by Stage 6's
  verdict, which is a coupling worth avoiding until there is evidence it helps.
- **Confidence is self-reported, and on this dataset it does not vary.** All 11
  live hypotheses came back `medium` — including the injected critical event and
  a Sunday at 3.3x baseline. That may be honest (aggregate KPIs genuinely cannot
  distinguish causes, and the prompt says most explanations should be medium or
  low) or it may be the model defaulting to the middle. Eleven samples cannot
  tell the difference. Treat `confidence` as a field that is *constrained*, not
  one that is *calibrated*.
- **Provider rate limits make `partial` the expected outcome for a full batch.**
  Groq's free tier allows 12,000 tokens per minute; one analysis costs ~3,400, so
  a batch of 11 exceeds it partway through and the rest return HTTP 429. The
  system handles this correctly — failures are isolated, the run is `partial`,
  and an idempotent re-run picks up only what did not persist (the live run
  converged in four rounds: 4 → 4 → 1 → 0 new). There is no 429-specific backoff
  in the provider; a paid tier or a scheduled retry is the answer, not a
  sleep loop inside the request path.
- **Cost scales with actionable anomalies, not with data volume.** 11 calls a day
  here. A noisier business, or a looser Stage 6 threshold, changes that
  arithmetic — `limit` is the guard, and it logs what it drops.

### Stage 8

- **A re-decision does not re-notify.** The idempotency key includes
  `decision_version`, so a Stage 6 re-run that changes a severity *within* the
  same version leaves the original notification standing, with the audit view
  reporting `decision_current = false`. Saying "that thing we sent you is now
  worse" is a real feature and this is not it.
- **One channel, one shape.** Every recipient gets the same message. No batching,
  no digest, no quiet hours, no per-recipient formatting — four notifications a
  day is comfortable, four hundred would not be.
- **`actor` is whatever the caller says it is.** With no authentication there is
  no identity to bind a claim or a resolution to, so the audit trail records an
  asserted name rather than an authenticated one.
- **The review queue has no ageing or escalation.** A critical item nobody claims
  sits in `pending` indefinitely and nothing chases it.
- **Retries are bounded at three attempts and then stop silently.** `abandoned`
  is visible in the audit view and in `ingestion_runs`, but nothing alerts on it —
  which means the failure mode of the alerting layer is that nobody is alerted.
- **The local sink is not a delivery guarantee.** It proves the webhook path
  works end to end; it says nothing about whether a real Slack workspace would
  accept the payload shape, which only a real destination can answer.

### Stage 9

- **`actor` is asserted, not authenticated.** The stage rests entirely on who
  approved something, and there is no authentication anywhere in this service.
  It is the largest gap in Stage 9, and no amount of state-machine rigour
  compensates for it.
- **The provider contacts nothing.** It records a request and returns. What a
  real integration would do about partial success, duplicate detection on the
  far side, or a ticket closed by somebody else is unexplored, because there is
  nothing here to explore it against.
- **An action stranded in `executing` needs a human.** The provider call is
  wrapped, so a raising provider becomes a recorded failure — but a process
  killed mid-call leaves the row claimed with nothing watching it. There is no
  reaper and no timeout; recovery is a manual UPDATE.
- **Retries stop at three and then stop quietly.** The action drops out of the
  work set, which is visible in `remediation_pending_execution` and in
  `ingestion_runs`, but nothing alerts on it.
- **Approval is per action type, not per plan.** A reviewer who wants an
  investigation *and* an operations review approves twice. That is honest but
  clumsy, and a real queue would want a single approval carrying several
  actions.
- **The eligibility policy is versioned but never migrated.** Changing
  `stage9-v1` in place would silently re-interpret existing rows; adding
  `stage9-v2` is the intended path, and nothing yet exists to move actions
  between them.

### Stage 10

- **`actor` is still asserted, not authenticated.** `stage10-recovery` is a
  convention, not an identity.
- **Recovery cannot reach a process that is still running.** It infers a crash
  from elapsed time, so a genuinely slow provider call that exceeds
  `stale_remediation_timeout_minutes` would be moved to `execution_unknown`
  while still in flight. The timeout is set well beyond any HTTP timeout, and
  the consequence is a reconciliation rather than a double execution.
- **`load_staged_batch()` duplicates the Stage 3 workflow's validation rules.**
  Replay must run exactly the validation the original run ran, and that logic
  lives inside n8n node parameters where PostgreSQL cannot reach it. Both
  implementations are tested independently against the same documented Stage 2
  rules; a drift fails one of the two suites. The right long-term fix is for the
  Stage 3 workflow to call this function.
- **Retention never deletes a failed row.** Correct, and unbounded: a warehouse
  accumulating dead letters grows forever. Archival is a deliberate decision
  nobody has made yet.
- **Nothing escalates.** Ageing labels an overdue review and the health view
  reports it; no notification is sent, because Stage 8 delivers anomalies rather
  than operational conditions and conflating the two would put pipeline noise in
  the same channel as revenue findings.
- **The maintenance workflow's branches are sequential, not parallel.** Each
  continues on error, so one failure never stops the rest — but a slow branch
  delays the others. At this scale that is measured in seconds.

### Stage 11

Stage 11 adds no service code — the presentation layer is SQL views and a
Metabase catalogue — but three of its suites live here, because they need this
package's database fixtures.

- **`test_presentation_views.py`** — the views against the live warehouse: the
  layer vocabulary, the 2026-08-05 chain end to end, the drill-down's reading
  order, audit-stream completeness, and the rule that no `exec_` view exposes a
  column the model wrote.
- **`test_dashboard_catalogue.py`** — the card catalogue as data, with no
  Metabase and no browser: single-statement read-only SQL, relations that exist,
  panels that do not overlap, and no secret in any dashboard file.
- **`test_dashboard_isolation.py`** — connects **as `salesops_readonly`** and
  asks it to write to every table the specification names, then runs all 31 card
  queries between two sets of Stage 6-10 fingerprints.

Two limitations belong to this stage:

- **The dashboards show the newest version of each thing.** A date can carry
  several detector versions, decision versions and hypothesis generations; the
  views take the latest of each so a panel cannot count one anomaly twice. Older
  versions remain stored and remain visible in `audit_event_stream`.
- **Running this suite empties the review queue and the action table.** The
  Stage 8 and Stage 9 fixtures purge what they create, as they always have, so
  the dashboards show an empty queue until the pipeline runs again. Stage 11's
  own fixtures rebuild the 2026-08-05 chain through the real endpoints rather
  than skipping on an empty queue - skipping would turn a dozen assertions about
  an end-to-end chain into a dozen silent passes.
