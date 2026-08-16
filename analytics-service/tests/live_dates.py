"""The two live dates every warehouse-backed suite is validated against.

These used to be literals - `date(2026, 8, 5)` and `date(2026, 8, 9)` - repeated
across seven test modules. That worked for exactly as long as the warehouse held
the dataset they were written against.

It does not survive a rebuild. The order generator anchors its ninety-day window
to today unless `MOCK_API_HISTORY_END_DATE` pins it, so `docker compose down -v`
produces a different series: different totals, different baselines, and a
different set of days that clear the anomaly threshold. A literal then points at
an ordinary Tuesday and the suite reports `severity 'none' != 'major'`, which
reads as a Stage 6 regression rather than as a stale constant.

So both dates are resolved by `bootstrap.sh` and recorded in `.env`:

* `SALESOPS_INCIDENT_DATE` is chosen *before* ingestion and injected into - a
  price collapse and a refund spike on one day, which is what Stage 6 needs to
  grade anything `critical`. It is deterministic because bootstrap put it there;

* `SALESOPS_MAJOR_DATE` is read back *after* the decision stage, because which
  ordinary days rise to `major` is a property of the generated series and not
  something any fixture can choose.

Read from a file rather than queried here on purpose: several suites use these
in `@pytest.mark.parametrize`, which is evaluated at import time, and opening a
database connection during collection would make an unreachable warehouse look
like a collection error instead of a skip.
"""

from __future__ import annotations

from datetime import date, timedelta

from tests.operations_fixtures import load_env_file

__all__ = ["INCIDENT_DATE", "MAJOR_DATE", "MINOR_DATE", "NORMAL_DATE", "resolve"]

#: Offset used only when nothing has been recorded yet - the same one
#: bootstrap.sh applies, so a fresh checkout and a fresh bootstrap agree.
INCIDENT_OFFSET_DAYS = 10


def resolve(key: str, fallback: date) -> date:
    """The date recorded under `key`, or `fallback` if nothing was recorded.

    Never raises on a missing entry. A checkout that has not been bootstrapped
    yet should fail in the assertion that actually needs the data, with the
    warehouse in the message, rather than during import of a fixture module.
    """
    recorded = load_env_file().get(key, "").strip()
    if not recorded:
        return fallback
    try:
        return date.fromisoformat(recorded)
    except ValueError:
        return fallback


#: The injected incident: severe revenue impact plus a severe refund spike, which
#: is the only combination V008 grades `critical`.
INCIDENT_DATE = resolve(
    "SALESOPS_INCIDENT_DATE",
    date.today() - timedelta(days=INCIDENT_OFFSET_DAYS),
)

#: One rung below it, and not injected - ordinary variation that happened to
#: clear the material-impact threshold. Routed to a human.
MAJOR_DATE = resolve("SALESOPS_MAJOR_DATE", INCIDENT_DATE)

#: Two rungs below: a real statistical anomaly whose business impact was too
#: small to be worth a person, so Stage 6 routed it to auto_notify instead. The
#: delivery suites need one, because a notification is only ever sent for these.
MINOR_DATE = resolve("SALESOPS_MINOR_DATE", MAJOR_DATE)

#: An ordinary day, and deliberately a Sunday - the baseline is per weekday, so
#: "normal" has to be normal against the right comparison set.
NORMAL_DATE = resolve("SALESOPS_NORMAL_DATE", INCIDENT_DATE)
