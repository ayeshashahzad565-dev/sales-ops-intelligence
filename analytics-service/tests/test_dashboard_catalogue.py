"""The dashboard catalogue, checked without Metabase running.

Every card is SQL plus a rectangle. That makes the catalogue testable as data:
whether a query is read-only, whether it references a relation that exists,
whether two panels sit on top of each other, and - the one that matters -
whether a language model's prose has found its way onto the executive page.

None of this needs the BI tool. If these pass and Metabase is down, the
dashboards are still correct; they are simply not being served.
"""

from __future__ import annotations

import re

import pytest

from analytics import repository
from tests.presentation_fixtures import (
    CRITICAL_DATE,
    METABASE_DIR,
    REPO_ROOT,
    dashboards_module,
    load_env_file,
    make_settings,
    query,
    sql_statements,
)

GRID_COLUMNS = 24

#: Anything that writes. Matched as whole words so a column called
#: `updated_at` or a view called `exec_remediation_status` is not a false hit -
#: the substring mistake this project has made before.
WRITE_KEYWORDS = (
    "insert", "update", "delete", "truncate", "drop", "alter", "create",
    "grant", "revoke", "copy", "merge", "vacuum", "refresh",
)


@pytest.fixture(scope="module")
def catalogue():
    return dashboards_module()


