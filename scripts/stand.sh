#!/usr/bin/env bash
# Long-lived test stand. The database volume is external and is never removed by this script.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VOLUME="packagehub_stand_pg_data"
export COMPOSE_FILE="docker-compose.yml:docker-compose.stand.yml"
COMPOSE=(docker compose)

usage() {
  cat <<EOF
Usage: $0 <command> [args]

  up                 create the DB volume if missing, build, start, apply migrations
  migrate            backup, stop worker/beat, apply Alembic migrations, start them again
  seed [pkg ...]     seed packages (default: SEED_PACKAGES)
  daily-update       run the daily update now
  status             containers, migration revision, catalog size
  logs [service]     follow logs
  psql               open psql in the stand database
  backup [dir]       pg_dump to backups/ (or dir)
  restore <file>     restore a dump (asks for confirmation)
  down               stop containers; the DB volume is kept
EOF
}

ensure_volume() {
  if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    docker volume create --label packagehub.stand=true "$VOLUME" >/dev/null
    echo "Created volume $VOLUME"
  fi
}

# Back up first (a failed backup aborts), keep worker/beat off the database while the schema changes,
# and bring them back even when the migration fails. Only services that were running are restarted,
# so the first `up` (nothing created yet) does not start them ahead of its own build step.
migrate() {
  scripts/db_backup.sh
  local running rc=0
  running="$("${COMPOSE[@]}" ps --services --status running worker beat | tr '\n' ' ')"
  if [[ -n "${running// /}" ]]; then
    # shellcheck disable=SC2086
    "${COMPOSE[@]}" stop $running
  fi
  "${COMPOSE[@]}" run --rm --no-deps api alembic upgrade head || rc=$?
  if [[ -n "${running// /}" ]]; then
    # shellcheck disable=SC2086
    "${COMPOSE[@]}" start $running || echo "WARNING: could not restart: $running" >&2
  fi
  return "$rc"
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift

case "$cmd" in
  up)
    ensure_volume
    "${COMPOSE[@]}" up -d --build --wait postgres redis
    migrate
    "${COMPOSE[@]}" up -d --build
    ;;
  migrate)
    migrate
    ;;
  seed)
    if [[ $# -gt 0 ]]; then
      "${COMPOSE[@]}" run --rm --no-deps api python -m app.cli seed --packages "$@"
    else
      "${COMPOSE[@]}" run --rm --no-deps api python -m app.cli seed
    fi
    ;;
  daily-update)
    "${COMPOSE[@]}" run --rm --no-deps api python -m app.cli daily-update
    ;;
  status)
    "${COMPOSE[@]}" ps
    "${COMPOSE[@]}" exec -T postgres sh -c \
      'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version" -c "select (select count(*) from packages) as packages, (select count(*) from package_versions) as versions, (select max(started_at) from update_runs) as last_update_run"'
    ;;
  logs)
    "${COMPOSE[@]}" logs -f --tail=100 "$@"
    ;;
  psql)
    "${COMPOSE[@]}" exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    ;;
  backup)
    scripts/db_backup.sh "$@"
    ;;
  restore)
    [[ $# -ge 1 ]] || { usage; exit 1; }
    read -r -p "Restore $1 into the stand database (current data is replaced)? [y/N] " answer
    [[ "$answer" == "y" ]] || exit 1
    scripts/db_restore.sh "$1" --yes
    ;;
  down)
    "${COMPOSE[@]}" down --remove-orphans
    ;;
  *)
    usage
    exit 1
    ;;
esac
