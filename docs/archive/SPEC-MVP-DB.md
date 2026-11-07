# PackageHub MVP — DB Appendix

> Source of truth для схемы данных, миграций и правил записи в PostgreSQL.

**Source of truth:** [SPEC-MVP.md](./SPEC-MVP.md)  
**Версия:** 1.3-mvp-db  
**Дата:** 2026-03-12

## 1. Область и принципы

- Этот документ владеет только DB-контрактом MVP.
- Схема forward-compatible с мульти-экосистемной моделью, но данные в MVP только `pypi`.
- Все операции записи в pipeline обязаны быть идемпотентными.

`Decisions fixed`
- Структура таблиц и upsert-semantics фиксируются здесь.

`Open questions`
- Нет.

## 2. Enum-справочники

### 2.1 Экосистема

- `ecosystem`: `pypi | npm | maven | go | crates | nuget`

### 2.2 Registry

- `registry_type`: `public | corporate | mirror`
- `tier`: `tier1 | tier2 | tier3`

### 2.3 Version/Dependency

- `package_version.status`: `active | yanked | deprecated | unlisted`
- `dependency.dependency_type`: `runtime | dev | optional | build | peer | provided`

### 2.4 Vulnerability/Sync

- `vulnerability.source`: `osv | pypi_native`
- `vulnerability.severity`: `unknown | low | medium | high | critical`
- `sync_state.sync_type`: `full | incremental`
- `sync_state.status`: `idle | running | failed`

`Decisions fixed`
- Enum-наборы не расширяются в рамках MVP без миграции.

`Open questions`
- Нет.

## 3. Таблицы и ключевые поля

Ниже приведены обязательные поля MVP (без исчерпывающего списка технических колонок).

## 3.1 `registry`

- PK: `id (UUID)`
- Уникальные поля: `slug`
- Ключевые поля:
  - `ecosystem`, `name`, `slug`, `registry_type`, `base_url`, `api_url`
  - `auth_required`, `auth_config`, `tier`, `sync_enabled`, `sync_priority`
  - `created_at`, `last_synced_at`

## 3.2 `package`

- PK: `id (UUID)`
- FK: `primary_registry_id -> registry.id`
- Уникальность: `(ecosystem, namespace, normalized_name)`
- Ключевые поля:
  - `ecosystem`, `namespace`, `name`, `normalized_name`
  - `description`, `homepage_url`, `repository_url`, `documentation_url`
  - `license_raw (TEXT)`, `license_spdx`, `keywords`
  - `latest_version`, `downloads_total`, `downloads_recent`
  - `created_at`, `updated_at`, `synced_at`
  - `deprecated`, `deprecation_message`, `deprecation_alternative`
  - `raw_metadata`

## 3.3 `package_version`

- PK: `id (UUID)`
- FK: `package_id -> package.id`
- Уникальность: `(package_id, version)`
- Ключевые поля:
  - `version`, `release_date`, `status`
  - `requires_python`, `download_count`, `features`
  - `needed_in_index`, `raw_metadata`
- Производное поле в `features`:
  - `dependencies_hydrated: bool`

## 3.4 `dependency`

- PK: `id (UUID)`
- FK: `version_id -> package_version.id`
- Nullable FK:
  - `resolved_package_id -> package.id`
  - `resolved_version_id -> package_version.id`
- Ключевые поля:
  - `dependency_name`, `dependency_namespace`, `dependency_ecosystem`
  - `version_constraint`, `dependency_type`, `extra_name`
  - `environment_markers`, `raw_spec`

## 3.5 `artifact`

- PK: `id (UUID)`
- FK:
  - `version_id -> package_version.id`
  - `registry_id -> registry.id`
- Ключевые поля:
  - `filename`, `download_url`, `size_bytes`
  - `checksum_sha256`, `checksum_md5`
  - `artifact_type (wheel|sdist)`, `platform_tags`
  - `requires_dist_metadata`

## 3.6 `package_registry`

- PK/Unique: `(package_id, registry_id)`
- FK:
  - `package_id -> package.id`
  - `registry_id -> registry.id`
- Ключевые поля:
  - `first_seen_at`, `source_url`, `is_primary`

## 3.7 `dist_tag`

- PK: `id (UUID)`
- FK: `package_id -> package.id`
- Unique: `(package_id, tag)`
- Ключевые поля: `tag`, `version`, `updated_at`

