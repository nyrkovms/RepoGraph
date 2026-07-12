# PackageHub MVP — API Appendix

> Source of truth для контрактов HTTP API MVP.

**Source of truth:** [SPEC-MVP.md](./SPEC-MVP.md)  
**Версия:** 1.3-mvp-api  
**Дата:** 2026-03-12

## 1. Область и правила владения

- Этот документ владеет только HTTP-контрактами.
- Он не меняет продуктовые требования и SLA; SLA берутся только из `SPEC-MVP.md`.
- Поведение endpoint-ов соответствует MVP, без добавления новой функциональности.

`Decisions fixed`
- Все request/response/error контракты описываются только здесь.

`Open questions`
- Нет.

## 2. Общие соглашения

### 2.1 Базовые правила

- Base path: `/api/v1`.
- Формат: `application/json` для request/response, кроме health без тела.
- Экосистема в MVP: только `pypi`.

### 2.2 Аутентификация

- Схема: `Authorization: Bearer <token>`.
- Роли:
  - `viewer`: read-only endpoints.
  - `admin`: read + admin endpoints.

### 2.3 Пагинация

Cursor-based ответ для списков:

```json
{
  "data": [],
  "cursor": "base64-token",
  "has_more": false
}
```

### 2.4 Единый формат ошибок

```json
{
  "error": {
    "code": "validation_error",
    "message": "Invalid request payload",
    "details": {},
    "trace_id": "req-123"
  }
}
```

`error.code` (таксономия MVP):

- `validation_error` → HTTP `422`
- `unauthorized` → HTTP `401`
- `forbidden` → HTTP `403`
- `not_found` → HTTP `404`
- `resolver_conflict` → HTTP `409`
- `upstream_timeout` → HTTP `504`
- `internal_error` → HTTP `500`

### 2.5 Политика lazy fetch

- Для каталожных read-endpoint-ов по конкретному пакету допустим lazy fetch при miss в БД.
- Для `POST /resolve` при сбое lazy fetch действует `fail-fast` с ошибкой (`upstream_timeout` или `not_found`), без partial result.

`Decisions fixed`
- Единый envelope ошибок обязателен для всех endpoint-ов.
- Для resolver конфликты всегда маппятся в `409`.

`Open questions`
- Нет.

## 3. Контракты каталога

## 3.1 `GET /api/v1/packages`

- Auth: `viewer|admin`
- Query:
  - `license: string?`
  - `has_vulnerabilities: boolean?`
  - `updated_after: datetime?`
  - `sort: enum(name|updated_at|downloads_total)?`
  - `limit: int=50 (max 200)`
  - `cursor: string?`
- Response `200`:

```json
{
  "data": [
    {
      "ecosystem": "pypi",
      "name": "flask",
      "description": "...",
      "license_spdx": "BSD-3-Clause",
      "latest_version": "3.0.0",
      "updated_at": "2026-03-01T10:00:00Z",
      "has_vulnerabilities": false
    }
  ],
  "cursor": "...",
  "has_more": true
}
```

- Errors: `401/403/422/500`

## 3.2 `GET /api/v1/packages/pypi/{name}`

- Auth: `viewer|admin`
- Path: `name` (PyPI canonical name rules)
- Behavior: при miss в БД запускает lazy fetch.
- Response `200`:

```json
{
  "ecosystem": "pypi",
  "name": "flask",
  "normalized_name": "flask",
  "description": "...",
  "homepage_url": "https://...",
  "repository_url": "https://...",
  "license_raw": "BSD-3-Clause",
  "license_spdx": "BSD-3-Clause",
  "latest_version": "3.0.0",
  "synced_at": "2026-03-08T09:10:00Z"
}
```

- Errors: `401/403/404/504/500`

## 3.3 `GET /api/v1/packages/pypi/{name}/versions`

- Auth: `viewer|admin`
- Query:
  - `limit: int=50 (max 200)`
  - `cursor: string?`
- Response `200`:

