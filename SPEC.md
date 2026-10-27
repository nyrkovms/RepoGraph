# PackageHub — Спецификация сервиса

> Внутренний корпоративный каталог метаданных пакетов из публичных репозиториев

**Версия:** 0.5
**Дата:** 2026-03-04
**Changelog:** v0.5 — Registry как first-class entity (таблица Registry, PackageRegistry, привязка к источникам), таблица Artifact (файлы/артефакты per version per registry, checksums, download URLs, raw metadata blob), DistTag, SyncState привязан к registry (не ecosystem), пересмотренная reachability (needed packages вместо reachable versions), готовность к registry proxy mode. v0.4 — Resolver Microservice (Rust), стратегии per-ecosystem, in-memory индекс, RESOLVER.md. v0.3 — structured environment_markers, resolved_version_id. v0.2 — 6 экосистем, SyncState, Vulnerability.

> **2026-10-27:** модель покрытия сменилась на курируемый каталог. Разделы §2.1 (sync-механизмы), §3.13, §4.1 (Rust Resolver), §6.1–6.2, §8.2–8.3, §11, §12.3–12.6 и приложение C устарели — см. [SPEC-MULTI-ECOSYSTEM.md](./SPEC-MULTI-ECOSYSTEM.md) §0.2.

---

## 1. Обзор

### 1.1 Проблема

Команды разработки используют пакеты из множества экосистем (Python, JavaScript, Java, Go, Rust, .NET). Информация о пакетах разрозненна по разным registry, каждый со своим API и форматом метаданных. Нет единого места для поиска, сравнения и просмотра метаданных. Это замедляет принятие решений о выборе зависимостей.

### 1.2 Решение

PackageHub — единый каталог, агрегирующий метаданные пакетов из публичных репозиториев в нормализованном виде. Предоставляет REST API и Web UI для поиска и просмотра.

### 1.3 Пользователи

Внутренние команды разработки (enterprise). Типичные сценарии:

- Разработчик ищет библиотеку для конкретной задачи
- Тимлид сравнивает альтернативные пакеты (лицензия, активность, зависимости)
- Архитектор проверяет дерево зависимостей перед добавлением в проект
- Секьюрити-инженер смотрит статус и мейнтейнеров пакета

---

## 2. Поддерживаемые экосистемы

### 2.1 Перечень и приоритеты

| Приоритет | Экосистема | Registry | API | Объём | Sync-механизм |
|-----------|-----------|----------|-----|-------|---------------|
| P0 | Python | PyPI (pypi.org) | JSON API | ~550K | `last_serial` (инкрементальный) |
| P0 | JavaScript | npm (npmjs.com) | Registry API | ~2.5M | CouchDB `_changes` feed |
| P1 | .NET | NuGet (nuget.org) | V3 API | ~400K | Catalog (append-only log) |
| P1 | Rust | crates.io | REST + Sparse Index | ~170K | Sparse index / DB dump |
| P2 | Java | Maven Central + Google Maven | Lucene Index + POM XML | ~700K GA / ~14.5M GAV | Lucene incremental index (daily) |
| P2 | Go | Go Module Proxy | Proxy Protocol | ~200K | index.golang.org (NDJSON feed) |

**Стратегия:** MVP на PyPI (вертикальный слайс), затем npm, затем остальные по приоритету.

### 2.2 Особенности каждого registry

**PyPI** — самый дружелюбный для MVP. Всё в одном JSON-ответе: описание, лицензия, авторы, зависимости (PEP 508), уязвимости (OSV). `last_serial` для инкрементальной синхронизации.

**npm** — самый большой по объёму. Полный packument может весить мегабайты. Есть abbreviated-формат через Accept-заголовок. CouchDB `_changes` feed — лучший real-time механизм синхронизации.

**NuGet** — самый зрелый API. Catalog — append-only event log (идеально для sync через cursor). Встроенные уязвимости (VulnerabilityInfo). Структурированная deprecation. Зависимости группируются по target framework.

**crates.io** — самый строгий. Лицензия в SPDX обязательна. Зависимости чётко типизированы (normal/dev/build). Нет change feed — используем sparse index (NDJSON) или database dump для initial load. API rate limit: 1 req/sec.

**Maven Central** — самый сложный для синхронизации. Артефакты идентифицируются координатами GAV (groupId:artifactId:version). Всего ~700K уникальных GA (компонентов) и ~14.5M GAV (версий). Для метаданных (лицензия, зависимости, авторы) нужно скачать и распарсить POM-файл (XML). POM-файлы содержат properties (`${...}`), parent chains, dependencyManagement с BOM-импортами — всё это требует рекурсивного раскрытия. Sync через Lucene index (`nexus-maven-repository-index.gz`, ~2.5 GB compressed) с daily incremental diffs. Помимо Central поддерживаем Google Maven (Android, ~15K GA). Корпоративные репозитории (Nexus, Artifactory) — через proxy mode (см. 3.12).

**Go** — самый минималистичный. Proxy API: только `{Version, Time}` в .info, зависимости в .mod файле (plain text). Нет описания, лицензии, авторов через API. Зато `index.golang.org` — отличный NDJSON change feed с параметром `since`.

---

## 3. Модель данных

### 3.1 Package (основная сущность)

```
Package
├── id: UUID
├── ecosystem: enum (pypi | npm | maven | go | crates | nuget)
├── namespace: string? — groupId (Maven), scope (npm), пусто для остальных
├── name: string — имя пакета в экосистеме
├── normalized_name: string — нормализованное имя для поиска (lowercase, без спецсимволов)
├── primary_registry_id: FK → Registry — из какого registry синкаем метаданные
├── description: text?
├── homepage_url: string?
├── repository_url: string?
├── documentation_url: string?
├── license_raw: string? — оригинальная строка лицензии из registry
├── license_spdx: string? — нормализованный SPDX-идентификатор (best-effort)
├── keywords: string[]? — ключевые слова / теги
├── latest_version: string
├── downloads_total: bigint? — общее кол-во загрузок (если доступно)
├── downloads_recent: bigint? — загрузки за последний месяц (если доступно)
├── created_at: datetime — дата первой публикации
├── updated_at: datetime — дата последнего обновления в registry
├── synced_at: datetime — когда мы последний раз синхронизировали
├── deprecated: boolean DEFAULT false
├── deprecation_message: text? — сообщение о deprecation
├── deprecation_alternative: string? — рекомендуемая замена
└── raw_metadata: jsonb — оригинальные метаданные из registry (полный JSON)
```

### 3.2 PackageVersion (версия пакета)

```
PackageVersion
├── id: UUID
├── package_id: FK → Package
├── version: string — семантическая версия
├── release_date: datetime
├── status: enum (active | yanked | deprecated | unlisted)
├── requires_python: string? — PEP 440 version spec (">=3.8")
├── requires_node: string? — npm engines.node (">=16.0.0")
├── rust_version: string? — MSRV для crates.io
├── go_version: string? — go directive из go.mod
├── download_count: bigint? — загрузки конкретной версии (если доступно)
├── features: jsonb? — feature flags (crates.io: {"serde": ["dep:serde"]}, PyPI: extras list)
├── needed_in_index: boolean DEFAULT false — нужен в in-memory индексе резолвера (см. 3.13)
└── raw_metadata: jsonb
```

Примечание: `size_bytes` и `checksum_sha256` перенесены в таблицу Artifact (3.8), т.к. одна версия может иметь несколько файлов (PyPI wheels для разных платформ, Maven JAR + POM + sources).

### 3.3 Dependency (зависимость)