## 3.8 `author` + `package_author`

- `author`:
  - PK: `id (UUID)`
  - поля: `name`, `email`, `url`
- `package_author`:
  - FK: `package_id`, `author_id`
  - поле: `role (author|maintainer)`

## 3.9 `vulnerability` + `package_vulnerability`

- `vulnerability`:
  - PK: `id (UUID)`
  - Unique: `source_id`
  - поля: `source`, `aliases`, `summary`, `details`, `severity`, `fixed_in`, `published_at`, `withdrawn_at`, `link`
- `package_vulnerability`:
  - FK: `package_id`, `vulnerability_id`
  - поле: `affected_versions`

## 3.10 `sync_state`

- PK: `id (UUID)`
- FK Unique: `registry_id -> registry.id`
- Ключевые поля:
  - `sync_type`, `status`
  - `last_success_at`, `last_error`
  - `cursor_data`, `packages_synced`, `packages_total`

## 3.11 `sync_failure`

- PK: `id (UUID)`
- Unique: `(operation, package_name)`
- Ключевые поля:
  - `operation`
  - `package_name`
  - `error_class`, `error_message`, `traceback`
  - `retryable`, `attempts`
  - `next_retry_at`, `last_seen_at`

`Decisions fixed`
- Для MVP все бизнес- и operational-сущности выше обязательны и не опционализируются.

`Open questions`
- Нет.

## 4. Ограничения и индексы

Обязательные индексы MVP:

```sql
-- packages
CREATE UNIQUE INDEX idx_pkg_eco_ns_name ON packages(ecosystem, namespace, normalized_name);
CREATE INDEX idx_pkg_name_trgm ON packages USING gin(normalized_name gin_trgm_ops);
CREATE INDEX idx_pkg_registry ON packages(primary_registry_id);
CREATE INDEX idx_pkg_license ON packages(license_spdx) WHERE license_spdx IS NOT NULL;
CREATE INDEX idx_pkg_downloads ON packages(downloads_total DESC NULLS LAST);

-- package_versions
CREATE UNIQUE INDEX idx_ver_pkg_ver ON package_versions(package_id, version);
CREATE INDEX idx_ver_release ON package_versions(release_date);
CREATE INDEX idx_ver_needed ON package_versions(needed_in_index) WHERE needed_in_index = true;

-- dependencies
CREATE INDEX idx_dep_version ON dependencies(version_id);
CREATE INDEX idx_dep_name ON dependencies(dependency_name, dependency_ecosystem);
CREATE INDEX idx_dep_resolved ON dependencies(resolved_version_id) WHERE resolved_version_id IS NOT NULL;

-- artifacts
CREATE INDEX idx_art_version ON artifacts(version_id);
CREATE INDEX idx_art_type ON artifacts(version_id, artifact_type);

-- vulnerabilities
CREATE UNIQUE INDEX idx_vuln_source ON vulnerabilities(source_id);
CREATE INDEX idx_pkg_vuln ON package_vulnerabilities(package_id);

-- sync_failures
CREATE UNIQUE INDEX uq_sync_failures_operation_package ON sync_failures(operation, package_name);
CREATE INDEX idx_sync_failures_retry_due ON sync_failures(retryable, next_retry_at);
CREATE INDEX idx_sync_failures_last_seen ON sync_failures(last_seen_at);
```

`Decisions fixed`
- Индексы из этого блока считаются минимальным обязательным набором MVP.

`Open questions`
- Нет.

## 5. Idempotent upsert правила (pipeline)

Pipeline: `fetch -> normalize -> validate -> license -> upsert_pkg -> upsert_ver -> parse_deps -> upsert_deps -> upsert_arts -> upsert_vuln -> resolve(Level1) -> cache_invalidate -> synced_at`.

## 5.1 Правила upsert по сущностям

- `registry`: upsert по `slug`.
- `package`: upsert по `(ecosystem, namespace, normalized_name)`.
- `package_registry`: upsert по `(package_id, registry_id)`.
- `package_version`: upsert по `(package_id, version)`.
- `dependency`: idempotent replace per `version_id`.
  - Операция: удалить старый срез зависимостей версии и вставить новый.
- `artifact`: idempotent replace per `version_id`.
  - Операция: delete+insert или upsert по `(version_id, filename)`.