```json
{
  "data": [
    {
      "version": "3.0.0",
      "release_date": "2026-01-11T00:00:00Z",
      "status": "active",
      "requires_python": ">=3.8"
    }
  ],
  "cursor": null,
  "has_more": false
}
```

- Errors: `401/403/404/422/500`

## 3.4 `GET /api/v1/packages/pypi/{name}/versions/{ver}`

- Auth: `viewer|admin`
- Response `200`:

```json
{
  "name": "flask",
  "version": "3.0.0",
  "release_date": "2026-01-11T00:00:00Z",
  "status": "active",
  "requires_python": ">=3.8",
  "features": {"extras": ["async"]}
}
```

- Errors: `401/403/404/500`

## 3.5 `GET /api/v1/packages/pypi/{name}/versions/{ver}/dependencies`

- Auth: `viewer|admin`
- Query:
  - `depth: int? (max 50, optional; default = full tree within server limit)`
  - `include_optional: bool=false`
  - `python_version: string?`
  - `os: string?`
- Semantics:
  - Level 1 дерево на основе `resolved_version_id`;
  - если зависимости выбранной версии ещё не гидратированы, backend выполняет lazy hydrate этой версии;
  - при `include_optional=true` optional/extra зависимости добавляются только для корневой версии, а не рекурсивно по всему графу;
  - response упорядочивает обычные runtime зависимости выше optional/extra.
- Response `200`:

```json
{
  "package": "flask",
  "version": "3.0.0",
  "depth": 50,
  "dependencies": [
    {
      "name": "werkzeug",
      "constraint": ">=3.0.0",
      "dependency_type": "runtime",
      "extra_name": null,
      "environment_markers": null,
      "resolved_version": "3.0.1",
      "cycle": false,
      "children": []
    }
  ]
}
```

- Errors: `401/403/404/422/500`

## 3.6 `GET /api/v1/packages/pypi/{name}/versions/{ver}/artifacts`

- Auth: `viewer|admin`
- Response `200`:

```json
{
  "data": [
    {
      "filename": "flask-3.0.0-py3-none-any.whl",
      "artifact_type": "wheel",
      "download_url": "https://files.pythonhosted.org/...",
      "size_bytes": 102400,
      "checksum_sha256": "...",
      "checksum_md5": "..."
    }
  ]
}
```

- Errors: `401/403/404/500`

## 3.7 `GET /api/v1/packages/pypi/{name}/vulnerabilities`

- Auth: `viewer|admin`
- Response `200`:

```json
{
  "package": "flask",
  "data": [
    {
      "source": "pypi_native",
      "source_id": "PYSEC-2021-9",
      "summary": "...",
      "severity": "high",
      "fixed_in": ["2.0.2"],
      "link": "https://..."
    }
  ]
}
```

- Errors: `401/403/404/500`

`Decisions fixed`
- Контракты каталога описаны только в этом разделе.

`Open questions`
- Нет.

## 4. Контракт resolve

## 4.1 `POST /api/v1/resolve`

- Auth: `viewer|admin`
- Request body:

```json
{
  "ecosystem": "pypi",
  "input_mode": "requirements",
  "dependencies": [
    {"name": "flask", "version_spec": ">=3.0"},
    {"name": "requests", "version_spec": ">=2.28", "extras": ["security"]}
  ],
  "environment": {
    "python_version": "3.12",
    "sys_platform": "linux",
    "os_name": "posix"
  },
  "options": {
    "include_dev": false,
    "max_depth": 50
  }
}
```

- Response `200` (success):

```json
{
  "success": true,
  "input_mode": "requirements",
  "resolved": {
    "flask": {
      "version": "3.0.0",
      "latest_compatible_version": "3.1.3",
      "has_newer_compatible_version": true,
      "dependencies": [
        {
          "name": "werkzeug",
          "version": "3.1.0",
          "constraint": ">=3.1.0",
          "dependency_type": "runtime",
          "extra_name": null,
          "environment_markers": null,
          "dependencies": []
        }
      ]
    }
  },
  "hidden_roots": [],
  "total_packages": 42,
  "total_versions": 42,
  "conflicts": [],
  "resolve_strategy": "resolvelib",
  "resolve_time_ms": 15
}
```

