# PackageHub MVP — API Appendix

> Source of truth для контрактов HTTP API MVP.

**Source of truth:** [SPEC-MVP.md](./SPEC-MVP.md); адресация по purl, источники и формат resolve — [SPEC-MULTI-ECOSYSTEM.md](./SPEC-MULTI-ECOSYSTEM.md) §9.1, §11  
**Версия:** 1.7-api  
**Дата:** 2026-10-30

## 1. Область и правила владения

- Этот документ владеет только HTTP-контрактами.
- Он не меняет продуктовые требования и SLA; SLA берутся только из `SPEC-MVP.md`.
- Описывает API, каким он стал после этапов E0-C, E1, E2 и E3: пакеты адресуются по purl, маршруты `/packages/pypi/{name}/…` остаются deprecated-алиасами до конца E7 (§3.4), resolve отдаёт граф (§4); в каталоге есть PyPI, npm, crates.io и Go.

`Decisions fixed`
- Все request/response/error контракты описываются только здесь.

`Open questions`
- Нет.

## 2. Общие соглашения

### 2.1 Базовые правила

- Base path: `/api/v1`.
- Формат: `application/json` для request/response, кроме health без тела.
- Экосистемы: в каталоге плагины `pypi`, `npm`, `crates` и `go`. Пути и фильтры принимают `ecosystem` (`pypi`, `npm`, `maven`, `go`, `crates`, `nuget`, `rpm`), остальные плагины появятся по планам SPEC-MULTI-ECOSYSTEM; для экосистемы без плагина пакетные маршруты отвечают `404`.
- Пакет в ответах называет его purl (`pkg:pypi/flask`), версия — purl с `@версия` (`pkg:pypi/flask@3.0.0`). Правила записи — [purl-spec](https://github.com/package-url/purl-spec), по ним строит purl плагин экосистемы. Scope npm — namespace purl с `@`, закодированным как `%40`: `@angular/core` — `pkg:npm/%40angular/core`, `left-pad` — `pkg:npm/left-pad`. Тип purl для crates.io — `cargo`, а слаг экосистемы — `crates`: `serde_json` — `pkg:cargo/serde_json`. Имя крейта записано как опубликовано, без свёртки регистра и `-`/`_` (`serde_json`, `Inflector`), namespace нет. Тип purl для Go — `golang`, слаг экосистемы — `go`: путь модуля без последнего элемента — namespace purl, последний элемент — имя, `github.com/gin-gonic/gin` — `pkg:golang/github.com/gin-gonic/gin`, `github.com/redis/go-redis/v9` — `pkg:golang/github.com/redis/go-redis/v9`. Идентичность Go — путь модуля как написан: регистр значим (`github.com/BurntSushi/toml`), суффикс мажорной версии `/v2` и выше делает другой модуль.

### 2.2 Аутентификация

- Read-эндпоинты открыты, токен не нужен. Это каталог (§3), `POST /resolve` (§4), `/registries` и `/profiles` (§5.8), дайджест `/updates` (§6.3, §6.4) и системные эндпоинты (§6).
- Доступ к ним ограничивается на уровне сети: внутренний контур или VPN. Позже перед UI и API встанет OIDC-прокси (SPEC.md §7).
- Admin-эндпоинты (§5, `/admin/*`) требуют заголовок `Authorization: Bearer <token>` с токеном `API_TOKEN_ADMIN`. Если токена нет или он неверный, ответ `401`.
- Роли `viewer` нет. Раньше её токен был вшит в бандл UI, поэтому доступ он не ограничивал, а только требовал общеизвестный заголовок.
- Открытый доступ включает запись: `POST /resolve` и дерево зависимостей делают lazy fetch (§2.5), то есть загружают незнакомые пакеты из источников и сохраняют их в БД (`origin=discovered`). Любой клиент, которому сеть даёт доступ к API, может пополнять каталог.

### 2.3 Пагинация

Cursor-based ответ для списков:

```json
{
  "data": [],
  "cursor": "base64-token",
  "has_more": false
}
```

### 2.4 Формат ошибок

Два формата, в зависимости от endpoint-а.

`POST /resolve` отдаёт envelope:

```json
{
  "error": {
    "code": "resolver_conflict",
    "message": "Dependency conflict",
    "details": {},
    "trace_id": null
  }
}
```

`error.code` для HTTP-ошибок resolve:

- `invalid_request` → HTTP `422` (неизвестная экосистема или профиль, корень не purl этой экосистемы, неверное ограничение версии)
- `resolve_limit_exceeded` → HTTP `422`
- `not_found` → HTTP `404`
- `resolver_conflict` → HTTP `409`
- `upstream_error` → HTTP `502`
- `resolve_timeout` → HTTP `504`
- `internal_error` → HTTP `500`

Остальные endpoint-ы (каталог, `/updates`, `/registries`, admin) отдают стандартный ответ FastAPI: `{"detail": "текст"}` для `401/404/409/422`; при ошибке валидации тела или query `422` с `detail` — списком ошибок pydantic.

`trace_id` в envelope пока всегда `null`.

### 2.5 Политика lazy fetch

- Для каталожных read-endpoint-ов по конкретному пакету допустим lazy fetch при miss в БД.
- Лимит lazy fetch из одного resolve — 500 пакетов, пакеты получают `origin=discovered`.
- Для `POST /resolve` в формате графа сбой всего графа (включая lazy fetch) приходит в поле `error` ответа `200`, узлы в нём могут быть частичными (§4.1). В алиасе с вложенным форматом (§4.2) действует `fail-fast`: HTTP-ошибка без partial result.

`Decisions fixed`
- Envelope обязателен для `POST /resolve`; остальные endpoint-ы отвечают `{"detail": …}`.
- В алиасе с вложенным форматом resolve конфликты маппятся в `409`.

`Open questions`
- Нет.

## 3. Контракты каталога

## 3.1 `GET /api/v1/packages`

- Auth: none
- Query:
  - `ecosystem: enum?` — можно повторять (`ecosystem=pypi&ecosystem=npm`);
  - `registry: string?` — slug источника, в котором есть пакет; неизвестный slug даёт пустую страницу, не ошибку;
  - `origin: enum(seed|manual|dependency|discovered)?` — можно повторять; как пакет попал в каталог;
  - `license: string?`
  - `has_vulnerabilities: boolean?`
  - `updated_after: datetime?`
  - `sort: enum(name|updated_at|downloads_total)=name`
  - `limit: int=50 (max 200)`
  - `cursor: string?`
- Response `200`:

```json
{
  "data": [
    {
      "ecosystem": "pypi",
      "namespace": "",
      "name": "Flask",
      "normalized_name": "flask",
      "purl": "pkg:pypi/flask",
      "origin": "seed",
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

- `namespace` — пустая строка, если у экосистемы нет пространства имён (PyPI, crates, Go, npm-пакет без scope). У npm это scope без `@`, а `name` — имя без scope: `@angular/core` приходит как `namespace: "angular"`, `name: "core"`. `latest_version` может быть `null` (проект без релизов).
- Errors: `422/500`

## 3.2 `GET /api/v1/search`

- Auth: none
- Query: `q: string` (обязателен, подстрока в имени или описании) и те же фильтры, сортировка и пагинация, что у `GET /packages`.
- Response `200`: как у `GET /packages` (`data` — те же элементы с `purl`, `namespace`, `origin`).
- Errors: `422/500`

## 3.3 Адресация пакета: `/packages/{ecosystem}/{coordinates}/-/…`

Пакет адресуется экосистемой и координатами. `{coordinates}` — родное написание экосистемы, может содержать `/` (`@angular/core`, `github.com/gin-gonic/gin`); для PyPI это имя проекта. Разделитель `/-/` отделяет координаты от подресурса, поэтому сам путь заканчивается на `/-/` (со слэшем) для карточки.

```
GET /api/v1/purl/{purl}
GET /api/v1/packages/{ecosystem}/{coordinates}/-/
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/dependencies
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/artifacts
GET /api/v1/packages/{ecosystem}/{coordinates}/-/vulnerabilities
```

Примеры для npm (scope пишется с `@`, `/` между scope и именем остаётся как есть):

```
GET /api/v1/packages/npm/@angular/core/-/
GET /api/v1/packages/npm/left-pad/-/versions
GET /api/v1/packages/npm/@angular/core/-/versions/19.0.0/dependencies?os=linux
GET /api/v1/purl/pkg:npm/%40angular/core          # 307 на /api/v1/packages/npm/@angular/core/-/
```

Примеры для crates.io (имя как опубликовано, регистр и `_` сохраняются):

```
GET /api/v1/packages/crates/serde_json/-/
GET /api/v1/packages/crates/serde_json/-/versions
GET /api/v1/packages/crates/serde_json/-/versions/1.0.140/dependencies?target=x86_64-pc-windows-msvc&features=std&default_features=false
GET /api/v1/purl/pkg:cargo/Inflector              # 307 на /api/v1/packages/crates/Inflector/-/
```

Crates.io отдаёт крейт только по опубликованному имени: `GET /packages/crates/serde-json/-/` не находит `serde_json`. Ввод в другом написании приводится к опубликованному только при добавлении через `POST /admin/packages` (§5.3).

Примеры для Go (путь модуля с его слэшами и регистром, суффикс `/v9` — часть пути):

```
GET /api/v1/packages/go/github.com/gin-gonic/gin/-/
GET /api/v1/packages/go/github.com/BurntSushi/toml/-/versions
GET /api/v1/packages/go/github.com/redis/go-redis/v9/-/versions/v9.7.0/dependencies
GET /api/v1/packages/go/github.com/gin-gonic/gin/-/versions/v2.0.0%2Bincompatible
GET /api/v1/purl/pkg:golang/github.com/redis/go-redis/v9   # 307 на /api/v1/packages/go/github.com/redis/go-redis/v9/-/
```

Go отдаёт модуль только по пути как он написан: `GET /packages/go/github.com/burntsushi/toml/-/` — это другой путь, чем `BurntSushi/toml`, и модуля с ним нет (`404`). Путь без точки в первом элементе (`gin/gin`) не является путём модуля, ответ `404`. Версия пишется с `v` (`v1.10.0`); `+` в пути кодируется как `%2B`. Для Go в Web UI путь страницы — `/package/go/{путь модуля}`.

Общее для всех маршрутов:

- Auth: none.
- Для экосистемы без плагина или неразбираемых координат ответ `404`.
- Если пакета нет в БД, запускается lazy fetch (§2.5); если его нет ни в одном источнике, `404`.
- Профиль источников. `profile` — slug профиля (`GET /profiles`); без параметра действует профиль экосистемы по умолчанию. Неизвестный профиль даёт `404`. Профиль определяет, версии каких источников видны (`versions`, `dependencies`).

### 3.3.1 `GET /api/v1/purl/{purl}`

- Path: `purl` целиком, например `/api/v1/purl/pkg:pypi/flask` или `/api/v1/purl/pkg:pypi/flask@3.0.0`.
- Response `307` с заголовком `Location` — канонический путь: `/api/v1/packages/pypi/flask/-/` для пакета, `/api/v1/packages/pypi/flask/-/versions/3.0.0` для purl с версией. Qualifiers в purl игнорируются.
- Errors: `404` (экосистема purl не поддерживается), `422` (purl не разбирается).

### 3.3.2 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/`

- Response `200`:

```json
{
  "ecosystem": "pypi",
  "name": "Flask",
  "normalized_name": "flask",
  "namespace": "",
  "purl": "pkg:pypi/flask",
  "origin": "seed",
  "description": "...",
  "homepage_url": "https://...",
  "repository_url": "https://...",
  "documentation_url": null,
  "license_raw": "BSD-3-Clause",
  "license_spdx": "BSD-3-Clause",
  "latest_version": "3.0.0",
  "synced_at": "2026-03-08T09:10:00Z",
  "unavailable_at": null,
  "sources": [
    {
      "registry": "pypi",
      "enabled": true,
      "primary": true,
      "first_seen_at": "2026-03-08T09:10:00Z",
      "checked_at": "2026-03-08T09:10:00Z",
      "missing_since": null
    }
  ]
}
```

- `unavailable_at` — когда пакет исчез из всех источников (`null`, пока он где-то есть).
- `sources` — источники, где есть пакет, по приоритету. `primary` — основной источник пакета; `checked_at` — последняя проверка наличия; `missing_since` — с какого времени источник перестал отдавать пакет. `latest_version` может быть `null`.
- Errors: `404/504/500`

### 3.3.3 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions`

- Query:
  - `profile: string?`
  - `limit: int=50 (max 200)`
  - `cursor: string?`
- Semantics: версии, которые публикуют источники профиля, от новых к старым по дате релиза. `status` (`active|yanked|deprecated|unlisted|removed|superseded`) — лучший статус среди видимых источников.
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

- Errors: `404/422/500`

### 3.3.4 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}`

- Response `200`:

```json
{
  "name": "Flask",
  "purl": "pkg:pypi/flask@3.0.0",
  "version": "3.0.0",
  "release_date": "2026-01-11T00:00:00Z",
  "status": "active",
  "requires_python": ">=3.8",
  "engines": {"python": ">=3.8"},
  "features": {"dependencies_hydrated": true},
  "dependencies_state": "hydrated",
  "sources": [
    {
      "registry": "pypi",
      "enabled": true,
      "status": "active",
      "first_seen_at": "2026-03-08T09:10:00Z"
    }
  ]
}
```

- `features` — данные версии, зависящие от экосистемы. У crates это карта features Cargo: имя feature → список того, что она включает (другие features, `dep:крейт`, `крейт/feature`), например `{"default": ["std"], "std": ["serde/std"], "preserve_order": ["dep:indexmap", "std"]}`. Служебный ключ `dependencies_hydrated`, если он есть, features не является: клиент его пропускает.
- Go: версия — SemVer с `v`: релиз (`v1.10.0`), версия модуля до v2 без тега в виде псевдоверсии (`v0.0.0-20261014143641-f0251eb4aa3e`, `v1.2.4-0.20261014143641-f0251eb4aa3e`) или `+incompatible` (`v2.0.0+incompatible`, равна `v2.0.0` при сравнении). Псевдоверсия — обычный prerelease (`is_prerelease`). В каталоге она есть только у модуля без тегов: его `@v/list` пуст, и единственная версия — псевдоверсия из `@latest`, а лента index.golang.org (§5.8) обновляет её. У модуля с тегами `@v/list` псевдоверсий не показывает, и каталог их не хранит; в resolve они встречаются в узлах как требования `go.mod`. В `versions` псевдоверсия не отличается от прочих версий, узнают её по виду. Отозванная (`retract` в go.mod) версия приходит со `status: "yanked"`; текст из комментария к `retract` приходит в `status_reason` версии (в списке версий и в карточке версии), объявление `// Deprecated:` — в `deprecation` карточки пакета (`{"message": ...}`; так же устаревший пакет npm). Описания, лицензии и авторов у Go нет (SPEC-MULTI-ECOSYSTEM Q11): `description`, `license_raw`, `license_spdx`, `homepage_url` — `null`.
- `engines` — требования к среде в нативной записи экосистемы (PyPI: `python`); может быть `null`. `requires_python` — алиас `engines.python`, сохраняется до конца E7.
- `dependencies_state` — состояние зависимостей версии (`pending` — ещё не загружены, `hydrated`, `unavailable` — зависимости неизвестны).
- `sources` — все источники версии (без фильтра по профилю) и статус версии в каждом.
- Errors: `404/500`

### 3.3.5 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/dependencies`

- Query:
  - `profile: string?`
  - `depth: int? (1..200, optional; default = full tree within server limit)`
  - `include_optional: bool=false`
  - `python_version: string?`
  - `os: string? (linux|windows|win32|darwin|mac|macos)`
  - `node_version`, `cpu: string?` (npm)
  - `target: string?`, `features: string?`, `default_features: bool?` (crates)
- Semantics:
  - Level 1 дерево на основе `resolved_version_id`;
  - если зависимости выбранной версии ещё не гидратированы, backend выполняет lazy hydrate этой версии;
  - при `include_optional=true` optional/extra зависимости добавляются только для корневой версии, а не рекурсивно по всему графу;
  - response упорядочивает обычные runtime зависимости выше optional/extra;
  - `python_version` и `os` включают проверку environment markers (PEP 508); без них применяются все зависимости;
  - npm: в дерево входят runtime-зависимости и обязательные peer-зависимости; optional добавляются при `include_optional=true`. Окружение npm — `node_version`, `os`, `cpu` (SPEC-MULTI-ECOSYSTEM §7.3) — маршрут пока принимает только `os` (`linux|darwin|win32`; `windows`, `macos` — синонимы); `python_version` для npm не действует, `node_version` и `cpu` не принимаются. Окружение целиком действует в resolve (§4.1.1).
  - crates: окружение — `target` (целевая тройка Rust; по умолчанию `x86_64-unknown-linux-gnu`; неизвестная даёт `422`, а `*` значит все платформы), `features` (через запятую) и `default_features` (по умолчанию `true`); `python_version` и `os` не действуют. В дерево входят runtime- и build-зависимости, применимые к `target`; у платформенной зависимости в `environment_markers` стоит `{"rust_cfg": "cfg(windows)"}`. Optional-зависимость входит, если её включает feature окружения (`default` включена, пока `default_features` не `false`) или при `include_optional=true`. `dependency_type` узла: `runtime`, `build` или `optional`; dev-зависимости в дерево не входят. Features задают только корень: ниже корня каждое ребро просит у крейта свои features, а `features` и `default_features` на них не действуют.
  - Go: окружения у дерева нет, `python_version`, `os` и остальные параметры не действуют. Каждое ребро — `require` из `go.mod` версии (`dependency_type: "runtime"`), `// indirect` тоже входит: это требования сборки. `name` узла — путь модуля, `constraint` и `resolved_version` — версия из `require`. Пометка `// indirect` приходит в `details.indirect = true` узла (`details` узла — подробности зависимости: у crates features и registry, у npm `optional` peer-зависимости); resolve отдаёт её в `edges[].details.indirect` (§4.1.3). Дерево показывает версии, которые требует `go.mod` как есть; выбор Minimal Version Selection делает только resolve. `replace` и `exclude` зависимостей не применяются (Go учитывает их только в главном модуле).
- Response `200`:

```json
{
  "package": "flask",
  "version": "3.0.0",
  "profile": "default",
  "depth": 1,
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

- `package` — нормализованное имя, `profile` — slug применённого профиля, `depth` — фактическая глубина возвращённого дерева.
- Errors: `404/422/500`

### 3.3.6 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/artifacts`

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
      "checksum_md5": "...",
      "registry": "pypi"
    }
  ]
}
```

- `registry` — slug источника, из которого взят файл. Один файл из двух источников приходит двумя элементами.
- Errors: `404/500`

### 3.3.7 `GET /api/v1/packages/{ecosystem}/{coordinates}/-/vulnerabilities`

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

- Errors: `404/500`

## 3.4 Алиасы PyPI (deprecated до конца E7)

Прежние маршруты остаются, помечены `deprecated` в OpenAPI и отвечают так же, как новые для `ecosystem=pypi`, с профилем по умолчанию (параметра `profile` у них нет):

| Алиас | Новый маршрут |
|---|---|
| `GET /packages/pypi/{name}` | `GET /packages/pypi/{name}/-/` |
| `GET /packages/pypi/{name}/versions` | `…/-/versions` |
| `GET /packages/pypi/{name}/versions/{ver}` | `…/-/versions/{ver}` |
| `GET /packages/pypi/{name}/versions/{ver}/dependencies` | `…/-/versions/{ver}/dependencies` |
| `GET /packages/pypi/{name}/versions/{ver}/artifacts` | `…/-/versions/{ver}/artifacts` |
| `GET /packages/pypi/{name}/vulnerabilities` | `…/-/vulnerabilities` |

`Decisions fixed`
- Контракты каталога описаны только в этом разделе.
- Новые клиенты ходят по `/-/` или по `/purl/{purl}`; алиасы убираются, когда UI переедет (конец E7).

`Open questions`
- Нет.

## 4. Контракт resolve

## 4.1 `POST /api/v1/resolve`: формат графа

- Auth: none
- Формат выбирается по телу: есть `roots` — граф (этот раздел); есть `dependencies` — вложенный формат MVP (§4.2).
- Request body:

```json
{
  "ecosystem": "pypi",
  "profile": null,
  "input_mode": "requirements",
  "roots": [
    {"purl": "pkg:pypi/flask", "constraint": ">=3.0"},
    {"purl": "pkg:pypi/requests", "constraint": ">=2.28", "extras": ["security"]}
  ],
  "environment": {
    "python_version": "3.12",
    "sys_platform": "linux",
    "os_name": "posix"
  },
  "options": {
    "include_dev": false,
    "include_optional": false,
    "timeout_s": 30
  }
}
```

| Поле | Значение |
|---|---|
| `ecosystem` | по умолчанию `pypi` |
| `profile` | slug профиля источников; без него профиль экосистемы по умолчанию |
| `input_mode` | `requirements` (по умолчанию) или `lock`; `freeze` — алиас `lock` |
| `roots` | 1..500 корней: `purl`, `constraint` (по умолчанию `*`, синтаксис экосистемы), `extras`. Версия в purl без `constraint` закрепляет её (`==версия`) |
| `environment` | окружение в терминах экосистемы; PyPI: имена PEP 508 (`python_version`, `sys_platform`, `os_name`, `platform_machine`, `platform_system`, `implementation_name`) или короткая форма `python_version` + `os`. npm: `node_version`, `os`, `cpu` (§4.1.1). crates: `target`, `features`, `default_features` (§4.1.2). Go: `go_version`, `replace`, `exclude` (§4.1.3). Пустое окружение — как у pip по умолчанию (Python 3.12, linux) |
| `options.include_dev` / `include_optional` | включать dev- и optional-зависимости; для PyPI `include_optional` не действует: extras задаются в корне явно |
| `options.timeout_s` | срок resolve, `0 < timeout_s <= 120`, по умолчанию 30 |
| `managed` | корни `dependencyManagement` (Maven); для PyPI не используется |

- В `input_mode=lock` корни, которых достигают зависимости других корней, приходят с `hidden: true` (вход трактуется как snapshot окружения).
- Response `200`:

```json
{
  "ecosystem": "pypi",
  "profile": "default",
  "input_mode": "requirements",
  "strategy": "pip-resolvelib",
  "emulates": "pip 24 (resolvelib)",
  "nodes": [
    {
      "id": "n1",
      "purl": "pkg:pypi/flask@3.0.0",
      "name": "flask",
      "version": "3.0.0",
      "registry": "pypi",
      "depth": 1,
      "root": true,
      "hidden": false,
      "latest_compatible": "3.1.3",
      "errors": []
    },
    {
      "id": "n2",
      "purl": "pkg:pypi/werkzeug@3.1.0",
      "name": "werkzeug",
      "version": "3.1.0",
      "registry": "pypi",
      "depth": 2,
      "root": false,
      "hidden": false,
      "latest_compatible": null,
      "errors": []
    }
  ],
  "edges": [
    {"from": "root", "to": "n1", "constraint": ">=3.0", "type": "runtime", "details": {}},
    {"from": "n1", "to": "n2", "constraint": ">=3.1.0", "type": "runtime", "details": {}}
  ],
  "conflicts": [],
  "warnings": [],
  "fetched": [],
  "details": {},
  "error": null,
  "resolved_at": "2026-10-29T08:00:00Z",
  "catalog_revision": "2026-10-29T07:48:18Z",
  "resolve_time_ms": 50.7
}
```

| Поле | Значение |
|---|---|
| `input_mode` | `requirements` или `lock` (`freeze` в запросе приходит как `lock`) |
| `strategy`, `emulates` | алгоритм и нативный инструмент, поведению которого он следует |
| `nodes[]` | узел на пакет; `id` — идентификатор в графе, `registry` — источник выбранной версии, `depth` — кратчайшее расстояние от корней, `root` — запрошенный корень, `hidden` — корень, достижимый из другого корня (только `lock`), `latest_compatible` — новейшая подходящая версия окружения без учёта ограничения (только у корней), `errors` — проблемы узла |
| `edges[]` | зависимость; `from` — `id` узла или `root`, `to` — `id` узла, `type` — `runtime|dev|optional|build|peer|provided`, `details` — PyPI: `extra_name`, `environment_markers`, `extras` |
| `conflicts[]` | причины конфликта: `{requirement, parent}` |
| `warnings[]` | `{code, node, message}`: `dependencies_unknown` (зависимости версии неизвестны), `dependency_not_found` (ни один источник профиля не имеет зависимости) |
| `fetched[]` | purl пакетов, подтянутых lazy fetch во время resolve |
| `details` | дополнительные сведения экосистемы (RPM — активные потоки модулей); для PyPI пусто |
| `error` | сбой всего графа или `null` |
| `resolved_at`, `catalog_revision`, `resolve_time_ms` | время ответа, версия каталога, по которой он построен, время resolve |

- `errors` узла: `"dependencies unknown"` (зависимости версии неизвестны, `dependencies_state=unavailable`), `"no source has <имя>"` (зависимость не найдена ни в одном источнике профиля). Остальной граф при этом строится.
- Сбой всего графа приходит с HTTP `200` и объектом `error`; `nodes` и `edges` в этом случае могут быть пустыми или частичными:

```json
{"code": "resolver_conflict", "message": "Dependency conflict"}
```

| `error.code` | Когда | Дополнительные поля |
|---|---|---|
| `resolver_conflict` | нет набора версий, удовлетворяющего всем ограничениям; число раундов превышено | причины в `conflicts` |
| `not_found` | корень не найден ни в одном источнике профиля | `packages` |
| `resolve_limit_exceeded` | больше узлов (2000) или lazy fetch (500), чем разрешено | `limit` (`nodes|lazy_fetches`), `value`, `packages_seen`, `lazy_fetches` |
| `resolve_timeout` | не уложился в `options.timeout_s` | `packages_seen` |
| `upstream_error` | источник или БД не ответили при lazy fetch | `packages_seen` |

- Ответ без `error` кэшируется по запросу и профилю; кэш сбрасывается, если изменились пакеты, которые он читал.
- Response `422` (`invalid_request`): неизвестные экосистема или профиль, корень не purl этой экосистемы, неверное ограничение. Ошибки формы тела (нет `roots`, `timeout_s` вне границ) приходят в стандартном виде FastAPI (§2.4).
- Response `500` (`internal_error`).

### 4.1.1 Resolve для npm

Тело и ответ те же, что в §4.1; отличается следующее.

- Запрос: `"ecosystem": "npm"`, корни — purl npm.

```json
{
  "ecosystem": "npm",
  "roots": [
    {"purl": "pkg:npm/express", "constraint": "^4.18.0"},
    {"purl": "pkg:npm/%40angular/core", "constraint": "^19.0.0"}
  ],
  "environment": {"node_version": "20.11.0", "os": "linux", "cpu": "x64"}
}
```

- `environment`: `node_version` (синоним `node`), `os` (`platform`), `cpu` (`arch`). Синонимы значений: `windows`→`win32`, `macos`→`darwin`, `x86_64`/`amd64`→`x64`, `aarch64`→`arm64`. Пропущенные ключи — Node.js 20.0.0, linux, x64; пустое окружение — то же самое. Версия, чьи `os`/`cpu` не подходят окружению, не ставится как optional-зависимость; как обычная зависимость она остаётся в графе с ошибкой узла.
- `strategy`: `npm-arborist`, `emulates`: `npm 10 (arborist)`. Граф повторяет дерево npm 10: дедупликация и подъём (hoisting) пакетов, выбор версии как в npm-pick-manifest (`latest`, если подходит; иначе лучшая из подходящих, не deprecated, совместимая с Node.js), peer-зависимости ставятся рядом с запросившим их пакетом.
- `nodes[].name` — имя пакета со scope (`@angular/core`); `edges[].type` — `runtime|optional|peer`; `details` ребра — `alias` для зависимостей вида `npm:`, `optional` для peer.
- `options.include_dev` не действует: devDependencies не ставятся. `include_optional` не действует: optional-зависимости ставятся, как у `npm install`.
- `conflicts[]` для npm: `{kind, package, constraint, required_by}`; `kind` — `unsatisfiable` (нет версии под ограничение) или `peer` (peer-зависимость расходится с уже стоящей версией, дополнительно `installed`).
- `warnings[]`: к `dependencies_unknown` и `dependency_not_found` добавляется `dependency_unresolvable` (`{code, node, package, spec}`): git-, file- и URL-зависимости нельзя разрешить по реестру, они отмечаются на узле, который их объявляет.

```json
{
  "ecosystem": "npm",
  "profile": "default",
  "strategy": "npm-arborist",
  "emulates": "npm 10 (arborist)",
  "nodes": [
    {"id": "n1", "purl": "pkg:npm/express@4.21.2", "name": "express", "version": "4.21.2", "registry": "npmjs",
     "depth": 1, "root": true, "hidden": false, "latest_compatible": "4.21.2", "errors": []}
  ]
}
```

### 4.1.2 Resolve для crates

Тело и ответ те же, что в §4.1; отличается следующее.

- Запрос: `"ecosystem": "crates"`, корни — purl `cargo`. `constraint` — требование Cargo (`^1.0`, `~0.8.5`, `>=1, <2`, `=1.0.140`, `*`; голая версия значит `^`). Features корня — в `extras`.

```json
{
  "ecosystem": "crates",
  "roots": [
    {"purl": "pkg:cargo/serde_json", "constraint": "^1.0", "extras": ["preserve_order"]},
    {"purl": "pkg:cargo/rand", "constraint": "^0.8"}
  ],
  "environment": {"target": "x86_64-unknown-linux-gnu", "features": ["std"], "default_features": true}
}
```

- `environment`: `target` (тройка из списка поддерживаемых, `*` — все платформы, как в `Cargo.lock`), `features` (список или строка через запятую, добавляются к `extras` корней) и `default_features` (по умолчанию `true`). Пропущенные ключи: `x86_64-unknown-linux-gnu`, без features, с default features; пустое окружение — то же самое. Неизвестная `target` даёт `422`.
- Версии выбираются для всех платформ, как в `Cargo.lock`; `target` отсекает из графа узлы, недоступные на платформе, как `cargo tree --target`. При `target: "*"` в графе все платформы, как в `Cargo.lock`. В этом режиме слабые features (`x?/feat`) тоже включают свою зависимость.
- `strategy`: `cargo-resolver`, `emulates`: `Cargo 1.86 (resolver v2)`. Идентичность узла — крейт и класс совместимости версии (`1.x`, `0.3.x`, `0.0.3`), поэтому крейт может встретиться в графе несколькими версиями (`rand 0.7.3` и `rand 0.8.5`). yanked-версии не выбираются никогда, даже для `=`-ограничения (как в Cargo).
- `nodes[].name` — имя крейта как опубликовано; `nodes[].details.features` — отсортированный список features, с которыми крейт попал в граф (объединение того, что просят все зависящие от него узлы, и `default`, если она включена).
- `edges[].type` — `runtime|build|optional`; `details.rust_cfg` ребра — условие платформы (`cfg(windows)`, `cfg(target_os = "linux")` или тройка), если зависимость платформенная. `options.include_dev` добавляет dev-зависимости только у корней; `include_optional` не действует: optional-зависимости приходят вместе с включающими их features.
- `conflicts[]` пуст: неудача resolve приходит в `error` с `code` `resolution_impossible` (нет версий под все требования к крейту), `not_found` (источники профиля не знают корня), `resolve_too_complex`, `resolve_timeout`, `resolve_limit_exceeded` или `upstream_error`.
- `warnings[]`: `dependencies_unknown` (зависимости версии неизвестны) и `unresolvable` (`{code, message}`, зависимость корня разрешить не удалось).

```json
{
  "ecosystem": "crates",
  "profile": "default",
  "strategy": "cargo-resolver",
  "emulates": "Cargo 1.86 (resolver v2)",
  "nodes": [
    {"id": "n1", "purl": "pkg:cargo/serde_json@1.0.140", "name": "serde_json", "version": "1.0.140", "registry": "crates-io",
     "depth": 1, "root": true, "hidden": false, "latest_compatible": "1.0.140", "errors": [],
     "details": {"features": ["default", "preserve_order", "std"]}}
  ]
}
```

### 4.1.3 Resolve для Go

Тело и ответ те же, что в §4.1; отличается следующее.

- Запрос: `"ecosystem": "go"`, корни — purl `golang`. `constraint` корня — версия, с которой стартует MVS (`v1.9.1`): корень требуется в этой версии, и итог может быть выше. `*` или отсутствие `constraint` берёт последний релиз профиля, а у модуля без релизов — последнюю псевдоверсию, как `go get путь@latest`. Диапазонов у Go нет.

```json
{
  "ecosystem": "go",
  "roots": [
    {"purl": "pkg:golang/github.com/gin-gonic/gin", "constraint": "v1.9.1"},
    {"purl": "pkg:golang/github.com/redis/go-redis/v9", "constraint": "*"}
  ],
  "environment": {
    "go_version": "1.23",
    "replace": [{"old": "golang.org/x/net@v0.20.0", "new": "golang.org/x/net@v0.21.0"}],
    "exclude": ["golang.org/x/sys@v0.17.0"]
  }
}
```

- `environment`: `go_version` (по умолчанию `1.23`, префикс `go` допускается), `replace` и `exclude` главного модуля. `replace` — список `{"old": "путь[@версия]", "new": "путь@версия"}`: `old` без версии заменяет все версии модуля, с версией — только её; замена только на модуль с версией (замена на каталог из прокси не разрешается). `exclude` — список `"путь@версия"`: исключённое требование заменяется ближайшей большей непсевдо-версией, которую знает прокси, как это делает Go. `replace` и `exclude` из `go.mod` зависимостей, как и в Go, не действуют. Неверная запись (`replace` без `new`, версия не SemVer с `v`) даёт `422`.
- `strategy`: `go-mvs`, `emulates`: `Go 1.23 (MVS with module graph pruning)`. Граф читается, как в Go 1.17+ (SPEC-MULTI-ECOSYSTEM §9.5, Q23): главный модуль считается на `go 1.23`, поэтому `go.mod` каждого корня читается, а у модуля с `go >= 1.17` в графе только его непосредственные требования, их `go.mod` дальше не читаются; ниже модуля на `go < 1.17` (и модуля без строки `go`) граф полный. Для каждого пути берётся наибольшая из требуемых версий среди прочитанных `go.mod` и корней, даже если требующий модуль сам не попал в итог.
- Что значит равенство с `go list -m all`: множество пар `(путь модуля, версия)` в `nodes` равно списку, который печатает `go list -m all` для главного модуля, требующего корни, без самого главного модуля. Цель — 100% совпадений без допустимых расхождений (SPEC-MULTI-ECOSYSTEM §9.5, Q23). `go.mod` читаются как `.mod` документы и в каталог не попадают: в каталог входят только модули итога (и корни).
- `nodes[].name` — путь модуля, `version` — выбранная версия (может быть псевдоверсией или `+incompatible`). `details.replaced_by` узла — `"путь@версия"` модуля, чей `go.mod` и содержимое заменяют выбранный (если сработал `replace`). `latest_compatible` у корней — последний релиз модуля, у прочих узлов `null`.
- `edges[]`: из `root` в каждый корень (`constraint` — выбранная версия корня) и между узлами итога по `require` их `go.mod` (`constraint` — требуемая версия, не обязательно выбранная; `type` — `runtime`); `details.indirect = true` у требований `// indirect`.
- `warnings[]`: `go_version` (`{code, node, message}`) — модулю нужен `go` новее, чем `environment.go_version` (строка `go` его `go.mod`); к `dependencies_unknown` у Go не относится. Модуль без `go.mod` в прокси или с нечитаемым `go.mod` остаётся в итоге, ошибка записывается в `nodes[].errors`.
- `conflicts[]` пуст: MVS конфликтов не имеет. Неудача resolve приходит в `error` с `code` `not_found` (прокси профиля не знают корня, `packages` — какие), `resolve_limit_exceeded`, `resolve_timeout` или `upstream_error`.

