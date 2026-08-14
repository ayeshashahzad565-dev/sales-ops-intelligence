# Sales & Revenue Operations Intelligence Pipeline

[![CI](https://github.com/ayeshashahzad565-dev/sales-ops-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/ayeshashahzad565-dev/sales-ops-intelligence/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17-336791.svg)](database/README.md)

An automated pipeline that ingests sales orders, computes daily KPIs, detects
revenue anomalies statistically, grades and routes them through deterministic
SQL rules — auto-notify for minor deviations, human review for major ones — and
only then asks an LLM to draft a root-cause hypothesis for the ones that matter.

**The LLM proposes. Deterministic rules decide. A human approves.** Severity and
routing are computed in SQL *before* the model is called, so it never grades its
own finding, and nothing executes that a person did not authorise by name.

```bash
git clone <repo-url> && cd sales-ops-intelligence && ./bootstrap.sh
```

Five minutes from clone to four live dashboards over 90 days of orders, eleven
detected anomalies, and a review queue waiting on a human.

| | |
|---|---|
| **Services** | PostgreSQL 17 · n8n · Metabase · two FastAPI services, on one Docker network |
| **Pipeline** | 10 n8n workflows, scheduled, with a shared run ledger and a global error handler |
| **Warehouse** | 13 migrations · 30 tables · 15 presentation views · every business rule in SQL |
| **Tests** | 277 schema checks · 555 service tests · 179 pipeline-SQL checks · 84 API tests |
| **Model use** | one call, one table, zero authority over any decision |

> **A portfolio project running on a laptop. It is not production-secure** —
> actors are asserted rather than authenticated, and there is no API auth. See
> [Limitations](docs/LIMITATIONS.md).

---

## The pipeline

```
Mock API ──► Orders Ingestion ──► raw_orders_staging ──► fact_orders
  hourly                            (JSONB, batch_id)    (idempotent)
                     │                      └─► failed + error_message
Frankfurter ─► FX Rate Sync ─────────────────────► attaches rates
  daily 05:00     (bounded carry-forward)                 │
                                                          ▼
               KPI Daily Refresh ────────────────────► kpi_daily
                 daily 06:00 (atomic full rebuild)        │
                                                          ▼
               Statistical Anomaly Detection ────────► anomaly_daily
                 daily 07:00 (robust, calendar-aware)     │
                                                          ▼
               Deterministic Anomaly Decision ──────► anomaly_decisions
                 daily 07:30 (SQL rules only)     severity · routing · reason codes
                     │                                    │
                     │                    actionable only ▼
               LLM Root Cause Analysis ────────────► anomaly_hypotheses
                 daily 08:00 (the ONLY LLM call)   explanation, never a verdict
                     │                                    │
                     │                     minor ─┐  ┌─ major/critical
                     │                            ▼  ▼
               Notification & Review Routing ──► notifications · review_queue
                 daily 08:30 (delivers, never acts)       │
                     │             a human approves ▼ and names an action
               Remediation Execution ────────────► remediation_actions
                 daily 09:00                     proposed → approved → executed,
                     │                           once, every step attributed
                     ▼
               Operational Maintenance ──────► operational_events · health
                 daily 09:30 (recovers; re-runs nothing)

               Metabase ◄── salesops_readonly ◄── presentation views
                 4 dashboards, SELECT only, no EXECUTE on anything volatile
```

**n8n orchestrates. PostgreSQL enforces integrity. Python does statistics. The
LLM explains, and never judges.** No business rule lives in an n8n expression —
validation, dimension resolution and idempotency are all SQL, reviewable and
testable without n8n running.

---

## The eleven stages

All complete. Each was specified, built, tested and documented before the next
began, and no later stage may modify an earlier one's verdict.

| | Stage | What it adds | Detail |
|---|---|---|---|
| 0 | Local environment | Five services, one network, health checks | [docs](docs/OPERATIONS.md) |
| 1 | Mock orders API | Seeded 90-day history, injectable anomalies | [docs](mock-api/README.md) |
| 2 | Warehouse schema | Star schema, staging, `NUMERIC` money, idempotent loads | [docs](database/README.md) |
| 3 | Ingestion | Staging, validation, dead-letter, run ledger | [docs](n8n/README.md) |
| 4 | FX + KPIs | Frankfurter rates, bounded carry-forward, atomic `kpi_daily` | [docs](database/README.md) |
| 5 | Anomaly detection | Median/MAD baselines per weekday, four signals, no ML | [docs](analytics-service/README.md) |
| 6 | **Decision layer** | Severity, routing, reason codes — in SQL, before any model | [docs](database/README.md#the-decision-layer--stage-6) |
| 7 | LLM hypotheses | The only model call. Receives the verdict; cannot change it | [docs](analytics-service/README.md#stage-7--root-cause-hypotheses) |
| 8 | Delivery + review | Notifications, review queue, state machine, audit trail | [docs](analytics-service/README.md#stage-8--delivery-and-human-review) |
| 9 | **Remediation** | Executes only what a human approved, once, fully attributed | [docs](analytics-service/README.md#stage-9--human-approved-remediation) |
| 10 | Reliability | Recovery, replay, retention, deterministic health | [docs](analytics-service/README.md#stage-10--operational-reliability) |
| 11 | Dashboards | Four read-only Metabase dashboards over fifteen views | [docs](metabase/README.md) |

Full narrative: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## One incident, end to end

The injected anomaly on **2026-08-05** runs the whole length of the platform.
The investigation dashboard shows it as ten steps:

```
 1 orders               52 orders, 136 units                       observed fact
 2 kpi                  net revenue 4,748.95 USD, AOV 91.33 USD    observed fact
 3 anomaly              score 8.925, dominant refund, 3 signals    statistical
 4 decision             critical / human_review /                  deterministic
                        CRITICAL_COMBINED_IMPACT
 5 hypothesis           unverified, stated confidence medium       ▲ LLM
 6 notification         not notified (routing = human_review)      not reached
 7 review               approved              — dana@finance       human review
 8 remediation          request_refund_review — priya@revops       approved
 9 execution            executed, reference local-record-3         completed
10 operational outcome  0 events recorded against this action      operational
```

Step 5 is the only line a language model wrote, and step 4 happened before it.

---

## Deterministic and probabilistic

One probabilistic component, downstream of every decision.

| | Deterministic | Probabilistic |
|---|---|---|
| **What** | ingestion, FX, KPIs, z-scores, severity, routing, eligibility, health, ageing | one LLM call per actionable anomaly |
| **Reproducible** | yes — same inputs, same outputs, forever | no |
| **Can change a verdict** | it *is* the verdict | never |
| **Audited** | every threshold is a row in a table | provenance stored; the claim is never verified |

On the dashboards this is a column, not a colour. Every presentation view carries
the layer its content came from, and a `CHECK` constraint makes exactly one of
the eight layers model-generated. `llm_verified` is `false` on every row that has
one — because nothing here verifies a hypothesis.

**Four human gates**, deliberately not collapsible into one call: Stage 6 decides
whether a person is needed; a reviewer claims and resolves; approving the review
*proposes* an action; authorising that action is a separate decision by a second
name. Three different mechanisms enforce it — a trigger, a foreign key, and a
conditional `UPDATE` — because one function doing all three would be one function
to get wrong. [More →](docs/ARCHITECTURE.md#human-in-the-loop)

---

## Tests

```bash
./database/migrate.sh --test               # 277 schema checks
python n8n/tests/test_ingestion_sql.py     # 34  ingestion
python n8n/tests/test_stage4_fx_kpi.py     # 66  FX and KPI
python n8n/tests/test_stage6_decisions.py  # 79  decisions
cd analytics-service && python -m pytest   # 555 detection through the dashboards
cd mock-api          && python -m pytest   # 84  the order API
```

The schema suite seeds the Stage 6 fixture it needs when the database is empty,
so a container eleven seconds old proves the same 277 guarantees as a warehouse
with 90 days of history — including the guard triggers behind authorisation,
exactly-once execution and reconciliation.

CI runs the linter, that schema suite against a real PostgreSQL 17, the 160
analytics tests that need no warehouse, and the mock API. It deliberately does
**not** run the integration suites that need a populated warehouse and an LLM
key — a badge that quietly skipped a third of the suite would be worse than no
badge. Run those locally after `./bootstrap.sh`.

---

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The eleven stages in depth, the layer model, the human gates |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Prerequisites, manual setup, verification, troubleshooting |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What this does not do, and what it is not safe for |
| [database/README.md](database/README.md) | Schema, decision layer, migrations, the reporting role |
| [analytics-service/README.md](analytics-service/README.md) | Detection, hypotheses, delivery, remediation, recovery |
| [n8n/README.md](n8n/README.md) | The ten workflows and the SQL behind them |
| [metabase/README.md](metabase/README.md) | The dashboards and how they are provisioned |
| [mock-api/README.md](mock-api/README.md) | The simulated upstream order system |

---

## Limitations

- **Actors are asserted, not authenticated.** Every `actor`, `approved_by` and
  `executed_by` is a string the caller supplied. The audit trail records what was
  *claimed*. This is the largest security limitation in the project.
- **No API authentication**, and ports are published to localhost.
- **Remediation has no external side effect** — the provider records what it was
  asked to do. Building a fake ERP would have made the audit trail fiction.
- **The order data is synthetic**, generated from a fixed seed with anomalies
  injected deliberately.
- **Nothing escalates.** Overdue reviews are labelled and reported; nobody is
  paged.

[The full list →](docs/LIMITATIONS.md)

---

## License

[MIT](LICENSE). The synthetic order data, the mock API and the dashboards are
all part of the project and carry the same licence.
