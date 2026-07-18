# Proposal: Факты по конкретной версии (version-scoped facts)

> **Статус: PROPOSAL — не реализовано.** Передача для следующей итерации (Opus).
> Автор наброска: Fable 5, 2026-07-13. Контекст — в конце файла.

## Why

Система проверок прокси спрашивает не «что такое `foo`», а «что такое `foo@1.2.3`» — ровно та версия, которую тянет установщик (pip/npm). Эта версия может отсутствовать в каталоге: её никто раньше не запрашивал. Сейчас сервис фактов (`GET /api/v1/facts/{ecosystem}/{name}`) умеет только latest — для проверки конкретной версии этого недостаточно. Дополнительно: при попытке классифицировать не-latest pypi-версию текущий код выдаст **неверный результат** (см. What Changes → BREAKING).

## What Changes

- Сервис фактов принимает версию: `GET /api/v1/facts/{ecosystem}/{name}/{version}` (или `?version=`). Гарантирует факты именно этой версии, синхронно достраивая недостающее: fetch версии → probe версии → классификация версии.
- Новый метод коннектора `fetch_version_metadata(name, version)` → нормализованные метаданные ОДНОЙ версии: pypi — `GET {base}/{name}/{version}/json` (полный `info`+`urls` этой версии); npm — из packument (манифесты всех версий уже там) либо versioned endpoint. Отсутствие версии в upstream → 404 (fail-fast, как в on-demand-classification).
- Строка версии сохраняет манифест, достаточный для классификации: pypi — `info`+`urls` **этой версии** в `package_version.raw_metadata` (сейчас там только `{"files": ...}`); npm — уже полный манифест.
- **BREAKING (исправление скрытого бага):** `classifier.service.build_manifest` для pypi берёт манифест **из строки версии**, а не из `package.raw_metadata` (там всегда latest). Текущее поведение корректно только когда классифицируемая версия == latest; для любой другой версии оно классифицирует чужой манифест. После фикса `build_manifest(package, version)` версионно-корректен для обеих экосистем.
- «Обеспечить версию» в pipeline: если пакета нет — fetch пакета (создаёт все версии); если версия всё же отсутствует (свежий релиз позже последнего sync) — точечный `fetch_version_metadata` создаёт недостающую строку версии + артефакты.

## Capabilities

### Modified Capabilities

- `classification`: сервис фактов и inline-оркестрация распространяются с latest на произвольную версию; фиксируется версионная корректность манифеста классификатора.

## Impact

- Код: `connectors/base|pypi|npm` (fetch_version_metadata), `fetcher/pipeline` (ensure_version_row + сохранение version-scoped manifest), `classifier/service.build_manifest` (версионный источник — **исправление бага**), `classifier/facts` (параметр версии, ensure по версии), `api/v1/facts` (маршрут с версией).
- Схема: миграции не требуются — `classifications`/`dist_probes` уже per-version, `package_version.raw_metadata` уже JSONB.
- Не затрагивается: правила классификатора (1:1 сохраняются), фоновые контуры, UI (отдельный вопрос — привязка блока к селектору версий, см. on-demand-classification Open Questions).

## Контекст для реализатора

Проверено 2026-07-13 (`backend/app/connectors/pypi.py:206`, `classifier/service.py:79-89`):
- pypi `_normalize_versions` кладёт в версию `raw_metadata={"files": files}` — БЕЗ `info`. Значит классификатору нечем классифицировать не-latest pypi-версию.
- `build_manifest` для pypi читает `package.raw_metadata` (latest info+urls). Докстринг честно фиксирует допущение «info соответствует latest» — оно и есть источник бага при версии != latest.
- npm: версия хранит полный манифест — классификация любой версии уже работает из БД.
- probe (`_probe_version_async(version_id, ...)`) и classify (`classify_one_version(db, version, package)`) УЖЕ пер-версионные — трогать не нужно, только скормить правильную версию.