```
Dependency
├── id: UUID
├── version_id: FK → PackageVersion
├── dependency_name: string — имя зависимости в целевой экосистеме
├── dependency_namespace: string? — groupId (Maven), scope (npm)
├── dependency_ecosystem: enum
├── version_constraint: string — ">= 1.0, < 2.0" / "^0.6" / "[1.0,2.0)"
├── dependency_type: enum (runtime | dev | optional | build | peer | provided)
├── extra_name: string? — название extra/feature, в рамках которого нужна зависимость
│                          (PyPI: extras_require["security"], crates.io: features["serde"])
├── environment_markers: jsonb? — структурированные условия применимости (см. 3.3.1)
├── raw_spec: string — оригинальная строка зависимости из registry
├── resolved_package_id: FK → Package? — ссылка если пакет есть в нашей БД
└── resolved_version_id: FK → PackageVersion? — конкретная версия (latest stable matching constraint)
```

#### 3.3.1 Структура environment_markers (JSONB)

Условия, при которых зависимость применима. Хранятся в нормализованном виде для фильтрации на стороне сервера.

```jsonc
// PyPI: requests>=2.20; python_version>="3.6" and sys_platform!="win32"
{
  "type": "python_environment",
  "conditions": [
    {"marker": "python_version", "op": ">=", "value": "3.6"},
    {"marker": "sys_platform", "op": "!=", "value": "win32"}
  ]
}

// NuGet: dependency group per target framework
{
  "type": "dotnet_target_framework",
  "framework": "net6.0"
}

// crates.io: cfg(target_os = "linux")
{
  "type": "rust_cfg",
  "expression": "target_os = \"linux\""
}

// Maven: <scope>test</scope> — нет условий, scope уже в dependency_type
// Go: // indirect — нет условий, тип уже в dependency_type
// Если условий нет — поле NULL (зависимость безусловная)
```

#### 3.3.2 Резолвинг версий

Два уровня резолвинга:

**Уровень 1: Best-effort (в PostgreSQL, при sync)**

Заполнение `resolved_version_id` при sync-пайплайне:
1. Найти Package по `(dependency_ecosystem, dependency_namespace, dependency_name)`
2. Выбрать `PackageVersion` как latest stable (не yanked, не prerelease), удовлетворяющую `version_constraint`
3. Записать в `resolved_version_id`
4. Пересчитывать при sync пакета-зависимости

Ограничения: не учитывает транзитивные конфликты, dependencyManagement, BOM, scopes. Достаточно для отображения дерева в UI (90% случаев).

**Уровень 2: Full resolve (Resolver Microservice)**

Полноценный dependency resolver для каждой экосистемы (см. раздел 12):
- PyPI: PubGrub (SAT-подобный с backtracking)
- Maven/Gradle: greedy BFS (nearest-wins или highest-wins)
- npm: greedy DFS (множественные версии допустимы)
- crates.io: PubGrub
- NuGet: highest-wins с target framework filtering
- Go: MVS (Minimal Version Selection)

API резолвера документирует стратегию: `"resolve_strategy": "latest_stable"` (уровень 1) или `"resolve_strategy": "full"` (уровень 2).

#### 3.3.3 Построение дерева зависимостей

API-эндпоинт `GET /api/v1/packages/{eco}/{name}/versions/{ver}/dependencies` поддерживает параметры:

```
?depth=3              — глубина рекурсии (default: 1, max: 5)
?include_dev=false    — включать dev-зависимости (default: false)
?include_optional=false — включать optional/extras (default: false)
?python_version=3.12  — версия Python (для фильтрации PyPI markers)
?os=linux             — ОС (для фильтрации platform markers)
?target_framework=net8.0 — .NET target framework (для NuGet)
```

Алгоритм на сервере:

```
1. Загрузить Dependency[] для version_id
2. Отфильтровать по dependency_type (исключить dev/optional если не запрошены)
3. Отфильтровать по environment_markers (вычислить маркеры с учётом переданного контекста)
4. Для каждой оставшейся зависимости:
   a. Если resolved_version_id != NULL и depth > 1 — рекурсия (шаг 1 для resolved_version_id)
   b. Если resolved_version_id == NULL — лист дерева (пакет не в нашей БД или constraint не резолвится)
5. Собрать дерево, обнаружить циклы (пометить cycle: true, не уходить в бесконечность)
```

PostgreSQL: recursive CTE для эффективного обхода, с `LIMIT` по глубине и детекцией циклов через массив visited.

### 3.4 Author (автор / мейнтейнер)

```
Author
├── id: UUID
├── name: string
├── email: string?
├── url: string?
└── registry_username: string? — username в registry (npm maintainers, crates.io owners)

PackageAuthor (many-to-many)
├── package_id: FK → Package
├── author_id: FK → Author
└── role: enum (author | maintainer | owner | contributor)
```

### 3.5 Registry (источник пакетов)

```
Registry
├── id: UUID
├── ecosystem: enum (pypi | npm | maven | go | crates | nuget)
├── name: string — "Maven Central", "Google Maven", "PyPI", "npm Registry"
├── slug: string (UNIQUE) — "maven-central", "google-maven", "pypi", "npm"
├── registry_type: enum (public | corporate | mirror)
├── base_url: string — "https://repo1.maven.org/maven2/"
├── api_url: string? — "https://search.maven.org/solrsearch/select"
├── url_pattern: string? — шаблон для генерации download URL (если layout стандартизирован)
│                           Maven: "${base}/${g/./\/}/${a}/${v}/${a}-${v}.${ext}"
│                           npm: "${base}/${name}/-/${name}-${version}.tgz"
│                           NULL для registry с непредсказуемыми URL (PyPI)
├── auth_required: boolean DEFAULT false
├── auth_config: jsonb? — encrypted credentials для corporate repos
│                          {"type": "basic", "username": "...", "password": "encrypted:..."}
│                          {"type": "bearer", "token": "encrypted:..."}
├── tier: enum (tier1 | tier2 | tier3)
├── sync_enabled: boolean DEFAULT true
├── sync_priority: int DEFAULT 0 — порядок при sync (Central=0, Google=1, ...)
├── created_at: datetime
└── last_synced_at: datetime?
```

Seed-данные (создаются при первом деплое):

```sql
INSERT INTO registries (ecosystem, name, slug, registry_type, base_url, tier) VALUES
-- Tier 1: полная синхронизация
('pypi',   'PyPI',             'pypi',             'public', 'https://pypi.org/',                        'tier1'),
('npm',    'npm Registry',     'npm',              'public', 'https://registry.npmjs.org/',              'tier1'),
('nuget',  'NuGet Gallery',    'nuget',            'public', 'https://api.nuget.org/',                   'tier1'),
('crates', 'crates.io',        'crates-io',        'public', 'https://crates.io/',                       'tier1'),
('maven',  'Maven Central',    'maven-central',    'public', 'https://repo1.maven.org/maven2/',          'tier1'),
('maven',  'Google Maven',     'google-maven',     'public', 'https://maven.google.com/',                'tier1'),
('go',     'Go Module Proxy',  'go-proxy',         'public', 'https://proxy.golang.org/',                'tier1'),

-- Tier 2: ленивая индексация при первом запросе
('maven',  'Gradle Plugins',    'gradle-plugins',   'public', 'https://plugins.gradle.org/m2/',          'tier2'),
('maven',  'Confluent',         'confluent',        'public', 'https://packages.confluent.io/maven/',    'tier2'),
('maven',  'Spring Milestones', 'spring-milestones','public', 'https://repo.spring.io/milestone/',       'tier2');

-- Tier 3: корпоративные — добавляются через admin API:
-- POST /api/v1/admin/registries
```

### 3.6 PackageRegistry (many-to-many: пакет ↔ registry)

Один пакет может присутствовать в нескольких registry (напр. `spring-core` в Maven Central и Spring Milestones). Связь фиксирует, в каких registry обнаружен пакет.

