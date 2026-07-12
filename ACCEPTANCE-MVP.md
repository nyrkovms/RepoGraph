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

---

## Прогон 2026-07-12 — fetcher-redesign (мультиреестры + probe)

Контур: локальный `docker compose` (`postgres`, `redis`, `api`, `worker`), миграции
`20260712_0004` (registry.protocol/enumerate_enabled, dist_probes, version_descriptions,
флаги коллизий) и `20260712_0005` (уникальность артефактов per-registry).

- `ruff check app tests` — PASS
- `pytest tests/unit` — **73 passed** (транспорт/auth/CDN-редиректы, pypi-simple коннектор
  PEP 503/691/658, фабрика протоколов, правила слияния и checksum-коллизии, курсор
  enumeration, селектор/распаковка probe, типизированный lazy fetch)
- `pytest tests/integration` — **22 passed**, включая герметичный мультиреестровый
  сценарий: enumeration внутреннего hosted → union версий (`1.0.0` + `1.0.0+internal1`),
  артефакты помнят реестр, флаги `name_collision`/`checksum_mismatch`, primary сохраняется
- `./scripts/acceptance_smoke.sh` — **ACCEPTANCE_SMOKE: PASS** (15 блоков; новые:
  admin registries + test connection, probe latest с ожиданием строки в `dist_probes`,
  probe-метрики)

Исправленные по ходу дефекты инфраструктуры:
- Celery-задачи не регистрировались воркером (`autodiscover_tasks` искал несуществующий
  модуль) — beat-задачи молча дропались; заменено явным `include`.
- Loop-affinity asyncpg-пула в worker (второй таск в форке падал) — NullPool в
  `worker_process_init`.
