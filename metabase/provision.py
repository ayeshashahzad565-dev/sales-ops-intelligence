#!/usr/bin/env python3
"""Provision the Stage 11 dashboards into the running Metabase instance.

Idempotent. Run it once to build the dashboards, run it again after editing
dashboards.py and it updates the cards in place rather than creating a second
copy of everything - cards and dashboards are matched by name inside the
Stage 11 collection.

    python metabase/provision.py            # build or update
    python metabase/provision.py --check    # report state, change nothing

Everything it needs comes from the environment (or the repository .env, which
is gitignored):

    METABASE_URL                    default http://localhost:3000
    METABASE_ADMIN_EMAIL            the Metabase login it creates or uses
    METABASE_ADMIN_PASSWORD
    METABASE_READONLY_DB_PASSWORD   password for the salesops_readonly role

No credential is written to a file, echoed, or included in an error message.
The only place any of them lands is Metabase's own encrypted settings, which is
what a BI tool's database connection is.

Stdlib only - urllib, not requests - so this runs against a bare Python without
adding a dependency to a project that otherwise has none on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboards import (  # noqa: E402
    CARDS,
    CARDS_BY_KEY,
    COLLECTION_DESCRIPTION,
    COLLECTION_NAME,
    DASHBOARDS,
    DATABASE_NAME,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The connection Metabase stores. Host and port are the DOCKER NETWORK names:
# Metabase reaches PostgreSQL as a container, not through the published host
# port, and hardcoding localhost here would work from a laptop and fail in
# every other environment.
DB_HOST = "postgres"
DB_PORT = 5432

SECRET_ENV_KEYS = (
    "METABASE_ADMIN_PASSWORD",
    "METABASE_READONLY_DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "LLM_API_KEY",
    "NOTIFICATION_WEBHOOK_URL",
    "N8N_ENCRYPTION_KEY",
)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
def load_env() -> dict:
    """Environment first, .env second. Never the other way round."""
    values: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for key, value in os.environ.items():
        if key in values or key.startswith(("METABASE_", "POSTGRES_")):
            values[key] = value
    return values


def require(env: dict, key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise SystemExit(
            f"{key} is not set. Add it to .env or the environment. "
            "No default is provided: a default credential is a hardcoded credential."
        )
    return value


def redact(text: str, env: dict) -> str:
    """Strip anything secret out of a string before it is printed.

    Metabase echoes submitted connection details back in some error bodies. That
    is fine until the body reaches a terminal, a CI log or a screenshot.
    """
    for key in SECRET_ENV_KEYS:
        secret = env.get(key, "")
        if secret and len(secret) > 3:
            text = text.replace(secret, f"<{key}>")
    return text


# ---------------------------------------------------------------------------
# A very small Metabase client
# ---------------------------------------------------------------------------
class Metabase:
    def __init__(self, base_url: str, env: dict):
        self.base = base_url.rstrip("/")
        self.env = env
        self.session_id: str | None = None

    def _call(self, method: str, path: str, body=None, expect_json=True):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.session_id:
            request.add_header("X-Metabase-Session", self.session_id)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode()[:800], self.env)
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} -> {exc.reason}") from None
        if not expect_json or not payload:
            return payload
        return json.loads(payload)

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    def put(self, path, body):
        return self._call("PUT", path, body)

    # -- lifecycle ----------------------------------------------------------
    def wait_until_up(self, timeout_seconds: int = 180) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                if self.get("/api/health").get("status") == "ok":
                    return
            except RuntimeError:
                pass
            time.sleep(3)
        raise SystemExit(f"Metabase did not become healthy at {self.base}")

    def authenticate(self, email: str, password: str, first: str, last: str) -> None:
        properties = self.get("/api/session/properties")
        if not properties.get("has-user-setup"):
            token = properties.get("setup-token")
            if not token:
                raise SystemExit(
                    "Metabase reports no user is set up but issued no setup token. "
                    "Finish setup in the browser once, then re-run."
                )
            print("  Metabase has no user yet - running first-time setup")
            result = self.post("/api/setup", {
                "token": token,
                "user": {
                    "email": email, "password": password, "password_confirm": password,
                    "first_name": first, "last_name": last, "site_name": "Sales Ops",
                },
                "prefs": {"site_name": "Sales Ops", "allow_tracking": False},
                # The analytics database is added separately, with the read-only
                # role. Adding it here would use whatever this step defaults to.
                "database": None,
            })
            self.session_id = result.get("id") if isinstance(result, dict) else result
        else:
            self.session_id = self.post(
                "/api/session", {"username": email, "password": password}
            )["id"]

    # -- database -----------------------------------------------------------
    def ensure_database(self, name: str, db_name: str, user: str, password: str) -> int:
        details = {
            "host": DB_HOST,
            "port": DB_PORT,
            "dbname": db_name,
            "user": user,
            "password": password,
            "ssl": False,
            "tunnel-enabled": False,
        }
        existing = next(
            (d for d in self.get("/api/database")["data"] if d["name"] == name), None
        )
        if existing:
            # Re-send the details so a rotated password takes effect, but never
            # print them and never diff them into the console.
            self.put(f"/api/database/{existing['id']}",
                     {"name": name, "engine": "postgres", "details": details})
            print(f"  Database connection '{name}' updated (id {existing['id']})")
            return existing["id"]

        created = self.post("/api/database", {
            "name": name, "engine": "postgres", "details": details,
            "is_full_sync": True, "is_on_demand": False,
        })
        print(f"  Database connection '{name}' created (id {created['id']})")
        return created["id"]

    def sync_database(self, database_id: int) -> None:
        self.post(f"/api/database/{database_id}/sync_schema", {})

    # -- collection ---------------------------------------------------------
    def ensure_collection(self, name: str, description: str) -> int:
        for collection in self.get("/api/collection"):
            if collection.get("name") == name and not collection.get("archived"):
                return collection["id"]
        created = self.post("/api/collection",
                            {"name": name, "description": description,
                             "parent_id": None})
        print(f"  Collection '{name}' created (id {created['id']})")
        return created["id"]

    # -- cards --------------------------------------------------------------
    def existing_cards(self, collection_id: int) -> dict:
        items = self.get(f"/api/collection/{collection_id}/items?models=card")
        return {i["name"]: i["id"] for i in items.get("data", [])}

    def ensure_card(self, spec, database_id: int, collection_id: int,
                    existing: dict) -> int:
        body = {
            "name": spec["name"],
            "description": spec["description"] or None,
            "display": spec["display"],
            "collection_id": collection_id,
            "visualization_settings": spec["visualization_settings"],
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {
                    "query": spec["sql"].strip(),
                    "template-tags": spec["template_tags"],
                },
            },
        }
        card_id = existing.get(spec["name"])
        if card_id:
            self.put(f"/api/card/{card_id}", body)
            return card_id
        return self.post("/api/card", body)["id"]

    # -- dashboards ---------------------------------------------------------
    def ensure_dashboard(self, spec, collection_id: int, card_ids: dict) -> int:
        items = self.get(f"/api/collection/{collection_id}/items?models=dashboard")
        by_name = {i["name"]: i["id"] for i in items.get("data", [])}

        dashboard_id = by_name.get(spec["name"])
        if dashboard_id is None:
            dashboard_id = self.post("/api/dashboard", {
                "name": spec["name"],
                "description": spec["description"],
                "collection_id": collection_id,
            })["id"]
            print(f"  Dashboard '{spec['name']}' created (id {dashboard_id})")

        dashcards = []
        next_placeholder = -1
        for entry in spec["cards"]:
            common = {
                "id": next_placeholder,
                "row": entry["row"], "col": entry["col"],
                "size_x": entry["size_x"], "size_y": entry["size_y"],
                "series": [], "parameter_mappings": [],
            }
            next_placeholder -= 1

            if entry["kind"] == "text":
                common.update({
                    "card_id": None,
                    "visualization_settings": {
                        "text": entry["text"],
                        **entry.get("settings", {}),
                        "virtual_card": {
                            "name": None, "display": "text",
                            "visualization_settings": {},
                            "dataset_query": {}, "archived": False,
                        },
                    },
                })
            else:
                card_spec = CARDS_BY_KEY[entry["card"]]
                card_id = card_ids[entry["card"]]
                common.update({"card_id": card_id, "visualization_settings": {}})
                tag = spec.get("parameter_tag")
                if tag and tag in card_spec["template_tags"]:
                    common["parameter_mappings"] = [{
                        "parameter_id": spec["parameters"][0]["id"],
                        "card_id": card_id,
                        "target": ["variable", ["template-tag", tag]],
                    }]
            dashcards.append(common)

        self.put(f"/api/dashboard/{dashboard_id}", {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
            "dashcards": dashcards,
            # Metabase centres a dashboard in a fixed-width column by default,
            # which leaves a wide empty margin either side and squeezes tables
            # that already carry seven or eight columns. These panels are sized
            # for their data, so they get the window.
            "width": spec.get("width", "full"),
        })
        return dashboard_id


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Provision the Stage 11 dashboards.")
    parser.add_argument("--check", action="store_true",
                        help="report what exists and change nothing")
    args = parser.parse_args()

    env = load_env()
    base_url = env.get("METABASE_URL", "http://localhost:3000")
    email = require(env, "METABASE_ADMIN_EMAIL")
    password = require(env, "METABASE_ADMIN_PASSWORD")
    readonly_password = require(env, "METABASE_READONLY_DB_PASSWORD")
    db_name = env.get("POSTGRES_DB", "salesops")

    metabase = Metabase(base_url, env)
    print(f"Metabase at {base_url}")
    metabase.wait_until_up()
    metabase.authenticate(email, password,
                          env.get("METABASE_ADMIN_FIRST_NAME", "Sales"),
                          env.get("METABASE_ADMIN_LAST_NAME", "Ops"))
    print("  Authenticated")

    if args.check:
        databases = [d["name"] for d in metabase.get("/api/database")["data"]]
        collections = [c["name"] for c in metabase.get("/api/collection")]
        print(f"  Databases:   {databases}")
        print(f"  Collections: {collections}")
        if COLLECTION_NAME in collections:
            collection_id = metabase.ensure_collection(COLLECTION_NAME,
                                                       COLLECTION_DESCRIPTION)
            cards = metabase.existing_cards(collection_id)
            print(f"  Cards:       {len(cards)}")
            dashboards = metabase.get(
                f"/api/collection/{collection_id}/items?models=dashboard")
            print("  Dashboards:  "
                  f"{[d['name'] for d in dashboards.get('data', [])]}")
        return 0

    database_id = metabase.ensure_database(
        DATABASE_NAME, db_name, "salesops_readonly", readonly_password)
    metabase.sync_database(database_id)

    collection_id = metabase.ensure_collection(COLLECTION_NAME, COLLECTION_DESCRIPTION)

    existing = metabase.existing_cards(collection_id)
    card_ids = {}
    for spec in CARDS:
        card_ids[spec["key"]] = metabase.ensure_card(
            spec, database_id, collection_id, existing)
    print(f"  {len(card_ids)} card(s) in place")

    for spec in DASHBOARDS:
        dashboard_id = metabase.ensure_dashboard(spec, collection_id, card_ids)
        print(f"  {spec['name']}: {base_url}/dashboard/{dashboard_id}")

    print("\nDone. The connection Metabase stores is salesops_readonly, which has "
          "SELECT and nothing else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