```
PackageRegistry
├── package_id: FK → Package
├── registry_id: FK → Registry
├── first_seen_at: datetime — когда мы обнаружили пакет в этом registry
├── source_url: string? — прямая ссылка на пакет в UI registry
│                          "https://search.maven.org/artifact/org.springframework/spring-core"
│                          "https://www.npmjs.com/package/react"
│                          "https://pypi.org/project/flask/"
└── is_primary: boolean DEFAULT false — из этого registry мы синкаем метаданные

UNIQUE(package_id, registry_id)
```

Для экосистем с одним registry (PyPI, npm, NuGet, crates.io, Go) — всегда одна запись с `is_primary = true`. Для Maven — может быть несколько (Central + Google Maven + corporate Nexus).

### 3.7 Artifact (файлы/артефакты версии)

Каждая версия пакета может иметь один или несколько загружаемых файлов. Примеры: PyPI — sdist `.tar.gz` + wheels `.whl` для разных платформ; Maven — `.jar` + `.pom` + `-sources.jar` + `-javadoc.jar`; npm — один `.tgz`.

```
Artifact
├── id: UUID
├── version_id: FK → PackageVersion
├── registry_id: FK → Registry — из какого registry этот артефакт
├── filename: string — "flask-3.0.0-py3-none-any.whl", "spring-core-6.1.1.jar"
├── download_url: string? — полный URL, NULL если генерируется из Registry.url_pattern
├── size_bytes: bigint?
├── checksum_sha256: string?
├── checksum_sha1: string? — Maven, Go
├── checksum_md5: string? — Maven legacy
├── checksum_integrity: string? — npm SRI hash "sha512-..."
├── artifact_type: enum (wheel | sdist | jar | pom | sources_jar | javadoc_jar |
│                         tgz | zip | nupkg | crate | go_mod | go_info)
├── platform_tags: jsonb? — платформенная специфика:
│                            PyPI: {"python": "cp312", "abi": "cp312", "platform": "manylinux_x86_64"}
│                            npm: {"os": ["linux","darwin"], "cpu": ["x64","arm64"]}
│                            Maven: {"classifier": "sources", "type": "jar"}
└── metadata_blob: text? — raw POM XML, raw go.mod, raw .nuspec (для proxy mode)
```

**Стратегия хранения `download_url`:**
- Maven, Go, crates.io: URL генерируется из `Registry.url_pattern` + координаты пакета → `download_url = NULL`
- PyPI: URL содержит непредсказуемый hash-путь → `download_url` хранится явно
- npm: URL предсказуемый, но хранить проще чем генерировать → `download_url` хранится явно
- NuGet: URL предсказуемый → `download_url = NULL`, генерация по pattern

**Стратегия хранения `metadata_blob`:**
- Вариант по умолчанию: хранить blob только для `needed_in_index` пакетов (см. 3.13)
- Maven POM: ~5M needed GAV × ~3 KB = ~15 GB (вместо 43 GB для всех 14.5M GAV)
- Go mod: ~600K × ~0.5 KB = ~0.3 GB
- Для остальных: blob не обязателен (метаданные реконструируются из structured data)
- При промахе (blob = NULL и нужен): fetch from upstream → заполнить blob → вернуть

### 3.8 DistTag (теги версий)

npm использует dist-tags (`latest`, `next`, `beta`, `canary`) для именования версий. Аналогичная концепция полезна и для других экосистем (PyPI latest stable, crates.io latest).

```
DistTag
├── id: UUID
├── package_id: FK → Package
├── tag: string — "latest", "next", "beta", "canary", "lts"
├── version: string — на какую версию указывает ("18.2.0")
└── updated_at: datetime

UNIQUE(package_id, tag)
```

Для npm: синкаем `dist-tags` из packument. Для остальных экосистем: вычисляем при sync (`latest` = последний stable релиз).

### 3.9 SyncState (состояние синхронизации per-registry)

Sync привязан к registry, не к ecosystem. Maven Central и Google Maven синхронизируются отдельно — разные механизмы, разные cursors, разные расписания.

```
SyncState
├── id: UUID
├── registry_id: FK → Registry (UNIQUE)
├── sync_type: enum (full | incremental)
├── status: enum (idle | running | failed)
├── last_success_at: datetime?
├── last_error: text?
├── cursor_data: jsonb — registry-специфичный курсор:
│   ├── pypi:             {"last_serial": 28456789}
│   ├── npm:              {"last_seq": "12345-abc"}
│   ├── maven-central:    {"lucene_chunk_index": 1456, "last_gav_count": 14523000}
│   ├── google-maven:     {"last_group_hash": "abc123"}
│   ├── go-proxy:         {"last_since": "2026-03-03T00:00:00Z"}
│   ├── crates-io:        {"last_dump_date": "2026-03-02", "last_index_etag": "W/\"abc\""}
│   └── nuget:            {"catalog_cursor": "2026-03-03T00:00:00Z"}
├── packages_synced: bigint — кол-во пакетов в текущем/последнем sync
└── packages_total: bigint — общее кол-во пакетов в этом registry
```

### 3.10 Vulnerability (уязвимости — единый слой)

```
Vulnerability
├── id: UUID
├── source: enum (osv | pypi_native | nuget_native)
├── source_id: string — "PYSEC-2021-9", "GHSA-xxx", "CVE-2021-3281"
├── aliases: string[] — альтернативные идентификаторы
├── summary: text
├── details: text?
├── severity: enum (unknown | low | medium | high | critical)?
├── fixed_in: string[]? — версии в которых исправлено
├── published_at: datetime?
├── withdrawn_at: datetime? — если отозвана
└── link: string — URL на источник

PackageVulnerability (many-to-many)
├── package_id: FK → Package
├── vulnerability_id: FK → Vulnerability
└── affected_versions: string? — диапазон затронутых версий
```

### 3.11 Индексы

```
-- Пакеты
Package(ecosystem, namespace, normalized_name) — уникальный, основной lookup
Package(normalized_name) — pg_trgm + GIN для fuzzy search
Package(ecosystem, updated_at) — для инкрементальных запросов
Package(primary_registry_id) — пакеты по registry
Package(license_spdx) — фильтрация по лицензии
Package(downloads_total DESC NULLS LAST) — сортировка по популярности

-- Версии
PackageVersion(package_id, version) — уникальный
PackageVersion(release_date) — сортировка по свежести
PackageVersion(needed_in_index) WHERE needed_in_index = true — для bulk load в resolver

-- Зависимости
Dependency(version_id) — быстрая загрузка дерева зависимостей
Dependency(dependency_name, dependency_ecosystem) — обратный поиск "кто зависит от X"
Dependency(resolved_version_id) — для построения рекурсивного дерева (recursive CTE)
Dependency(version_id) WHERE environment_markers IS NOT NULL — partial index

-- Registry
Registry(ecosystem, tier) — все registry экосистемы по tier
Registry(slug) — уникальный, для API lookup

-- PackageRegistry
PackageRegistry(package_id, registry_id) — уникальный
PackageRegistry(registry_id) — "все пакеты из Google Maven"

-- Артефакты
Artifact(version_id) — все файлы версии
Artifact(version_id, artifact_type) — быстрый lookup POM / wheel / etc.
Artifact(registry_id) — все артефакты из registry

-- Dist Tags
DistTag(package_id, tag) — уникальный
DistTag(package_id) — все теги пакета

-- Уязвимости
Vulnerability(source_id) — уникальный
PackageVulnerability(package_id) — поиск уязвимостей пакета

-- SyncState
SyncState(registry_id) — уникальный
```

### 3.12 Repository Tiers

Стратегия индексации репозиториев зависит от tier'а (определяется в Registry.tier):

