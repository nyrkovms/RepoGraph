# PackageHub MVP

PyPI-first MVP: каталог метаданных пакетов, demand-driven sync и dependency resolve. Экосистемы каталога: PyPI, npm и crates.io.

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
- API stats: `http://localhost:8000/api/v1/stats` (счётчики по экосистемам и источникам)
- API metrics (Prometheus): `http://localhost:8000/api/v1/metrics`

## API: адресация по purl и resolve

Полный контракт: [`SPEC-MVP-API.md`](./SPEC-MVP-API.md).

Пакет адресуется экосистемой и координатами (`/packages/{ecosystem}/{coordinates}/-/…`) или purl; маршруты `/packages/pypi/{name}/…` остаются deprecated-алиасами до конца E7. Фильтры `ecosystem`, `registry`, `origin` есть у `/packages`, `/search` и (кроме `origin`) у `/updates`.

```bash
curl http://localhost:8000/api/v1/packages/pypi/flask/-/                # карточка: purl, origin, sources
curl http://localhost:8000/api/v1/packages/pypi/flask/-/versions/3.1.3  # версия: engines, sources
curl -i http://localhost:8000/api/v1/purl/pkg:pypi/flask                # 307 на канонический путь
```

npm работает так же; scope пакета пишется с `@`, в purl он закодирован как `%40`:

```bash
curl http://localhost:8000/api/v1/packages/npm/@angular/core/-/                                      # карточка: purl, namespace
curl 'http://localhost:8000/api/v1/packages/npm/left-pad/-/versions/1.3.0/dependencies?os=linux'      # дерево зависимостей
curl 'http://localhost:8000/api/v1/search?q=react&ecosystem=npm'                                       # поиск только по npm
```

`POST /resolve` отвечает графом: узлы с purl и источником версии, рёбра, конфликты. Сбой всего графа приходит в поле `error`
ответа `200`. Старый вложенный формат (`dependencies` вместо `roots`) работает как алиас до конца E7.

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"roots": [{"purl": "pkg:pypi/flask", "constraint": ">=3.0"}], "options": {"timeout_s": 30}}'
```

Для npm добавьте `"ecosystem": "npm"` и окружение (`node_version`, `os`, `cpu`); граф строит стратегия `npm-arborist`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "npm", "roots": [{"purl": "pkg:npm/express", "constraint": "^4.18.0"}], "environment": {"node_version": "20.11.0", "os": "linux", "cpu": "x64"}}'
```

crates.io: слаг экосистемы `crates`, тип purl `cargo` (`pkg:cargo/serde_json`), имя крейта пишется как опубликовано, без свёртки
регистра и `-`/`_` (`serde_json`, `Inflector`). Окружение дерева: `target` (целевая тройка Rust, по умолчанию
`x86_64-unknown-linux-gnu`), `features` (через запятую) и `default_features`:

```bash
curl http://localhost:8000/api/v1/packages/crates/serde_json/-/                                      # карточка: purl, ссылки
curl 'http://localhost:8000/api/v1/packages/crates/serde_json/-/versions/1.0.140/dependencies?target=x86_64-pc-windows-msvc&features=std'
curl 'http://localhost:8000/api/v1/search?q=serde&ecosystem=crates'                                  # поиск только по crates.io
```

Для resolve укажите `"ecosystem": "crates"`; features корня идут в `extras`, окружение — `target` (`"*"` — все платформы, как в
`Cargo.lock`), `features`, `default_features`. Граф строит стратегия `cargo-resolver`, один крейт может быть в нескольких версиях
разных классов совместимости (`rand 0.7` и `rand 0.8`); в `nodes[].details.features` лежат features, с которыми крейт попал в граф:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "crates", "roots": [{"purl": "pkg:cargo/serde_json", "constraint": "^1.0", "extras": ["preserve_order"]}], "environment": {"target": "*"}}'
```

Добавить крейт в каталог можно по purl через admin API. Ввод в другом написании приводится к опубликованному имени
(`pkg:cargo/serde-json` сохранится как `serde_json`); остальные маршруты ждут точное имя:

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:cargo/serde_json", "pkg:cargo/tokio"]}'
```

В Web UI crates.io выбирается в переключателе экосистем на странице поиска; на странице крейта окружение дерева — target, features и
default features, а выбранной версии показываются её features.

## Test stand

A long-lived stand whose database survives for the whole project lifecycle. Postgres data lives in
the external volume `packagehub_stand_pg_data`: `docker compose down -v`, image rebuilds and
`scripts/stand.sh down` never remove it. Containers restart automatically, beat runs the daily update.

```bash
scripts/stand.sh up        # first run creates the volume; later runs rebuild and migrate
scripts/stand.sh seed      # SEED_PACKAGES, or: scripts/stand.sh seed flask requests
scripts/stand.sh status
scripts/stand.sh backup    # backups/packagehub_<timestamp>.sql
```

Do not run the plain `docker compose up` alongside the stand: both use ports 5432, 6379, 8000 and 5173.
Removing the data is a deliberate manual step: `docker volume rm packagehub_stand_pg_data`.

## Production baseline

- Production env template: [`.env.prod.example`](/Users/mikhail/Documents/Work/repograph/.env.prod.example)
- Full go-live checklist: [`PRODUCTION-CHECKLIST.md`](/Users/mikhail/Documents/Work/repograph/PRODUCTION-CHECKLIST.md)
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
docker compose run --rm api python -m app.cli daily-update
docker compose run --rm api python -m app.cli presence-probe
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

### Offline fixtures

CI does not use the live internet: `backend-integration-tests` and `acceptance-smoke` run with
`HTTP_REPLAY_DIR=/app/tests/fixtures/http` and read upstream responses recorded in `backend/tests/fixtures/http/`
(a request that was not recorded fails with `ReplayMiss`). To re-record them (needs the live network, uses its own
compose project `packagehub-record`, does not touch the stand):

```bash
scripts/record_fixtures.sh
```

The nightly workflow [`.github/workflows/nightly.yml`](/Users/mikhail/Documents/Work/repograph/.github/workflows/nightly.yml)
runs the same flow against the live sources (02:30 UTC, or manually via `workflow_dispatch`).

## Implemented MVP slices

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
- `packagehub_package_events_total{kind}`

## Shutdown

```bash
docker compose down
```
