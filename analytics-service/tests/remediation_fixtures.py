"""Shared setup for the Stage 9 suites.

Stage 9 cannot be tested against a synthetic database. Its guard triggers read
the live Stage 6 decision and the live Stage 8 review, so an authorisation
assembled from invented rows would exercise nothing but the CHECK constraints.
Every test here therefore runs against the real warehouse and skips when it is
unreachable - the same arrangement Stages 5, 7 and 8 use.

Nothing in this module contacts a provider, and no Stage 9 test ever does. The
recording provider is the only implementation there is, and the point of it is
that a test suite for a remediation system must not remediate anything.
"""

from __future__ import annotations

import os
import pathlib
from datetime import date

import pytest

from analytics.config import Settings
from analytics.notifications import service as notification_service
from analytics.notifications.provider import RecordingProvider
from analytics.remediation import service as remediation_service
from analytics.remediation.models import ActionType
from tests.live_dates import INCIDENT_DATE as LIVE_CRITICAL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The live anomalies the whole project is validated against. Recorded by
#: bootstrap.sh rather than written here - see tests/live_dates.py for why a
#: literal cannot survive a `docker compose down -v`.

#: Never a real address, and never one that could reach anybody: .invalid is
#: reserved by RFC 2606 precisely so it cannot resolve.
TEST_RECIPIENT = "stage9-tests@example.invalid"
APPROVER = "approver@example.invalid"
AUTHORIZER = "authorizer@example.invalid"


def load_env_file() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def make_settings() -> Settings:
    env_file = load_env_file()
    password = os.getenv("ANALYTICS_DB_PASSWORD") or env_file.get("POSTGRES_PASSWORD")
    if not password:
        pytest.skip("No database password available - is the stack configured?")
    return Settings(
        host=os.getenv("ANALYTICS_DB_HOST", "localhost"),
        port=int(os.getenv("ANALYTICS_DB_PORT") or env_file.get("POSTGRES_HOST_PORT") or 5432),
        database=os.getenv("ANALYTICS_DB_NAME") or env_file.get("POSTGRES_DB") or "salesops",
        user=os.getenv("ANALYTICS_DB_USER") or env_file.get("POSTGRES_USER") or "salesops",
        password=password,
    )


def purge(connection) -> None:
    """Remove everything the Stage 8 and Stage 9 suites create.

    Actions first: they have no foreign key to `review_queue`, so nothing would
    cascade, and a leftover action would collide with the idempotency key of the
    next test's approval.
    """
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM salesops.remediation_actions")
        cursor.execute("DELETE FROM salesops.review_queue")
        cursor.execute(
            "DELETE FROM salesops.notifications WHERE recipient = %s", (TEST_RECIPIENT,)
        )
    connection.commit()


def populate(settings) -> None:
    """Run Stage 8 routing so a real, correctly-authorised review queue exists."""
    notification_service.run_routing(
        settings=settings,
        provider=RecordingProvider(),
        recipients=[TEST_RECIPIENT],
    )


def query(connection, sql: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        return cursor.fetchall()


def review_id_for(connection, anomaly_date: date) -> int:
    rows = query(connection, """
        SELECT review_id FROM salesops.review_queue WHERE calendar_date = %(d)s
    """, {"d": anomaly_date})
    assert rows, f"no review item for {anomaly_date}"
    return rows[0]["review_id"]


def action_row(connection, remediation_id: int) -> dict:
    return query(connection, """
        SELECT * FROM salesops.remediation_actions WHERE remediation_id = %(id)s
    """, {"id": remediation_id})[0]


def events_for(connection, remediation_id: int) -> list[dict]:
    return query(connection, """
        SELECT from_status, to_status, actor, reason FROM salesops.remediation_events
        WHERE remediation_id = %(id)s ORDER BY occurred_at, event_id
    """, {"id": remediation_id})


def claim(settings, review_id: int, actor: str = APPROVER) -> None:
    notification_service.claim_review(settings, review_id, actor)


def approve(
    settings,
    review_id: int,
    action_type: ActionType = ActionType.CREATE_INVESTIGATION,
    actor: str = APPROVER,
    resolution: str = "confirmed",
) -> dict:
    """Claim (if needed) and approve, returning the created action."""
    return remediation_service.approve_review_for_remediation(
        settings, review_id, actor, action_type, resolution
    )


def authorized_action(
    settings,
    connection,
    anomaly_date: date = LIVE_CRITICAL,
    action_type: ActionType = ActionType.CREATE_INVESTIGATION,
) -> int:
    """A remediation action ready to execute: claimed, approved and authorised."""
    review_id = review_id_for(connection, anomaly_date)
    claim(settings, review_id)
    created = approve(settings, review_id, action_type)
    remediation_service.authorize_action(settings, created["remediation_id"], AUTHORIZER)
    return created["remediation_id"]


def stage6_fingerprint(connection) -> str:
    """Every Stage 6 verdict, in one comparable string."""
    return query(connection, """
        SELECT md5(string_agg(
            decision_id || '|' || severity || '|' || routing || '|' || decision || '|' ||
            notification_allowed || '|' || human_review_required || '|' ||
            decision_reason_code, ',' ORDER BY decision_id)) AS fingerprint
        FROM salesops.anomaly_decisions
    """)[0]["fingerprint"]


def stage7_fingerprint(connection) -> str:
    return query(connection, """
        SELECT md5(COALESCE(string_agg(
            hypothesis_id || '|' || summary || '|' || confidence || '|' ||
            primary_hypothesis, ',' ORDER BY hypothesis_id), '')) AS fingerprint
        FROM salesops.anomaly_hypotheses
    """)[0]["fingerprint"]


def warehouse_fingerprint(connection) -> str:
    """The business data itself: orders, KPIs, detections, customers.

    Stage 9 executes remediation. If any of this ever changed as a result, the
    system would have taken a business action - which is the one thing it must
    not do.
    """
    return query(connection, """
        SELECT md5(concat_ws('|',
            (SELECT count(*) || coalesce(sum(net_amount_usd)::text, '')
               FROM salesops.fact_orders),
            (SELECT count(*) || coalesce(sum(net_revenue_usd)::text, '')
               FROM salesops.kpi_daily),
            (SELECT count(*) || coalesce(sum(anomaly_score)::text, '')
               FROM salesops.anomaly_daily),
            (SELECT count(*)::text FROM salesops.dim_customer)
        )) AS fingerprint
    """)[0]["fingerprint"]
