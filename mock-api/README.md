# Mock Sales/Orders API

Stands in for the internal ERP / order-management system the pipeline ingests
from. Real order data lives behind internal APIs, never public ones, so
simulating that system is the realistic pattern — and it makes the demo
reproducible instead of dependent on a third party having an interesting day.

Two jobs:

1. **Serve a believable order history** — 90 days, seeded, with weekday
   seasonality, regional volume differences, multi-currency billing, repeat
   customers and a baseline refund rate.
2. **Let an operator inject anomalies on demand** — by rewriting the underlying
   orders, so the downstream detector has to find them on its own.

---

## Endpoints

| Method | Path | Audience | Purpose |
|---|---|---|---|
| `GET` | `/health` | container | Liveness probe |
| `GET` | `/orders` | **pipeline** | Order data, optionally date-filtered |
| `POST` | `/admin/generate-orders` | operator | Append new orders |
| `POST` | `/admin/inject-anomaly` | operator | Rewrite orders into an anomaly |
| `GET` | `/admin/anomalies` | operator | What has been injected |

Interactive docs: <http://localhost:8000/docs>

The split is deliberate. The n8n pipeline only ever calls `GET /orders`; it is
never told where the anomalies are. `/admin/anomalies` exists to check the
detector's answer *against*, never to supply it.

### `GET /orders`

```bash
curl "http://localhost:8000/orders?limit=3"
curl "http://localhost:8000/orders?from=2026-08-01&to=2026-08-07"
```

```json
{ "count": 312, "orders": [ { "order_id": "ORD-2026-002841", "...": "..." } ] }
```

Both bounds are inclusive. Results are sorted by `(order_date, order_id)`, so a
given window always returns in the same order. `limit` applies after filtering.
`from` later than `to` is a 422.

The intended ingestion pattern is **one date window per run**
(`?from=2026-08-09&to=2026-08-09`) rather than offset pagination: it is
naturally idempotent, and it survives new orders arriving mid-backfill.

### `POST /admin/generate-orders`

```bash
curl -X POST http://localhost:8000/admin/generate-orders \
  -H "Content-Type: application/json" -d '{"count": 25}'
```

New orders are stamped with the current business date — the later of today and
the newest date already stored — so calling this between ingestion runs
simulates a live system producing fresh data. `order_date` overrides it.
`count` is 1–1000.

`order_id` is globally unique and stays that way: the store owns a strictly
increasing sequence rebuilt from disk on startup, so ids never collide across
restarts. That is what makes `order_id` safe to use as the pipeline's
idempotency key.

### `POST /admin/inject-anomaly`

```bash
# Revenue collapse in North America
curl -X POST http://localhost:8000/admin/inject-anomaly \
  -H "Content-Type: application/json" \
  -d '{"type":"revenue_drop","region":"North America","date":"2026-08-09","severity":0.6}'
```

| `type` | What it does to the data | How it presents |
|---|---|---|
| `revenue_drop` | Cuts unit prices by `severity`; order count untouched | AOV collapse — pricing bug, over-discounting, mix shift |
| `refund_spike` | Converts a `severity`-scaled share of orders to full refunds; gross revenue untouched | Refund-rate jump — quality or fulfilment failure |
| `regional_drop` | Deletes a `severity` share of one region's orders | Volume *and* revenue fall together — lost demand |

Each targets a different KPI, so the three are distinguishable by a detector
rather than being one anomaly with three labels.

Parameters:

- `type` — required, one of the three above.
- `date` — required. Must be a date that has orders.
- `region` — optional (all regions if omitted), **required for `regional_drop`**.
  Accepts codes or display names, case-insensitively: `NA`, `na`,
  `North America`, `north_america`, `americas`.
- `severity` — `0 < severity <= 1`, default `0.5`.

Responses: `201` with the anomaly record, `422` for bad parameters, `409` if no
orders match the date/region (with the available date range in the message —
silently succeeding here would waste a demo take).

**Injection mutates real order rows. It does not set a flag.** Nothing
downstream is told an anomaly happened. That is the whole point: it is the only
way the Stage 5 detector gets tested rather than handed the answer.

Injections **compound** — applying the same one twice stacks the effect.

`regional_drop` is destructive, so the removed `order_id`s are recorded on the
anomaly record.

### `GET /admin/anomalies`

Returns every injection with before/after figures. Money is reported **per
currency**, not as a single total: this service holds no exchange rates, and
inventing some would create a second source of FX truth alongside the
Frankfurter integration that Stage 3 owns.

---

## The generated data

~3,000–4,000 orders across 90 days and four regions.

| | NA | EMEA | APAC | LATAM |
|---|---|---|---|---|
| Weekday mean orders | 18 | 13 | 9 | 5 |
| Currency | USD | EUR + GBP | JPY | BRL |
| Baseline refund rate | 4.2% | 5.5% | 2.8% | 6.1% |
| Channel skew | web | partner-heavy | mobile-first | reseller-led |

Deliberate properties, each there because a later stage depends on it:

- **Weekly seasonality** — weekends run at ~45% of weekday volume. Strong enough
  that a naive z-score ignoring the day of week would false-positive every
  weekend, which is what the Stage 5 trend-aware tier exists to handle.
- **Right-skewed daily noise** — lognormal, not normal. Revenue is bounded below
  by zero with a long upper tail; symmetric noise would be too tidy.
