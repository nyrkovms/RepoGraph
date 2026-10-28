#!/usr/bin/env bash
# Migrations on data (IMPL P0.7): fresh database -> base revision -> synthetic catalog -> head -> invariants,
# then downgrade to the base and upgrade again. Uses the compose stack of the caller (CI: default project;
# locally: COMPOSE_FILE=docker-compose.yml:docker-compose.stand.yml to borrow the stand's postgres).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DB="${MIGRATION_CHECK_DB:-migration_check}"
BASE_REV="20261027_0005"
SEED="backend/tests/migrations/seed_${BASE_REV##*_}.sql"
CHECKS="backend/tests/migrations/check_head.sql"
COMPOSE=(docker compose)

psql_admin() { "${COMPOSE[@]}" exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_db() { "${COMPOSE[@]}" exec -T postgres sh -c "psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d $DB"; }
alembic() { "${COMPOSE[@]}" run --rm --no-deps -e POSTGRES_DB="$DB" api alembic "$@"; }

cleanup() { echo "DROP DATABASE IF EXISTS $DB;" | psql_admin; }
trap cleanup EXIT

"${COMPOSE[@]}" build -q api
echo "DROP DATABASE IF EXISTS $DB;" | psql_admin
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
