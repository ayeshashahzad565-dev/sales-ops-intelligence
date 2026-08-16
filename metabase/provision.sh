#!/usr/bin/env bash
#
# Stage 11. Two steps, in this order:
#
#   1. give the salesops_readonly role a password so it can log in at all.
#      V013 creates the role NOLOGIN and passwordless on purpose - a password in
#      a migration is a password in version control;
#   2. build or update the Metabase dashboards.
#
# Both are idempotent. Re-running rotates the reporting password and re-points
# Metabase at it in one go.
#
# Usage:
#   ./metabase/provision.sh            build or update
#   ./metabase/provision.sh --check    report state, change nothing
#
# The password is passed to psql through stdin as a psql variable, never as a
# command-line argument: arguments are visible in the container's process list.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname "$script_dir")"
cd "$repo_root"

if [[ ! -f .env ]]; then
    echo "No .env found. Copy .env.example and fill it in." >&2
    exit 1
fi

# Read the two values this script needs without exporting the whole file - .env
# also holds the LLM key and the webhook URL, and neither belongs in this
# process's environment.
readonly_password="$(grep -E '^METABASE_READONLY_DB_PASSWORD=' .env | cut -d= -f2- || true)"
db_user="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2- || echo salesops)"
db_name="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2- || echo salesops)"

# The investigation dashboard's default date. bootstrap.sh writes it here after
# injecting the incident; an inherited value from the caller wins, so a run
# inside bootstrap does not depend on the file having been written yet. Left
# unset, the catalogue falls back to its own offset from today - correct on the
# day the incident was injected and wrong the morning after, which is precisely
# why it is persisted rather than recomputed.
if [[ -z "${SALESOPS_INCIDENT_DATE:-}" ]]; then
    export SALESOPS_INCIDENT_DATE="$(grep -E '^SALESOPS_INCIDENT_DATE=' .env | cut -d= -f2- | tr -d '\r' || true)"
fi

if [[ -z "$readonly_password" ]]; then
    echo "METABASE_READONLY_DB_PASSWORD is not set in .env." >&2
    echo "Generate one:  python -c \"import secrets;print(secrets.token_urlsafe(24))\"" >&2
    exit 1
fi

if [[ "${1:-}" != "--check" ]]; then
    echo "Granting the reporting role a login..."
    printf '%s\n' \
        "\\set pw '${readonly_password}'" \
        "ALTER ROLE salesops_readonly WITH LOGIN PASSWORD :'pw';" \
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole" \
        "  FROM pg_roles WHERE rolname = 'salesops_readonly';" \
    | docker compose exec -T postgres \
        psql -U "$db_user" -d "$db_name" -v ON_ERROR_STOP=1 --quiet

    # Proof rather than assertion: the role is asked to write, and must be
    # refused.
    #
    # In TWO steps, and the order matters. "The DELETE failed" is not evidence
    # of anything on its own - a wrong password, a role without LOGIN, or a
    # database that is not listening all fail the same way, and treating any of
    # them as proof would print a reassuring message in exactly the situation
    # where nothing has been verified. So the role must first demonstrate it can
    # connect and read; only then does a refused write mean what it says.
    as_reporting() {
        docker compose exec -T -e PGPASSWORD="$readonly_password" postgres \
            psql -U salesops_readonly -d "$db_name" -h 127.0.0.1 \
            -v ON_ERROR_STOP=1 -qtA -c "$1" 2>&1
    }

    echo "Verifying the reporting role can read..."
    if ! readable="$(as_reporting 'SELECT count(*) FROM salesops.kpi_daily;')"; then
        echo "FATAL: salesops_readonly could not connect or read: $readable" >&2
        exit 1
    fi
    echo "  it can - kpi_daily is readable ($readable row(s))."

    echo "Verifying the reporting role cannot write..."
    if refusal="$(as_reporting 'DELETE FROM salesops.kpi_daily;')"; then
        echo "FATAL: salesops_readonly was able to DELETE from kpi_daily." >&2
        exit 1
    fi
    # And refused for the RIGHT reason. A syntax error would also "fail".
    case "$refusal" in
        *"permission denied"*) echo "  it cannot - $refusal" ;;
        *) echo "FATAL: the write failed, but not for lack of privilege: $refusal" >&2
           exit 1 ;;
    esac
fi

echo
python "$script_dir/provision.py" "$@"
