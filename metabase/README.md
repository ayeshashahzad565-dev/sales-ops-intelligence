# Stage 11 — the dashboard layer

Four read-only dashboards over the pipeline, provisioned into Metabase from
code. Nothing here computes a KPI, a severity, a health status or an ageing
bucket. Every number on every panel was already stored by the stage that owns
it, and every card is a `SELECT` against a view in
[V013__presentation_layer.sql](../database/migrations/V013__presentation_layer.sql).

```bash
./metabase/provision.sh          # build or update
./metabase/provision.sh --check  # report state, change nothing
```

Then open <http://localhost:3000>.

---

## The one idea

A dashboard is where a measurement, a statistic, a rule, a language model, a
person and a machine all end up rendered in the same typeface. That is the risk
this stage exists to manage.

Every presentation view carries the **layer** its content came from, and the
layers are a table with a `CHECK` constraint rather than a convention:

| # | Layer | Kind | Model-generated | Stage |
|---|---|---|---|---|
| 1 | Observed fact | measured | no | 3–4 |
| 2 | Statistical signal | statistical | no | 5 |
| 3 | Business decision | deterministic | no | 6 |
| 4 | **LLM hypothesis (unverified)** | **model_generated** | **yes** | **7** |
| 5 | Human review | human_judgement | no | 8 |
| 6 | Approved remediation | human_judgement | no | 9 |
| 7 | Completed remediation | system_action | no | 9 |
| 8 | Operational event | system_action | no | 10 |

```sql
CONSTRAINT presentation_layers_model_flag_chk CHECK (
    is_model_generated = (evidence_kind = 'model_generated')
)
```

Exactly one layer is model-generated, and the flag and the kind cannot be
written apart. In practice that means:

- **the executive dashboard shows no model prose.** It says whether a hypothesis
  exists; it never says what one claims. The forbidden column list has a single
  definition (`LLM_TEXT_COLUMNS` in [dashboards.py](dashboards.py)) and the
  tests read that same list rather than a copy;
- **the investigation dashboard shows the hypothesis in full** — underneath the
  deterministic evidence, behind a warning panel, under column names prefixed
  `llm_`;
- **`llm_verified` is `false` on every row that has one.** Not because
  verification failed. Because nothing in this system verifies a hypothesis, and
  an unverifiable claim rendered next to an audited one has to say so.

---

## The dashboards

**Executive Overview** — revenue, orders, AOV, refund rate, revenue against its
day-of-week baseline, anomalies by severity, what is actionable, what is waiting
on a human, notification/review/remediation state, pipeline health, and a legend
explaining the layers.

**Anomaly Investigation** — one incident, layer by layer, in the order it has to
be read: facts → statistics → decision → hypothesis → missing evidence →
notification/review → remediation → audit history. Defaults to the incident
`bootstrap.sh` injected, recorded as `SALESOPS_INCIDENT_DATE`. Change the date
filter at the top for any other day.

**Operational Health** — Stage 10's vocabulary, unaltered:
`healthy | warning | degraded | failed`. Per-pipeline runs with the latest
*successful* run tracked separately from the latest run, stale and overdue items,
replay counts, and remediation actions whose execution outcome is unknown.

**Audit Trail** — every recorded transition from all six streams in one shape:
who, when, from what state to what state, under which version.

---

## Read-only is a role, not a promise

Writing only `SELECT` statements would satisfy "the dashboard cannot mutate
business data" until the first person opens the SQL editor in Metabase.

So V013 creates `salesops_readonly`:

- `USAGE` on the schema and `SELECT` on relations — nothing else;
- **no `EXECUTE`.** PostgreSQL grants `EXECUTE` on every function to `PUBLIC` by
  default, and `salesops.purge_staging()` and `salesops.replay_failed_batch()`
  are write operations reachable from a `SELECT` box. `PUBLIC` loses it here;
- except on the four **non-`VOLATILE`** functions, granted by volatility rather
  than by name. A function marked `STABLE` or `IMMUTABLE` is one the author
  declared cannot change the database, and those are exactly the configuration
  readers that `operational_health` and its neighbours call. A helper added by a
  later migration is covered correctly without anyone editing the grant.

The role is created `NOLOGIN` with no password, because a password in a
migration is a password in version control. `provision.sh` gives it a login from
`.env` — and then **proves** the result by asking the role to `DELETE FROM
kpi_daily` and requiring the attempt to be refused before it goes any further.

Metabase connects as this role and no other.

---

## What provisioning does

`provision.py` is idempotent and matches by name inside the Stage 11 collection,
so re-running after editing `dashboards.py` updates the cards in place rather
than leaving a second copy of everything.

1. waits for `/api/health`;
2. runs first-time setup if Metabase has no user, otherwise signs in;
3. creates or updates the `Sales Ops Analytics (read-only)` connection — pointed
   at `postgres:5432` by **container name**, because Metabase reaches PostgreSQL
   as a container and a `localhost` here would work on a laptop and nowhere else;
4. creates or updates the collection, 31 cards and 4 dashboards.

Stdlib only — `urllib`, not `requests` — so it runs against a bare Python
without adding a host dependency to a project that otherwise has none.

### Credentials

Three, all from the environment, none with a default anywhere in the repository:

| Variable | What it is |
|---|---|
| `METABASE_ADMIN_EMAIL` / `METABASE_ADMIN_PASSWORD` | the Metabase login it creates or uses |
| `METABASE_READONLY_DB_PASSWORD` | the password for `salesops_readonly` |

Metabase echoes submitted connection details back in some error bodies, which is
fine until the body reaches a terminal or a CI log. Every error passes through
`redact()` first.

---

## Tests

```bash
cd analytics-service && python -m pytest tests/test_presentation_views.py \
                                          tests/test_dashboard_catalogue.py \
                                          tests/test_dashboard_isolation.py
```

`test_dashboard_catalogue.py` needs neither Metabase nor a browser: a card is
SQL plus a rectangle, so the catalogue is testable as data — whether a query is
read-only, whether it names a relation that exists, whether two panels sit on
top of each other, and whether model prose has reached the executive page.

`test_dashboard_isolation.py` connects **as `salesops_readonly`** and asks it to
write to every table Section 5 names, then runs all 31 card queries between two
sets of Stage 6–10 fingerprints. Reading is allowed to be slow, expensive or
wrong. It is not allowed to leave a mark.

---

## Limitations

- **Actors are asserted, not authenticated.** Nothing proves a caller naming
  itself `dana@finance` is `dana@finance`. The audit trail records what was
  claimed. This is the platform's largest security limitation and it is stated
  on the Audit Trail dashboard itself, not only here.
- **Metabase has no per-user permissions configured.** Anyone who can reach
  port 3000 sees everything. The database role bounds what any of them can *do*;
  it does not bound what they can see.
- **The dashboards are unauthenticated behind a published localhost port.** This
  is a development environment. It is not production-secure.
- **Card layout is fixed in code.** Rearranging panels in the browser is
  overwritten by the next `provision.sh` run. That is deliberate — the
  dashboards are versioned artefacts — but it does mean the UI is not the source
  of truth.
- **`exec_kpi_daily` and its neighbours show the newest version** of each
  detector run, decision and hypothesis. Older versions remain in the database
  and remain visible in the audit stream; they are simply not what the headline
  panels count.