```
Tier 1 — полная синхронизация (при каждом sync):
  pypi, npm, nuget, crates-io, go-proxy  — по одному registry, sync по расписанию
  maven-central                           — основной, ~14.5M GAV, Lucene index
  google-maven                            — Android, ~15K GA, отдельный sync

Tier 2 — ленивая индексация (при первом запросе артефакта):
  gradle-plugins     — при resolve Gradle plugin
  confluent          — при запросе kafka-артефакта
  spring-milestones  — при запросе milestone-версии Spring
  → fetch POM on demand → сохранить в PostgreSQL → добавить в индекс

Tier 3 — proxy mode (корпоративные):
  Nexus, Artifactory, CodeArtifact, Azure Artifacts
  → пользователь регистрирует через POST /api/v1/admin/registries
  → fetch on demand → кэш в Redis (TTL 1h) → НЕ сохранять в PostgreSQL
  → при miss в публичном индексе резолвер пробует tier3 по очереди
```

### 3.13 Needed Packages (оптимизация in-memory индекса)

Не все данные из PostgreSQL нужны в in-memory индексе резолвера. Стратегия — определить **нужные пакеты** (packages, а не versions) и загрузить все их версии.

**Почему пакеты, а не версии:** резолвер не знает заранее какую версию выберет. Если constraint `jackson-core >= 2.10, < 2.16` — нужны ВСЕ версии jackson-core в этом диапазоне. Обрезка на уровне версий привела бы к промахам.

**Определение:** пакет (GA для Maven, name для остальных) нужен в индексе если:
1. Имеет хотя бы одну версию, опубликованную за последние 5 лет, ИЛИ
2. На него ссылается зависимость из другого нужного пакета (транзитивно)

**Вычисление** (еженедельно после sync):

```sql
-- Шаг 1: Reset
UPDATE package_versions SET needed_in_index = FALSE;

-- Шаг 2: Найти нужные пакеты (рекурсивно)
WITH RECURSIVE needed_pkgs AS (
    -- Корни: пакеты с недавней публикацией
    SELECT DISTINCT p.id FROM packages p
    JOIN package_versions pv ON pv.package_id = p.id
    WHERE pv.release_date > now() - interval '5 years'

    UNION

    -- Транзитивно: пакеты, от которых зависят нужные
    SELECT DISTINCT d.resolved_package_id FROM dependencies d
    JOIN package_versions pv ON d.version_id = pv.id
    WHERE pv.package_id IN (SELECT id FROM needed_pkgs)
      AND d.resolved_package_id IS NOT NULL
)
-- Шаг 3: Пометить ВСЕ версии нужных пакетов (минус yanked/SNAPSHOT)
UPDATE package_versions SET needed_in_index = TRUE
WHERE package_id IN (SELECT id FROM needed_pkgs)
  AND status != 'yanked'
  AND version NOT LIKE '%-SNAPSHOT';
```

**Результат по экосистемам:**

| Экосистема | Всего пакетов | Нужных пакетов | Версий в индексе | Обрезка |
|------------|--------------|----------------|-----------------|---------|
| Maven | ~700K GA | ~550K | ~10M (из 14.5M) | 31% |
| npm | ~2.5M | ~1M | ~28M (из 60M) | 53% |
| PyPI | ~550K | ~400K | ~3.2M (из 4M) | 20% |
| NuGet | ~400K | ~280K | ~2.2M (из 3M) | 27% |
| crates.io | ~170K | ~140K | ~420K (из 500K) | 16% |
| Go | ~200K | ~150K | ~750K (из 1M) | 25% |

**Fallback при промахе:** пакет не найден в индексе → запрос в PostgreSQL → загрузить все версии пакета → добавить в LRU cache (100K entries) → пометить `needed_in_index` при следующем пересчёте. Latency: +5-20ms, ожидаемая частота: <2% запросов.

---

## 4. Архитектура

### 4.1 Компоненты

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Web UI     │────▶│   REST API   │────▶│  PostgreSQL   │
│  (React/Vue) │     │  (FastAPI)   │────▶│              │
└──────────────┘     └──────┬───────┘     └──────┬───────┘
                            │                     │
                     ┌──────▼───────┐      ┌──────▼───────┐
                     │    Redis     │      │   Resolver   │
                     │ (кэш+очередь)│      │ Microservice │
                     └──────┬───────┘      │   (Rust)     │
                            │              │              │
                     ┌──────▼───────┐      │ In-memory    │
                     │   Workers    │      │ index per    │
                     │  (Celery)    │      │ ecosystem    │
                     │              │      └──────────────┘
                     │ ┌──────────┐ │       ┌──────────────┐
                     │ │ PyPI     │ │       │              │
                     │ ├──────────┤ │       │   OSV.dev    │
                     │ │ npm      │ │──────▶│ (уязвимости) │
                     │ ├──────────┤ │       │              │
                     │ │ NuGet    │ │       └──────────────┘
                     │ ├──────────┤ │
                     │ │ crates   │ │
                     │ ├──────────┤ │
                     │ │ Maven    │ │
                     │ ├──────────┤ │
                     │ │ Go       │ │
                     │ └──────────┘ │
                     └──────────────┘
```

**Resolver Microservice** — отдельный сервис на Rust, держит in-memory индекс зависимостей всех экосистем. Загружает данные из PostgreSQL (только `needed_in_index` версии, см. 3.13). Реализует PubGrub для Python/Rust, greedy BFS для Maven/NuGet, greedy DFS для npm, MVS для Go. Подробная архитектура — см. раздел 12 и RESOLVER.md.

### 4.2 Стек технологий

| Компонент | Технология | Обоснование |
|-----------|-----------|-------------|
| API | FastAPI | Async, автодокументация OpenAPI, Pydantic |
| ORM | SQLAlchemy 2.0 | Async-поддержка, зрелость |
| БД | PostgreSQL 16 | JSONB, pg_trgm, надёжность |
| Кэш/Очередь | Redis 7 | Кэш API + брокер для Celery |
| Воркеры | Celery | Периодические задачи + ручной запуск |
| Resolver | Rust (PubGrub + custom) | In-memory индекс, <5ms p95, PubGrub для SAT-экосистем |
| Web UI | React (Vite) | Простой SPA для поиска и просмотра |
| Деплой | Docker Compose | Простота для MVP |

### 4.3 Абстрактный интерфейс коннектора

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Any

@dataclass
class SyncCursor:
    """Ecosystem-специфичный курсор синхронизации."""
    data: dict[str, Any]  # {"last_serial": 123} / {"last_seq": "abc"} / ...

class BaseConnector(ABC):
    """Абстрактный коннектор к пакетному registry."""

    ecosystem: str  # "pypi", "npm", ...

    @abstractmethod
    async def fetch_changes(
        self, cursor: SyncCursor | None = None
    ) -> AsyncIterator[tuple[str, SyncCursor]]:
        """
        Получить список изменённых пакетов начиная с cursor.
        Yields: (package_name, updated_cursor)
        """

    @abstractmethod
    async def fetch_package_metadata(
        self, package_name: str
    ) -> NormalizedPackage:
        """Получить нормализованные метаданные одного пакета."""

    @abstractmethod
    async def fetch_version_metadata(
        self, package_name: str, version: str
    ) -> NormalizedVersion:
        """Получить метаданные конкретной версии."""

    @abstractmethod
    async def fetch_dependencies(
        self, package_name: str, version: str
    ) -> list[NormalizedDependency]:
        """Получить список зависимостей версии."""

    @abstractmethod
    def get_rate_limit(self) -> float:
        """Минимальный интервал между запросами в секундах."""
```

### 4.4 Детали реализации коннекторов

**PyPI коннектор (одноступенчатый)**
- Источник: `GET https://pypi.org/pypi/{name}/json`
- Один запрос = полные метаданные + все версии + уязвимости
- Sync: Serial API (`last_serial`)
- Rate limit: разумный (без документированных ограничений)

