# PackageHub MVP

PyPI-first MVP: каталог метаданных пакетов, demand-driven sync и dependency resolve.

## Prerequisites

- Docker + Docker Compose

## Quick start

1. (Опционально) создать локальный env:

```bash
cp .env.example .env
```

Если metadata JSON нужно брать через приватный forward proxy, переопределите в `.env`:

```bash
SYNC_PYPI_JSON_BASE_URL=https://your-proxy.example/pypi
```

2. Поднять стек (включая `worker` и `beat`):

```bash
docker compose up --build
```

3. Применить миграции:

```bash
docker compose run --rm api alembic upgrade head
```

4. Запустить seed (через CLI или API):

```bash
docker compose run --rm api python -m app.cli seed
```

или

```bash
curl -X POST http://localhost:8000/api/v1/admin/seed \
  -H "Authorization: Bearer phub_admin_dev" \
  -H "Content-Type: application/json" \
  -d '{}'
```

## URLs

- Frontend: `http://localhost:5173`
- API health: `http://localhost:8000/api/v1/health`
- API stats: `http://localhost:8000/api/v1/stats` (Bearer viewer/admin)
- API metrics (Prometheus): `http://localhost:8000/api/v1/metrics`

## Production baseline

- Production env template: [`.env.prod.example`](/Users/mikhail/Documents/Work/repograph/.env.prod.example)
- Full go-live checklist: [`PRODUCTION-CHECKLIST.md`](/Users/mikhail/Documents/Work/repograph/PRODUCTION-CHECKLIST.md)
- DB backup: `./scripts/db_backup.sh`
- DB restore: `./scripts/db_restore.sh <backup-file.sql> --yes` (перезапускает `api/worker` после восстановления)

## Tokens (dev defaults)

- Viewer: `phub_viewer_dev`
- Admin: `phub_admin_dev`

Frontend token можно менять в верхней панели приложения.

## Available CLI

```bash
docker compose run --rm api python -m app.cli seed --packages flask django
docker compose run --rm api python -m app.cli fetch requests
docker compose run --rm api python -m app.cli refresh --limit 100
docker compose run --rm api python -m app.cli retry-failed --limit 100
docker compose run --rm api python -m app.cli stats
```

## Retry failed sync

- Транзиентные ошибки синка (SSL/timeout/remote protocol/429/5xx) складываются в `sync_failures`.
- `celery beat` планирует автоматический ретрай task `retry-failed-sync`.
- Ручной ретрай:

```bash
docker compose run --rm api python -m app.cli retry-failed --limit 100
```

## Acceptance smoke

```bash
./scripts/acceptance_smoke.sh
```

Отчёт последнего прогона: [`ACCEPTANCE-MVP.md`](/Users/mikhail/Documents/Work/repograph/ACCEPTANCE-MVP.md).

## Frontend integration tests

```bash
cd frontend
npm ci
npm run test:run
```

Если локально нет Node/npm, можно запускать тесты в контейнере:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e npm_config_cache=/tmp/.npm \
  -v "$PWD:/workspace" -w /workspace/frontend node:20-alpine \
  sh -lc "npm ci && npm run test:run"
```

## Lint checks

Backend:

```bash
docker compose build api
docker compose run --rm --no-deps api ruff check app tests
```

Frontend:

```bash
cd frontend
npm ci
npm run lint
```

## Backend integration tests

```bash
docker compose up -d --build postgres redis api
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.cli seed --packages flask django requests
docker compose run --rm api pytest tests/integration
docker compose down --remove-orphans
```

## Backend unit tests

```bash
docker compose build api
docker compose run --rm --no-deps api pytest tests/unit
```

## CI

GitHub Actions workflow: [`.github/workflows/ci.yml`](/Users/mikhail/Documents/Work/repograph/.github/workflows/ci.yml)

- `frontend-tests`: `vitest` + production build frontend
- `backend-lint`: `ruff check app tests`
- `frontend-lint`: `eslint .`
- `backend-unit-tests`: `pytest tests/unit`
- `backend-integration-tests`: миграции + seed + `pytest tests/integration`
- `acceptance-smoke`: Docker Compose stack + migrations + seed + `scripts/acceptance_smoke.sh`

## Implemented MVP slices

- `S00-S04`: infra, app scaffold, DB models, Alembic migration, PyPI registry seed
- `S05-S07`: PEP 508 parser/normalizers, PyPI connector, transactional sync pipeline
- `S08-S10`: recursive seed, lazy fetch task flow, batch refresh, DB-backed sync status
- `S11-S12`: catalog/search/package endpoints with filters/pagination/dependency tree
- `S13-S15`: `POST /resolve` (`resolvelib`), lazy-fetch fail-fast, Redis resolve cache
- `S16-S18`: frontend API integration + Prometheus metrics + runbook finalization

## Metrics exposed

- `packagehub_api_latency_seconds`
- `packagehub_resolve_latency_seconds`
- `packagehub_resolve_cache_hits_total`
- `packagehub_resolve_cache_misses_total`
- `packagehub_sync_duration_seconds`
- `packagehub_sync_last_duration_seconds`
- `packagehub_sync_last_status`

## Shutdown

```bash
docker compose down
```
