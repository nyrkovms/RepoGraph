#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-backups}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${OUT_DIR}/packagehub_${STAMP}.sql"

docker compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=plain --no-owner --no-privileges --clean --if-exists' \
  > "$OUT_FILE"

echo "Backup created: ${OUT_FILE}"
