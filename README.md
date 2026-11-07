# PackageHub

Внутренний каталог метаданных пакетов семи экосистем: PyPI, npm, crates.io, Go, NuGet, Maven и RPM. Страницы каталога и карточка пакета, поиск, дайджест обновлений (Updates), demand-driven sync и dependency resolve.

## Документы

- [`CLAUDE.md`](CLAUDE.md): правила работы агентов и карта репозитория
- [`SPEC-MULTI-ECOSYSTEM.md`](SPEC-MULTI-ECOSYSTEM.md): дизайн мультиэкосистемного каталога
- [`IMPL-MULTI-ECOSYSTEM.md`](IMPL-MULTI-ECOSYSTEM.md): план реализации по стадиям
- [`SPEC-MVP-API.md`](SPEC-MVP-API.md): контракты API
- [`SPEC-DAILY-UPDATE.md`](SPEC-DAILY-UPDATE.md): ежедневное обновление и дайджест
- [`SPEC-CATALOG-PAGES.md`](SPEC-CATALOG-PAGES.md): главная, поиск и карточка пакета
- [`SPEC-UPDATES-DIGEST.md`](SPEC-UPDATES-DIGEST.md): редизайн страницы Updates (черновик)
- [`RESOLVER.md`](RESOLVER.md): dependency resolve
- [`docs/ecosystems.md`](docs/ecosystems.md): адресация пакетов и resolve по экосистемам
- [`PRODUCTION-CHECKLIST.md`](PRODUCTION-CHECKLIST.md): чек-лист выката
- `docs/archive/`: история MVP

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

2. Поднять стек (включая `worker`, `beat` и `rpm-worker`):

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
- API stats: `http://localhost:8000/api/v1/stats` (счётчики по экосистемам и источникам)
- API metrics (Prometheus): `http://localhost:8000/api/v1/metrics`
- OpenAPI docs: `http://localhost:8000/docs` (также `/redoc`, `/openapi.json`); включены по умолчанию, отключаются `API_DOCS_ENABLED=false`
- Страницы frontend: `/` (главная), `/search` (поиск), `/updates` (обновления), `/resolve` (resolve зависимостей), `/package/<экосистема>/...` (карточка пакета)

## API: адресация по purl и resolve

Пакет адресуется экосистемой и координатами или purl; resolve принимает корни и ограничения:

```bash
curl http://localhost:8000/api/v1/packages/pypi/flask/-/
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"roots": [{"purl": "pkg:pypi/flask", "constraint": ">=3.0"}]}'
```

Группы эндпоинтов (полный контракт: [`SPEC-MVP-API.md`](SPEC-MVP-API.md); примеры по каждой экосистеме: [`docs/ecosystems.md`](docs/ecosystems.md)):

- пакеты и версии: `/packages`, `/packages/{ecosystem}/{координаты}/-/…`, `/search`, `/purl/{purl}`;
- resolve: `POST /resolve`;
- страницы каталога: `/catalog/summary`, `/packages/{ecosystem}/{координаты}/-/overview`, `/-/dependents`, `/-/versions/compare`;
- обновления: `/updates`, `/updates/summary`;
- источники и профили: `/registries`, `/profiles`;
- admin: `/admin/*` (токен администратора);
- `/metrics`: метрики Prometheus.

## Test stand

A long-lived stand whose database survives for the whole project lifecycle. Postgres data lives in
the external volume `packagehub_stand_pg_data`: `docker compose down -v`, image rebuilds and
`scripts/stand.sh down` never remove it. Containers restart automatically, beat runs the daily update.

```bash
scripts/stand.sh up        # first run creates the volume; later runs rebuild and migrate
scripts/stand.sh seed      # SEED_PACKAGES, or: scripts/stand.sh seed flask requests
scripts/stand.sh migrate   # backup, stop worker/beat, alembic upgrade head, start them again
scripts/stand.sh daily-update  # run the daily update now
scripts/stand.sh status    # containers, migration revision, catalog size
scripts/stand.sh logs [service]
scripts/stand.sh psql
scripts/stand.sh backup [dir]  # backups/packagehub_<timestamp>.sql
scripts/stand.sh restore <file>  # asks for confirmation
scripts/stand.sh down      # stop containers; the volume is kept
```

