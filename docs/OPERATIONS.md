# Running it

Prerequisites, the manual path, how to verify each service, and what to do
when something will not start.

For the one-command version, see [`bootstrap.sh`](../bootstrap.sh) and the
[README](../README.md).

---

## Prerequisites

- Docker Desktop (or Docker Engine) with Compose v2 — running
- ~4 GB free RAM (Metabase is a JVM service)
- Ports `5432`, `5678`, `3000`, `8000`, `8001` free — all remappable in `.env`
- `bash` (Git Bash on Windows) and Python 3.11+ on the host, for the scripts


---

## Start the environment

One command, from a fresh clone to a populated stack with live dashboards:

```bash
git clone <repo-url>
cd sales-ops-intelligence
./bootstrap.sh
```

Roughly five minutes, most of it Metabase booting. It generates the two secrets
that have no safe default, starts the five services, applies the thirteen
migrations, runs the 277 schema checks, imports the ten workflows, **runs the
whole pipeline once in dependency order**, and provisions the dashboards.

That last part is why the script exists. Every workflow is on a daily schedule,
so a freshly imported stack sits empty until the clock catches up — and nobody
evaluating this is going to wait until 09:30 tomorrow.

It is idempotent, so it is also the right thing to run when you are not sure
what state the stack is in.

<details>
<summary>Or do it by hand</summary>

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
# set POSTGRES_PASSWORD and N8N_ENCRYPTION_KEY; the rest have safe defaults

docker compose up -d --build  # five services
./database/migrate.sh --test  # V001..V013, then 277 schema checks
./n8n/import-workflows.sh --activate
./metabase/provision.sh       # dashboards, behind a read-only database role
```

Then either wait for the schedules or run the workflows once each, in the order
printed by `import-workflows.sh`.

</details>

First run pulls images and builds the two Python services; expect a few minutes.
Metabase takes 1–3 minutes to become healthy after its container starts — it runs
its own schema migrations on boot.

```bash
docker compose ps             # all five reach Up (healthy)
```

**`bootstrap.sh` deliberately leaves the review queue pending.** Nothing is
approved, because approving is a human act and a shell script is not a human.
The pipeline detects, grades, explains and escalates on its own, and then stops
— which is the entire architecture in one sentence. The script prints the two
calls that finish the chain, so you make that decision yourself and watch the
audit trail record your name against it.


---

## Stop the environment

```bash
docker compose down            # stop containers, keep all data
docker compose down -v         # stop and DESTROY all data (see note below)
```

`down -v` deletes the Postgres volume. That is the *only* way to re-run
`database/init/`, so use it deliberately — it also wipes your n8n workflows and
Metabase dashboards.


---

## Verify each service

**PostgreSQL** — expect `accepting connections`, four databases, then the
analytics schema with its dimensions seeded and `fact_orders` still empty:

```bash
docker compose exec postgres pg_isready -U salesops -d salesops
docker compose exec postgres psql -U salesops -d salesops -c "\l"
docker compose exec postgres psql -U salesops -d salesops -c "\dt salesops.*"
docker compose exec postgres psql -U salesops -d salesops \
  -c "SELECT region_code, region_name FROM salesops.dim_region ORDER BY region_code;"
```

**Mock API** — expect `{"status":"ok",...}` and ~3,900 orders over 90 days:

```bash
curl http://localhost:8000/health
curl "http://localhost:8000/orders?limit=3"
curl "http://localhost:8000/orders?from=2026-08-01&to=2026-08-07"
```

Interactive docs: <http://localhost:8000/docs> · Details: [mock-api/README.md](../mock-api/README.md)

**n8n** — expect `{"status":"ok"}`, then the setup screen in a browser:

```bash
curl http://localhost:5678/healthz
```

<http://localhost:5678> — create the local owner account on first visit.

**Metabase** — expect `{"status":"ok"}`, then provision the dashboards:

```bash
curl http://localhost:3000/api/health
./metabase/provision.sh          # idempotent; safe to re-run
```

<http://localhost:3000> — the four dashboards live in the *Sales Ops
Intelligence* collection. The provisioner runs Metabase's first-time setup for
you if it has never been opened.

**Container-to-container reachability** — proves the Docker network, not just
the published ports:

```bash
docker compose exec n8n wget -qO- http://mock-api:8000/health
```

#### Checklist

- [ ] `docker compose ps` shows five services, all `Up (healthy)`
- [ ] `psql -c "\l"` lists `salesops`, `n8n`, `metabase`
- [ ] `\dt salesops.*` lists 30 tables; `dim_region` has 4 rows
- [ ] `migrate.ps1 -Test` (or `migrate.sh --test`) reports 275 passed, 0 failed
- [ ] `python n8n/tests/test_ingestion_sql.py` reports 34 passed, 0 failed
- [ ] `SELECT * FROM salesops.ingestion_runs` shows a `success` run
- [ ] Re-running the ingestion leaves `count(*) FROM salesops.fact_orders` unchanged
- [ ] `GET localhost:8000/orders` returns ~3,900 orders spanning 90 days
- [ ] `?from=&to=` narrows the result to that window
- [ ] n8n loads at `localhost:5678` and the setup screen appears
- [ ] `./metabase/provision.sh` reports 31 cards and 4 dashboards
- [ ] The reporting role is refused a write (the script checks this and stops if not)
- [ ] The *Anomaly Investigation* dashboard traces the injected incident in ten steps
- [ ] n8n can reach `http://mock-api:8000/health` from inside the network
- [ ] `docker compose restart mock-api` preserves the order count
- [ ] `docker compose down` then `up -d` preserves the n8n account you created