@pytest.fixture(scope="module")
def connection():
    try:
        conn = repository.connect(make_settings().dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Warehouse not reachable ({exc}) - is the stack running?")
    with conn:
        yield conn


# =============================================================================
# Structure
# =============================================================================
def test_the_catalogue_is_not_empty(catalogue):
    assert len(catalogue.CARDS) >= 25
    assert len(catalogue.DASHBOARDS) == 4


def test_card_keys_and_names_are_unique(catalogue):
    keys = [c["key"] for c in catalogue.CARDS]
    names = [c["name"] for c in catalogue.CARDS]
    assert len(keys) == len(set(keys))
    # Names matter as much as keys: provisioning matches existing cards BY NAME,
    # so a duplicate name would make re-running the provisioner update one card
    # twice and orphan the other.
    assert len(names) == len(set(names))


def test_every_dashboard_card_exists(catalogue):
    for dashboard in catalogue.DASHBOARDS:
        for entry in dashboard["cards"]:
            if entry["kind"] == "card":
                assert entry["card"] in catalogue.CARDS_BY_KEY, entry["card"]


def test_every_card_is_placed_on_a_dashboard(catalogue):
    """An unplaced card is a query nobody will ever see."""
    placed = {
        entry["card"]
        for dashboard in catalogue.DASHBOARDS
        for entry in dashboard["cards"]
        if entry["kind"] == "card"
    }
    orphans = {c["key"] for c in catalogue.CARDS} - placed
    assert not orphans, orphans


def test_no_panel_overflows_the_grid(catalogue):
    for dashboard in catalogue.DASHBOARDS:
        for entry in dashboard["cards"]:
            assert entry["col"] >= 0
            assert entry["col"] + entry["size_x"] <= GRID_COLUMNS, entry


def test_no_two_panels_overlap(catalogue):
    """Metabase will happily render two cards on the same cells, and the loser
    is invisible rather than missing - which is how a dashboard silently stops
    showing the thing it was built for."""
    for dashboard in catalogue.DASHBOARDS:
        occupied: dict[tuple[int, int], object] = {}
        for entry in dashboard["cards"]:
            for row in range(entry["row"], entry["row"] + entry["size_y"]):
                for col in range(entry["col"], entry["col"] + entry["size_x"]):
                    previous = occupied.get((row, col))
                    assert previous is None, (
                        f"{dashboard['name']}: {entry} overlaps {previous} "
                        f"at row {row}, col {col}"
                    )
                    occupied[(row, col)] = entry


# =============================================================================
# Read-only
# =============================================================================
def test_every_card_is_a_single_statement(catalogue):
    for card in catalogue.CARDS:
        statements = sql_statements(card["sql"])
        assert len(statements) == 1, f"{card['key']} has {len(statements)} statements"


def test_every_card_only_reads(catalogue):
    for card in catalogue.CARDS:
        body = card["sql"].strip().lower()
        assert body.startswith(("select", "with")), card["key"]
        words = set(re.findall(r"[a-z_]+", body))
        forbidden = words & set(WRITE_KEYWORDS)
        assert not forbidden, f"{card['key']} contains {forbidden}"


def test_every_relation_a_card_reads_exists(connection, catalogue):
    """Catches a renamed view before someone finds it as an empty panel."""
    known = {
        r["table_name"] for r in query(connection, """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'salesops'
        """)
    }
    for card in catalogue.CARDS:
        for relation in re.findall(r"salesops\.([a-z_][a-z0-9_]*)", card["sql"]):
            # Function calls are matched by the same pattern; skip anything the
            # schema does not hold as a relation only if it is a known function.
            assert relation in known, f"{card['key']} reads salesops.{relation}"


def test_no_card_reads_a_base_table_that_a_view_already_covers(catalogue):
    """Section 5 prefers views over duplicating operational tables. Two cards
    legitimately read a table directly - both are documented here - and any
    third one should be a deliberate decision rather than a habit."""
    allowed_direct = {
        "presentation_layers",      # the legend IS the reference table
        "ingestion_replays",        # a plain count; no view adds anything
        "remediation_actions",      # execution_unknown, a single-status filter
    }
    for card in catalogue.CARDS:
        for relation in re.findall(r"salesops\.([a-z_][a-z0-9_]*)", card["sql"]):
            if relation.startswith(("exec_", "ops_", "anomaly_", "incident_",
                                    "audit_", "review_")):
                continue
            assert relation in allowed_direct, f"{card['key']} reads {relation}"


# =============================================================================
# Model output stays where it was put
# =============================================================================
def _cards_of(catalogue, dashboard_key):
    dashboard = next(d for d in catalogue.DASHBOARDS if d["key"] == dashboard_key)
    return [
        catalogue.CARDS_BY_KEY[e["card"]]
        for e in dashboard["cards"] if e["kind"] == "card"
    ]


def test_the_executive_dashboard_shows_no_model_prose(catalogue):
    for card in _cards_of(catalogue, "executive"):
        for column in catalogue.LLM_TEXT_COLUMNS:
            assert column not in card["sql"], f"{card['key']} selects {column}"


def test_the_operational_dashboard_shows_no_model_prose(catalogue):
    """A pipeline's health must not be describable by a language model."""
    for card in _cards_of(catalogue, "operational"):
        for column in catalogue.LLM_TEXT_COLUMNS:
            assert column not in card["sql"], f"{card['key']} selects {column}"


def test_only_the_investigation_dashboard_carries_the_hypothesis(catalogue):
    carriers = {
        card["key"] for card in catalogue.CARDS
        if any(column in card["sql"] for column in catalogue.LLM_TEXT_COLUMNS)
    }
    investigation = {c["key"] for c in _cards_of(catalogue, "investigation")}
    assert carriers
    assert carriers <= investigation, carriers - investigation


def test_the_hypothesis_card_states_that_nothing_verified_it(catalogue):
    card = catalogue.CARDS_BY_KEY["inv_hypothesis"]
    assert "llm_verified" in card["sql"]
    assert "unverified" in card["name"].lower()


def test_the_investigation_dashboard_warns_before_the_model_panel(catalogue):
    """The warning has to come first on the page, not after."""
    dashboard = next(d for d in catalogue.DASHBOARDS if d["key"] == "investigation")
    warning_rows = [
        e["row"] for e in dashboard["cards"]
        if e["kind"] == "text" and "language-model output" in e["text"]
    ]
    hypothesis_rows = [
        e["row"] for e in dashboard["cards"]
        if e["kind"] == "card" and e["card"] == "inv_hypothesis"
    ]
    assert warning_rows and hypothesis_rows
    assert max(warning_rows) < min(hypothesis_rows)


def test_the_executive_dashboard_explains_its_layers(catalogue):
    keys = {e["card"] for e in
            next(d for d in catalogue.DASHBOARDS if d["key"] == "executive")["cards"]
            if e["kind"] == "card"}
    assert "exec_layers" in keys


# =============================================================================
# The incident parameter
# =============================================================================
def test_the_investigation_defaults_to_the_injected_incident(catalogue):
    assert catalogue.DEFAULT_INCIDENT_DATE == CRITICAL_DATE
    dashboard = next(d for d in catalogue.DASHBOARDS if d["key"] == "investigation")
    assert dashboard["parameters"][0]["default"] == CRITICAL_DATE


def test_every_parameterised_card_declares_its_tag(catalogue):
    for card in catalogue.CARDS:
        uses = "{{incident_date}}" in card["sql"]
        declares = "incident_date" in card["template_tags"]
        assert uses == declares, card["key"]


def test_the_parameter_is_wired_to_the_cards_that_use_it(catalogue):
    dashboard = next(d for d in catalogue.DASHBOARDS if d["key"] == "investigation")
    tag = dashboard["parameter_tag"]
    wired = [
        e["card"] for e in dashboard["cards"]
        if e["kind"] == "card" and tag in catalogue.CARDS_BY_KEY[e["card"]]["template_tags"]
    ]
    assert len(wired) >= 4, wired


def test_only_the_investigation_dashboard_declares_a_parameter(catalogue):
    for dashboard in catalogue.DASHBOARDS:
        if dashboard["key"] != "investigation":
            assert dashboard["parameters"] == [], dashboard["key"]


# =============================================================================
# Secrets
# =============================================================================
SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),                 # Groq
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                  # OpenAI-shaped
    re.compile(r"hooks\.slack\.com/services/\S+"),
    re.compile(r"webhook\.site/\S+"),
    # An assignment whose value is a quoted credential-shaped literal. The
    # value has to look like a token: `PASSWORD='` followed by a space is a
    # grep pattern in a shell script, not a secret, and matching it would make
    # this test the boy who cried wolf.
    re.compile(r"(?i)(password|api_key|secret|token)\s*[:=]\s*"
               r"[\"'][A-Za-z0-9+/=_\-]{8,}[\"']"),
)


