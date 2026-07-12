#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup-file.sql> [--yes]"
  exit 1
fi

BACKUP_FILE="$1"
CONFIRM="${2:-}"

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Backup file not found: $BACKUP_FILE"
  exit 1
fi

if [[ "$CONFIRM" != "--yes" ]]; then
  echo "Restore is destructive. Re-run with --yes to continue."
  exit 1
fi

cat "$BACKUP_FILE" | docker compose exec -T postgres sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

docker compose restart api worker >/dev/null 2>&1 || true

echo "Restore completed from: ${BACKUP_FILE}"