```json
{
  "ecosystem": "go",
  "profile": "default",
  "strategy": "go-mvs",
  "emulates": "Go 1.23 (MVS with module graph pruning)",
  "nodes": [
    {"id": "n1", "purl": "pkg:golang/github.com/gin-gonic/gin@v1.9.1", "name": "github.com/gin-gonic/gin", "version": "v1.9.1",
     "registry": "proxy-golang-org", "depth": 1, "root": true, "hidden": false, "latest_compatible": "v1.10.0", "errors": [],
     "details": {}},
    {"id": "n2", "purl": "pkg:golang/golang.org/x/net@v0.21.0", "name": "golang.org/x/net", "version": "v0.20.0",
     "registry": "proxy-golang-org", "depth": 2, "root": false, "hidden": false, "latest_compatible": null, "errors": [],
     "details": {"replaced_by": "golang.org/x/net@v0.21.0"}}
  ],
  "edges": [
    {"source": "root", "target": "n1", "constraint": "v1.9.1", "type": "runtime", "details": {}},
    {"source": "n1", "target": "n2", "constraint": "v0.20.0", "type": "runtime", "details": {"indirect": true}}
  ],
  "warnings": [
    {"code": "go_version", "node": "n2", "message": "golang.org/x/net v0.20.0 needs go 1.24"}
  ]
}
```