- Response `409` (`resolver_conflict`):

```json
{
  "error": {
    "code": "resolver_conflict",
    "message": "No version of X satisfies all constraints",
    "details": {
      "package": "X",
      "required_by": [
        {"package": "A", "requires": "X>=2.0"},
        {"package": "B", "requires": "X<2.0"}
      ]
    },
    "trace_id": "req-123"
  }
}
```

- Response `404` (`not_found`): запрошенный пакет отсутствует на PyPI.
- Response `504` (`upstream_timeout`): lazy fetch не завершился в допустимое время.
- Response `422/500`: валидация/внутренняя ошибка.

`Decisions fixed`
- Fail-fast обязателен: partial resolve response запрещён.
- `input_mode=freeze` разрешён и означает: вход трактуется как snapshot окружения, а top-level пакеты, уже покрытые транзитивно, скрываются в `hidden_roots`.
- В `input_mode=freeze` root package может возвращать `latest_compatible_version` и `has_newer_compatible_version`, если в БД есть более новая версия, совместимая с выбранными `python_version` и `os`.

`Open questions`
- Нет.

## 5. Admin API

## 5.1 `POST /api/v1/admin/seed`

- Auth: `admin`
- Request body (optional):

```json
{
  "packages": ["flask", "django"]
}
```

- Response `202`:

```json
{
  "task_id": "celery-task-id",
  "status": "queued",
  "operation": "seed"
}
```

- Errors: `401/403/422/500`

## 5.2 `POST /api/v1/admin/fetch/{name}`

- Auth: `admin`
- Behavior: запустить lazy fetch вручную.
- Response `202` with task info.
- Errors: `401/403/404/500`

## 5.3 `POST /api/v1/admin/refresh`

- Auth: `admin`
- Request body (optional):

```json
{
  "limit": 500
}
```

- Response `202` with task info.
- Errors: `401/403/422/500`

## 5.4 `POST /api/v1/admin/retry-failed`

- Auth: `admin`
- Request body (optional):

```json
{
  "limit": 100
}
```

- Response `202` with task info.
- Errors: `401/403/422/500`

## 5.5 `GET /api/v1/admin/sync/status`

- Auth: `admin`
- Response `200`:

```json
{
  "status": "running",
  "sync_type": "incremental",
  "last_success_at": "2026-03-08T08:00:00Z",
  "last_error": null,
  "packages_synced": 1200,
  "packages_total": 3000,
  "cursor_data": {
    "last_serial": null,
    "last_operation": "refresh",
    "last_duration_ms": 342177.92
  }
}
```

- Errors: `401/403/500`

`Decisions fixed`
- Admin endpoints возвращают `202` для долгих операций через Celery.

`Open questions`
- Нет.

## 6. Системные endpoint-ы

## 6.1 `GET /api/v1/health`

- Auth: none
- Response `200`:

```json
{"status": "ok"}
```

## 6.2 `GET /api/v1/stats`

- Auth: `viewer|admin`
- Response `200`:

```json
{
  "ecosystem": "pypi",
  "packages": 3000,
  "versions": 60000,
  "dependencies": 300000,
  "last_sync_at": "2026-03-08T08:00:00Z"
}
```

`Decisions fixed`
- `health` остаётся публичным для orchestration probes.

`Open questions`
- Нет.

## 7. Связь с другими документами

- Product intent, scope, SLA: [SPEC-MVP.md](./SPEC-MVP.md)
- DB schema, upsert semantics, migration order: [SPEC-MVP-DB.md](./SPEC-MVP-DB.md)
- Delivery plan и тестовые срезы: [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md)

`Decisions fixed`
- Любые изменения контрактов API правятся сначала в этом документе.

`Open questions`
- Нет.