Do not run the plain `docker compose up` alongside the stand: both use ports 5432, 6379, 8000 and 5173.
Removing the data is a deliberate manual step: `docker volume rm packagehub_stand_pg_data`.

## Production baseline

- Production env template: [`.env.prod.example`](.env.prod.example)
- Full go-live checklist: [`PRODUCTION-CHECKLIST.md`](PRODUCTION-CHECKLIST.md)
- DB backup: `./scripts/db_backup.sh`
- DB restore: `./scripts/db_restore.sh <backup-file.sql> --yes` (перезапускает `api/worker` после восстановления)

## Access

- Read API и Web UI открыты без токена. Доступ к ним ограничивайте сетью (внутренний контур, VPN, позже OIDC-прокси).
- Admin API (`/admin/*`) требует `Authorization: Bearer <API_TOKEN_ADMIN>`. Dev-значение: `phub_admin_dev`.

## Available CLI

```bash
docker compose run --rm api python -m app.cli seed --packages flask django pkg:pypi/requests
docker compose run --rm api python -m app.cli fetch pkg:pypi/requests
docker compose run --rm api python -m app.cli retry-failed --limit 100
docker compose run --rm api python -m app.cli daily-update [--ecosystem pypi|npm|...]
docker compose run --rm api python -m app.cli presence-probe [--ecosystem pypi]
docker compose run --rm api python -m app.cli registries import <file.yaml>
docker compose run --rm api python -m app.cli registries test <slug>
docker compose run --rm api python -m app.cli registries list [--ecosystem ...]
docker compose run --rm api python -m app.cli stats
```

## Daily update & digest

- Раз в сутки `celery beat` запускает task `run_daily_update`: для каждой экосистемы спрашивает источники, что изменилось, обновляет изменившиеся пакеты каталога, пишет журнал изменений в `package_events` и прогон в журнал `runs`.
- Ручной запуск:

```bash
docker compose run --rm api python -m app.cli daily-update
curl -X POST -H "Authorization: Bearer phub_admin_dev" http://localhost:8000/api/v1/admin/daily-update
```

- `GET /api/v1/updates` — лента событий (`since`, `until`, `kind`, `ecosystem`, `registry`, `package` — purl или имя PyPI, `cursor`, `limit`), окно по `detected_at`.
- `GET /api/v1/updates/summary` — дайджест по пакетам (`period=day|week` или `since`/`until`, `tz` — IANA-зона для границы «сегодня», `ecosystem`, `registry`); `last_runs` — статус последнего прогона каждой экосистемы.
- `GET /api/v1/admin/sync/status` собирается из журнала `runs`: кроме сводки отдаёт `last_update_run`, `recent_runs` и состояние источников (`registries`).
- UI: страница `/updates` («Обновления»): Сегодня / Неделя / Период, фильтр по типу события.
- Метрики: `packagehub_daily_update_*`, `packagehub_package_events_total{kind}`.
- Раз в неделю (воскресенье, 04:00 UTC; `PRESENCE_PROBE_ENABLED`, `PRESENCE_PROBE_DAY_OF_WEEK`, `PRESENCE_PROBE_HOUR_UTC`) beat запускает presence probe: ищет пакеты каталога в источниках, где они появились позже. Вручную: `python -m app.cli presence-probe` или `POST /api/v1/admin/presence-probe`. Время ежедневного прогона: `DAILY_UPDATE_HOUR_UTC` / `DAILY_UPDATE_MINUTE_UTC` (по умолчанию 03:00 UTC).
- Остальные admin-триггеры (`Authorization: Bearer <API_TOKEN_ADMIN>`): `POST /admin/seed`, `/admin/fetch/{purl}`, `/admin/packages` (список purl), `/admin/retry-failed`; `GET /admin/sync/status`.

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

Отчёт последнего прогона: [`docs/archive/ACCEPTANCE-MVP.md`](docs/archive/ACCEPTANCE-MVP.md).

## Frontend integration tests

Локально (после `cd frontend && npm ci`): `make frontend` — lint и тесты frontend. Или:

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