def _metabase_sources():
    return sorted(
        p for p in METABASE_DIR.rglob("*")
        if p.is_file() and p.suffix in {".py", ".sh", ".md", ".json"}
    )


def test_the_dashboard_configuration_contains_no_secrets():
    for path in _metabase_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{path.name}: {pattern.pattern}"


def test_no_live_credential_appears_in_the_dashboard_configuration():
    """Not a pattern match - the actual values from .env, searched for
    literally. This is what catches a password pasted into a card while
    debugging."""
    env = load_env_file()
    secrets = {
        key: value for key, value in env.items()
        if key.endswith(("PASSWORD", "API_KEY", "WEBHOOK_URL", "ENCRYPTION_KEY"))
        and len(value) > 8
    }
    if not secrets:
        pytest.skip("No .env to check against")
    for path in _metabase_sources():
        text = path.read_text(encoding="utf-8")
        for key, value in secrets.items():
            assert value not in text, f"{path.name} contains the value of {key}"


def test_the_provisioner_redacts_before_printing():
    """Metabase echoes submitted connection details back in some error bodies.
    That is fine until the body reaches a terminal or a CI log."""
    source = (METABASE_DIR / "provision.py").read_text(encoding="utf-8")
    assert "def redact(" in source
    assert "redact(exc.read().decode()" in source
    for key in ("METABASE_ADMIN_PASSWORD", "METABASE_READONLY_DB_PASSWORD",
                "POSTGRES_PASSWORD", "LLM_API_KEY", "NOTIFICATION_WEBHOOK_URL"):
        assert key in source, f"{key} is not in the redaction list"


def test_the_provisioner_has_no_default_credential():
    """A default credential is a hardcoded credential."""
    source = (METABASE_DIR / "provision.py").read_text(encoding="utf-8")
    for key in ("METABASE_ADMIN_PASSWORD", "METABASE_READONLY_DB_PASSWORD"):
        assert f'env.get("{key}"' not in source
        assert f'require(env, "{key}")' in source


def test_metabase_credentials_are_documented_without_values():
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("METABASE_ADMIN_EMAIL", "METABASE_ADMIN_PASSWORD",
                "METABASE_READONLY_DB_PASSWORD"):
        assert key in example, f"{key} is undocumented in .env.example"
    for pattern in SECRET_PATTERNS:
        assert pattern.search(example) is None, pattern.pattern