## 4.2 `POST /api/v1/resolve`: вложенный формат (алиас до конца E7)

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

- `ecosystem` — только `pypi`; `input_mode` — `requirements` или `freeze`; `options.max_depth` 1..200 (по умолчанию 50), `options.max_rounds` 20..2000 (принимается, на результат не влияет). Профиль — по умолчанию.
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

- Ответ строится из того же графа. Ошибки — HTTP-коды в envelope (§2.4), без partial result:
  - `404` (`not_found`): запрошенный пакет или его зависимость отсутствует в источниках (`details.package`);
  - `409` (`resolver_conflict`): конфликт ограничений (`details.causes` — `{requirement, parent}`);
  - `422` (`invalid_request`, `resolve_limit_exceeded`): неверный запрос, превышен лимит;
  - `502` (`upstream_error`): источник или БД не ответили при lazy fetch;
  - `504` (`resolve_timeout`): resolve не завершился в срок;
  - `422` (`invalid_request`): неподдерживаемые `ecosystem` или `input_mode`.
  - `500` (`internal_error`): прочие сбои.

`Decisions fixed`
- В формате графа сбой всего графа — поле `error` в ответе `200`; во вложенном формате — HTTP-ошибка, partial resolve response запрещён.
- `input_mode=freeze` разрешён и означает: вход трактуется как snapshot окружения, а top-level пакеты, уже покрытые транзитивно, скрываются (`hidden` в графе, `hidden_roots` во вложенном формате). В графе `freeze` приходит как `lock`.
- Корень может вернуть `latest_compatible` (`latest_compatible_version` и `has_newer_compatible_version` во вложенном формате), если в БД есть более новая версия, совместимая с окружением.