- **A mild growth trend** — +14% across the window, so "expected value" is not
  simply the historical mean.
- **Regional heterogeneity** — four distinguishable baselines, ~3.6× between
  largest and smallest, not four copies of one.
- **Repeat customers** — accounts are drawn with Pareto-distributed loyalty
  weights, so a minority generate a majority of orders. A uniform draw would
  make `dim_customer` meaningless.
- **Occasional quiet days** — 3% of region-days run at 55%, so the detector has
  to distinguish real slumps from injected ones.
- **JPY has no minor units** — prices are whole numbers, which is a rounding
  edge case the ingestion layer has to get right.

**Local prices are not FX conversions.** Each currency has a fixed list-price
factor. Companies set local prices on round numbers and revise them slowly, so
they drift from spot rates. Stage 3 normalises to USD using live Frankfurter
rates and the two will *not* agree exactly — which is precisely why FX
normalisation has to be a real pipeline step rather than a constant.

### Reproducibility

Same `MOCK_API_SEED` + `MOCK_API_HISTORY_DAYS` + `MOCK_API_HISTORY_END_DATE`
→ byte-identical dataset. Nothing reads the global `random` module or the clock
during generation; Faker is seeded per-instance.

`MOCK_API_HISTORY_END_DATE` defaults to **today**, so a fresh environment shows
recent dates. Pin it to a fixed date for exact cross-day reproducibility — the
test suite does.

### Where Faker is used

Faker generates the **customer base**: locale-appropriate company names per
region (`en_US`, `de_DE`, `ja_JP`, `pt_BR`), which will back `dim_customer` in
Stage 2. The numeric and categorical order fields come from explicit
distributions in `generation.py` instead, because those need to be tuned
deliberately against what the detector must find.

---

## Persistence

Orders live in `/data/orders.jsonl` (one JSON object per line), anomalies in
`/data/anomalies.json`, on the `mock_api_data` Docker volume.

**Why not a database.** This service *simulates* the system the pipeline reads
from. Giving it its own database would mean the project's first real schema
belonged to the mock rather than to the analytics model, and would add a
component whose only job is to hold a few thousand rows the pipeline is about to
copy out anyway. JSONL is inspectable with `head`, greppable, diffable, and
needs no migration story.

**Why the whole file is rewritten on mutation.** Anomaly injection edits and
deletes existing rows, so append-only does not fit. At this scale a full rewrite
is sub-millisecond, and it goes through a temp file plus `os.replace`, so a
crash mid-write cannot leave a half-written order book.

Generation happens **once per volume**. Changing `MOCK_API_SEED` or
`MOCK_API_HISTORY_DAYS` afterwards has no effect until the volume is removed —
the persisted file wins, because silently regenerating history under a running
pipeline would be worse than ignoring the setting.

```bash
docker compose exec mock-api head -n 2 /data/orders.jsonl   # inspect
docker compose down -v                                      # reset (wipes everything)
```

---

## Layout

```text
mock-api/
├── app/
│   ├── main.py          # application factory + lifespan
│   ├── routes.py        # public router (pipeline) + admin router (operator)
│   ├── models.py        # pydantic request/response schemas
│   ├── store.py         # in-memory order book + JSONL persistence
│   ├── generation.py    # seeded synthetic generator
│   ├── anomalies.py     # anomaly strategies (pure functions)
│   ├── catalog.py       # regions, products, channels, currencies
│   └── config.py        # environment-derived settings
├── tests/
└── Dockerfile
```

`anomalies.py` is pure — list in, list out, no I/O — so the mutation logic is
testable without an app. `main.py` is a factory so tests build an app against a
temp directory instead of the real `/data` volume.

---

## Tests

```bash
cd mock-api
pip install -r requirements-dev.txt
python -m pytest
```

`python -m pytest` rather than bare `pytest`: the console script only works if
your Python installation's `Scripts/` directory is on `PATH`, which it often is
not on Windows. The module form always works.

78 tests. The load-bearing ones recompute the affected KPI from `GET /orders`
before and after injection, rather than trusting the injection's own report — an
endpoint that recorded an anomaly without moving the numbers would pass a naive
test and fail the project's whole premise.

Coverage: health, retrieval, date filtering and its edge cases, schema
validation, global id uniqueness, generation, all three anomaly scenarios,
scenario containment (other days and regions unaffected), invalid parameters,
generator determinism and realism, and persistence across restart.

---

## Known limitations

Deliberate, and revisited in later stages:

- **`/admin/*` is unauthenticated.** The service holds no real data and is bound
  to a private Docker network. Adding auth would complicate the demo without
  protecting anything.
- **Generated data is always schema-valid.** No nulls, no malformed currencies,
  no duplicate ids. Schema-level dirty data belongs with Stage 3's dead-letter
  queue — there is nowhere for a bad row to *go* until that exists.
- **No pagination.** Date-window ingestion is the intended pattern and is
  idempotent; `limit` covers the smoke-test case.
- **Injections compound and cannot be undone.** `docker compose down -v` is the
  reset. Recording removed ids keeps destructive changes auditable meanwhile.
- **Money is `float`.** JSON has no decimal type and real order APIs return
  numbers. Conversion to `NUMERIC` happens on the way into Postgres; no
  financial arithmetic happens in this service.