---

## How the containers communicate

All five services share the `salesops` bridge network and resolve each other by
**service name** via Docker's embedded DNS. Published host ports exist purely
for your browser and local database client.

| From | To | Address to use |
|---|---|---|
| n8n | PostgreSQL | `postgres:5432` |
| n8n | Mock API | `http://mock-api:8000` |
| Metabase | PostgreSQL | `postgres:5432` |
| Your machine | any service | `localhost:<published port>` |

The pitfall this avoids: inside a container, `localhost` means *that container*.
An n8n Postgres node pointed at `localhost:5432` looks for Postgres inside the
n8n container and fails. Use `postgres`. Likewise, when adding the analytics
database to Metabase, the host is `postgres`, not `localhost`.


---

## Repository layout

```text
sales-ops-intelligence/
├── bootstrap.sh                # cold start: clone -> populated stack, one command
├── docker-compose.yml          # the five services, one network, four volumes
├── ruff.toml                   # lint configuration for every Python package
├── .env.example                # every required variable, no secrets
├── database/                   # the salesops warehouse       [see its README]
│   ├── init/                   # runs once on first Postgres start
│   ├── migrations/             # V001 schema · V002 seed · V003 views · V004 run ledger
│   │                           # V005 kpi_daily · V006 anomaly_daily · V007 baseline
│   │                           # median · V008 decision layer · V009 hypotheses
│   │                           # V010 notifications + review queue
│   │                           # V011 remediation + review approval
│   │                           # V012 recovery, replay, retention, health
│   │                           # V013 presentation views + read-only role
│   ├── tests/                  # 277 schema validation checks, self-seeding
│   ├── examples/               # runnable Stage 3 ingestion walkthrough
│   ├── migrate.ps1             # runners
│   └── migrate.sh
├── mock-api/                   # FastAPI order-management stub  [see its README]
│   ├── app/
│   │   ├── main.py             # application factory
│   │   ├── routes.py           # /health, /orders, /admin/*
│   │   ├── generation.py       # seeded 90-day synthetic generator
│   │   ├── anomalies.py        # controlled anomaly injection
│   │   ├── store.py            # JSONL-backed order book
│   │   ├── catalog.py          # regions, products, currencies
│   │   ├── models.py           # pydantic schemas
│   │   └── config.py           # environment settings
│   ├── tests/                  # 84 tests
│   ├── requirements.txt
│   └── Dockerfile
├── analytics-service/          # anomaly detection        [see its README]
│   ├── analytics/              # statistics · baseline · detector · repository
│   └── tests/                  # 555 tests
├── n8n/                        # orchestration layer          [see its README]
│   ├── workflows/              # Orders Ingestion · FX Rate Sync · KPI Refresh
│   │                           # Anomaly Detection · Anomaly Decision
│   │                           # LLM Root Cause · Notification & Review
│   │                           # Remediation Execution
│   │                           # Operational Maintenance · Error Handler
│   ├── tests/                  # 100 behavioural checks on the pipeline SQL
│   └── import-workflows.sh     # credential + workflow import
└── metabase/                   # dashboard layer               [see its README]
    ├── dashboards.py           # the card and layout catalogue, as data
    ├── provision.py            # idempotent Metabase API provisioner (stdlib)
    ├── provision.sh            # grants the reporting login, then provisions
    └── README.md
```

`./mock-api/app` is bind-mounted into its container and uvicorn runs with
`--reload`, so edits to the mock API take effect without a rebuild. Rebuild only
when `requirements.txt` changes:

```bash
docker compose up -d --build mock-api
```

Run the mock API's tests:

```bash
cd mock-api
pip install -r requirements-dev.txt
python -m pytest          # module form: does not need Scripts/ on PATH
```

Reset just the order book, leaving Postgres, n8n and Metabase untouched:

```bash
docker compose rm -sf mock-api
docker volume rm sales-ops-intelligence_mock_api_data
docker compose up -d mock-api
```


---

## Troubleshooting

**A service is stuck `starting`** — check its logs: `docker compose logs -f metabase`.
Metabase's `start_period` is 120s; it is not unhealthy until that elapses.

**Port already in use** — change the matching `*_HOST_PORT` in `.env` and re-run
`docker compose up -d`. Internal addressing is unaffected.

**Postgres auth fails after changing `.env`** — `POSTGRES_USER`/`POSTGRES_PASSWORD`
are only applied when the data volume is first created. Either change the
password in-place with `ALTER ROLE`, or `docker compose down -v` and start over.

**n8n credentials stopped decrypting** — `N8N_ENCRYPTION_KEY` changed. Restore
the original value; there is no recovery without it.