`Open questions`
- Нет.

## 5. Admin API

Все `/admin/*` требуют `Authorization: Bearer <token>` (§2.2), без него `401`. Долгие операции уходят в Celery и отвечают `202` с описанием задачи:

```json
{
  "task_id": "celery-task-id",
  "status": "queued",
  "operation": "seed"
}
```

## 5.1 `POST /api/v1/admin/seed`

- Auth: `admin`
- Request body (optional): `packages` — имена PyPI или purl; без списка берётся `SEED_PACKAGES`.

```json
{
  "packages": ["flask", "pkg:pypi/requests"]
}
```

- Response `202`, `operation: "seed"`.
- Errors: `401/422/500`

## 5.2 `POST /api/v1/admin/fetch/{target}`

- Auth: `admin`
- Path: `target` — purl (`pkg:pypi/flask`; слэши purl входят в путь) или голое имя проекта PyPI (`flask`).
- Behavior: запустить lazy fetch вручную, вместе с зависимостями.
- Response `202`, `operation: "fetch"`.
- Errors: `401/422` (`target` не разбирается)`/500`

## 5.3 `POST /api/v1/admin/packages`

- Auth: `admin`
- Behavior: загрузить пакеты по purl (до 500 за вызов). Если хоть один purl неверен, не запускается ничего.
- Request body:

```json
{
  "purls": ["pkg:pypi/flask", "pkg:pypi/requests"]
}
```

- Response `202`, `operation: "fetch_packages"`.
- Errors: `401/422/500`

Для crates purl записывается с типом `cargo`: `pkg:cargo/serde_json`. Если крейта с таким именем нет, а источник знает его под другим написанием (регистр, `-` вместо `_`), пакет сохраняется под опубликованным именем: `pkg:cargo/serde-json` добавляет `serde_json`. Остальные способы обращения к крейту требуют точного имени.

Для Go purl записывается с типом `golang`: `pkg:golang/github.com/gin-gonic/gin`, `pkg:golang/github.com/redis/go-redis/v9`. Путь модуля берётся как написан, регистр значим (`pkg:golang/github.com/BurntSushi/toml`); purl без namespace (`pkg:golang/gin`) неверен, и тогда не запускается ничего.

## 5.4 `POST /api/v1/admin/retry-failed`

- Auth: `admin`
- Request body (optional):

```json
{
  "limit": 100
}
```

- `limit`: 1..5000, по умолчанию 100.
- Response `202`, `operation: "retry_failed"`.
- Errors: `401/422/500`

## 5.5 `POST /api/v1/admin/daily-update`

- Auth: `admin`
- Behavior: ежедневное обновление вручную для всех экосистем (SPEC-DAILY-UPDATE, SPEC-MULTI-ECOSYSTEM §8). Тело не нужно. Прогон пишется в журнал `runs`.
- Response `202`, `operation: "daily_update"`.
- Errors: `401/500`

