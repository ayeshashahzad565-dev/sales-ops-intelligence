#!/usr/bin/env bash
#
# Applies the salesops analytics migrations to the running PostgreSQL container.
#
# Runs every database/migrations/V*.sql in filename order via psql, stopping at
# the first error. Migrations are idempotent, so re-running is safe and is the
# normal way to bring an existing database up to date.
#
# Files are read from inside the container (./database is mounted at /database)
# rather than piped through the shell, so encoding and line endings cannot be
# altered in transit.
#
# Usage:
#   ./database/migrate.sh              apply migrations
#   ./database/migrate.sh --test       apply migrations, then validate the schema
#   ./database/migrate.sh --test-only  validate the schema, applying nothing
#
# --test-only exists because re-applying idempotent migrations to a database
# that already has them prints a "already exists, skipping" notice per object,
# which buries the validation result under output that reads like failure. The
# suite tests the schema as it stands; it does not need the migrations replayed
# first to do that.

set -euo pipefail

DATABASE="${POSTGRES_DB:-salesops}"
USER_NAME="${POSTGRES_USER:-salesops}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname "$script_dir")"

# Run from the repo root so `docker compose` finds docker-compose.yml on its
# own. That keeps every remaining path argument container-absolute, which
# matters for the Git Bash workaround below.
cd "$repo_root"

run_test=false
apply_migrations=true
case "${1:-}" in
    --test)      run_test=true ;;
    --test-only) run_test=true; apply_migrations=false ;;
    "")          ;;
    *)           echo "Unknown option: $1" >&2
                 echo "Usage: ${0##*/} [--test | --test-only]" >&2
                 exit 2 ;;
esac

psql_file() {
    # Git Bash on Windows rewrites arguments that look like Unix paths into host
    # paths, turning /database/migrations/x.sql into C:/Program Files/Git/...
    # MSYS_NO_PATHCONV disables that for this command only. Other shells ignore it.
    MSYS_NO_PATHCONV=1 \
    docker compose exec -T postgres \
        psql -U "$USER_NAME" -d "$DATABASE" -v ON_ERROR_STOP=1 --quiet -f "$1"
}

if [[ "$apply_migrations" == true ]]; then
    shopt -s nullglob
    migrations=("$script_dir"/migrations/V*.sql)
    shopt -u nullglob

    if [[ ${#migrations[@]} -eq 0 ]]; then
        echo "No migrations found in $script_dir/migrations" >&2
        exit 1
    fi

    echo "Applying ${#migrations[@]} migration(s) to '$DATABASE'..."

    for migration in "${migrations[@]}"; do
        name="$(basename "$migration")"
        echo "  -> $name"
        psql_file "/database/migrations/$name"
    done

    echo "Migrations applied."

    docker compose exec -T postgres \
        psql -U "$USER_NAME" -d "$DATABASE" --quiet \
        -c "SELECT version, description, applied_at FROM salesops.schema_migrations ORDER BY version;"
fi

if [[ "$run_test" == true ]]; then
    echo
    echo "Running schema validation..."
    psql_file "/database/tests/test_analytics_schema.sql"
    echo "Schema validation passed."
fi
