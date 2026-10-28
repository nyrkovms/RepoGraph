#!/usr/bin/env bash
# Migrations on data (IMPL P0.7): fresh database -> base revision -> synthetic catalog -> head -> invariants,
# then downgrade to the base and upgrade again. Runs its own compose project (postgres + api image built from
# this checkout), so it is safe next to the long-lived stand; the project is removed at the end.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="${MIGRATION_CHECK_PROJECT:-packagehub-migcheck}"
if [[ "$PROJECT" == "packagehub-stand" ]]; then
  echo "refusing to run against the stand project" >&2
  exit 1
fi

DB="migration_check"
BASE_REV="20261027_0005"
SEED="backend/tests/migrations/seed_${BASE_REV##*_}.sql"
CHECKS="backend/tests/migrations/check_head.sql"
unset COMPOSE_FILE HTTP_REPLAY_DIR HTTP_RECORD_DIR
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.isolated.yml)

psql_admin() { "${COMPOSE[@]}" exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_db() { "${COMPOSE[@]}" exec -T postgres sh -c "psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d $DB"; }
alembic() { "${COMPOSE[@]}" run --rm --no-deps -e POSTGRES_DB="$DB" api alembic "$@"; }

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${COMPOSE[@]}" build -q api
"${COMPOSE[@]}" up -d --wait postgres
echo "CREATE DATABASE $DB;" | psql_admin

alembic upgrade "$BASE_REV"
psql_db < "$SEED"
alembic upgrade head
psql_db < "$CHECKS"

HEAD="$(alembic heads | awk '{print $1}')"
if [[ "$HEAD" != "$BASE_REV" ]]; then
  # Contract migrations may be declared irreversible; they must say so explicitly by raising in downgrade().
  alembic downgrade "$BASE_REV"
  alembic upgrade head
  psql_db < "$CHECKS"
fi

echo "MIGRATION_CHECK: PASS (base $BASE_REV, head $HEAD)"