## 5.6 `POST /api/v1/admin/presence-probe`

- Auth: `admin`
- Behavior: найти пакеты каталога, которые появились в источнике профиля по умолчанию позже, чем мы их увидели (ежедневное обновление следит только за источниками, где пакет уже известен). По расписанию запускается раз в неделю.
- Request body (optional):

```json
{
  "ecosystem": "pypi"
}
```

- Response `202`, `operation: "presence_probe"`.
- Errors: `401/422` (нет плагина экосистемы)`/500`

Для npm: `{"ecosystem": "npm"}`, для crates: `{"ecosystem": "crates"}`, для Go: `{"ecosystem": "go"}`. Проверка идёт по источникам профиля экосистемы по умолчанию (стартовые — `npmjs`, `crates-io` и `proxy-golang-org`, §5.8). Без тела проверяется PyPI.

## 5.7 `GET /api/v1/admin/sync/status`

- Auth: `admin`
- Response `200`:

```json
{
  "status": "idle",
  "sync_type": "incremental",
  "last_success_at": "2026-03-08T08:00:00Z",
  "last_error": null,
  "packages_synced": 1200,
  "packages_total": 3000,
  "cursor_data": {},
  "last_update_run": {
    "started_at": "2026-03-08T06:00:00Z",
    "finished_at": "2026-03-08T06:04:00Z",
    "status": "success",
    "packages_checked": 3000,
    "packages_changed": 12,
    "packages_updated": 12,
    "packages_failed": 0,
    "packages_unavailable": 0,
    "events_created": 15
  },
  "recent_runs": [
    {
      "operation": "daily_update",
      "ecosystem": "pypi",
      "status": "success",
      "started_at": "2026-03-08T06:00:00Z",
      "finished_at": "2026-03-08T06:04:00Z",
      "packages_total": 3000,
      "packages_checked": 3000,
      "packages_changed": 12,
      "packages_updated": 12,
      "packages_failed": 0,
      "packages_unavailable": 0,
      "events_created": 15,
      "error": null
    }
  ],
  "registries": [
    {
      "slug": "pypi",
      "ecosystem": "pypi",
      "enabled": true,
      "last_success_at": "2026-03-08T08:00:00Z",
      "last_error": null,
      "consecutive_failures": 0,
      "degraded_until": null,
      "cursor_data": {}
    }
  ]
}
```

- Сводные ключи `status`, `sync_type`, `packages_synced`, `packages_total`, `last_error` берутся из последней записи журнала `runs` любой операции: `status` — `running` или `failed`, если такой статус у прогона, иначе `idle`; `sync_type` — `full` для `seed`, иначе `incremental`; `packages_synced` — `packages_checked` прогона. Без прогонов: `idle`, `incremental`, нули.
- `last_success_at` и `cursor_data` — по источнику PyPI.
- `last_update_run` — последнее ежедневное обновление PyPI (`null`, если не было).
- `recent_runs` — до 20 последних прогонов журнала `runs`, новые первыми; `ecosystem` бывает `null` (прогон не по одной экосистеме, например `seed`).
- `registries` — состояние каждого источника: последний успех и ошибка, подряд неудачных обращений, до какого времени источник считается деградировавшим, курсор детектора изменений.
- Errors: `401/500`

## 5.8 Источники и профили

Чтение открыто (Auth: none), изменения — `admin`. Детали: SPEC-MULTI-ECOSYSTEM §6, §11.2.

### Чтение

- `GET /api/v1/registries?ecosystem=` — список источников.
- `GET /api/v1/registries/{slug}` — один источник; `404`, если нет.
- `GET /api/v1/profiles?ecosystem=` — профили источников.