**npm коннектор (одноступенчатый, два формата)**
- Полные данные: `GET https://registry.npmjs.org/{name}`
- Abbreviated: `Accept: application/vnd.npm.install-v1+json`
- Sync: `_changes` feed (real-time)
- Загрузки: отдельный API `api.npmjs.org/downloads`
- ⚠ Ответы могут быть мегабайтами для популярных пакетов

**NuGet коннектор (двухступенчатый)**
- Service Index: `GET https://api.nuget.org/v3/index.json` → RegistrationsBaseUrl
- Registration: `GET {base}/{id-lower}/index.json` → все версии + метаданные
- Sync: Catalog resource (cursor-based append-only log)
- Уязвимости: VulnerabilityInfo endpoint (нативно)

**crates.io коннектор (двухступенчатый)**
- Sparse index: `GET https://index.crates.io/{prefix}/{name}` → NDJSON всех версий
- API: `GET https://crates.io/api/v1/crates/{name}` → полные метаданные
- Sync: database dump (initial) + periodic API polling
- Rate limit: 1 req/sec, обязательный User-Agent

**Maven коннектор (многоступенчатый)**
- Initial load: Lucene index (`nexus-maven-repository-index.gz`, ~2.5 GB compressed) → список всех GAV
- POM: `GET https://repo1.maven.org/maven2/{g}/{a}/{v}/{a}-{v}.pom` → XML с полными метаданными
- Incremental: daily Lucene diff index (`nexus-maven-repository-index.{N}.gz`, ~10-50 MB)
- Google Maven: `GET https://maven.google.com/master-index.xml` → group list, затем POM'ы
- ⚠ POM содержит properties (`${...}`), parent chains, BOM imports — рекурсивное раскрытие
- ⚠ Initial full crawl: ~12M POM × ~3 KB = ~36 GB трафика, 3-6 часов с параллелизмом

**Go коннектор (трёхступенчатый)**
- Index: `GET https://index.golang.org/index?since={timestamp}&limit=2000` → NDJSON feed
- Info: `GET https://proxy.golang.org/{module}/@v/{version}.info` → `{Version, Time}`
- Mod: `GET https://proxy.golang.org/{module}/@v/{version}.mod` → go.mod (зависимости)
- ⚠ Нет описания, лицензии, авторов через API. Только имя, версия, время, зависимости.

---

## 5. REST API

### 5.1 Эндпоинты

**Пакеты:**

```
GET  /api/v1/packages                — список пакетов (фильтры, пагинация)
GET  /api/v1/packages/{ecosystem}/{name}  — метаданные пакета
GET  /api/v1/packages/{ecosystem}/{name}/versions  — список версий
GET  /api/v1/packages/{ecosystem}/{name}/versions/{version}  — одна версия
GET  /api/v1/packages/{ecosystem}/{name}/versions/{version}/dependencies  — дерево зависимостей (см. 3.3.3)
GET  /api/v1/packages/{ecosystem}/{name}/versions/{version}/dependents  — обратные зависимости
GET  /api/v1/packages/{ecosystem}/{name}/versions/{version}/artifacts  — файлы/артефакты версии (см. 3.7)
GET  /api/v1/packages/{ecosystem}/{name}/vulnerabilities  — уязвимости пакета
```

Для Maven и npm с namespace:
```
GET  /api/v1/packages/maven/com.google.guava:guice  — namespace:name
GET  /api/v1/packages/npm/@angular:core  — scope:name
```

**Поиск:**

```
GET  /api/v1/search?q=...&ecosystem=...&license=...&registry=...  — полнотекстовый поиск
```

**Registry:**

```
GET  /api/v1/registries                          — список всех registry (с фильтрами по ecosystem, tier)
GET  /api/v1/registries/{slug}                   — метаданные registry
GET  /api/v1/registries/{slug}/packages          — пакеты из конкретного registry
GET  /api/v1/registries/{slug}/sync-status       — состояние синхронизации
```

**Синхронизация (admin):**

```
POST /api/v1/admin/sync/{registry_slug}          — запустить синхронизацию registry
POST /api/v1/admin/sync/{registry_slug}/{name}   — синхронизировать один пакет из registry
GET  /api/v1/admin/sync/status                    — статус текущих задач по всем registry
```

**Registry Management (admin):**

```
POST   /api/v1/admin/registries                  — добавить registry (для Tier 2/3)
PUT    /api/v1/admin/registries/{slug}            — обновить настройки registry
DELETE /api/v1/admin/registries/{slug}            — удалить registry (только Tier 3)
POST   /api/v1/admin/registries/{slug}/test       — проверить подключение к registry
```

**Dependency Resolution (V2):**

```
POST /api/v1/resolve                         — полноценный resolve через Resolver Microservice (см. 12.4)
```

**Системные:**

```
GET  /api/v1/health        — healthcheck
GET  /api/v1/stats         — статистика (кол-во пакетов по экосистемам, время последнего sync)
```

### 5.2 Пагинация

Cursor-based пагинация (не offset) для стабильной работы на больших объёмах:

```json
{
  "data": [...],
  "cursor": "eyJpZCI6ICIxMjM0In0=",
  "has_more": true
}
```

### 5.3 Фильтры для GET /api/v1/packages

| Параметр | Тип | Описание |
|----------|-----|----------|
| ecosystem | string | Фильтр по экосистеме (pypi, npm, maven, go, crates, nuget) |
| registry | string | Фильтр по registry slug (maven-central, google-maven, ...) |
| license | string | Фильтр по SPDX лицензии |
| author | string | Фильтр по автору |
| has_vulnerabilities | boolean | Только пакеты с/без уязвимостей |
| updated_after | datetime | Пакеты обновлённые после даты |
| sort | string | name / updated_at / created_at / downloads_total |
| limit | int | Размер страницы (default: 50, max: 200) |
| cursor | string | Курсор пагинации |

---

## 6. Синхронизация данных

### 6.1 Стратегия

- **Полная синхронизация** — первичная загрузка. Однократно при добавлении новой экосистемы.
- **Инкрементальная синхронизация** — по расписанию. Только изменения с момента последнего sync.
- **Ручная синхронизация** — через admin API для конкретной экосистемы или пакета.

### 6.2 Расписание по умолчанию

| Registry | Интервал | Обоснование |
|----------|----------|-------------|
| pypi | каждые 6 часов | Средний объём изменений |
| npm | каждые 4 часа | Самый большой объём, хороший change feed |
| nuget | каждые 6 часов | Catalog делает sync эффективным |
| crates-io | каждые 12 часов | Rate limit, нет change feed |
| maven-central | каждые 24 часа | Lucene incremental diff index (daily) |
| google-maven | каждые 24 часа | Маленький, отдельный от Central |
| go-proxy | каждые 6 часов | Отличный index feed |
| Tier 2 registry | по запросу | Ленивая индексация при первом обращении |
| Tier 3 registry | никогда | Proxy mode, fetch on demand |

### 6.3 Пайплайн

```
1.  discover    — получить список изменённых пакетов (через change feed / cursor per registry)
2.  fetch       — загрузить метаданные каждого пакета (через коннектор)
3.  normalize   — привести к единой модели (NormalizedPackage)
4.  validate    — проверить обязательные поля, нормализовать лицензию в SPDX
5.  store       — upsert в PostgreSQL (Package, PackageVersion, Dependency)
6.  artifacts   — upsert Artifact записи (файлы, checksums, download URLs, metadata_blob)
7.  registry    — upsert PackageRegistry связь, обновить DistTag
8.  resolve     — заполнить resolved_package_id и resolved_version_id (best-effort, см. 3.3.2)
9.  cache       — инвалидировать Redis-кэш для обновлённых пакетов
10. checkpoint  — сохранить курсор в SyncState (per registry)
11. needed_pkgs — (еженедельно) пересчитать needed_in_index (см. 3.13)
12. index_refresh — (после needed_pkgs) обновить in-memory индекс Resolver Microservice
```

