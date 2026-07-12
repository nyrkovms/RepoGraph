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

---

## Прогон 2026-07-12 (финальный) — объединённое решение: фетчер + классификатор

Ветка `feature/unified-fetcher-classifier`, миграции 0004–0008.
Этапы: fetcher-redesign → npm-connector → classification-layer →
classification-api → classification-ui → хвосты (retry per-ecosystem,
enumeration ecosystem-aware нормализация). Спеки: openspec package-classifire
(main specs: fetch-metadata, fetch-probe, fetch-reliability, registry-access,
classification; 5 архивных чейнджей).

- `ruff check` — PASS; `pytest tests/unit` — **121 passed**;
  `pytest tests/integration` — **30 passed** (мультиреестры, классификация
  в API, RBAC аннотаций, llm_input); vitest frontend — **17 passed**
- `./scripts/acceptance_smoke.sh` — **ACCEPTANCE_SMOKE: PASS** (16 блоков;
  новые: classification через worker — событийная и батч, фильтр role,
  блок классификации в /stats)
- Живая проверка UI (браузер): карточка flask с ролями кураторского override
  и эффективными свойствами; фильтр Role=dev-tool возвращает eslint (npm),
  Flask и ruff — мультиэкосистемный каталог с классификацией работает
  end-to-end.

Датасет прогона: 8 вердиктов правил 2.4.0 (pypi: flask/httpie/ruff/six/
requests; npm: eslint/left-pad/@babel/helper-plugin-utils), эталоны classifire
воспроизведены (ruff → dev-tool + binary-payload без importable-api).