```json
{
  "slug": "pypi",
  "name": "PyPI",
  "ecosystem": "pypi",
  "protocol": "pypi-warehouse",
  "base_url": "https://pypi.org/",
  "priority": 0,
  "enabled": true,
  "routing": null,
  "config": {"api_url": "https://pypi.org/pypi/"},
  "auth_type": "none",
  "credentials_ref": null,
  "state": {
    "status": "idle",
    "last_success_at": "2026-03-08T08:00:00Z",
    "last_error": null,
    "consecutive_failures": 0,
    "degraded_until": null,
    "degraded": false,
    "cursor_data": {}
  },
  "packages": 3000
}
```

Стартовый источник npm — `npmjs` (миграция этапа E1), он входит в профиль `default` экосистемы npm:

```json
{
  "slug": "npmjs",
  "name": "npm registry",
  "ecosystem": "npm",
  "protocol": "npm-registry",
  "base_url": "https://registry.npmjs.org/",
  "priority": 0,
  "enabled": true,
  "config": {"changes_url": "https://replicate.npmjs.com/registry/"}
}
```

Стартовый источник crates — `crates-io` (миграция этапа E2), он входит в профиль `default` экосистемы crates. `base_url` — sparse-индекс, адрес API crates.io лежит в `config.api_url` (описание, ссылки и лицензию оттуда в фоне дозаполняет задача `crates_api` раз в минуту, SPEC Q22; у только что добавленного крейта они появляются через минуты):

```json
{
  "slug": "crates-io",
  "name": "crates.io",
  "ecosystem": "crates",
  "protocol": "cargo-sparse",
  "base_url": "https://index.crates.io/",
  "priority": 0,
  "enabled": true,
  "config": {"api_url": "https://crates.io"}
}
```

Стартовый источник Go — `proxy-golang-org` (миграция этапа E3), он входит в профиль `default` экосистемы go. `base_url` — адрес прокси по протоколу GOPROXY, в `config.index_url` лежит лента index.golang.org, по которой ежедневное обновление находит новые версии модулей (`?since=…`, до 2000 строк за запрос, строка — модуль, версия и время; следит за модулями, уже известными каталогу). Другие прокси (Athens, корпоративные) подключаются как источники с протоколом `goproxy`; у них нет ленты, версии узнаются по `@v/list` модуля. Прокси не отдаёт описание, лицензию и авторов модуля, поэтому у Go они пусты.

```json
{
  "slug": "proxy-golang-org",
  "name": "Go module proxy",
  "ecosystem": "go",
  "protocol": "goproxy",
  "base_url": "https://proxy.golang.org/",
  "priority": 0,
  "enabled": true,
  "config": {"index_url": "https://index.golang.org/index"}
}
```

Список источников приходит как `{"data": [...]}`, профилей — как `{"data": [{"slug": "default", "name": "PyPI", "ecosystem": "pypi", "is_default": true, "registries": [{"slug": "pypi", "routing_override": null}]}]}`.

### Изменения (`admin`)

| Запрос | Что делает |
|---|---|
| `POST /admin/registries` | завести источник: `slug`, `name`, `ecosystem`, `protocol`, `base_url`, `priority`, `config`, `routing`, `auth_type`, `credentials_ref`. Новый источник выключен, пока не пройдёт проверку подключения. Ответ `201` |
| `PUT /admin/registries/{slug}` | частичное изменение (`name`, `base_url`, `priority`, `config`, `routing`, `auth_type`, `credentials_ref`); `slug`, `ecosystem`, `protocol` неизменяемы |
| `POST /admin/registries/{slug}/test` | проверка подключения и авторизации: `{"ok": true, "error": null, "elapsed_ms": 120.5}` |
| `POST /admin/registries/{slug}/enable` | включить после успешной проверки подключения; неудачная проверка — `409` |
| `POST /admin/registries/{slug}/disable` | выключить: источник не опрашивается, его данные остаются |
| `DELETE /admin/registries/{slug}` | удалить; `204`. Источник с пакетами удалить нельзя (`409`), только выключить |
| `PUT /admin/profiles/{ecosystem}/{slug}` | создать или заменить профиль: `name`, `is_default`, `registries` (`[{"slug", "routing_override"}]` по порядку) |

- Errors: `401/404/409/422/500`

`Decisions fixed`
- Admin endpoints возвращают `202` для долгих операций через Celery.
- Пакетного обновления (`/admin/refresh`) больше нет: пакеты обновляет ежедневный прогон (`/admin/daily-update`).

`Open questions`
- Нет.

## 6. Системные endpoint-ы и дайджест

## 6.1 `GET /api/v1/health`

- Auth: none
- Response `200`:

```json
{"status": "ok"}
```

## 6.2 `GET /api/v1/stats`

- Auth: none
- Response `200`:

```json
{
  "ecosystem": "pypi",
  "packages": 3000,
  "versions": 60000,
  "dependencies": 300000,
  "last_sync_at": "2026-03-08T08:00:00Z",
  "ecosystems": [
    {
      "ecosystem": "pypi",
      "packages": 3000,
      "versions": 60000,
      "dependencies": 300000,
      "last_sync_at": "2026-03-08T08:00:00Z",
      "registries": [
        {
          "slug": "pypi",
          "enabled": true,
          "packages": 3000,
          "versions": 60000,
          "last_success_at": "2026-03-08T08:00:00Z",
          "degraded": false
        }
      ]
    }
  ]
}
```

- Поля верхнего уровня (`ecosystem`, `packages`, `versions`, `dependencies`, `last_sync_at`) — счётчики PyPI, как в MVP. `ecosystems` — по элементу на каждую экосистему, у которой есть пакеты или источники, по алфавиту.
- `registries[]` — источники экосистемы по приоритету. `packages` — пакеты, которые источник сейчас отдаёт (без `missing_since`); `versions` — версии, которые он публикует; `degraded` — источник временно отключён защитой от сбоев.
- `last_sync_at` — последний успех среди источников экосистемы.

## 6.3 `GET /api/v1/updates`

- Auth: none
- Behavior: лента событий `package_events` (журнал изменений, который пишет ежедневное обновление), окно по `detected_at`, новые первыми. Алгоритм и смысл событий — SPEC-DAILY-UPDATE.
- Query:
  - `since: datetime?`, `until: datetime?` — по умолчанию последние сутки; `since >= until` даёт `422`;
  - `kind: enum?` — можно повторять;
  - `ecosystem: enum?` — можно повторять;
  - `registry: string?` — slug источника, в котором есть пакет (у событий нет собственного источника);
  - `package: string?` — purl (`pkg:pypi/flask`, `pkg:golang/github.com/gin-gonic/gin`) или голое имя PyPI;
  - `limit: int=50 (max 200)`, `cursor: string?`
- Виды событий (`kind`): `tracking_started`, `release_added`, `release_yanked`, `release_unyanked`, `release_removed`, `release_restored`, `release_deprecated`, `vulnerability_added`, `vulnerability_withdrawn`, `project_unavailable`, `project_restored`, `source_added`. `release_deprecated` — версия объявлена deprecated, `source_added` — пакет появился в ещё одном источнике (его slug в `details.registry`).
- Response `200`:

```json
{
  "data": [
    {
      "id": 18,
      "ecosystem": "pypi",
      "package": "flask",
      "purl": "pkg:pypi/flask",
      "version": "3.1.3",
      "kind": "release_added",
      "detected_at": "2026-03-08T06:01:00Z",
      "occurred_at": "2026-03-07T12:00:00Z",
      "details": {"previous_version": "3.1.2", "bump": "patch"}
    }
  ],
  "cursor": "...",
  "has_more": false
}
```

- Errors: `422/500`

## 6.4 `GET /api/v1/updates/summary`

