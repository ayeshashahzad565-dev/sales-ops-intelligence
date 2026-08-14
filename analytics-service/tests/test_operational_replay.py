"""Replay and retention: the two operations that touch historical data.

Replay is the only thing in Stage 10 that deliberately repeats work, and
retention is the only thing that deletes. Both are therefore written to be
conservative in the same direction: **evidence survives.**

The rule replay is built around is that it must never make a failure look like
it never happened. The original staging rows are never modified; their payloads
are copied into a new batch under a new run, so

    did the first attempt fail?     raw_orders_staging.processing_status
    did a replay of it succeed?     ingestion_replays.outcome

have separate answers in separate places, and both stay true at once.

The rule retention is built around is that only rows which have SETTLED
successfully are ever eligible. `pending` is unfinished work and `failed` is the
dead-letter trail; deleting either would destroy the thing the staging layer
exists to provide.
"""

from __future__ import annotations

import pytest

from analytics import repository
from analytics.operations import service
from tests.operations_fixtures import (
    TEST_ACTOR,
    all_fingerprints,
    execute,
    make_failed_batch,
    make_old_staging,
    make_settings,
    purge_test_data,
    query,
    threshold,
)


@pytest.fixture(scope="session")
def settings():
    return make_settings()


@pytest.fixture(scope="session")
def connection(settings):
    try:
        conn = repository.connect(settings.dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")
    with conn:
        yield conn


@pytest.fixture(autouse=True)
def clean(connection):
    purge_test_data(connection)
    yield
    purge_test_data(connection)


# =============================================================================
# Replay
# =============================================================================


def test_replay_reuses_the_original_payload(settings, connection):
    """Copied verbatim. A replay that quietly repaired its input would be a
    different record of what the source actually sent."""
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    original = _staging(connection, batch)[0]

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    replayed = _staging(connection, str(result["replay_batch_id"]))[0]
    assert replayed["source_payload"] == original["source_payload"]
    assert replayed["order_id"] == original["order_id"]


def test_replay_does_not_modify_the_original(settings, connection):
    """The central rule. A replay must not make a failure look like it never
    happened."""
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    before = _staging(connection, batch)[0]

    service.replay_batch(settings, batch, TEST_ACTOR)

    after = _staging(connection, batch)[0]
    assert after["processing_status"] == "failed" == before["processing_status"]
    assert after["error_message"] == before["error_message"]
    assert after["ingestion_id"] == before["ingestion_id"]


def test_both_facts_stay_true_at_once(settings, connection):
    """"The first attempt failed" and "a replay succeeded" are both recorded."""
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    service.replay_batch(settings, batch, TEST_ACTOR)

    row = _replays(connection, batch)[0]
    assert row["outcome"] == "succeeded"
    assert _staging(connection, batch)[0]["processing_status"] == "failed"


def test_replay_recovers_a_genuinely_recoverable_row(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    before = _fact_count(connection)

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    assert result["records_accepted"] == 1
    assert result["run_status"] == "success"
    assert _fact_count(connection) == before + 1


def test_a_permanently_invalid_row_fails_again(settings, connection):
    """Validation is deterministic, so replaying a malformed payload must
    produce the same verdict rather than a different one."""
    batch = make_failed_batch(connection, recoverable=0, permanent=1)
    before = _fact_count(connection)

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    assert result["records_accepted"] == 0
    assert result["records_rejected"] == 1
    assert result["run_status"] == "failed"
    assert _fact_count(connection) == before
    assert _replays(connection, batch)[0]["outcome"] == "failed_again"


def test_a_partial_replay_is_reported_as_partial(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=1)

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    assert result["records_accepted"] == 1
    assert result["records_rejected"] == 1
    assert result["run_status"] == "partial"
    outcomes = sorted(row["outcome"] for row in _replays(connection, batch))
    assert outcomes == ["failed_again", "succeeded"]


def test_replay_never_duplicates_an_order(settings, connection):
    """`ON CONFLICT (order_id) DO NOTHING`, and a row that was already loaded
    settles as `skipped` rather than `processed`."""
    batch = make_failed_batch(connection, recoverable=0, permanent=0, duplicate=1)
    before = _fact_count(connection)

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    assert result["records_duplicate"] == 1
    assert result["records_accepted"] == 0
    assert _fact_count(connection) == before
    assert _replays(connection, batch)[0]["outcome"] == "duplicate"


def test_repeated_replay_creates_no_duplicates(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    before = _fact_count(connection)

    first = service.replay_batch(settings, batch, TEST_ACTOR)
    second = service.replay_batch(settings, batch, TEST_ACTOR)

    assert first["records_accepted"] == 1
    assert second["records_accepted"] == 0
    assert second["records_duplicate"] == 1
    assert _fact_count(connection) == before + 1


def test_replay_preserves_provenance(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    original = _staging(connection, batch)[0]

    result = service.replay_batch(settings, batch, TEST_ACTOR)

    row = _replays(connection, batch)[0]
    assert row["original_ingestion_id"] == original["ingestion_id"]
    assert str(row["original_batch_id"]) == batch
    assert str(row["replay_batch_id"]) == str(result["replay_batch_id"])
    assert row["original_error"] == original["error_message"]
    assert row["actor"] == TEST_ACTOR
    assert row["attempt_number"] == 1


def test_a_replay_creates_a_new_run_rather_than_rewriting_one(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    result = service.replay_batch(settings, batch, TEST_ACTOR)

    run = query(connection, """
        SELECT * FROM salesops.ingestion_runs WHERE run_id = %(id)s
    """, {"id": result["replay_run_id"]})[0]
    assert run["source"] == "ingestion-replay"
    assert run["status"] in ("success", "partial", "failed")
    assert run["finished_at"] is not None


def test_a_replay_run_cannot_move_the_ingestion_window(settings, connection):
    """The reason a replay uses its own source.

    Stage 3 computes its next window from the newest successful `mock-sales-api`
    run. A replay landing in that source would move the window forward and
    silently skip a day of real orders.
    """
    before = _window_high_water(connection)
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    service.replay_batch(settings, batch, TEST_ACTOR)
    assert _window_high_water(connection) == before


def test_replay_is_bounded(settings, connection):
    """A row that has failed validation three times is failing for a reason
    replay cannot fix."""
    batch = make_failed_batch(connection, recoverable=0, permanent=1)

    for attempt in range(3):
        result = service.replay_batch(settings, batch, TEST_ACTOR)
        assert result["rows_staged"] == 1, f"attempt {attempt + 1} was not staged"

    fourth = service.replay_batch(settings, batch, TEST_ACTOR)
    assert fourth["rows_staged"] == 0
    assert fourth["rows_skipped"] == 1
    assert fourth["run_status"] == "failed"


def test_a_spent_batch_leaves_the_candidate_list(settings, connection):
    batch = make_failed_batch(connection, recoverable=0, permanent=1)
    for _ in range(3):
        service.replay_batch(settings, batch, TEST_ACTOR)

    candidates = repository.replay_candidates(connection)
    row = next(c for c in candidates if str(c["original_batch_id"]) == batch)
    assert row["replay_eligible"] is False

    queue = repository.retry_queue(connection, entity_type="staging_batch")
    entry = next(q for q in queue if q["entity_id"] == batch)
    assert entry["disposition"] == "REPLAY_ATTEMPTS_SPENT"
    assert entry["terminal"] is True


def test_an_unknown_batch_is_refused(settings):
    with pytest.raises(service.OperationsError):
        service.replay_batch(settings, "00000000-0000-0000-0000-000000000000", TEST_ACTOR)


def test_replay_is_audited(settings, connection):
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    service.replay_batch(settings, batch, TEST_ACTOR)

    events = query(connection, """
        SELECT * FROM salesops.operational_events
        WHERE event_type = 'staging_replayed' AND entity_id = %(b)s
    """, {"b": batch})
    assert len(events) == 1
    assert events[0]["reason_code"] == "REPLAY_STAGED"
    assert events[0]["detail"]["original_rows_modified"] is False


def test_replay_changes_no_downstream_stage(settings, connection):
    """A replay writes facts. It does not re-decide, re-explain or re-notify."""
    before = all_fingerprints(connection)
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    service.replay_batch(settings, batch, TEST_ACTOR)

    after = all_fingerprints(connection)
    for key in ("stage6", "stage7", "stage8", "reviews", "stage9"):
        assert after[key] == before[key], key
    # The warehouse legitimately gains one order - that is what a replay is for.
    assert after["warehouse"] != before["warehouse"]


# =============================================================================
# Retention
# =============================================================================


def test_a_pending_row_is_never_deleted(settings, connection):
    """Unfinished work."""
    order_id = make_old_staging(connection, "pending", age_days=500)

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert _exists(connection, order_id)


def test_a_failed_row_is_never_deleted(settings, connection):
    """The dead-letter trail, and the replay source.

    Keeping every failed row forever is stricter than a retention policy needs
    to be. It is also the only default that cannot lose evidence.
    """
    order_id = make_old_staging(connection, "failed", age_days=500)

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert _exists(connection, order_id)


def test_a_recent_settled_row_is_not_deleted(settings, connection):
    order_id = make_old_staging(connection, "processed", age_days=1)

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert _exists(connection, order_id)


@pytest.mark.parametrize("status", ["processed", "skipped"])
def test_an_old_settled_row_is_deleted(settings, connection, status):
    order_id = make_old_staging(connection, status, age_days=500)

    result = service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert result["rows_deleted"] >= 1
    assert not _exists(connection, order_id)


def test_a_dry_run_deletes_nothing(settings, connection):
    order_id = make_old_staging(connection, "processed", age_days=500)

    result = service.purge_staging(settings, dry_run=True, actor=TEST_ACTOR)

    assert result["dry_run"] is True
    assert result["rows_eligible"] >= 1
    assert result["rows_deleted"] == 0
    assert _exists(connection, order_id)


def test_the_report_names_why_each_row_is_protected(settings, connection):
    make_old_staging(connection, "pending", age_days=500)
    make_old_staging(connection, "failed", age_days=500)
    make_old_staging(connection, "processed", age_days=500)

    dispositions = {row["disposition"] for row in service.retention_report(settings)}
    assert {"protected_pending", "protected_failed", "eligible"} <= dispositions


def test_retention_is_idempotent(settings, connection):
    make_old_staging(connection, "processed", age_days=500)

    first = service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)
    second = service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert first["rows_deleted"] >= 1
    assert second["rows_deleted"] == 0
    assert second["rows_eligible"] == 0


def test_the_retention_period_is_configurable(settings, connection):
    order_id = make_old_staging(connection, "processed", age_days=30)

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)
    assert _exists(connection, order_id)

    with threshold(connection, "staging_retention_days", 7):
        service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert not _exists(connection, order_id)


def test_a_row_involved_in_a_replay_is_protected(settings, connection):
    """Provenance. The foreign key would refuse it anyway; the predicate makes
    the rule findable without reading the constraint list."""
    batch = make_failed_batch(connection, recoverable=1, permanent=0)
    result = service.replay_batch(settings, batch, TEST_ACTOR)

    execute(connection, """
        UPDATE salesops.raw_orders_staging
        SET received_at = now() - interval '500 days'
        WHERE batch_id = %(b)s::uuid
    """, {"b": str(result["replay_batch_id"])})

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    remaining = _staging(connection, str(result["replay_batch_id"]))
    assert remaining, "a replayed row was deleted, losing its provenance"


def test_purging_is_audited_only_when_it_deletes(settings, connection):
    before = _purge_events(connection)

    service.purge_staging(settings, dry_run=True, actor=TEST_ACTOR)
    assert _purge_events(connection) == before, "a dry run wrote an audit event"

    make_old_staging(connection, "processed", age_days=500)
    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)
    assert _purge_events(connection) == before + 1


def test_retention_touches_no_other_stage(settings, connection):
    make_old_staging(connection, "processed", age_days=500)
    before = all_fingerprints(connection)

    service.purge_staging(settings, dry_run=False, actor=TEST_ACTOR)

    assert all_fingerprints(connection) == before


# =============================================================================
# Helpers
# =============================================================================


def _staging(connection, batch: str) -> list[dict]:
    return query(connection, """
        SELECT * FROM salesops.raw_orders_staging
        WHERE batch_id = %(b)s::uuid ORDER BY ingestion_id
    """, {"b": batch})


def _replays(connection, batch: str) -> list[dict]:
    return query(connection, """
        SELECT * FROM salesops.ingestion_replays
        WHERE original_batch_id = %(b)s::uuid ORDER BY replay_id
    """, {"b": batch})


def _fact_count(connection) -> int:
    return query(connection, "SELECT count(*) AS n FROM salesops.fact_orders")[0]["n"]


def _window_high_water(connection):
    return query(connection, """
        SELECT max(window_to) AS w FROM salesops.ingestion_runs
        WHERE source = 'mock-sales-api' AND status IN ('success', 'partial')
    """)[0]["w"]


def _exists(connection, order_id: str) -> bool:
    return bool(query(connection, """
        SELECT 1 FROM salesops.raw_orders_staging WHERE order_id = %(o)s
    """, {"o": order_id}))


def _purge_events(connection) -> int:
    return query(connection, """
        SELECT count(*) AS n FROM salesops.operational_events
        WHERE event_type = 'staging_purged'
    """)[0]["n"]
