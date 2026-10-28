#!/usr/bin/env bash
# Migrations on data (IMPL P0.7). In a throwaway compose project (safe next to the stand and other sessions):
# 1. fresh database -> base revision -> synthetic catalog -> head -> invariants -> `alembic check`;
# 2. downgrade to the base: the schema must match the base schema (allowed leftovers listed below), upgrade again;
# 3. negative case: duplicate packages that 0006 would merge (IMPL T16) must stop the upgrade at the base revision.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="${MIGRATION_CHECK_PROJECT:-packagehub-migcheck-$$}"
if [[ "$PROJECT" == "packagehub-stand" ]]; then
  echo "refusing to run against the stand project" >&2
  exit 1
fi

BASE_REV="20261027_0005"
SEED="backend/tests/migrations/seed_${BASE_REV##*_}.sql"
CHECKS="backend/tests/migrations/check_head.sql"
# Differences a downgrade may leave on purpose (see the downgrade() of each migration).
ALLOWED_AFTER_DOWNGRADE='^C:artifacts\.download_url:'
unset COMPOSE_FILE HTTP_REPLAY_DIR HTTP_RECORD_DIR
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.isolated.yml)

psql_admin() { "${COMPOSE[@]}" exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_db() { "${COMPOSE[@]}" exec -T postgres sh -c "psql -v ON_ERROR_STOP=1 -q -At -U \"\$POSTGRES_USER\" -d $1"; }
alembic() { local db="$1"; shift; "${COMPOSE[@]}" run --rm --no-deps -e POSTGRES_DB="$db" api alembic "$@"; }
schema_snapshot() {
  psql_db "$1" <<'SQL'
SELECT 'T:' || table_name FROM information_schema.tables WHERE table_schema = 'public'
UNION ALL SELECT 'C:' || table_name || '.' || column_name || ':' || is_nullable FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL SELECT 'K:' || conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public'
UNION ALL SELECT 'I:' || indexname FROM pg_indexes WHERE schemaname = 'public'
ORDER BY 1;
SQL
}

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${COMPOSE[@]}" build -q api
"${COMPOSE[@]}" up -d --wait postgres
echo "CREATE DATABASE migration_check; CREATE DATABASE migration_dup;" | psql_admin

# 1. base -> data -> head; the models must describe exactly the schema the migrations produce.
alembic migration_check upgrade "$BASE_REV"
BASE_SCHEMA="$(schema_snapshot migration_check)"
psql_db migration_check < "$SEED"
alembic migration_check upgrade head
psql_db migration_check < "$CHECKS"
alembic migration_check check

# 2. downgrade restores the base schema, upgrade again
HEAD="$(alembic migration_check heads | awk '{print $1}')"
if [[ "$HEAD" != "$BASE_REV" ]]; then
  # Contract migrations may be declared irreversible; they must say so explicitly by raising in downgrade().
  alembic migration_check downgrade "$BASE_REV"
  if ! diff <(echo "$BASE_SCHEMA" | grep -Ev "$ALLOWED_AFTER_DOWNGRADE") \
            <(schema_snapshot migration_check | grep -Ev "$ALLOWED_AFTER_DOWNGRADE"); then
    echo "schema after downgrade differs from $BASE_REV" >&2
    exit 1
  fi
  alembic migration_check upgrade head
  psql_db migration_check < "$CHECKS"
fi

# 3. T16: duplicates by (ecosystem, namespace NULL/'', name) stop the upgrade before any change
alembic migration_dup upgrade "$BASE_REV"
psql_db migration_dup <<'SQL'
INSERT INTO packages (id, ecosystem, namespace, name, normalized_name, primary_registry_id, latest_version, updated_at, synced_at, raw_metadata)
SELECT md5('dup' || n)::uuid, 'pypi', NULL, 'dup', 'dup', (SELECT id FROM registries WHERE slug = 'pypi'), '1.0', now(), now(), '{}'
FROM generate_series(1, 2) AS n;
SQL
DUP_LOG="$(mktemp)"
if alembic migration_dup upgrade head > "$DUP_LOG" 2>&1; then
  echo "upgrade with duplicate packages should have stopped (IMPL T16)" >&2
  exit 1
fi
grep -q "duplicate packages" "$DUP_LOG" || { cat "$DUP_LOG" >&2; exit 1; }
rm -f "$DUP_LOG"
if [[ "$(psql_db migration_dup <<< 'SELECT version_num FROM alembic_version')" != "$BASE_REV" ]]; then
  echo "failed upgrade moved alembic_version" >&2
  exit 1
fi

echo "MIGRATION_CHECK: PASS (base $BASE_REV, head $HEAD; downgrade restores the base schema; T16 stop)"
