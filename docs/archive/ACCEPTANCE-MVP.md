# PackageHub MVP — Acceptance Report

Дата прогона: 2026-03-09  
Контур: локальный `docker compose` (`postgres`, `redis`, `api`, `worker`, `frontend`)

## Подготовка

```bash
docker compose up -d --build
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.cli seed --packages flask django requests
```

## Smoke / Integration checks

```bash
./scripts/acceptance_smoke.sh
```

Результат: `ACCEPTANCE_SMOKE: PASS`

Проверенные блоки:
- health/status endpoints (`/health`, `/stats`)
- catalog/read API (`/packages`, `/search`, package detail + versions + deps + artifacts + vulnerabilities)
- resolver endpoint (`POST /resolve`)
- RBAC (`viewer` denied on admin endpoints, `admin` allowed)
- metrics endpoint (`/metrics` ключевые метрики присутствуют)

## Обнаруженный и исправленный дефект

- Seed падал на реальных данных PyPI из-за ограничения `packages.license_raw VARCHAR(512)`.
- Исправление:
  - модель: `license_raw -> Text` в [`backend/app/models/package.py`](/Users/mikhail/Documents/Work/repograph/backend/app/models/package.py)
  - миграция: [`backend/alembic/versions/20260309_0002_license_raw_text.py`](/Users/mikhail/Documents/Work/repograph/backend/alembic/versions/20260309_0002_license_raw_text.py)

## Итог

- Acceptance smoke проходит после фикса.
- Текущий MVP-контур поднимается одной командой и отрабатывает ключевые сценарии.