- Auth: none
- Behavior: дайджест по пакетам за окно. Пакеты разных экосистем с одним именем (`pkg:pypi/x`, `pkg:npm/x`) — разные строки. События `tracking_started` в дайджест не входят.
- Query:
  - `period: enum(day|week)=day`; `since`, `until` — явное окно, перекрывает `period`;
  - `tz: string="UTC"` — IANA-зона, по которой считается граница «сегодня»;
  - `ecosystem: enum?` — можно повторять;
  - `registry: string?`
- Response `200`:

```json
{
  "since": "2026-03-08T00:00:00Z",
  "until": "2026-03-08T09:00:00Z",
  "last_run": {
    "started_at": "2026-03-08T06:00:00Z",
    "finished_at": "2026-03-08T06:04:00Z",
    "status": "success",
    "packages_checked": 3000,
    "packages_changed": 12,
    "packages_updated": 12,
    "packages_failed": 0,
    "packages_unavailable": 0,
    "events_created": 15
  },
  "last_runs": [
    {
      "ecosystem": "pypi",
      "started_at": "2026-03-08T06:00:00Z",
      "finished_at": "2026-03-08T06:04:00Z",
      "status": "success",
      "packages_checked": 3000,
      "packages_changed": 12,
      "packages_updated": 12,
      "packages_failed": 0,
      "packages_unavailable": 0,
      "events_created": 15
    }
  ],
  "totals": {
    "packages": 1,
    "releases": 1,
    "yanked": 0,
    "removed": 0,
    "deprecated": 0,
    "sources_added": 0,
    "vulnerabilities": 0,
    "unavailable": 0
  },
  "packages": [
    {
      "ecosystem": "pypi",
      "name": "flask",
      "purl": "pkg:pypi/flask",
      "from_version": "3.1.2",
      "to_version": "3.1.3",
      "max_bump": "patch",
      "releases": ["3.1.3"],
      "yanked": [],
      "removed": [],
      "deprecated": [],
      "sources_added": [],
      "vulnerabilities": [],
      "flags": [],
      "last_detected_at": "2026-03-08T06:01:00Z"
    }
  ]
}
```

- `last_run` — последнее ежедневное обновление PyPI (`null`, если не было). `last_runs` — последнее обновление каждой экосистемы, по алфавиту; пустой список, если обновлений не было.
- `flags` — по убыванию важности: `unavailable`, `vulnerability_added`, `removed`, `yanked`, `deprecated`, `major`. `sources_added` — slug источников, в которых пакет появился в окне. `max_bump` — `major|minor|patch|pre|other`.
- Errors: `422` (неизвестная зона, `since >= until`)`/500`

`Decisions fixed`
- `health` остаётся публичным для orchestration probes.
- Верхние поля `/stats` и `last_run` в `/updates/summary` остаются PyPI-шными ради совместимости; по экосистемам — `ecosystems` и `last_runs`.

`Open questions`
- Нет.

## 7. Связь с другими документами

- Product intent, scope, SLA: [SPEC-MVP.md](./SPEC-MVP.md)
- DB schema, upsert semantics, migration order: [SPEC-MVP-DB.md](./SPEC-MVP-DB.md)
- Delivery plan и тестовые срезы: [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md)
- Мульти-экосистемный каталог (purl, источники, профили, граф resolve): [SPEC-MULTI-ECOSYSTEM.md](./SPEC-MULTI-ECOSYSTEM.md)
- Ежедневное обновление и дайджест: [SPEC-DAILY-UPDATE.md](./SPEC-DAILY-UPDATE.md)

`Decisions fixed`
- Любые изменения контрактов API правятся сначала в этом документе.

`Open questions`
- Нет.

## 8. Изменения

- **1.7-api (2026-10-30), этап E3.** Каталог содержит Go. Слаг экосистемы `go`, тип purl `golang`, путь модуля как написан (регистр значим, `/vN` — другой модуль), namespace purl — путь без последнего элемента (§2.1, §3.3). Версии Go: релизы, псевдоверсии (prerelease), `+incompatible`, отозванные версии со статусом `yanked` (§3.3.4); дерево зависимостей Go без окружения, `// indirect` входит (§3.3.5). Resolve для Go: `ecosystem: "go"`, окружение `go_version` / `replace` / `exclude`, стратегия `go-mvs` (`Go 1.23 (MVS with module graph pruning)`), результат равен `go list -m all`, `details.replaced_by` узлов, `details.indirect` рёбер, предупреждение `go_version` (§4.1.3). `/admin/packages` принимает purl `golang` (§5.3), `/admin/presence-probe` — `ecosystem: "go"` (§5.6); стартовый источник `proxy-golang-org` с лентой index.golang.org (§5.8).
- **1.6-api (2026-10-30), этап E2.** Каталог содержит crates.io. Слаг экосистемы `crates`, тип purl `cargo`, имена как опубликовано (`serde_json`, `Inflector`), без namespace (§2.1, §3.1, §3.3); ввод в другом написании приводится к опубликованному только в `POST /admin/packages` (§5.3). Карта features Cargo в `features` версии (§3.3.4). Дерево зависимостей crates принимает `target`, `features` и `default_features`, показывает runtime-, build- и optional-зависимости, платформенные помечает `environment_markers.rust_cfg` (§3.3.5). Resolve для crates: `ecosystem: "crates"`, окружение `target` (в том числе `*`) / `features` / `default_features`, features корня в `extras`, стратегия `cargo-resolver` (`Cargo 1.86 (resolver v2)`), несколько версий крейта в разных классах совместимости, `details.features` узлов, `details.rust_cfg` рёбер (§4.1.2). `/admin/presence-probe` принимает `ecosystem: "crates"` (§5.6); стартовый источник `crates-io` (§5.8).
- **1.5-api (2026-10-29), этап E1.** Каталог содержит npm. Имена npm в путях пишутся со scope (`@angular/core`), purl кодирует `@` как `%40` (§2.1, §3.3); `namespace` списка и поиска — scope без `@` (§3.1). Дерево зависимостей npm принимает из окружения только `os` (§3.3.5). Resolve для npm: `ecosystem: "npm"`, окружение `node_version`/`os`/`cpu`, стратегия `npm-arborist` (`npm 10 (arborist)`), конфликты вида `unsatisfiable`/`peer` и предупреждение `dependency_unresolvable` (§4.1.1). `/admin/presence-probe` принимает `ecosystem: "npm"` (§5.6); стартовый источник `npmjs` (§5.8).
- **1.4-api (2026-10-29), этап E0-C.** Адресация по purl: `/purl/{purl}` и `/packages/{ecosystem}/{coordinates}/-/…` с `profile`; маршруты `/packages/pypi/{name}/…` — deprecated-алиасы до конца E7 (§3). Новые поля карточки (`purl`, `namespace`, `origin`, `unavailable_at`, `sources`), версии (`purl`, `engines`, `dependencies_state`, `sources`), файлов (`registry`), списка и поиска (`purl`, `namespace`, `origin`; фильтры `ecosystem`, `registry`, `origin`); `latest_version` бывает `null`. Resolve в формате графа, вложенный формат — алиас (§4). `/stats` по экосистемам и источникам, `/updates` и `/updates/summary` с фильтрами и `last_runs`, новые виды событий `release_deprecated` и `source_added` (§6). `/admin/sync/status` строится по журналу `runs` и отдаёт `recent_runs` и `registries`; добавлены `/admin/packages`, `/admin/daily-update`, `/admin/presence-probe`, `/registries`, `/profiles` и admin-маршруты источников (§5). Удалён `/admin/refresh`. Ошибки resolve: `invalid_request`, `resolve_limit_exceeded`, `upstream_error`, `resolve_timeout` вместо `upstream_timeout` (§2.4).
- **1.3-mvp-api (2026-03-12).** Версия MVP.