- `dist_tag`: upsert по `(package_id, tag)`.
- `author`: upsert по `(name, email)` если email есть, иначе `(name)`.
- `package_author`: upsert по `(package_id, author_id, role)`.
- `vulnerability`: upsert по `source_id`.
- `package_vulnerability`: upsert по `(package_id, vulnerability_id)`.
- `sync_state`: upsert по `registry_id`.
- `sync_failure`: upsert по `(operation, package_name)`.

## 5.2 Транзакционная модель

- Одна транзакция на один package pipeline run.
- При ошибке парсинга пакета транзакция откатывается только для этого пакета.
- Ошибка одного пакета не останавливает batch/seed целиком.

## 5.3 Worker error persistence

- После package-level rollback worker обязан:
  - записать/обновить `sync_failure`;
  - обновить `sync_state.status=failed` с `last_error`;
  - зафиксировать traceback в file log.
- Для `retryable=true` вычисляется `next_retry_at` по exponential backoff с jitter.
- Для `PackageNotFound` и иных permanent failures ставится `retryable=false`.

`Decisions fixed`
- Idempotency достигается повторяемыми upsert/delete+insert операциями на детерминированных ключах.

`Open questions`
- Нет.

## 6. Политика производных полей

## 6.1 `package.latest_version`

- Вычисляется из всех `package_version` как latest stable:
  - исключить `yanked`.
  - исключить prerelease, если есть хотя бы одна stable версия.
- Пересчитывается при каждом upsert версии.

## 6.2 `dependency.resolved_package_id`

- Заполняется на Level 1, если целевой пакет уже есть в БД.
- Иначе `NULL` до lazy fetch/фонового дозаполнения.

## 6.3 `dependency.resolved_version_id`

- Заполняется best-effort: latest stable версия зависимости, удовлетворяющая `version_constraint`.
- Не учитывает транзитивные конфликты.
- При отсутствии кандидата ставится `NULL`.

## 6.4 `package.synced_at`

- Обновляется в конце успешного package pipeline run.
- В batch refresh обновляется даже если контент не изменился.

## 6.5 `package_version.features.dependencies_hydrated`

- Для `latest_version` ставится `true` во время основного package pipeline run.
- Для исторических версий по умолчанию `false`, пока не выполнен lazy hydrate `requires_dist`.
- После успешного `ensure_version_dependencies(...)` значение переводится в `true`.

## 6.6 `sync_state.cursor_data`

- Разрешённые ключи MVP:
  - `last_serial`
  - `last_operation`
  - `last_duration_ms`
- `cursor_data` не используется для changelog-based sync в MVP.

## 6.7 `sync_failure.next_retry_at`

- Вычисляется только для retryable ошибок.
- При превышении `max_attempts` строка остаётся в таблице, но переводится в `retryable=false`.

`Decisions fixed`
- Производные поля Level 1 не заменяют full resolve и используются только для UI/быстрых деревьев.

`Open questions`
- Нет.

## 7. Порядок миграций и инварианты

## 7.1 Порядок миграций

1. Enable extensions (`pg_trgm`, `uuid-ossp`/`pgcrypto` по выбранной стратегии UUID).
2. Create enum types.
3. Create base tables: `registry`, `package`, `package_version`.
4. Create relational tables: `dependency`, `artifact`, `package_registry`, `dist_tag`.
5. Create identity/support tables: `author`, `package_author`, `vulnerability`, `package_vulnerability`, `sync_state`.
6. Create operational table `sync_failures`.
7. Create indexes.
8. Seed row for PyPI registry (`slug=pypi`).

## 7.2 Инварианты после миграций

- В `registry` есть строка `pypi`.
- На уровне схемы гарантированы все уникальные ключи из раздела 4.
- Все FK связности валидны.
- Повторный запуск миграций не меняет состояние (`alembic upgrade head` идемпотентен).

`Decisions fixed`
- Seed registry выполняется миграцией или post-migration step, но обязателен до запуска sync.
- После миграций доступны: `registry.slug=pypi`, `sync_states`, `sync_failures`.

`Open questions`
- Нет.

## 8. Связь с другими документами

- Product intent, SLA, acceptance criteria: [SPEC-MVP.md](./SPEC-MVP.md)
- HTTP contracts и error taxonomy: [SPEC-MVP-API.md](./SPEC-MVP-API.md)
- Delivery slices и required tests: [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md)

`Decisions fixed`
- Любые изменения схемы сначала обновляются здесь, затем в коде/миграциях.

`Open questions`
- Нет.
