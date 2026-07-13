# PackageHub MVP

PyPI-first MVP: каталог метаданных пакетов, demand-driven sync и dependency resolve.

## Prerequisites

- Docker + Docker Compose

## Quick start

1. (Опционально) создать локальный env:

```bash
cp .env.example .env
```

Если сборка контейнера должна ходить в PyPI через приватный forward proxy, переопределите в `.env`:

```bash
PYPI_REPOSITORY_URL=http://your-proxy.example/repository/pypi/simple
```

`PYPI_REPOSITORY_URL` прокидывается в `docker compose` как build arg для backend-образа. Допускается `http://` без TLS: при такой схеме Docker build автоматически добавляет `pip --trusted-host` для хоста proxy.

Если metadata JSON приложение тоже должно забирать через proxy, задайте отдельный runtime-параметр:

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
docker compose run --rm api python -m app.cli fetch left-pad --ecosystem npm
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

## Registries (multi-registry)

Фетчер работает per-registry: у каждого реестра свои протокол, auth, расписание и курсор.
Протокол задаётся полем `protocol` и определяет коннектор:

- `pypi-json` — JSON API pypi.org;
- `pypi-simple` — PEP 503/691 simple index (+PEP 658 metadata): PyTorch index, внутренние Nexus/Artifactory hosted;
- `npm` — протокол npm registry: npmjs.org, Verdaccio, Nexus npm hosted (scoped-имена, dist-tags, deprecated, shasum/integrity).

Из Nexus подключаются **только hosted**-репозитории (group и proxy не регистрируются).

Регистрация внутреннего реестра:

```bash
curl -X POST http://localhost:8000/api/v1/admin/registries \
  -H "Authorization: Bearer phub_admin_dev" -H "Content-Type: application/json" \
  -d '{"ecosystem":"pypi","name":"Nexus PyPI hosted","slug":"nexus-pypi",
       "protocol":"pypi-simple","registry_type":"corporate",
       "base_url":"https://nexus.corp/repository/pypi-hosted/simple/",
       "auth_config":{"type":"basic","username":"svc","password":"..."},
       "enumerate_enabled":true}'
curl -X POST http://localhost:8000/api/v1/admin/registries/nexus-pypi/test \
  -H "Authorization: Bearer phub_admin_dev"
```

- `enumerate_enabled: true` включает полную индексацию (контур enumeration, каждые
  `ENUMERATION_INTERVAL_HOURS`, батч `ENUMERATION_BATCH_LIMIT`, курсор в sync_state).
- Auth-заголовок отправляется только на хост реестра: при редиректе артефакта на CDN
  credentials не передаются.
- Коллизии имён internal↔public и расхождение checksum одинаковых файлов помечаются
  флагами `name_collision` / `checksum_mismatch` на связке пакет↔реестр (сигнал
  dependency confusion).

## Probe дистрибутивов

После sync пакета фоновая задача probe скачивает представительный wheel latest-версии
(предпочтение `none-any`, лимит `PROBE_MAX_WHEEL_BYTES`) и извлекает офлайн-факты:
`entry_points.txt`, `top_level.txt`, состав файлов, скрипты из `.data/scripts/`,
описание из `METADATA` (в `version_descriptions`). Результат — immutable-строка в
`dist_probes` (одна на пару версия+файл); ошибка («no wheel available», превышение
лимита, битый архив) — тоже результат и не ретраится.

```bash
# probe конкретной версии/дистрибутива
curl -X POST http://localhost:8000/api/v1/admin/probe \
  -H "Authorization: Bearer phub_admin_dev" -H "Content-Type: application/json" \
  -d '{"name":"ruff","version":"0.15.21"}'
# сброс результата для re-probe
curl -X POST http://localhost:8000/api/v1/admin/probe/delete \
  -H "Authorization: Bearer phub_admin_dev" -H "Content-Type: application/json" \
  -d '{"name":"ruff","version":"0.15.21"}'
```

Разбор ошибок probe: `SELECT artifact_filename, error FROM dist_probes WHERE error IS NOT NULL;`
сетевые сбои probe ретраятся Celery и после исчерпания попыток видны в `sync_failures`
(operation `probe`). Метрики: `packagehub_probe_*`, `packagehub_enumeration_*`,
`packagehub_fetch_errors_total` — в разрезе реестра.

## Facts API (интеграция с системой проверок прокси)

PackageHub — **каталог фактов**: роли, свойства, статусы, evidence. Вердикты
допуска/блокировки решение не выдаёт — их принимает система проверок
прокси-репозитория на основе фактов.

```bash
# синхронно: fetch + probe + классификация latest, если чего-то нет
curl -H "Authorization: Bearer phub_viewer_dev" \
  "http://localhost:8000/api/v1/facts/pypi/ruff"
# принудительная пересинхронизация метаданных (свежий deprecated / сдвиг latest)
curl -H "Authorization: Bearer phub_viewer_dev" \
  "http://localhost:8000/api/v1/facts/npm/left-pad?refresh=true"
```

Семантика: fail-fast (404 — нет в upstream, 502 — auth реестра, 503 — upstream
недоступен, 504 — таймаут `FACTS_TIMEOUT_SECONDS`); «нет пригодного
дистрибутива» — не ошибка, а факт (классификация по правилам probe-недоступности).

> **⚠ Требует проработки (решение не принято):** режим обновления вердиктов.
> Автоматическая классификация сейчас — только latest-версия каждого пакета.
> Открытые вопросы: ре-классификация при изменении жизненного цикла
> (deprecated/yanked после классификации), пер-версионная классификация
> (кандидаты-политики: новые версии при refresh, версии из resolve/access-log
> прокси, кураторский --all-versions), привязка блока классификации к селектору
> версий в UI (модель deps.dev). Детали: openspec, архив чейнджа
> on-demand-classification (design.md → Open Questions).

## Shutdown

```bash
docker compose down
```