### 6.4 Нормализация лицензий

Лицензии приходят в разных форматах. Стратегия:

1. Если registry даёт SPDX (crates.io, NuGet) — использовать напрямую
2. Если registry даёт произвольную строку (PyPI, npm, Maven) — best-effort маппинг через таблицу алиасов ("MIT License" → "MIT", "Apache 2.0" → "Apache-2.0")
3. Всегда сохранять `license_raw` + `license_spdx`
4. Если нормализация не удалась — `license_spdx = NULL`, только `license_raw`

### 6.5 Обработка ошибок

- Если registry недоступен — retry с экспоненциальным backoff (3 попытки)
- Если пакет не парсится — логировать, пропустить, продолжить (не ронять весь sync)
- Rate limiting — каждый коннектор знает лимиты своего registry и соблюдает их
- Checkpoint — курсор сохраняется после каждого батча (не в конце), чтобы при сбое не начинать сначала

### 6.6 Уязвимости

Основной источник — OSV.dev API (`https://api.osv.dev/v1/query`). Покрывает все 6 экосистем единообразно. Дополнительно:

- PyPI: уязвимости встроены в JSON API (бесплатный бонус при sync)
- NuGet: VulnerabilityInfo endpoint (нативный)
- Остальные: только OSV.dev

Sync уязвимостей — отдельный периодический job (каждые 12 часов).

---

## 7. Аутентификация и авторизация

### 7.1 API-доступ

- Bearer-токены (JWT или opaque tokens)
- Токены создаются администратором через CLI или admin UI
- Каждый токен привязан к сервисному аккаунту с правами read-only или admin

### 7.2 Web UI

- SSO через OIDC (Keycloak / corporate IdP)
- После аутентификации — получение JWT для вызовов API
- Роли: viewer (по умолчанию), admin (управление синхронизацией)

### 7.3 MVP-упрощение

На первом этапе:

- Read API и Web UI открыты, доступ к ним ограничивается сетью.
- Admin-операции защищены одним API-токеном (`API_TOKEN_ADMIN`).
- Отдельного read-only токена нет (SPEC-MVP-API §2.2).

SSO и роли viewer/admin из IdP появятся во втором этапе.

---

## 8. Acceptance Criteria

### 8.1 MVP (вертикальный слайс — PyPI)

- [ ] Полная синхронизация загружает ≥500K пакетов из PyPI без ошибок
- [ ] Инкрементальная синхронизация (по last_serial) отрабатывает за <15 минут
- [ ] Поиск по имени пакета возвращает результат за <200ms (p95)
- [ ] API отдаёт метаданные пакета со всеми полями модели (включая license_raw/license_spdx)
- [ ] Зависимости парсятся из PEP 508 формата и нормализуются
- [ ] Уязвимости из PyPI JSON API сохраняются и доступны через API
- [ ] Web UI позволяет искать, фильтровать и просматривать пакеты
- [ ] Docker Compose поднимает все сервисы одной командой
- [ ] API-эндпоинты защищены токенами

### 8.2 V1 (мульти-экосистема — 4 registry)

- [ ] npm-коннектор: packument парсится, abbreviated-формат для листинга
- [ ] NuGet-коннектор: Catalog-based sync, зависимости per target framework
- [ ] crates.io-коннектор: sparse index для sync, features в JSONB
- [ ] Кросс-экосистемный поиск работает (один запрос — результаты из всех)
- [ ] Нормализация лицензий в SPDX для всех экосистем
- [ ] OSV.dev интеграция для единого слоя уязвимостей
- [ ] SSO-аутентификация через OIDC
- [ ] Мониторинг синхронизации (Prometheus-метрики, SyncState dashboard)

### 8.3 V2 (полное покрытие + resolver)

- [ ] Maven-коннектор: Lucene index + POM-парсер с parent chain, BOM expansion, property interpolation
- [ ] Go-коннектор: index.golang.org + go.mod парсер
- [ ] Reachability graph вычисляется еженедельно, сокращает индекс на 50%+
- [ ] Resolver Microservice (Rust): PubGrub для PyPI/crates, greedy BFS для Maven, greedy DFS для npm
- [ ] POST /api/v1/resolve — полноценный resolve с выбором стратегии
- [ ] p95 resolve latency < 50ms для типичного проекта (200 транзитивных deps)
- [ ] Граф зависимостей (визуализация)
- [ ] Reverse dependencies: "кто зависит от этого пакета"
- [ ] Maven repository tiers: Central + Google Maven (Tier 1), lazy fetch (Tier 2), proxy mode (Tier 3)

---

## 9. Тестовая стратегия

### 9.1 Юнит-тесты

- Нормализация метаданных: входные данные из каждого registry корректно маппятся в общую модель
- Парсинг зависимостей: PEP 508 (PyPI), semver ranges (npm/crates), POM XML (Maven), go.mod (Go), dependency groups (NuGet)
- Нормализация лицензий: маппинг произвольных строк в SPDX
- Валидация: невалидные данные отклоняются с понятными ошибками
- Пагинация: cursor-based пагинация корректно работает на граничных случаях

### 9.2 Контрактные тесты

- Каждый коннектор проходит общий тест-сьют BaseConnector
- Тесты на фикстурах (сохранённые JSON-ответы от registry) — быстро, CI
- Тесты на реальных API — медленно, nightly, с проверкой что формат не изменился

### 9.3 Интеграционные тесты

- Полный пайплайн sync → normalize → store → API для каждого коннектора
- Поиск по 10K загруженных пакетов отвечает в рамках SLA
- Docker Compose поднимается и проходит smoke-тесты
- SyncState корректно обновляется, checkpoint работает при прерывании

### 9.4 Нагрузочные тесты

- 100 RPS на эндпоинт поиска при 500K пакетов в БД — p95 <200ms
- Синхронизация не деградирует API-производительность
- Concurrent sync нескольких экосистем не вызывает deadlocks

---

## 10. Структура проекта