Локально: `make check` — всё, что запускает CI без docker: ruff, mypy, backend unit, frontend lint и тесты (frontend требует `npm ci`). Отдельно: `make backend-lint`, `make mypy`.

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
docker compose up -d --build postgres redis api rpm-worker
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.cli seed --packages flask django requests
docker compose run --rm api pytest tests/integration
docker compose down --remove-orphans
```

Тесты идут офлайн по записанным ответам источников (`HTTP_REPLAY_DIR`, см. «Offline fixtures»): перед запуском задайте `HTTP_REPLAY_DIR=/app/tests/fixtures/http`, как в CI.

## Backend unit tests

Локально: `make backend-unit`. В контейнере:

```bash
docker compose build api
docker compose run --rm --no-deps api pytest tests/unit
```

## CI

GitHub Actions workflow: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)

- `typecheck`: `mypy`
- `backend-lint`: `ruff check app tests`
- `frontend-lint`: `eslint .`
- `frontend-tests`: `vitest` + production build frontend
- `backend-unit-tests`: `pytest tests/unit`
- `rpm-worker-tests`: тесты rpm-worker в его образе (без сети)
- `migrations`: миграции на синтетических данных (base -> head -> downgrade -> head)
- `backend-integration-tests`: миграции + seed + `pytest tests/integration` (стек с `rpm-worker`)
- `acceptance-smoke`: Docker Compose stack + migrations + seed + `scripts/acceptance_smoke.sh`

Nightly (`nightly.yml`): `live-integration-tests` и `live-acceptance-smoke` против живых источников.

### Offline fixtures

CI does not use the live internet: `backend-integration-tests` and `acceptance-smoke` run with
`HTTP_REPLAY_DIR=/app/tests/fixtures/http` and read upstream responses recorded in `backend/tests/fixtures/http/`
(a request that was not recorded fails with `ReplayMiss`). To re-record them (needs the live network, uses its own
compose project `packagehub-record`, does not touch the stand):

```bash
scripts/record_fixtures.sh
```

The nightly workflow [`.github/workflows/nightly.yml`](.github/workflows/nightly.yml)
runs the same flow against the live sources (02:30 UTC, or manually via `workflow_dispatch`).

## Implemented MVP slices (history)

Срезы MVP ниже — история. Текущая работа идёт по стадиям: [`IMPL-MULTI-ECOSYSTEM.md`](IMPL-MULTI-ECOSYSTEM.md), [`SPEC-CATALOG-PAGES.md`](SPEC-CATALOG-PAGES.md), [`SPEC-UPDATES-DIGEST.md`](SPEC-UPDATES-DIGEST.md).

- `S00-S04`: infra, app scaffold, DB models, Alembic migration, PyPI registry seed
- `S05-S07`: PEP 508 parser/normalizers, PyPI connector, transactional sync pipeline
- `S08-S10`: recursive seed, lazy fetch task flow, DB-backed sync status (batch refresh was removed in E0-B: packages are updated by the daily update)
- `S11-S12`: catalog/search/package endpoints with filters/pagination/dependency tree
- `S13-S15`: `POST /resolve` (`resolvelib`; graph format, the nested one is an alias), lazy fetch, Redis resolve cache
- `S16-S18`: frontend API integration + Prometheus metrics + runbook finalization

## Metrics exposed

- `packagehub_api_latency_seconds`
- `packagehub_resolve_latency_seconds`
- `packagehub_resolve_cache_hits_total`
- `packagehub_resolve_cache_misses_total`
- `packagehub_sync_duration_seconds`
- `packagehub_sync_last_duration_seconds`
- `packagehub_sync_last_status`
- `packagehub_daily_update_duration_seconds`
- `packagehub_daily_update_packages{result}`
- `packagehub_daily_update_last_success_timestamp`
- `packagehub_source_degraded{ecosystem,registry}`
- `packagehub_source_consecutive_failures{ecosystem,registry}`
- `packagehub_source_last_success_timestamp{ecosystem,registry}`
- `packagehub_source_cursor{ecosystem,registry,key}`
- `packagehub_package_events_total{kind}`

## Shutdown

```bash
docker compose down
```

Против стенда никогда не запускайте `docker compose down -v`: это удалит его данные.
