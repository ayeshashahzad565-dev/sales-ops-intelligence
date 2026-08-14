"""Shared setup for the Stage 11 suites.

Stage 11 adds no behaviour, so these tests are not about what the platform now
does. They are about three things it must still not do:

* **read-only means read-only.** Not "we only wrote SELECTs" - the reporting
  role is asked to write, and must be refused by PostgreSQL rather than by our
  good intentions;

* **model output stays labelled.** The executive dashboard may say a hypothesis
  exists; it may not say what the hypothesis claims. The list of model-written
  columns has one definition, in metabase/dashboards.py, and these tests read
  that same list rather than a copy of it;

* **nothing upstream moved.** Every Stage 6-10 fingerprint is taken before and
  after the presentation queries run.

Stage 11 itself writes nothing, so there is no teardown here - a presentation
layer that needed cleaning up after would not be a presentation layer.

There is one fixture that writes, and it is worth understanding why.
`ensure_critical_incident` rebuilds the 2026-08-05 chain when an earlier suite
has purged the review queue, because the alternative is to skip - and skipping
would turn a dozen assertions about an end-to-end chain into a dozen silent
passes. It builds the chain by calling the real Stage 8 and Stage 9 entry
points, so every guard still runs and every transition is still attributed.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from analytics.config import Settings
from tests.operations_fixtures import REPO_ROOT, load_env_file, make_settings, query

__all__ = [
    "CRITICAL_DATE",
    "MAJOR_DATE",
    "REPO_ROOT",
    "REVIEWER",
    "AUTHORISER",
    "dashboards_module",
    "ensure_critical_incident",
    "load_env_file",
    "make_settings",
    "query",
    "readonly_settings",
    "sql_statements",
]

#: The injected incident, and the one that must stay one rung below it. Both are
#: asserted by date rather than by "the worst anomaly", so a change that
#: reclassified either would fail here instead of quietly re-pointing the test.
CRITICAL_DATE = "2026-08-05"
MAJOR_DATE = "2026-08-09"

METABASE_DIR = REPO_ROOT / "metabase"


def dashboards_module():
    """Import metabase/dashboards.py as a module.

    Loaded by path rather than added to sys.path: the dashboard catalogue is not
    part of the analytics service and should not become importable by it.
    """
    path = METABASE_DIR / "dashboards.py"
    if not path.exists():
        pytest.fail(f"The dashboard catalogue is missing: {path}")
    spec = importlib.util.spec_from_file_location("stage11_dashboards", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stage11_dashboards", module)
    spec.loader.exec_module(module)
    return module


def readonly_settings() -> Settings:
    """Connection settings for salesops_readonly - the role Metabase uses.

    Skips rather than fails when the password is absent: the role exists after
    V013 but cannot log in until metabase/provision.sh has given it a password,
    and a checkout that has never been provisioned is not a broken checkout.
    """
    base = make_settings()
    env_file = load_env_file()
    password = env_file.get("METABASE_READONLY_DB_PASSWORD", "").strip()
    if not password:
        pytest.skip(
            "METABASE_READONLY_DB_PASSWORD is not set - run metabase/provision.sh"
        )
    return Settings(
        host=base.host,
        port=base.port,
        database=base.database,
        user="salesops_readonly",
        password=password,
    )


#: Named separately from the actors any other suite uses, so the demo state is
#: identifiable and the two human roles the architecture distinguishes - the
#: reviewer who confirms the anomaly, the authoriser who approves the action -
#: are visibly two people rather than one convenience account.
REVIEWER = "dana@finance"
AUTHORISER = "priya@revops"


def ensure_critical_incident(settings, connection) -> int:
    """Drive 2026-08-05 from review to executed, if it is not there already.

    Stage 11's assertions are about a complete chain, and the earlier suites
    legitimately empty the review queue and the action table on teardown. Making
    these tests skip on an empty queue would turn every assertion about the
    incident into a silent pass - the exact failure this project has already
    been bitten by twice.

    So the fixture populates instead, and populates by calling the real Stage 8
    and Stage 9 entry points rather than by inserting rows. Every guard runs;
    every transition is recorded with the actor that made it. If the chain
    cannot be built through the front door, that is a genuine failure and the
    test should see it.

    Idempotent throughout, because each of those entry points is.
    """
    from analytics.notifications import service as notification_service
    from analytics.notifications.provider import RecordingProvider
    from analytics.remediation import service as remediation_service
    from analytics.remediation.models import ActionType
    from analytics.remediation.provider import RecordingRemediationProvider

    def review_row():
        rows = query(connection, """
            SELECT review_id, status FROM salesops.review_queue
            WHERE calendar_date = %(d)s
            ORDER BY review_id DESC LIMIT 1
        """, {"d": CRITICAL_DATE})
        return rows[0] if rows else None

    if review_row() is None:
        notification_service.run_routing(
            settings=settings,
            provider=RecordingProvider(),
            recipients=["stage11-demo@example.invalid"],
        )
    review = review_row()
    if review is None:
        pytest.fail(
            f"Stage 8 queued no review for {CRITICAL_DATE} - the critical "
            "decision it depends on is missing"
        )

    if review["status"] == "pending":
        notification_service.claim_review(settings, review["review_id"], REVIEWER)

    approval = remediation_service.approve_review_for_remediation(
        settings, review["review_id"], REVIEWER,
        ActionType.CREATE_INVESTIGATION,
        "confirmed",
        "Stage 11: the incident this platform was built to demonstrate.",
    )
    remediation_id = approval["remediation_id"]

    action = remediation_service.fetch_action(settings, remediation_id)
    if action["status"] == "proposed":
        remediation_service.authorize_action(settings, remediation_id, AUTHORISER)
    if remediation_service.fetch_action(settings, remediation_id)["status"] != "executed":
        remediation_service.execute_action(
            settings, RecordingRemediationProvider(), remediation_id, AUTHORISER
        )
    return remediation_id


def sql_statements(text: str) -> list[str]:
    """Split a card's SQL into statements, ignoring comments and blank lines."""
    lines = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