```
packagehub/
├── docker-compose.yml
├── .env.example
├── README.md
├── SPEC.md                          ← этот документ
├── RESOLVER.md                      ← архитектура резолвера per-ecosystem
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic/                     — миграции БД
│   ├── app/
│   │   ├── main.py                  — FastAPI app
│   │   ├── config.py                — настройки
│   │   ├── models/                  — SQLAlchemy модели
│   │   │   ├── registry.py          — Registry, PackageRegistry
│   │   │   ├── package.py
│   │   │   ├── version.py
│   │   │   ├── dependency.py
│   │   │   ├── artifact.py           — Artifact, DistTag
│   │   │   ├── author.py
│   │   │   ├── vulnerability.py
│   │   │   └── sync_state.py         — SyncState (per-registry)
│   │   ├── schemas/                 — Pydantic схемы (request/response)
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   ├── packages.py       — CRUD пакетов + артефакты
│   │   │   │   ├── registries.py     — CRUD registry + sync status
│   │   │   │   ├── search.py
│   │   │   │   ├── resolve.py        — REST API для резолвера (проксирует в gRPC)
│   │   │   │   ├── vulnerabilities.py
│   │   │   │   └── admin.py          — sync triggers, registry management
│   │   │   └── deps.py               — зависимости (auth, db session)
│   │   ├── connectors/
│   │   │   ├── base.py              — BaseConnector + SyncCursor
│   │   │   ├── pypi.py
│   │   │   ├── npm.py
│   │   │   ├── nuget.py
│   │   │   ├── crates.py
│   │   │   ├── maven.py
│   │   │   └── golang.py
│   │   ├── normalizers/
│   │   │   ├── license.py           — SPDX нормализация
│   │   │   ├── dependencies.py      — парсинг PEP 508, semver, POM, go.mod
│   │   │   └── names.py             — нормализация имён пакетов
│   │   ├── services/
│   │   │   ├── sync.py              — пайплайн синхронизации (per-registry)
│   │   │   ├── search.py            — логика поиска
│   │   │   ├── vulnerability.py     — OSV.dev клиент
│   │   │   ├── needed_packages.py   — вычисление needed_in_index (см. 3.13)
│   │   │   └── cache.py             — Redis-кэш
│   │   └── auth/
│   │       ├── tokens.py
│   │       └── oidc.py
│   └── tests/
│       ├── unit/
│       │   ├── test_normalizers/
│       │   └── test_connectors/
│       ├── integration/
│       ├── fixtures/                — сохранённые JSON-ответы от registry
│       │   ├── pypi/
│       │   ├── npm/
│       │   ├── nuget/
│       │   ├── crates/
│       │   ├── maven/
│       │   └── golang/
│       └── conftest.py
│
├── resolver/                        — Rust-микросервис
│   ├── Cargo.toml
│   ├── Dockerfile
│   ├── src/
│   │   ├── main.rs                  — gRPC server + index loader
│   │   ├── index/
│   │   │   ├── mod.rs               — in-memory index (string interning, GAV storage)
│   │   │   ├── loader.rs            — bulk load from PostgreSQL
│   │   │   └── refresh.rs           — incremental refresh + double buffering
│   │   ├── resolve/
│   │   │   ├── mod.rs               — dispatch по ecosystem
│   │   │   ├── pubgrub.rs           — PubGrub resolver (PyPI, crates.io)
│   │   │   ├── greedy_bfs.rs        — Maven/NuGet/Gradle resolver
│   │   │   ├── greedy_dfs.rs        — npm tree builder
│   │   │   └── mvs.rs               — Go MVS resolver
│   │   ├── maven/
│   │   │   ├── bom.rs               — BOM expansion + parent chain
│   │   │   ├── properties.rs        — ${...} property interpolation
│   │   │   └── scope.rs             — scope propagation matrix
│   │   ├── proto/
│   │   │   └── resolver.proto       — gRPC schema
│   │   └── cache.rs                 — Redis integration + LRU fallback
│   └── tests/
│       ├── fixtures/                — known dependency trees for validation
│       └── integration/
│
└── frontend/
    ├── package.json
    ├── src/
    │   ├── App.tsx
    │   ├── pages/
    │   │   ├── SearchPage.tsx
    │   │   ├── PackagePage.tsx
    │   │   ├── ResolvePage.tsx       — UI для dependency resolution
    │   │   └── VulnerabilitiesPage.tsx
    │   └── components/
    └── vite.config.ts
```

---

## 11. Roadmap разработки

| Этап | Что делаем | Результат |
|------|-----------|-----------|
| **0. Setup** | Docker Compose, структура проекта, CI | Пустой проект, который запускается |
| **1. Ядро** | Модели БД (все 3.x таблицы), миграции, BaseConnector, normalizers | Схема в PostgreSQL, контракты |
| **2. PyPI слайс** | PyPI-коннектор, PEP 508 парсер, sync pipeline, API, базовый UI | Работающий каталог PyPI-пакетов |
| **3. Рефакторинг** | Уточнение абстракций, license normalizer | Стабильная архитектура |
| **4. npm** | npm-коннектор (full + abbreviated), `_changes` feed | Два registry в одном каталоге |
| **5. NuGet** | NuGet-коннектор, Catalog sync, dependency groups per TFM | Три registry |
| **6. crates.io** | crates.io-коннектор, sparse index, features | Четыре registry |
| **7. Уязвимости** | OSV.dev интеграция, vulnerability API | Единый слой безопасности |
| **8. Auth** | Токены + SSO (OIDC) | Защищённый доступ |
| **9. Maven** | Maven-коннектор: Lucene index, POM XML парсер, parent/BOM resolution, reachability graph | Пять registry + оптимизированный индекс |
| **10. Go** | Go-коннектор, go.mod парсер, index.golang.org feed | Шесть registry |
| **11. Resolver** | Rust Resolver Microservice: PubGrub (PyPI/crates), greedy BFS (Maven/NuGet), greedy DFS (npm), MVS (Go) | Полноценный dependency resolver |
| **12. Polish** | Redis cache, нагрузочные тесты, мониторинг, документация, reverse deps | Production-ready |

---

## 12. Dependency Resolution Architecture

> Подробный анализ алгоритмов, сравнение подходов, расчёты ресурсов — см. **RESOLVER.md**

### 12.1 Обзор

Dependency resolution — задача построения полного дерева зависимостей с учётом ограничений версий и конфликтов. Сложность зависит от экосистемы: от NP-complete (Python, Rust — SAT-задача) до линейного обхода (npm — множественные версии допустимы).

PackageHub предоставляет два уровня resolve (см. 3.3.2):
- **Best-effort** — в PostgreSQL при sync, для UI (90% случаев)
- **Full resolve** — через Resolver Microservice, для SCA и точного анализа

### 12.2 Стратегии per-ecosystem

| Экосистема | Алгоритм | Суть | Backtracking |
|------------|----------|------|-------------|
| PyPI | PubGrub (CDCL SAT) | Одна версия пакета, constraint solving | Да |
| npm | Greedy DFS | Множественные версии допустимы, нет конфликтов* | Нет |
| Maven | Greedy BFS (nearest-wins) | Одна версия, ближайшая к корню побеждает | Нет |
| Gradle | Greedy BFS (highest-wins) | Одна версия, наивысшая побеждает | Нет |
| crates.io | PubGrub | Одна версия, как PyPI | Да |
| NuGet | Highest-wins + TFM filter | Одна версия per target framework | Нет |
| Go | MVS (Minimal Version Selection) | Минимальная удовлетворяющая версия | Нет |

*npm: peerDependencies — исключение, создают real conflicts.

### 12.3 Resolver Microservice

**Архитектура:** Rust-сервис с in-memory индексом, загружаемым из PostgreSQL.

```
┌─────────────────────────────────────────────────┐
│              Resolver Microservice               │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │         In-Memory Index                   │   │
│  │                                           │   │
│  │  PyPI:    3.2M versions, ~1.2 GB         │   │
│  │  npm:     28M versions,  ~5.0 GB (needed)│   │
│  │  Maven:   10M GAV,       ~2.4 GB (needed)│   │
│  │  NuGet:   2.2M versions, ~0.8 GB         │   │
│  │  crates:  420K versions, ~0.15 GB        │   │
│  │  Go:      750K versions, ~0.3 GB         │   │
│  │                                           │   │
│  │  Total: ~10 GB (single buffer)           │   │
│  │  Double buffer для обновления: ~20 GB     │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌───────────┐ ┌───────────┐ ┌───────────────┐  │
│  │ PubGrub   │ │ Greedy    │ │ Greedy DFS    │  │
│  │ Resolver  │ │ BFS       │ │ (npm tree     │  │
│  │ (PyPI,    │ │ (Maven,   │ │  builder)     │  │
│  │  crates)  │ │  NuGet,   │ │               │  │
│  │           │ │  Gradle)  │ │               │  │
│  └───────────┘ └───────────┘ └───────────────┘  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  LRU Fallback Cache (100K entries)        │   │
│  │  Redis Cache (resolved trees, TTL 1h)     │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

**Загрузка индекса:**
1. При старте — bulk SELECT из PostgreSQL (только `needed_in_index = true`)
2. Периодический refresh (каждые 6ч) — инкрементальный UPDATE
3. Double buffering: новый индекс строится в фоне, атомарный swap

**Fallback при промахе:**
- Пакет не найден в индексе → загрузить ВСЕ версии пакета из PostgreSQL → добавить в LRU cache
- Latency: +5-20ms (приемлемо для <2% запросов)

### 12.4 API резолвера

**Внутренний gRPC** (Resolver ↔ FastAPI):

```protobuf
service DependencyResolver {
    rpc Resolve(ResolveRequest) returns (ResolveResponse);
}

