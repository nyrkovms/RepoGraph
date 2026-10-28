#!/usr/bin/env bash
# Re-record upstream responses for offline CI (IMPL P0.5): runs the CI flow (migrate, seed, integration tests,
# acceptance smoke) against the live sources and saves every upstream response into backend/tests/fixtures/http.
# Needs the live network. Uses its own compose project, so the long-lived stand and its volume are not touched.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="${RECORD_PROJECT:-packagehub-record}"
if [[ "$PROJECT" == "packagehub-stand" ]]; then
  echo "refusing to use the stand project for recording" >&2
  exit 1
fi

FIXTURES="backend/tests/fixtures/http"
API_PORT="${RECORD_API_PORT:-18000}"
export RECORD_API_PORT="$API_PORT"
unset COMPOSE_FILE HTTP_REPLAY_DIR HTTP_RECORD_DIR
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.record.yml)

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans; }
trap cleanup EXIT

rm -rf "$FIXTURES"
mkdir -p "$FIXTURES"

"${COMPOSE[@]}" up -d --build --wait postgres redis api worker
"${COMPOSE[@]}" run --rm api alembic upgrade head
"${COMPOSE[@]}" run --rm api python -m app.cli seed --packages flask django requests
"${COMPOSE[@]}" run --rm api pytest tests/integration
BASE_URL="http://localhost:${API_PORT}/api/v1" ./scripts/acceptance_smoke.sh

# The worker writes as root inside the container; make the files ours before counting.
"${COMPOSE[@]}" exec -T api chmod -R a+rwX /app/tests/fixtures/http || true

count="$(find "$FIXTURES" -type f | wc -l | tr -d ' ')"
size="$(du -sh "$FIXTURES" | cut -f1)"
echo "recorded ${count} files, ${size} in ${FIXTURES}"