message ResolveRequest {
    string ecosystem = 1;               // "pypi", "maven", "npm", ...
    repeated DirectDependency deps = 2; // прямые зависимости
    ResolveOptions options = 3;

    // Maven-специфичные
    repeated ManagedDependency managed_deps = 10;  // dependencyManagement + BOM
    ParentPOM parent = 11;
    map<string, string> properties = 12;

    // npm-специфичные
    // (нет специфичных полей, всё в deps)

    // Python-специфичные
    PythonEnvironment python_env = 20;  // python_version, os, platform
}

message ResolveOptions {
    string strategy = 1;                // "nearest" | "highest" | "pubgrub" | "mvs"
    repeated string include_scopes = 2; // ["compile", "runtime"]
    bool include_optional = 3;
    bool include_dev = 4;
    int32 max_depth = 5;                // default 50, max 100
    repeated string excluded = 6;       // excluded packages
}

message ResolveResponse {
    ResolvedNode root = 1;
    int32 total_nodes = 2;
    int32 total_unique_packages = 3;
    repeated Conflict conflicts = 4;    // обнаруженные конфликты
    string resolve_strategy = 5;        // фактически использованная стратегия
    int64 resolve_time_us = 6;          // время резолвинга в микросекундах
}
```

**Публичный REST** (для пользователей):

```
POST /api/v1/resolve
Content-Type: application/json

{
    "ecosystem": "maven",
    "dependencies": [
        {
            "group_id": "org.springframework.boot",
            "artifact_id": "spring-boot-starter-web",
            "version": "3.2.0",
            "scope": "compile"
        }
    ],
    "managed_dependencies": [...],
    "options": {
        "strategy": "nearest",
        "include_scopes": ["compile", "runtime"],
        "max_depth": 20
    }
}
```

### 12.5 Ресурсы и производительность

| Экосистема | Index RAM | p50 | p95 | p99 | Throughput (4 cores) |
|------------|-----------|-----|-----|-----|---------------------|
| PyPI | 1.2 GB | 3 ms | 15 ms | 50 ms | 1500 req/s |
| npm | 5.0 GB | 5 ms | 40 ms | 200 ms | 800 req/s |
| Maven | 2.4 GB | 1 ms | 5 ms | 20 ms | 5000 req/s |
| NuGet | 0.8 GB | 1 ms | 5 ms | 15 ms | 5000 req/s |
| crates.io | 0.15 GB | 2 ms | 10 ms | 30 ms | 3000 req/s |
| Go | 0.3 GB | 0.5 ms | 3 ms | 10 ms | 8000 req/s |

**Суммарно:** ~10 GB индекс, рекомендуется 32 GB RAM (double buffer + runtime + overhead).

**Deployment topology (рекомендуемая — 3 группы по профилю нагрузки):**

```
"Heavy" pod (npm):         RAM 16 GB, CPU 4 cores, replicas 2-4
"Medium" pod (PyPI+Maven): RAM 8 GB,  CPU 4 cores, replicas 2-3
"Light" pod (NuGet+crates+Go): RAM 4 GB, CPU 2 cores, replicas 2

Один бинарник, флаг --ecosystems определяет загружаемые экосистемы.
```

**Стоимость (baseline / peak):**
- Heavy: 2-4 × r6g.xlarge = $200-400/мес
- Medium: 2-3 × r6g.xlarge = $200-300/мес
- Light: 2 × r6g.medium = $60/мес
- Redis cache (shared): $50/мес
- Total: $510-810/мес
- С Redis cache (85% hit rate): эффективный throughput ×5-10

### 12.6 Phased Rollout

| Фаза | Что | Когда |
|------|-----|-------|
| MVP | Best-effort resolve в PostgreSQL (текущая 3.3.2 Level 1) | Этап 2-6 |
| V1 | Resolver Microservice: PubGrub для PyPI/crates, greedy для Maven/npm | Этап 9-10 |
| V2 | Redis cache, Redis-backed resolved tree API, Tier 3 proxy mode | Этап 11 |
| V3 | Forking resolver (PyPI multi-env), npm peer deps conflict detection | Backlog |

---

## Приложение A. Сравнение метаданных по экосистемам

| Поле | PyPI | npm | Maven | Go | crates.io | NuGet |
|---|---|---|---|---|---|---|
| Описание | ✅ | ✅ | ✅ (POM) | ❌ | ✅ | ✅ |
| Лицензия | ~~ (хаос) | ~~ (SPDX) | ✅ (POM) | ❌ | ✅✅ (SPDX обяз.) | ✅ (SPDX) |
| Авторы | ~~ | ✅ | ✅ (POM) | ❌ | ❌ (убрали) | ✅ |
| Зависимости | ✅ PEP 508 | ✅ semver | ✅ POM XML | ✅ go.mod | ✅ semver | ✅ per TFM |
| Уязвимости в API | ✅ OSV | ❌ | ❌ | ❌ | ❌ | ✅ |
| Загрузки | ❌ (отдельно) | ❌ (отдельно) | ❌ | ❌ | ✅ | ✅ |
| Change feed | ✅ serial | ✅✅ _changes | ~~ Lucene daily | ✅✅ NDJSON | ~~ sparse | ✅✅ Catalog |
| Полнота за 1 запрос | ✅✅ | ✅ | ❌ (2 шага) | ❌ (3 шага) | ✅ (2 шага) | ✅ (2 шага) |

## Приложение B. Формат зависимостей по экосистемам

| Экосистема | Формат | Пример | Типы |
|---|---|---|---|
| PyPI | PEP 508 | `requests>=2.20; python_version>="3.6"` | runtime, extras (optional) |
| npm | semver dict | `{"lodash": "^4.17.0"}` | dependencies, devDependencies, peerDependencies, optionalDependencies |
| Maven | POM XML | `<dependency><groupId>...</groupId><scope>test</scope></dependency>` | compile, test, runtime, provided, system |
| Go | go.mod | `require golang.org/x/text v0.3.0 // indirect` | direct, indirect |
| crates.io | semver + kind | `{"name": "serde", "version_req": "^1.0", "kind": "normal"}` | normal, dev, build + optional + target(cfg) |
| NuGet | per TFM groups | `{"targetFramework": "net6.0", "dependencies": [...]}` | per target framework |

## Приложение C. Оценка объёма хранения

```
Таблица              Строк          Размер на диске
────────────────────────────────────────────────────
packages             ~4.5M          ~3 GB
package_versions     ~80M           ~12 GB
dependencies         ~170M          ~25 GB
authors              ~2M            ~0.3 GB
package_authors      ~5M            ~0.5 GB
vulnerabilities      ~300K          ~0.2 GB
package_vulns        ~1M            ~0.1 GB
registries           ~20            negligible
package_registries   ~5M            ~0.5 GB
dist_tags            ~3M            ~0.3 GB
sync_states          ~10            negligible
artifacts            ~107M          ~50 GB (без blob)
  metadata_blob      (в artifacts)  ~15 GB (только needed)
────────────────────────────────────────────────────
Итого:                              ~107 GB (без blob)
                                    ~122 GB (с blob для needed)
Индексы PostgreSQL:                 ~40-60 GB
────────────────────────────────────────────────────
Total disk:                         ~160-180 GB
Рекомендация:                       256 GB SSD (gp3)
Стоимость:                          ~$20/мес (EBS gp3)
```
