# PackageHub — Мультиэкосистемность

> Расширяем курируемый каталог с PyPI на RPM, npm, Maven, NuGet, Go и crates.io, с несколькими источниками в каждой экосистеме. Для каждой экосистемы нужны каталог, ежедневное обновление, дерево зависимостей, полный resolve и дайджест. Уязвимости — расширение на потом (§10).

**Версия:** 0.5-draft
**Дата:** 2026-10-28
**Changelog:**
- 0.5 — валидация по опыту Pulp, Nexus, Artifactory, deps.dev, socket.dev (§17). Исправления: варианты зависимостей по источникам (найдено на `torch 2.9.1`), режимы routing `allow/block/priority`, ошибки источника не кэшируются как «нет пакета», circuit breaker, ревизия конфига в курсоре RPM, ошибки на уровне узла графа, эмулируемые версии нативных инструментов, bundled-зависимости npm, схемы jsonb по экосистемам.
- 0.4 — решения по Q14–Q17: зависимости без PEP 658 недоступны, Gradle Module Metadata — известное ограничение, источники добавляем вручную, при расхождении — приоритетный источник.
- 0.3 — несколько источников в экосистеме: протоколы, routing, профили источников, слияние (§6). Целевая схема БД проверена на Postgres, а `sort_key` — по эталонам (§5, [design/](./design/)). Контракт плагинов уточнён и проверен `mypy --strict`. Найдены дефекты текущей схемы (§5.2). Решения по Q6, Q7, Q11–Q13.
- 0.2 — уязвимости вынесены в расширение. Решения по Q1–Q5, Q8–Q10. RHEL/Fedora и модульность EL8. npm через `_changes`.

**Статус:** проект, к обсуждению.
**База:** `main`. Работают PyPI-only каталог, ежедневное обновление и дайджест ([SPEC-DAILY-UPDATE.md](./SPEC-DAILY-UPDATE.md)), resolve для PyPI.
**Заменяет:** часть [SPEC.md](./SPEC.md) и [RESOLVER.md](./RESOLVER.md), подробности в §0.2.
**Артефакты проектирования:** [design/schema-target.sql](./design/schema-target.sql), [design/ecosystem_contract.py](./design/ecosystem_contract.py), [design/schema-check/](./design/schema-check/).

---

## 0. Решения и рамки

### 0.1 Зафиксированные решения

| # | Решение | Следствие |
|---|---|---|
| D1 | **Курируемый каталог** для всех экосистем. Пакет попадает в БД через seed, admin API или лениво (дерево и resolve) | Зеркала реестров нет. Работаем с десятками тысяч пакетов, а не с миллионами. Один Postgres, без in-memory индекса |
| D2 | **Первая волна:** RPM, npm, Maven, NuGet, Go, crates.io, плюс уже готовый PyPI | Нужна плагинная архитектура: 7 реализаций одного контракта |
| D3 | **Полный resolve сразу** для каждой экосистемы | В каждый плагин входит свой алгоритм разрешения, проверенный на эталонах нативных инструментов (§14) |
| D4 | **Корпоративные репозитории** (Nexus, Artifactory) закладываем в модель, но не реализуем | Модель источников общая (§6), авторизация нужна уже для RHEL (§6.4) |
| D5 | **Обновление раз в сутки**, только пакетов из каталога | Самый дешёвый способ обнаружить изменения выбирается для каждого **протокола** источника (§8) |
| D6 | **Уязвимости не собираем** для новых экосистем. В целевом виде они будут приходить из другого источника, в формате, близком к OSV | Только точка расширения (§10). Существующий сбор уязвимостей PyPI не трогаем |
| D7 | **RPM: целевой дистрибутив — RHEL** 8 и 9, с модульностью. Разработка и тесты — на Rocky Linux, AlmaLinux и зеркалах Fedora. Источник RHEL появится позже | Источники RPM описываются конфигурацией registry, RHEL подключается тем же коннектором (§6.4) |
| D8 | **deps.dev** — только вспомогательный инструмент разработчика | Не используется ни в рантайме, ни в CI, ни в эталонах тестов |
| D9 | **В экосистеме несколько внешних источников:** PyPI + PyTorch и другие индексы, десятки Maven-репозиториев и Gradle Plugin Portal, дополнительные npm-реестры | Источник (`registry`) с протоколом, правила routing, профили источников, слияние данных из нескольких источников (§6) |

### 0.2 Что устаревает в SPEC.md и RESOLVER.md

Эти разделы описывали полное зеркало реестров. При D1 они больше не действуют:

- SPEC.md §2.1, колонка «Sync-механизм». Механизмы описаны в §8 этого документа.
- SPEC.md §3.13 (needed packages), §4.1 (Rust Resolver Microservice), §6.1–6.2, §8.2–8.3, §11, §12.3–12.6, приложение C.
- SPEC.md §3.5–3.6 (Registry, PackageRegistry) уточнены в §6. Tier'ы заменены протоколом, routing и профилями.
- RESOLVER.md: in-memory индекс, расчёты RAM и стоимости. Описания алгоритмов по экосистемам остаются справочником.

### 0.3 Что не делаем в этой волне

- Зеркало реестров и полную синхронизацию.
- Скачивание и разбор файлов артефактов (jar, nupkg, tgz, wheel, rpm). Метаданные берём из API, индексов и метаданных-документов реестров: POM, `go.mod`, PEP 658 `.metadata`. Скачивание пакетов понадобится позже для классификатора. Под него заложены таблица `artifacts` с контрольными суммами и `source_documents`. Хранилище файлов тогда делаем адресуемым по содержимому (ключ — sha256, один файл на все источники и версии), как Artifact в Pulp.
- Уязвимости (D6) — только точка расширения.
- Корпоративные репозитории (D4), SSO, reverse dependencies, SBOM-импорт.

---

## 1. Цели и критерии готовности

Экосистема считается подключённой, когда выполнены все пункты:

1. **Каталог.** Пакет добавляется по purl (§4) через seed, admin API и CLI. Карточка показывает метаданные, версии, источники, лицензию (SPDX, если её удалось вывести) и артефакты.
2. **Источники.** Подключены все источники экосистемы из §6.3, routing и профили работают (§6).
3. **Ежедневное обновление.** Изменения обнаруживаются механизмом своего протокола (§8). События пишутся в `package_events`, дайджест фильтруется по экосистеме.
4. **Дерево зависимостей** (best-effort, уровень 1) учитывает окружение (§7.3) и профиль источников.
5. **Resolve.** `POST /resolve` строит граф по правилам экосистемы с учётом профиля. На эталонных проектах результат совпадает с нативным инструментом (§14.2).
6. **Тесты:** контрактный сьют плагина на фикстурах, эталоны резолвера, интеграционный прогон на стенде.

---

## 2. Модель покрытия (курируемый каталог)

### 2.1 Как пакет попадает в каталог

| Путь | Источник | `packages.origin` |
|---|---|---|
| Seed | Список purl в настройках или CLI `seed` | `seed` |
| Ручное добавление | `POST /admin/packages` (список purl) | `manual` |
| Через зависимости | Seed обходит обязательные зависимости (runtime) с ограничением глубины, как сейчас в PyPI | `dependency` |
| Lazy fetch | Дерево или resolve встретили пакет, которого нет в БД (лимиты в §9.1) | `discovered` |

- `origin` — только **происхождение**: как пакет попал в каталог. На отслеживание и дайджест он не влияет. Ежедневное обновление следит за всеми пакетами каталога, дайджест по умолчанию показывает все (Q6).
- `origin` пригодится, чтобы чистить каталог от разово подтянутого и чтобы разбираться, откуда взялся пакет.
- Как уменьшать шум (прежде всего в npm) — отдельная задача, не этой спеки.

### 2.2 Исключение: RPM

В RPM зависимости — это **capabilities** (`libssl.so.3()(64bit)`, `/usr/bin/sh`, `pkgconfig(zlib)`), а не имена пакетов. Определить, какой пакет удовлетворяет capability, можно только по метаданным **всего репозитория**. Поэтому:

- каталог RPM остаётся курируемым: карточки, события и дайджест есть только у пакетов из БД;
- метаданные репозиториев целиком (repodata: `primary`, `filelists`, `modules.yaml`) хранятся в файловом кэше `rpm-worker`, а не в Postgres. Через worker идут и дерево (capability → провайдер), и resolve (§9.8). Это единственная экосистема, где индекс шире каталога.

---

## 3. Архитектура: плагины экосистем и адаптеры источников

### 3.1 Три слоя

| Слой | Сколько экземпляров | Отвечает за |
|---|---|---|
| `EcosystemPlugin` | Один на экосистему | Семантика экосистемы: имена и purl, схема версий, окружение, routing по источникам, слияние данных из нескольких источников, связь зависимости с пакетом, резолвер |
| `SourceAdapter` | Один на **источник** (по `registry.protocol`) | Чтение конкретного источника: пакет, зависимости версии, проверка наличия. pypi.org и PyTorch index — два адаптера одного плагина |
| `ChangeDetector` | Создаётся адаптером | Ежедневный ответ «что изменилось» для своего источника: лента с курсором, условные GET или repodata |

Полный контракт — [design/ecosystem_contract.py](./design/ecosystem_contract.py), проверен `mypy --strict`. Ключевые решения:

- **Источник отвечает только за себя.** `fetch_package` возвращает то, что знает **один** источник. У PEP 503-индекса есть файлы, но нет описания и лицензии, поэтому все поля пакетного уровня необязательные. Объединяет данные `plugin.merge()` по правилам §6.5.
- **`SourceLookup`.** Адаптер может достать вспомогательный документ из другого источника того же профиля: parent POM или BOM часто лежат не там, где артефакт.
- **`sources_for(ref, profile)`.** Routing вычисляет плагин, а не SQL. Правило `exclusive` нельзя выразить простым join, и проверка схемы это подтвердила (§5.4). Дальше SQL фильтрует версии по готовому набору источников.
- **`dependency_target()`.** Связывает зависимость с пакетом для дерева. Для всех экосистем это тождество, кроме RPM: там провайдера capability определяет `rpm-worker`.
- **`ChangeSet.reconcile_all`.** Детектор с лентой (npm `_changes`) сообщает, что курсор потерян и нужна полная сверка условными GET.
- **`VersionScheme.validate()`** отбраковывает версии, которые экосистема считает невалидными: в npm это числа больше 2⁵³−1 и ведущие нули в prerelease-идентификаторах (проверено против node-semver). Строка такой версии остаётся в `package_versions` с запасным `sort_key` ниже всех валидных и `raw_metadata.invalid_version=true`; резолвер, «последняя версия» и связи уровня 1 её не используют (IMPL T15).

### 3.2 Компоненты

```
             ┌─────────────┐   ┌──────────────────────────────────────────┐
  Web UI ───▶│  REST API   │──▶│ core: pipeline, merge, events, digest,   │──▶ PostgreSQL
             │  (FastAPI)  │   │ search, tree, resolve facade, licenses   │
             └──────┬──────┘   └───────────────┬──────────────────────────┘
                    │                          │ get_plugin(eco) → adapter(registry)
                    │          ┌───────────────▼──────────────────────────┐
                    │          │ ecosystems/<eco>/plugin.py                │
                    │          │   adapters/<protocol>.py, changes.py      │
                    │          └───────────────┬──────────────────────────┘
                    ▼                          │
             Redis + Celery ── очереди: default, src.<registry-slug> (rate limit), rpm
                    │          (Redis: negative cache наличия пакета в источнике, кэш resolve)
                    └──▶ rpm-worker (Rocky 9 + python3-solv + libmodulemd, repodata на volume)
```

- **Очереди по источникам**, а не по экосистемам. У каждого источника свои лимиты: API crates.io — 1 запрос/с, у Maven Central — свои пределы. Медленный источник не тормозит остальные.
- **rpm-worker** — отдельный образ: у libsolv нет wheel на PyPI. Он обслуживает задачи `rpm.fetch_package`, `rpm.detect_changes`, `rpm.whatprovides`, `rpm.resolve`. Адаптер RPM в API — тонкий клиент этих задач.
- Резолверы остальных экосистем работают в процессе API (asyncio, lazy fetch через адаптеры).

### 3.3 Раскладка кода

```
backend/app/
├── ecosystems/
│   ├── base.py               — контракт (design/ecosystem_contract.py)
│   ├── registry.py           — регистрация плагинов, get_plugin()
│   ├── purl.py, routing.py, merge.py   — общие реализации, плагины их переопределяют
│   ├── pypi/   plugin.py  coordinates.py  versions.py  environment.py  changes.py  resolver.py
│   │           adapters.py (pypi-warehouse; pypi-simple в E0.5 — модуль делится на adapters/, когда адаптеров станет два)
│   ├── npm/    adapters/registry.py  changes.py (_changes, etag)
│   ├── maven/  adapters/maven2.py  pom.py (effective model)  resolver.py (maven | gradle)
│   ├── nuget/  adapters/v3.py  frameworks.py (TFM)
│   ├── go/     adapters/goproxy.py
│   ├── crates/ adapters/sparse.py
│   └── rpm/    adapters/repodata.py (клиент rpm-worker)
├── fetcher/pipeline.py       — общий pipeline: снимок → merge → upsert → события
├── tasks/                    — seed, daily_update, presence_probe, retry: цикл по источникам
rpm_worker/                   — Dockerfile, worker.py, modules.py, solver.py
```

---

## 4. Идентификация пакетов: purl

Канонический внешний идентификатор — [Package URL (purl)](https://github.com/package-url/purl-spec), стандарт для OSV, CycloneDX и SPDX. В БД хранятся `ecosystem`, `namespace` (пустая строка, если namespace нет) и `normalized_name` (**локальная** часть имени). Полное имя для отображения и purl собираются на лету.

| Экосистема | namespace | name | Нормализация | purl |
|---|---|---|---|---|
| pypi | — | project | PEP 503: lowercase, `[-_.]+` → `-` | `pkg:pypi/flask` |
| npm | scope без `@` | имя без scope | Нет: новые имена lowercase, старые (`JSONStream`) хранятся как есть | `pkg:npm/%40angular/core` |
| maven | groupId | artifactId | Без изменений, регистр важен | `pkg:maven/org.springframework/spring-core` |
| nuget | — | id | `normalized_name` lowercase, `name` — исходное написание | `pkg:nuget/newtonsoft.json` (имя в purl NuGet без учёта регистра, пишем в нижнем) |
| go | — | module path | Без изменений; в URL к proxy заглавные экранируются (`!x`). Мажорные версии `/v2` — **другой** модуль | `pkg:golang/github.com/redis/go-redis/v9` |
| crates | — | crate | Нет: имя как опубликовано (`serde_json`, `Inflector`). crates.io не допускает двух крейтов, отличающихся только регистром или `-`/`_`; ввод `serde-json` приводится к каноническому имени через API (Q21) | `pkg:cargo/serde_json` |
| rpm | дистрибутив и мажорный релиз: `rhel-9`, `rocky-9`, `epel-9`, `fedora-41` | имя бинарного пакета | Без изменений | `pkg:rpm/redhat/openssl?distro=rhel-9` |

- Источник, из которого взята версия, в purl можно указать qualifier'ом `repository_url`. Например, `pkg:pypi/torch@2.5.1%2Bcu121?repository_url=https:%2F%2Fdownload.pytorch.org%2Fwhl%2Fcu121` (каноническая форма purl кодирует `/` в значениях qualifiers; на входе принимается и некодированная).
- **RPM.** `openssl` в RHEL 9, Rocky 9 и Fedora 41 — разные пакеты с разной историей сборок, поэтому дистрибутив входит в идентичность. Каталог стенда разработки (Rocky, Alma, Fedora) и целевой среды (RHEL) различается именно namespace. Namespace задаёт конфигурация источника (`config.namespace`). EPEL получает свой namespace `epel-9`. Архитектура — свойство артефакта, поток модуля (`nodejs:18`) — свойство версии.

---

## 5. Модель данных

### 5.1 Целевая схема

Полный DDL — [design/schema-target.sql](./design/schema-target.sql). Ниже назначение таблиц, которые затрагивает мультиэкосистемность. Остальные (`authors`, `vulnerabilities`, `package_events`) не меняются.

| Таблица | Что хранит | Ключевые решения |
|---|---|---|
| `registries` | Источник: экосистема, **протокол**, `base_url`, `config`, `routing`, `priority`, `enabled`, `auth_type` + `credentials_ref` | `UNIQUE (id, ecosystem)` — цель составных внешних ключей. `CHECK`: при авторизации обязательна ссылка на секрет, сами секреты в БД не хранятся |
| `source_profiles`, `source_profile_registries` | Упорядоченный набор источников для дерева, resolve и поиска новых пакетов | Не больше одного профиля по умолчанию на экосистему (частичный уникальный индекс). Составные FK запрещают положить в профиль источник чужой экосистемы |
| `packages` | Пакет в экосистеме | `namespace NOT NULL DEFAULT ''`. Ключ `(ecosystem, namespace, normalized_name)`. Основной источник — составной FK той же экосистемы. `origin`, `deprecation jsonb`. `latest_version` допускает NULL (проект без релизов) |
| `package_registries` | В каких источниках есть пакет + состояние обнаружения изменений по каждому: `upstream_revision`, `etag`, `last_modified`, `checked_at`, `missing_since` | Заменяет `packages.last_serial`. Флаг `is_primary` убран: основной источник один, `packages.primary_registry_id` |
| `package_versions` | Версия | `sort_key bytea` (§5.4), `is_prerelease`, `status` (без `deprecated`), `status_reason`, `deprecation jsonb`, `engines jsonb`, `metadata_registry_id` (откуда взяты зависимости), `dependencies_state` (`pending/hydrated/unavailable`) |
| `version_sources` | Какие источники публикуют версию и её статус в каждом | Основа resolve с учётом профиля: версия видна, только если её публикует источник из набора `sources_for()`. `metadata_digest` выявляет источники, которые расходятся в зависимостях одной версии |
| `artifacts` | Файлы версии в конкретном источнике | Ключ `(version_id, registry_id, filename)`: одинаковый файл в двух источниках — две строки. `arch`, все контрольные суммы, `has_metadata_file` (PEP 658, Gradle `.module`) |
| `source_documents` | Вспомогательные документы, у которых нет версии в каталоге: parent POM, BOM, `go.mod` транзитивных модулей для MVS, PEP 658 `.metadata` | Ключ `(registry_id, path)` + `etag`. Без этой таблицы parent POM было бы некуда положить (§5.2, п. 5) |
| `dependencies` | Зависимость версии | `dependency_namespace NOT NULL DEFAULT ''`, `alias`, `scope` (исходный тип), `details jsonb` (exclusions, features, подсказка источника), `environment_markers` с типом. `variant_registry_id`: NULL — канонический набор, иначе вариант источника, чьи метаданные этой версии отличаются (§6.5). Индекс по `(ecosystem, namespace, name)` для связей уровня 1 |
| `dist_tags` | npm dist-tags | Ключ `(package_id, tag)` вместо суррогатного id |
| `package_events` | Журнал событий | `version` → `text`, новые виды `release_deprecated` и `source_added` |
| `sync_states` | Курсор ленты **по источнику**: serial, `seq`, commit timestamp, `since`, ревизия `repomd` | Одна строка на источник |
| `runs` | Журнал операций (IMPL R1): `operation` (daily_update, seed, presence_probe, retry), `ecosystem` | Заменяет `update_runs` и поля прогресса в `sync_states`; `per_registry jsonb` со статистикой по источникам |
| `sync_failures` | Ошибки синхронизации | Ключ `UNIQUE NULLS NOT DISTINCT (operation, ecosystem, registry_id, package_ref)`; `package_ref` — purl без версии |

### 5.2 Дефекты текущей схемы, найденные при проверке

| # | Где | Проблема | Как исправляет целевая схема |
|---|---|---|---|
| 1 | `packages`: уникальный индекс `(ecosystem, namespace, normalized_name)` | `namespace` допускает NULL, а NULL в уникальном индексе Postgres не равны друг другу. **Для пакетов без namespace дубликаты не запрещены.** Сегодня это все пакеты PyPI, дальше — Go, crates.io, NuGet | `namespace NOT NULL DEFAULT ''`. Проверено: дубликат отвергается |
| 2 | `package_versions.features` | Флаг `dependencies_hydrated` хранится в одном JSON с данными features. У crates.io в `features` будут настоящие features пакета | Отдельная колонка `dependencies_state` |
| 3 | `package_versions` | Два одинаковых уникальных индекса на `(package_id, version)` | Один ключ |
| 4 | `version_status` | `deprecated` — статус жизненного цикла, хотя deprecation с ним не связана: версия npm может быть одновременно deprecated и unlisted | `deprecation jsonb` отдельно, `deprecated` из статусов убран |
| 5 | `artifacts.metadata_blob` (план SPEC.md) | Parent POM и `go.mod` транзитивных модулей не принадлежат версиям каталога, а `artifacts` ссылается на версию | Таблица `source_documents` |
| 6 | Связь пакета и источника | Ничто не мешает связать пакет PyPI с npm-реестром или сделать основным источником Maven-репозиторий | Составные FK `(registry_id, ecosystem)`. Проверено: такие вставки отвергаются |
| 7 | `sync_failures` | Ключ `(operation, package_name)`: имена разных экосистем и источников конфликтуют | Ключ с экосистемой, источником и purl |

### 5.3 Миграции

1. **0006 (схема, обратно совместима).**
   - Новые типы, колонки и таблицы.
   - `namespace`: NULL → `''` и `NOT NULL`. Перед этим проверяем, что дубликатов нет.
   - Переносы: `requires_python` → `engines`, `last_serial` → `package_registries.upstream_revision`, `features.dependencies_hydrated` → `dependencies_state`.
   - Миграция данных заполняет `sort_key` и `is_prerelease` по схеме PyPI.
   - Создаются `version_sources` для существующих версий (источник `pypi`) и профиль по умолчанию `pypi`.
2. **0007 (после переключения кода).** Удаление старых колонок и дубля индекса, новые ключи.

Обе миграции проверяем на копии базы стенда, как миграцию 0005.

### 5.4 Как проверена схема

- **DDL применён на Postgres 16** стенда во временной базе. Сценарии [design/schema-check/gen_scenarios.py](./design/schema-check/gen_scenarios.py) охватывают все экосистемы:
  - PyPI + PyTorch index: один `torch`, версии из двух источников, CUDA-сборка только в профиле `cuda121`;
  - scoped-пакет npm из корпоративного реестра;
  - Maven: GA в двух репозиториях и маркер Gradle-плагина;
  - RPM: `openssl` в RHEL и Rocky как два пакета, `superseded`, модульные потоки `nodejs`, зависимости по arch;
  - Go: `/v8` и `/v9` как разные модули и псевдоверсии;
  - NuGet: регистр id и unlisted;
  - crates.io: переименованная и feature-зависимая зависимость.
- **Результат: 21/21 проверка.** 8 негативных (недопустимые вставки отвергаются) и 13 позитивных (запросы дают ожидаемый порядок и набор), включая варианты зависимостей `torch 2.9.1` по источникам (§6.5).
- **Найденный пробел.** Наивный join версий с профилем пропускает сборки PyPI в профиль, где `torch` закреплён за CUDA-индексом. Отсюда правило: routing вычисляет плагин (`sources_for`), SQL фильтрует по готовому набору источников. Сценарий на это есть.

**Схемы jsonb.** `details`, `environment_markers`, `engines`, `features`, `raw_metadata` типизируются Pydantic-моделями каждой экосистемы. Плагин валидирует данные при записи. Это ответ на главный аргумент в пользу отдельных таблиц на тип контента (как в Pulp): без схем общий jsonb расползается.

### 5.5 Схемы версий и `sort_key`

`sort_key` — байтовый ключ, порядок которого совпадает с порядком версий экосистемы. Благодаря ему `ORDER BY`, «последняя стабильная», курсоры и выбор кандидатов работают в SQL. Прототипы кодировщиков ([design/schema-check/sortkey_proto.py](./design/schema-check/sortkey_proto.py)) сверены попарным сравнением с эталонными реализациями:

| Схема | Экосистемы | Эталон | Результат |
|---|---|---|---|
| SemVer 2.0 | npm, crates, go | node-semver 6.3.1 | 29 версий, 841 пара, 0 расхождений (prerelease-идентификаторы, build metadata, псевдоверсии Go) |
| RPM EVR | rpm | `rpm.labelCompare` (rpm 4.16, Rocky 9) | 28 версий, 784 пары, 0 расхождений (`~`, `^`, эпохи, `el9_4`, модульные сборки, UEK) |
| PEP 440 | pypi | `packaging` | 38 версий, 1444 пары, 0 расхождений (dev/pre/post, local `+cpu`/`+cu121`, эпохи) |
| Maven ComparableVersion | maven | — | Не проверено: нужен эталон на Java (`maven-artifact`). Делается на E5 |
| NuGetVersion | nuget | NuGet.Versioning 6.11.1 | 153 версии попарно, 232 диапазона (Satisfies, FindBestMatch), 0 расхождений; байтовый ключ с четвёртым числом и тремя видами меток (NuGet считает `-1` числом) |

Если для Maven или NuGet байтовый ключ не сойдётся с эталоном, есть запасной вариант: ранг версии внутри пакета, пересчитываемый при добавлении версий. Он тоже хранится в `bytea`, так что схема не меняется.

| Экосистема | Схема | Диапазоны | Prerelease | Реализация |
|---|---|---|---|---|
| pypi | PEP 440 | PEP 440 specifiers | a/b/rc/dev | `packaging` (есть) |
| npm | SemVer 2.0, числа ≤ 2⁵³−1 | node-semver: `^ ~ x \|\| -`, hyphen ranges | `-tag`; по умолчанию матчится только в своей тройке major.minor.patch | Своя, проверенная тестами node-semver (Q8) |
| crates | SemVer 2.0, u64 | Cargo: голая версия = `^`, `~ * = >= <` | `-tag` | Своя |
| go | SemVer с `v`, псевдоверсии, `+incompatible` | Диапазонов нет (MVS) | `-tag`, псевдоверсии | Своя |
| maven | ComparableVersion | `[1.0,2.0)`, `(,1.0]`, `1.0` как «мягкая» рекомендация | alpha/beta/milestone/rc/snapshot | Своя |
| nuget | NuGetVersion (SemVer 2 + 4-й сегмент, регистронезависимо) | `[1.0,2.0)`, `1.0` = `>=1.0`, floating `1.*` | `-tag` | Своя |
| rpm | EVR + `rpmvercmp` | flags `LT/LE/EQ/GE/GT` у capability | Нет (`~` сортируется раньше) | Своя (~60 строк) |

Классификация обновления (`major/minor/patch/pre/other`) переезжает в `VersionScheme.bump()`. Псевдоверсии Go → `pre`. У RPM `major/minor/patch` определяются по `version`, а изменение только `release` даёт `patch`.

---

## 6. Источники (registries)

### 6.1 Протоколы

Как говорить с источником, определяет протокол, а не экосистема. В одной экосистеме могут быть источники разных протоколов, и наоборот: Nexus и Artifactory говорят теми же протоколами, что и публичные реестры.

| Протокол | Источники | Что умеет | Чего нет |
|---|---|---|---|
| `pypi-warehouse` | pypi.org | JSON API (метаданные, зависимости), Simple API с `_last-serial` | — |
| `pypi-simple` | download.pytorch.org (`/whl/cu121`, `/whl/cpu`, …), pypi.nvidia.com, devpi | Список файлов (HTML PEP 503, иногда JSON PEP 691), `data-upload-time`, yanked, ETag страницы проекта | Описания, лицензии, serial. Зависимости — только через PEP 658 `.metadata`. **Проверено на `torch`:** у файлов 2023–2024 годов метаданных нет совсем (0 из 2055), у файлов 2025–2026 — есть у 93% (1530 из 1647) |
| `npm-registry` | registry.npmjs.org, другие реестры | packument (полный и abbreviated), dist-tags, ETag | Лента `_changes` — только у npmjs (`replicate.npmjs.com`) |
| `maven2` | Maven Central, Google Maven, Gradle Plugin Portal, JBoss, Spring, Confluent, … | `maven-metadata.xml` (версии) с ETag, POM, `.module` (Gradle Module Metadata) | Ленты изменений, описания репозитория. Список GA по репозиторию недоступен |
| `nuget-v3` | nuget.org, другие фиды | Registration (версии, зависимости по TFM), ETag | Catalog (лента) — только у nuget.org |
| `goproxy` | proxy.golang.org, Athens, … | `@v/list`, `.info`, `.mod` | Лента `index.golang.org` — только у Google |
| `cargo-sparse` | index.crates.io, альтернативные sparse-реестры | Индекс по крейту с ETag | API метаданных — только у crates.io |
| `rpm-repodata` | RHEL CDN или Satellite, Rocky, Alma, Fedora, EPEL | `repomd.xml`, `primary`, `filelists`, `modules.yaml` | — |

### 6.2 Routing и профили

**Routing** (`registries.routing`) описывает, какие пакеты источник обслуживает:

| Экосистема | Правило | Пример |
|---|---|---|
| npm | `scopes` | `{"mode": "exclusive", "scopes": ["corp"]}` — `@corp/*` берём **только** из корпоративного реестра |
| maven | `group_patterns` (как `includeGroup` в Gradle) | Google Maven: `{"group_patterns": ["androidx.*", "com.android.*", "com.google.android.*"]}` |
| pypi | `packages` (как index pinning в uv) | `{"mode": "exclusive", "packages": ["torch", "torchvision", "torchaudio"]}` для CUDA-индекса |
| все | нет правила | Источник обслуживает любые пакеты |

Режимы правила (`mode`):

| Режим | Смысл | Аналог |
|---|---|---|
| `allow` (по умолчанию, если есть паттерны) | Источник обслуживает только совпавшие пакеты | Include patterns в Artifactory, ALLOW в Nexus |
| `block` | Источник никогда не обслуживает совпавшие пакеты | Exclude patterns в Artifactory, BLOCK в Nexus |
| `exclusive` | Совпавший пакет берётся **только** из этого источника, даже если его там нет | uv index pinning, scope-реестр npm |
| `priority` | Если пакет **есть** в этом источнике, остальные для него игнорируются; паттерны не нужны | Priority Resolution в Artifactory |

- `exclusive` и `priority` защищают от dependency confusion (подмены пакета одноимённым из другого источника). `priority` подходит для источника внутренних пакетов, имена которых заранее не перечислить. Для `priority` наличие пакета проверяется на этапе обнаружения (§6.6) и запоминается в `package_registries`.
- **Профиль** (`source_profiles`) — упорядоченный список источников экосистемы плюс переопределения routing. Аналоги: `--index-url`/`--extra-index-url` pip, `<repositories>` Maven, `.npmrc` со scope-реестрами. У экосистемы есть профиль по умолчанию, дополнительные выбираются в дереве и в resolve (`profile=cuda121`).
- **`sources_for(ref, profile)`** возвращает упорядоченный набор источников для пакета. Порядок задаёт профиль, затем применяются фильтры routing и правило `exclusive`.

### 6.3 Известные источники первой волны

| Экосистема | Источники | Примечания |
|---|---|---|
| pypi | pypi.org; PyTorch index (по варианту: `cpu`, `cu121`, `cu124`, …); другие внешние индексы (NVIDIA и т. п.) | Каждый вариант PyTorch — отдельный источник со своим routing |
| npm | registry.npmjs.org; дополнительные внешние реестры | Добавляются вручную (Q16) |
| maven | Maven Central, Google Maven, Gradle Plugin Portal, остальные внешние репозитории («целый вагон») | Добавляются вручную (Q16). Snapshot-репозитории не подключаем |
| nuget | nuget.org | Дополнительные фиды — по запросу |
| go | proxy.golang.org (+ index.golang.org) | — |
| crates | index.crates.io (+ API crates.io) | — |
| rpm | Rocky и Alma 8/9 (BaseOS, AppStream, CRB/PowerTools), EPEL 8/9, Fedora N и N-1 (`fedora`, `updates`); позже RHEL 8/9 (BaseOS, AppStream, CodeReady Builder) | §6.4 |

**Источники добавляем вручную (Q16).** Автоматического обнаружения источников нет. Администратор заводит источник через admin API или CLI: протокол, URL, приоритет, routing, авторизация. Перед включением выполняется проверка подключения (`test`): адаптер запрашивает служебный документ протокола (`repomd.xml`, `index.json` NuGet, корень Simple, `maven-metadata.xml` известного GA) и проверяет авторизацию. Новый источник включается выключенным (`enabled=false`). После включения `presence_probe` находит в нём пакеты каталога, подходящие по routing.

**Выключенный источник замораживается** (решение владельца 2026-10-29). Так же ведёт себя источник, который перестал подходить пакету по routing. Его больше не опрашивают, а его данные остаются как были: версии, известные только ему, не помечаются удалёнными, событий нет. Скрывать такие версии в дереве и resolve будут профили (E0-C), там учитываются только включённые источники. Отсюда же: если самая новая версия пакета есть только у замороженного источника, `latest_version` не откатывается; пакет, пропавший из всех опрошенных источников, но ещё известный замороженному, не получает `project_unavailable`.

Стартовый источник экосистемы (pypi.org, npmjs, Maven Central, nuget.org, proxy.golang.org, crates.io) создаёт миграция её этапа, вместе с плагином (IMPL R4). Остальное, включая PyTorch-индексы, Google Maven, Gradle Plugin Portal и RPM-репозитории, — через admin API или конфигурационный файл для CLI (`registries.yaml`, импорт идемпотентен по `slug`).

### 6.4 RPM: дистрибутивы и репозитории

Целевой дистрибутив — RHEL 8 и 9 (D7). Источник RHEL появится позже (Q12). Разработка идёт на совместимых дистрибутивах и зеркалах Fedora: они отдают те же repodata, поэтому коннектор один, а различия задаются конфигурацией источника.

**Один источник = один репозиторий одного релиза:** `rhel-9-appstream`, `rocky-9-crb`, `fedora-41-updates`. Архитектуры перечислены в конфиге. Пакет может быть в BaseOS и AppStream одновременно — тогда это одна строка `packages` и две строки `package_registries`.

```jsonc
// registries.config
{
  "namespace": "rhel-9",                   // namespace пакетов этого источника
  "release": "9", "repo_id": "appstream",
  "arch": ["x86_64", "aarch64"],
  "baseurl": "https://cdn.redhat.com/content/dist/rhel9/$releasever/$basearch/appstream/os",
  // либо "mirrorlist" / "metalink" (Fedora): metalink даёт контрольную сумму repomd.xml
  "releasever": "9",                       // RHEL: "9" — текущий минорный, "9.4" — закреплённый (EUS)
  "modular": true,                         // есть modules.yaml
  "eol": null                              // после EOL обновление выключается, данные остаются
}
// registries.auth_type = tls_client_cert (CDN Red Hat) | basic (Satellite/Pulp) | none; credentials_ref — имя секрета
```

| Источник | Подключение | Особенности |
|---|---|---|
| RHEL 8/9 | CDN Red Hat по entitlement-сертификату или внутренний Satellite/Pulp | Пока недоступен. Конфиг проверяем на Rocky/Alma с той же структурой, авторизацию по сертификату — на тестовом nginx с клиентским сертификатом |
| Rocky Linux, AlmaLinux 8/9 | Публичные зеркала | BaseOS, AppStream, CRB/PowerTools. Прототип RHEL для разработки |
| Fedora N, N-1 | `metalink` или закреплённый `baseurl` зеркала | `fedora` после релиза заморожен, `updates` меняется ежедневно. Модульности с Fedora 39 нет. По EOL источник выключается |
| EPEL 8/9 | Зеркала Fedora | Отдельный источник с namespace `epel-N` |

Авторизация `tls_client_cert` и `basic` нужна для RHEL, поэтому в коннекторе RPM она реализуется на E6. Это та же модель `credentials_ref`, что и для будущих корпоративных репозиториев (§13).

### 6.5 Слияние данных нескольких источников

`plugin.merge(per_source)` собирает одну строку каталога из ответов всех источников, где есть пакет:

1. **Основной источник** пакета — источник с наивысшим приоритетом, где пакет есть. Поля пакетного уровня (описание, лицензия, ссылки) берутся у первого по приоритету источника, где они **заполнены**. Так `torch` получает описание с pypi.org, даже если основным стал CUDA-индекс.
2. **Версия** — объединение версий всех источников. Канонические метаданные версии (зависимости, engines, дата) берутся у источника с наивысшим глобальным приоритетом, который её публикует (`metadata_registry_id`). Их видят карточка и дерево по умолчанию. Статус в каждом источнике хранится в `version_sources`. Агрегированный `status` — статус в источнике метаданных.
3. **Расхождения (Q17): приоритетный источник — в контексте профиля.** Если источник отдаёт ту же версию с другими зависимостями (другой `metadata_digest`), его набор сохраняется **вариантом** (`dependencies.variant_registry_id`). Дерево и resolve берут зависимости у источника, через который версия выбрана в данном профиле (первый по `sources_for`): его вариант, а если варианта нет — канонический набор. Расхождение не блокирует, событий не порождает и показывается в карточке версии.

   Это не теоретический случай. 2026-10-28 у `torch 2.9.1` колесо на PyPI требует 16 пакетов `nvidia-*-cu12`, а колесо той же версии из PyTorch-индекса — ни одного. Внутри PyPI метаданные разных колёс совпадают. С одним набором на версию resolve в профиле с PyTorch-индексом тянул бы CUDA-зависимости, которых pip там не поставит.
4. **Артефакты** хранятся отдельно по каждому источнику.

### 6.6 Обнаружение пакетов в источниках

- **Lazy fetch нового пакета.** Перебираем `sources_for(ref, profile)` и вызываем `exists()`. Где пакет найден — `fetch_package`, затем `merge`.
  - Отрицательные ответы кэшируются в Redis, TTL настраивается на источнике (по умолчанию 1 час). Без кэша каждый неизвестный GA стоил бы десятков запросов к Maven-репозиториям. Для сравнения: Not Found Cache в Nexus — 24 часа, Missed Retrieval Cache в Artifactory — 30 минут. Мы выбрали середину, чтобы только что опубликованный пакет находился в пределах часа.
  - **Кэшируется только настоящее «нет»:** 404, отсутствие в repodata. 429, 5xx, таймауты и ошибки авторизации — это ошибки источника. Они не превращаются в «пакета нет» и в кэш не попадают. Артефакт без этого правила известен по Artifactory: 429 от Maven Central превращались в 404.
  - **Circuit breaker на источник.** После N подряд ошибок источник считается `degraded` на M минут, по аналогии с auto-blocking в Nexus. Обнаружение его пропускает и пишет предупреждение, ежедневное обновление помечает прогон `partial` с указанием источника. Состояние видно в `GET /registries`.
  - Для npm при `exclusive`-scope опрашивается только закреплённый реестр.
- **Проверка присутствия** (еженедельная задача `presence_probe`). Ежедневное обновление следит только за источниками, где пакет уже есть (`package_registries`). Если пакет **появится** в другом источнике (новый CUDA-вариант, перенос в Google Maven), ежедневное обновление этого не увидит. Поэтому раз в неделю для каждого пакета проверяем `exists()` в тех источниках профиля по умолчанию, которые подходят по routing, но где пакета ещё нет. Найденное оформляется событием `source_added`.

---

## 7. Метаданные и зависимости

### 7.1 Источники метаданных по протоколам

| Протокол | Пакет | Зависимости версии | Лицензия | Примечания |
|---|---|---|---|---|
| `pypi-warehouse` | JSON API `/pypi/{name}/json` | `/pypi/{name}/{ver}/json` (лениво) | classifiers и `license` | Есть |
| `pypi-simple` | Страница проекта (файлы, даты, yanked) | PEP 658 `.metadata` файла версии. Если его нет — `dependencies_state=unavailable` (Q14): в основном это старые сборки. В карточке, дереве и resolve такая версия помечается «зависимости неизвестны» | — | Описание и лицензию даёт `merge` из pypi.org, если пакет там есть |
| `npm-registry` | Полный packument (Q19): версии, зависимости, engines, deprecated, dist-tags, даты релизов из `time` | Там же | Из полного документа версии `/{name}/{ver}` | Храним все версии, зависимости — у latest и лениво (Q9) |
| `maven2` | `maven-metadata.xml` | POM + effective model (§7.4) | `<licenses>` из POM → SPDX-алиасы | Gradle Module Metadata (`.module`) пока не читаем (Q15) |
| `nuget-v3` | Registration index | Там же: группы по TFM, deprecation | `licenseExpression` или `licenseUrl` | `vulnerabilities` не сохраняем (D6) |
| `goproxy` | `@v/list`, `@latest`, `.info` | `.mod` | **Нет.** В первой волне пусто, лицензии даст будущий классификатор (Q11) | Описаний и авторов нет |
| `cargo-sparse` | Индекс крейта: версии, зависимости, features, yanked, MSRV | Там же | API crates.io (1 запрос/с) — при добавлении пакета и при изменении, в фоне: крейт попадает в каталог из индекса сразу, описание, ссылки и лицензию дозаполняет фоновая задача (Q22) | В зависимостях из другого реестра — поле `registry` → `details.registry` |
| `rpm-repodata` | `primary`: имя, EVR, arch, summary, description, license, url | requires/recommends из `primary`, provides — в `raw_metadata` | `License` из spec, обычно SPDX | Модуль версии — из `modules.yaml` |

### 7.2 Нормализация типов зависимостей

`dependency_type` (`runtime | dev | optional | build | peer | provided`) — общий тип для фильтров UI. Исходный тип сохраняется в `scope`.

| Экосистема | Исходный тип → `dependency_type` |
|---|---|
| pypi | обычная → runtime, `extra == "x"` → optional (`extra_name=x`) |
| npm | dependencies → runtime, devDependencies → dev, peerDependencies → peer (`details.optional` из peerDependenciesMeta), optionalDependencies → optional. Алиас `npm:` → `alias` |
| maven | compile/runtime → runtime, test → dev, provided/system → provided, `<optional>true` → optional. import (BOM) — не зависимость, участвует только в effective model |
| nuget | Зависимости группы TFM → runtime, группа — в `environment_markers` (`dotnet_target_framework`), `exclude`/`include` assets → `details` |
| go | require → runtime, `// indirect` → `details.indirect=true` |
| crates | normal → runtime, dev → dev, build → build, `optional=true` → optional (`extra_name` = включающая feature), `package =` → `alias`, `target` → `environment_markers` (`rust_cfg`) |
| rpm | Requires → runtime, Recommends/Suggests/Supplements/Enhances → optional (`details.kind`). BuildRequires не загружаем. `dependency_name` = capability. Зависимости, разные для разных arch, помечаются `environment_markers` `rpm_arch` |

### 7.3 Окружение

Окружение задаёт, какие зависимости применимы. Его принимают дерево и resolve. Отдельно от окружения передаётся профиль источников (`profile`).

| Экосистема | Параметры | Что фильтруют |
|---|---|---|
| pypi | `python_version`, `os` (есть) | PEP 508 markers |
| npm | `node_version`, `os`, `cpu` | `engines.node` (предупреждение), `os`/`cpu` у optionalDependencies |
| maven | `scopes` (по умолчанию `compile,runtime`), `java_version`, `strategy` (`maven`/`gradle`) | Scope, активация профилей (только `activeByDefault` и `jdk`), алгоритм разрешения (§9.7) |
| nuget | `target_framework` (`net8.0`) | Выбор группы зависимостей (§9.6) |
| go | `go_version`, `replace`/`exclude` корня | Директива `go`/`toolchain` (предупреждение), директивы корня |
| crates | `target` (`x86_64-unknown-linux-gnu`), `features[]`, `default_features` | `cfg(...)`, включение optional через features |
| rpm | `distro`, `arch`, `repos[]`, `weak_deps`, `module_streams` (`{"nodejs": "18"}`), `disabled_modules[]`, `module_install[]` (`nodejs:18/common`) | Набор репозиториев, arch и noarch, Recommends, активные потоки модулей и профили (§9.8) |

### 7.4 Maven: effective model

Зависимости версии Maven нельзя взять из одного POM. При загрузке версии строим effective model:

1. **Цепочка `<parent>`.** Родительские POM подтягиваются рекурсивно через `SourceLookup`: родитель может лежать в другом репозитории профиля. Документы кэшируются в `source_documents`, в каталог как пакеты не попадают.
2. **Интерполяция `${...}`**: `project.*`, `<properties>`, наследуемые свойства. Системные свойства, `env.*` и профили с условиями на ОС не поддерживаем, такие места помечаются `details.unresolved_property`.
3. **`dependencyManagement`**, в том числе `<scope>import</scope>` (BOM), объединяется по правилам Maven.
4. Зависимости без `<version>` получают версию из dependencyManagement.
5. **Relocation** (`<distributionManagement><relocation>`) → `packages.deprecation.alternative`.
6. `<repositories>` из POM зависимостей не используем: источники задаёт только профиль. Это осознанное расхождение с Maven ради предсказуемости и защиты от подмены.

Результат — `dependencies` с конкретными constraints и scope. `dependencyManagement` версии сохраняется в `raw_metadata.managed` для резолвера (§9.7).

---

## 8. Ежедневное обновление

Общий алгоритм [SPEC-DAILY-UPDATE.md](./SPEC-DAILY-UPDATE.md) не меняется: обнаружить изменения → перекачать изменившиеся → получить события сравнением состояния. Меняется шаг обнаружения: он идёт **по источникам**, каждый своим `ChangeDetector`.

### 8.1 Детекторы по протоколам

| Протокол / источник | Механизм | Стоимость | Ревизия |
|---|---|---|---|
| `pypi-warehouse` (pypi.org) | Корневой Simple index, `_last-serial` | 1 запрос, ~10 МБ | serial |
| `pypi-simple` | Условный GET страницы проекта (ETag) | N лёгких запросов | ETag |
| `npm-registry`: npmjs | Лента `_changes` (§8.2) + еженедельная сверка по ETag | ~330 тыс. изменений в сутки, ~33 страницы, ~14 МБ, меньше минуты | ETag packument |
| `npm-registry`: остальные | Условный GET packument | N лёгких запросов | ETag |
| `maven2` | Условный GET `maven-metadata.xml` по каждому GA в каждом источнике, где он есть | N × источники лёгких запросов | `lastUpdated` / ETag |
| `nuget-v3`: nuget.org | Catalog с курсора `commitTimeStamp`, фильтр по id, включая `PackageDelete` | Страницы каталога за сутки | `commitTimeStamp` |
| `nuget-v3`: остальные | Условный GET registration | N запросов | ETag |
| `goproxy`: proxy.golang.org | Лента `index.golang.org/index?since=` | Десятки тыс. строк в сутки, по 2000 | версия + timestamp |
| `goproxy`: остальные | `@v/list` по модулю | N запросов | список версий |
| `cargo-sparse` | Условный GET индекса крейта | N лёгких запросов (CDN) | ETag |
| `rpm-repodata` | `repomd.xml` репозитория. При изменении `revision` — `primary` и `modules.yaml`, сравнение EVR пакетов каталога. Пропуск по неизменной `revision` не применяется при первом прогоне и после изменения конфигурации источника (arch, baseurl): в курсоре хранится хэш конфига. Так же поступает `optimize` в Pulp | 1 запрос + десятки МБ при изменении | EVR |

Если пакет изменился хотя бы в одном источнике, перекачиваем его из **всех** его источников и делаем `merge`. Так проще и надёжнее, чем хранить частичные снимки по источникам, а пакетов в одном прогоне мало.

### 8.2 npm: лента `_changes`

`replicate.npmjs.com/registry/_changes` — журнал всех изменений реестра: `{"seq", "id", "changes":[{"rev"}], "deleted"?}`. Курсор — `seq`. Документы пакетов в ленте не приходят, только имена.

1. **Курсор** хранится в `sync_states.cursor_data.last_seq` источника `npmjs`. Перед первым seed запоминаем текущий `update_seq` (`GET /registry/`), чтобы не потерять изменения, случившиеся во время seed.
2. **Ежедневный прогон** читает `_changes?since=<last_seq>&limit=10000` страницами до `update_seq`, снятого в начале прогона. Каждая страница фильтруется по именам каталога (множество в памяти). Курсор сохраняется после каждой страницы.
3. **Изменившиеся пакеты** перекачиваются. `deleted: true` проверяется запросом: 404 → `project_unavailable`.
4. **Отставание CDN.** Лента может опередить `registry.npmjs.org` (`max-age=300`). Если ETag не изменился, хотя пакет есть в ленте, повторяем через `stale_retry_seconds`, как для PyPI.
5. **Потеря курсора.** Ошибка на `since` или `last_seq` меньше сохранённого означают, что лента пересоздана. Детектор возвращает `reconcile_all`: полная сверка по ETag всех npm-пакетов каталога, затем курсор ставится на текущий `update_seq`.
6. **Еженедельная сверка по ETag** — страховка от пропусков ленты.

Замер 2026-10-27: ~3,8 изменения в секунду, страница на 10 тыс. изменений — 0,43 МБ и 0,7 с.

### 8.3 События

- **`release_added`** — когда версия впервые появилась **в любом** источнике. Появление той же версии во втором источнике (Maven-зеркало, PyTorch-индекс) — событие `source_added` с `details.registry`, без отдельного релиза. Если источник для пакета новый, событие одно на пакет (без `version`), а не по событию на каждую его версию: у `torch` в PyTorch-индексе их тысячи.
- **Статус в одном источнике.** Изменение статуса только в одном источнике меняет `version_sources`. Событие возникает, только если изменился агрегированный статус версии.
- **`release_deprecated`** (новое) — deprecation версии в npm и NuGet.
- **`release_removed` не бывает у Maven** (релизы не удаляются) **и у RPM**: там вытесненная сборка получает статус `superseded` без события.
- **RPM, модульные пакеты.** В `details` события пишется поток модуля: `nodejs 18.20.4 (nodejs:18)`.
- **`project_unavailable`** — пакет исчез из **всех** своих источников. Исчезновение из одного источника отмечается `package_registries.missing_since`.

---

## 9. Resolve

### 9.1 Общий контракт

```http
POST /api/v1/resolve
{
  "ecosystem": "pypi",
  "profile": "cuda121",                         // по умолчанию — профиль экосистемы по умолчанию
  "roots": [{"purl": "pkg:pypi/torch", "constraint": ">=2.5", "extras": []}],
  "environment": { ...параметры экосистемы, §7.3... },
  "options": {"include_dev": false, "include_optional": false, "timeout_s": 30},
  "managed": []                                 // Maven: dependencyManagement корня, в том числе BOM (purl с qualifier type=pom)
}
```

Ответ — граф (`ResolveGraph` в контракте). В npm и crates.io в нём может быть несколько версий одного пакета. У каждого узла указан источник выбранной версии.

```json
{
  "strategy": "pip-resolvelib",
  "nodes": [{"id": "n1", "purl": "pkg:pypi/torch@2.5.1%2Bcu121", "registry": "pytorch-cu121", "depth": 1}],
  "edges": [{"from": "root", "to": "n1", "constraint": ">=2.5", "type": "runtime"}],
  "conflicts": [], "warnings": [], "fetched": ["pkg:pypi/nvidia-cublas-cu12"],
  "details": {}, "resolve_time_ms": 412
}
```

- **Неизвестные зависимости.** Версия с `dependencies_state=unavailable` (Q14) остаётся кандидатом. Её узел помечается в `warnings` как «зависимости неизвестны», поддерево не строится.
- **Ошибки узла.** У узла есть `errors[]`: неразрешимое требование, неизвестные зависимости. Ошибка всего графа — в `error`. Частичный граф с ошибками узлов полезнее отказа. Так же устроен граф deps.dev.
- **Воспроизводимость.** В ответе есть `resolved_at` и ревизия каталога (максимальный `synced_at` участвовавших пакетов). Результат resolve зависит от времени, поэтому без них два ответа нельзя сравнить.
- **Видимость версий.** Кандидаты пакета — версии, которые публикуют источники из `sources_for(ref, profile)`, со статусом `active` в этом источнике. `yanked` разрешены только при точном закреплении, как в pip. Cargo yanked-версию в новый lock не берёт никогда, даже при `=x.y.z` (§9.4).
- **Где выполняется.** pypi, npm, crates, go, nuget и maven — в процессе API (asyncio, lazy fetch через адаптеры). RPM — в `rpm-worker` через Celery, API ждёт до `timeout_s`.
- **Лимиты.** Не больше 2000 узлов и 500 lazy fetch за запрос, общий таймаут. При превышении — `resolve_limit_exceeded` с частичной статистикой.
- **Кэш** в Redis по хэшу запроса и ревизиям каталога, TTL 1 час.
- **Режимы ввода** (IMPL R2): `input_mode: requirements | lock` для всех экосистем; `freeze` — алиас `lock`. В режиме `lock` корни, достижимые из других корней, помечаются `hidden`. У корней есть `latest_compatible` — самая новая совместимая версия.
- Старый формат ответа PyPI остаётся алиасом до переделки UI (E7).

**Какой нативный инструмент эмулирует каждый резолвер.** Поведение пакетных менеджеров меняется между версиями. Например, npm 7 начал ставить peer-зависимости автоматически. Поэтому резолвер фиксирует, какую версию инструмента он повторяет (`Resolver.emulates`), и эталоны генерируются ею же. deps.dev делает так же: Maven 3.6.3, npm 6.14.12.

| Экосистема | Эмулируем | Почему |
|---|---|---|
| pypi | pip 24 (resolvelib) | Текущий pip |
| npm | npm 10 | Автоустановка peer-зависимостей (≥ 7). deps.dev эмулирует npm 6, поэтому его графы для npm с нашими расходятся |
| crates | Cargo 1.8x, resolver v2 | Стандарт для edition 2021; MSRV-aware resolver v3 — отдельным флагом окружения |
| go | Go 1.23 | MVS с подрезкой графа, как в Go 1.17+ (§9.5, Q23) |
| nuget | .NET SDK 8 | Правила NuGet 6.x |
| maven | Maven 3.9 / Gradle 8 | Две стратегии (§9.7) |
| rpm | dnf 4 (EL8/EL9) | Модульность EL8/EL9 |

### 9.2 PyPI

resolvelib (backtracking), PEP 508 markers, extras — без изменений. Добавляется видимость версий по профилю. Local-версии (`+cu121`) участвуют в выборе так же, как в pip: `==2.5.1` совпадает с `2.5.1+cu121`.

### 9.3 npm — дерево `node_modules` (arborist, Q20)

1. Обход в ширину от корней, зависимости узла по имени. Для `name@range` ищем пакет вверх по цепочке папок `node_modules` от места, где он нужен. Подходящая версия переиспользуется (dedupe). Неподходящая останавливает поиск, и новая версия ставится сразу под ней. Иначе новая версия поднимается так высоко, как позволяют конфликты (hoisting, как в npm). Версию выбираем как npm-pick-manifest: `dist-tags.latest`, если он удовлетворяет range, иначе лучшая из подходящих — не deprecated, совместимая с версией Node.js, максимальная.
2. optionalDependencies, не подходящие по `os`/`cpu`, пропускаем без ошибки.
3. **peerDependencies** (npm ≥ 7 ставит их автоматически) разрешаем как зависимость родителя. Несовпадение → `conflicts[kind=peer]`. Опциональные peer-зависимости не ставим.
4. Алиасы `npm:` разрешаем. `git+`, `file:` и URL возвращаем как неразрешаемые узлы с предупреждением.
5. **bundleDependencies** не разрешаются из реестра: они лежат внутри tarball пакета. Без чтения артефакта мы их не знаем. Узел помечается «есть bundled-зависимости», в граф они не попадают. deps.dev видит их только через разбор пакета.

Это эмуляция дерева, которое строит npm 10 (arborist), с dedupe и hoisting: от места узла в дереве зависит, какая версия будет переиспользована, поэтому без hoisting множество `(name, version)` расходится с npm. Эталон — `npm install --package-lock-only`, сравнение по множеству `(name, version)`, цель — 100% без допустимых расхождений.

### 9.4 crates.io — перебор с семвер-совместимостью

- Идентичность в решателе — `(crate, класс совместимости)`: `1.x`, `0.3.x`, `0.0.3`. Поэтому `rand 0.7` и `rand 0.8` могут сосуществовать, как в Cargo.
- Реализация — перебор с откатом, как в Cargo: класс, в который попадёт зависимость, известен только после выбора кандидата (`>=0.9` подходит и к 0.9.x, и к 1.x), поэтому resolvelib с заранее известной идентичностью не годится. Первой идёт зависимость с наименьшим числом кандидатов. Кандидаты от новой версии к старой, yanked исключены всегда: Cargo 1.86 отвечает на `=1.0.95` ошибкой «version 1.0.95 is yanked». Одна активация на значение `links`. Неудача откатывает к последнему выбору среди её причин (как conflict-driven backjumping в Cargo), а если такого выбора нет — сразу ошибка с причиной.
- **Features** объединяются по идентичности. Optional-зависимости включаются через `dep:` и `feature = ["crate/feature"]`, `default-features` учитывается. Слабая `crate?/feature` при сборке зависимость не включает, но в `Cargo.lock` Cargo её кладёт (rust-lang/cargo#10801); резолвер повторяет и то и другое.
- Версии выбираются для всех платформ, как в `Cargo.lock`: он не зависит от платформы. `cfg(...)` вычисляется по `environment.target` и отсекает неприменимые узлы графа; `target: "*"` даёт граф для всех платформ, то есть содержимое `Cargo.lock`. Dev-зависимости — только у корня и при `include_dev`.
- Эталон — `cargo generate-lockfile` (множество для всех платформ) и `cargo tree --target <t> -e normal,build` (граф для платформы).
- Известное расхождение: платформенные зависимости build-зависимостей и proc-macro Cargo оценивает для host, мы — для `target`. Для target = host (эталоны на linux x86_64) это одно и то же.

### 9.5 Go — Minimal Version Selection

1. `go.mod` корня и каждого модуля, который читается по правилам подрезки (п. 4). Эти `.mod` кэшируются в `source_documents`; resolve каталог не расширяет: ни транзитивные модули, ни build list в него не попадают (Q24), только корни.
2. Build list: для каждого модуля **максимальная** из требуемых версий.
3. `replace` и `exclude` — только из корня (`environment`). Требование исключённой версии отбрасывается, как в Go с 1.16. Если MVS поднял корень выше заданной версии, граф перечитывается от выбранной версии (`expandGraph` в Go).
4. Граф с подрезкой, как в Go 1.17+ (Q23): у модуля, чей `go.mod` объявляет `go >= 1.17`, в графе только его непосредственные требования, их `go.mod` дальше не читаются; у модулей на `go < 1.17` граф полный. Без подрезки MVS выбрал бы другие версии общих модулей, чем Go.
- Эталон — `go list -m all`, цель 100% без допустимых расхождений.

### 9.6 NuGet

1. **Lowest applicable version.** Из диапазона берётся минимальная подходящая версия (`FindBestMatch`); unlisted-версии участвуют: restore читает полный список версий.
2. **Direct dependency wins.** Ссылка затеняется, если предок на её же пути объявляет тот же пакет напрямую; если затенённая хотела больше — понижение `NU1605`, в .NET SDK это ошибка restore.
3. **Cousin dependencies.** Ссылки из разных путей — кузены на любой глубине: побеждает наибольшая из их версий, проигравшие уходят вместе со своими поддеревьями. Если победитель не удовлетворяет чьему-то диапазону — конфликт (`NU1107`). Цикл — `NU1108`. Неудачный restore оставляет граф, код ошибки — в `error`.
4. **Группа зависимостей по TFM.** В первой волне — только современные целевые платформы (Q7): `net5.0` и новее (на nuget.org уже есть `net10.0` и `net11.0`) и `netstandard1.0`–`2.1`. Проект на `netX.0` использует ближайшую группу из `netX.0 → … → net5.0 → netcoreapp3.1 → … → netstandard2.1 → … → netstandard1.0`. .NET Framework 4.x и платформенные TFM (`net8.0-windows`, `-android`, `-ios`) — когда понадобятся.
5. Floating-версии `1.*` → максимальная подходящая.
- Эталон — `dotnet restore` + `project.assets.json`.

### 9.7 Maven — две стратегии

Источники — профиль Maven: Central, Google, Gradle Plugin Portal, остальные. Каждая версия ищется в порядке `sources_for`.

**`strategy=maven` (nearest wins)** — как Maven:
1. Корень: зависимости запроса + `managed`.
2. BFS по уровням. Для GA выигрывает **ближайшее** к корню объявление, на одной глубине — первое. dependencyManagement корня переопределяет версию и scope транзитивных.
3. Точная версия («мягкая») берётся как есть, из диапазона — максимальная доступная.
4. **Exclusions** накапливаются по пути. **Optional** не транзитивны. **Scope** пересчитывается по стандартной матрице.

**`strategy=gradle` (highest wins)** — для Gradle-сборок и плагинов:
1. Тот же обход, но при конфликте версий одного GA выигрывает **наибольшая**. `managed` действует как Gradle platform/constraints.
2. **Gradle-плагины:** корень `id("org.springframework.boot") version "3.3.5"` превращается в маркер `org.springframework.boot:org.springframework.boot.gradle.plugin:3.3.5` в Gradle Plugin Portal. Его POM ведёт на артефакт реализации.
3. Метаданные — из POM. **Известное ограничение (Q15):** Gradle Module Metadata (`.module`: варианты, capabilities, атрибуты, constraints) в первой волне не читаем. Gradle публикует `.module` рядом с POM и при сборке предпочитает его, а POM — упрощённый запасной вариант. Где это заметно:
   - **Kotlin Multiplatform** (`kotlinx-coroutines`, `ktor`): в POM корневого артефакта нет зависимостей конкретной платформы (`-jvm`, `-android`), поэтому граф будет неполным;
   - **часть AndroidX** и библиотеки с `api`/`implementation`: разделение compile/runtime в POM бывает неточным;
   - **platform/BOM через constraints** Gradle: в POM их нет.

   Если у версии есть `.module` (`artifacts.has_metadata_file`), резолвер добавляет в `warnings` пометку «результат по POM, Gradle Module Metadata не учтена». Чтение GMM — кандидат на следующую волну, схема под него готова: `.module` кэшируется в `source_documents`.
- Эталоны: `mvn dependency:list` для `maven`, `gradle dependencies --configuration runtimeClasspath` для `gradle`.

### 9.8 RPM — libsolv и модульность

1. `rpm-worker` держит актуальные repodata каждого источника (`primary`, `filelists`, `modules.yaml`) и `.solv`-кэш.
2. **Пул.** Источники профиля нужного `distro` (по умолчанию все включённые для этого namespace), пакеты `arch` и `noarch`.
3. **Модульность** (EL8 и EL9; в RHEL 10 и Fedora ≥ 39 её нет). libsolv модули не понимает, поэтому фильтр строим сами, как dnf:
   1. Разбираем `modules.yaml` через `libmodulemd`: `modulemd` (name, stream, version, context, артефакты-NEVRA, зависимости потоков) и `modulemd-defaults`.
   2. **Активные потоки:** явно заданные (`module_streams`), потоки по умолчанию для остальных модулей, кроме `disabled_modules`, и зависимости активных потоков (`requires: {perl: [5.26]}`, `platform: [el8]`). При нескольких version/context берём последнюю version с context, совместимым с уже выбранными потоками. Два потока одного модуля → `module_conflict`.
   3. **Исключения:** артефакты неактивных потоков убираются из пула. Немодульные пакеты с именем артефакта активного потока тоже убираются: модульные побеждают («modular filtering»).
   4. Пул с исключениями передаётся в libsolv через `pool.considered`.
   5. `module_install` (`nodejs:18/common`) раскрывается в пакеты профиля модуля и включает поток.
   6. В `details` ответа — активные потоки. Если пакет есть только в неактивном потоке, в `conflicts` попадает подсказка, какой поток включить.
4. **Задача** — `job install` корней по имени или capability. Recommends — при `weak_deps=true`.
5. **Результат** — транзакция libsolv: пакеты с EVR и arch, рёбра «capability → провайдер» из `solver.describe_decision`.
6. Пакеты результата добавляются в каталог как `discovered`.
- Эталон — `dnf install --assumeno --installroot=<tmp> --releasever=<N> [module enable ...]` в контейнере дистрибутива.

---

## 10. Уязвимости — расширение, в этой волне не делаем

По решению D6 уязвимости новых экосистем не собираем. PyPI продолжает сохранять уязвимости из JSON API, события и дайджест для PyPI работают как сейчас.

Чтобы расширение потом было дешёвым, закладываем:

- **Контракт источника**, не привязанный к экосистеме:
  ```python
  class VulnerabilitySource(Protocol):
      async def query(self, refs: list[PackageRef]) -> list[Advisory]: ...
  ```
  `Advisory` — подмножество OSV schema: `id`, `aliases`, `summary`, `details`, `severity`, `published`, `modified`, `withdrawn`, `affected[]` (`package{ecosystem, name, purl}`, `ranges[]{type, events}`, `versions[]`), `references`.
- **Сопоставление с версиями.** Диапазоны `ECOSYSTEM` вычисляются через `VersionScheme` (`sort_key`, `constraint`), отдельной работы не нужно.
- **Хранение** — существующие `vulnerabilities` и `package_vulnerabilities`. Таблица затронутых версий добавится вместе с расширением.
- **Справка.** OSV покрывает PyPI, npm, Maven, NuGet, Go, crates.io, Red Hat, Rocky Linux и AlmaLinux. Fedora и EPEL не покрывает (проверено 2026-10-27).

---

## 11. API

### 11.1 Адресация пакетов

```
GET /api/v1/purl/{purl}                                   — пакет или версия по purl (редирект на канонический путь)
GET /api/v1/packages/{ecosystem}/{coordinates}/-/         — карточка, в ней список источников
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions?profile=
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}       — в ней источники версии и их статусы
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/dependencies?profile=&<environment>
GET /api/v1/packages/{ecosystem}/{coordinates}/-/versions/{version}/artifacts
GET /api/v1/packages/{ecosystem}/{coordinates}/-/vulnerabilities          — пока только PyPI (§10)
```

- `{coordinates}` — родное написание экосистемы, может содержать `/`: `@angular/core`, `github.com/gin-gonic/gin`, `org.springframework:spring-core`, `rhel-9/openssl`. Разделитель `/-/` однозначно отделяет координаты от подресурса.
- Текущие маршруты `/packages/pypi/{name}/...` остаются алиасами, пока UI не переедет на новые (конец E7), и помечаются deprecated в OpenAPI.

### 11.2 Остальное

- `GET /packages` и `GET /search` — фильтры `ecosystem` (несколько значений), `registry`, `origin`. В ответе — `purl`.
- `GET /registries`, `GET /registries/{slug}` — источники, состояние детектора, курсор, последний успех. `GET /profiles?ecosystem=`.
- **Admin:**
  - `POST /admin/packages` (`{"purls": [...]}`), `POST /admin/seed` по purl;
  - `POST /admin/registries`, `PUT /admin/registries/{slug}` — завести источник, изменить приоритет, routing, авторизацию (Q16);
  - `POST /admin/registries/{slug}/test` — проверка подключения и авторизации;
  - `POST /admin/registries/{slug}/enable` / `disable`; удаление источника, у которого есть пакеты, запрещено — только выключение;
  - `PUT /admin/profiles/{ecosystem}/{slug}`;
  - CLI: `registries import registries.yaml`, `registries test <slug>`.
- `GET /updates`, `/updates/summary` — фильтры `ecosystem`, `registry`. `last_run` — по каждой экосистеме.
- `POST /resolve` — §9.1. `GET /stats` — счётчики по экосистемам и источникам.

---

## 12. UI

- **Поиск.** Переключатель экосистем, значок экосистемы и purl в выдаче.
- **Карточка пакета.** `/package/{ecosystem}/{coordinates}`. Блок «Источники»: где есть пакет, статус и время последней проверки. У версии — список источников и статусы.
- **Дерево.** Выбор профиля и форма окружения по экосистеме: Python/OS, Node/OS/CPU, TFM, target и features, Go version, distro/arch/repos/потоки модулей, стратегия Maven/Gradle.
- **Resolve.** Экосистема, профиль, корни (purl + constraint). Импорт нативных манифестов — после первой волны. В результате граф по уровням: несколько версий одного пакета, источник каждой версии, конфликты, для RPM — активные потоки.
- **Updates.** Фильтры экосистемы и источника, статус последнего прогона по экосистемам.
- **Источники** (admin). Список, состояние детекторов, включение/выключение.

---

## 13. Корпоративные репозитории (заложено, не делаем)

- Модель источников, протоколов, routing и профилей (§6) одинакова для публичных и корпоративных источников. Nexus и Artifactory подключаются как ещё один источник с тем же протоколом.
- Авторизация `basic`/`bearer`/`tls_client_cert` + `credentials_ref` (имя секрета вне БД) заложены в схеме. `tls_client_cert` и `basic` реализуются уже на E6 для RHEL.
- **Dependency confusion.** Защита — правила `exclusive` в routing. Политику («корпоративный источник побеждает для namespace X») зафиксируем, когда появятся корпоративные источники.

---

## 14. Тестирование

### 14.1 Контрактный сьют

Общий набор тестов, параметризованный плагином и протоколом, проверяет на сохранённых ответах источников:

- purl и нормализацию имён;
- схему версий: `validate`, `sort_key` против эталонных таблиц (как в §5.5), диапазоны;
- разбор метаданных и зависимостей;
- `merge` нескольких источников, в том числе расхождения;
- routing и `sources_for`;
- детектор изменений: «без изменений», «новая версия», «удалён», «курсор продвинулся», «курсор потерян»;
- события через общий `diff_package`.

### 14.2 Эталоны резолвера

Для каждой экосистемы 10–20 реальных проектов, включая известные сложные случаи:
- peer-конфликты npm;
- BOM Spring Boot;
- Gradle-плагины;
- cousin-зависимости NuGet;
- features tokio;
- модульные пакеты EL8;
- PyTorch с CUDA-индексом.

Для каждого проекта храним **результат нативного инструмента** (§9.3–9.8), который генерирует скрипт в контейнере с тулчейном.

- В CI сравнение идёт с сохранёнными эталонами, резолвер работает на сохранённых ответах источников, без сети.
- Ночной job перегенерирует эталоны, расхождения публикуются отчётом.
- Эталоны генерируются **только нативными инструментами**. deps.dev и другие внешние сервисы в CI не используются (D8).
- Метрика — совпадение множества `(пакет, версия)`, цель — 100%. Допустимые расхождения (GMM в Gradle) перечислены в фикстуре явно.

### 14.3 Проверка схемы

[design/schema-check/](./design/schema-check/) — сценарии §5.4 и прототипы `sort_key` §5.5. Их перезапускают при каждом изменении схемы до появления миграций. Потом эти сценарии переезжают в интеграционные тесты миграций.

### 14.4 Интеграция

На стенде: seed по purl для каждой экосистемы → ежедневное обновление → дайджест → resolve. Acceptance smoke дополняется проверкой по одному пакету на экосистему и одному сценарию с несколькими источниками (`torch` + CUDA-индекс).

---

## 15. Этапы

Каждый этап заканчивается PR и рабочим стендом. Резолвер — часть этапа экосистемы (D3).

| Этап | Содержание | Сложность | Исполнитель |
|---|---|---|---|
| **E0. Ядро** | Контракт, purl, источники/протоколы/routing/профили, admin API и CLI источников с проверкой подключения, merge, `version_sources`, `source_documents`, presence probe, миграции 0006/0007 (с исправлением дефектов §5.2), перенос PyPI в плагин (`pypi-warehouse`) без изменения поведения, общий pipeline, новые маршруты API + алиасы | Высокая | Opus: контракт, merge, pipeline, миграции. Sonnet: маршруты, схемы, тесты |
| **E0.5. PyPI-индексы** | Адаптер `pypi-simple` (PEP 503/691, PEP 658), PyTorch-индексы с routing, профили в resolve PyPI | Средняя | Sonnet: адаптер. Opus: видимость версий в resolve |
| **E1. npm** | Адаптер `npm-registry`, лента `_changes`, semver, дерево, резолвер §9.3, дополнительные реестры | Средняя | Opus: semver-диапазоны, резолвер. Sonnet: адаптер, фикстуры |
| **E2. crates.io** | `cargo-sparse` + API с лимитом, Cargo semver, features, cfg, резолвер §9.4 | Средняя | Opus: features + резолвер |
| **E3. Go** | `goproxy`, лента index.golang.org, псевдоверсии, MVS §9.5 | Средняя | Sonnet в основном, Opus — ревью MVS |
| **E4. NuGet** | `nuget-v3`, catalog, NuGetVersion + `sort_key` против эталона, современные TFM, резолвер §9.6 | Средняя | Opus: TFM и правила разрешения |
| **E5. Maven** | `maven2` для всех репозиториев, effective model §7.4, ComparableVersion + `sort_key` против эталона, стратегии `maven` и `gradle`, Gradle Plugin Portal | Высокая | Opus |
| **E6. RPM** | Конфиги дистрибутивов §6.4, авторизация `basic`/`tls_client_cert`, rpm-worker + libsolv, `rpmvercmp`, `superseded`, модульность §9.8 | Высокая | Opus: libsolv и модульность. Sonnet: worker и конфиги |
| **E7. UI** | Параллельно E0–E6: экосистемы, источники, профили, формы окружения, граф resolve | Средняя | Sonnet |

Порядок E1–E6 — по нарастанию сложности. E0.5 идёт сразу после ядра: на PyPI с PyTorch-индексом модель нескольких источников проверяется раньше, чем появятся новые экосистемы.

---

## 16. Решения по вопросам и открытые вопросы

### 16.1 Решено

| # | Вопрос | Решение |
|---|---|---|
| Q1 | RPM-дистрибутивы | Целевой — RHEL 8/9. Разработка — Rocky, Alma, EPEL, зеркала Fedora (§6.4) |
| Q2 | EL8 и модульность | Поддерживаем полностью, включая модули EL9 (§9.8) |
| Q3 | Вытеснение RPM-сборок | Статус `superseded`, без события |
| Q4 | deps.dev | Только вспомогательный инструмент разработчика (D8) |
| Q5 | Lazy fetch из resolve | Разрешён, лимит 500 на запрос, `origin=discovered` |
| Q6 | Дайджест по `origin` | Понятия «следим сознательно» нет: дайджест показывает все пакеты, `origin` — только происхождение. Шум npm — отдельная задача |
| Q7 | TFM NuGet | Только современные: `net5.0+` и `netstandard` |
| Q8 | node-semver | Своя реализация |
| Q9 | Версии npm | Все строки версий, зависимости — у latest и лениво |
| Q10 | Обнаружение изменений npm | `_changes` + еженедельная сверка по ETag |
| Q11 | Лицензии Go | Пусто, их даст будущий классификатор со скачиванием пакетов |
| Q12 | Источник RHEL | Появится позже. Конфиг поддерживает CDN (сертификат) и Satellite/Pulp |
| Q13 | Репозитории RHEL | BaseOS, AppStream, CodeReady Builder, EPEL — отдельным источником |
| Q14 | Версии из PEP 503-индексов без PEP 658 | `dependencies_state=unavailable`, «зависимости неизвестны». Это в основном старые сборки (§6.1) |
| Q15 | Gradle Module Metadata | Не в первой волне. Известное ограничение с предупреждением в resolve (§9.7) |
| Q16 | Список внешних источников | Источники добавляем вручную: admin API, CLI, проверка подключения (§6.3) |
| Q17 | Разные метаданные одной версии в разных источниках | Берём приоритетный источник, расхождение видно в карточке (§6.5) |
| Q18 | Индекс отвечает 403, а не 404, на отсутствующий проект (download.pytorch.org — S3 AccessDenied) | Отсутствие — только 404; поведение конкретного хостинга в код не закладываем (в корпоративном контуре доступ к репозиториям будет устроен иначе). Такие индексы подключаются с routing, ограничивающим их своими пакетами (решение владельца, 2026-10-29) |
| Q19 | Даты релизов npm (в abbreviated packument их нет) | Всегда читаем полный packument (`Accept: application/json`): даты из `time`, его ETag — ревизия пакета. Трафик на изменение в 2–3 раза больше (решение владельца, 2026-10-30) |
| Q20 | Резолвер npm: логическое дерево без hoisting (как было в §9.3) или дерево npm 10 | Дерево npm 10 (arborist): dedupe и hoisting, эталоны совпадают на 100% (решение владельца, 2026-10-30) |
| Q21 | Имена crates: свёртка регистра и `-`/`_` (как PyPI) или имя как опубликовано | Как опубликовано: индекс crates.io отдаёт крейт только по точному имени (`serde-json` — 404), purl и OSV используют опубликованное имя. Дублей нет: crates.io сам запрещает имена, отличающиеся регистром или `-`/`_`. Ввод в другом написании приводится к каноническому имени запросом к API при добавлении (решение владельца, 2026-10-30) |
| Q22 | Данные API crates.io (описание, ссылки, лицензия) при лимите 1 запрос/с: в пути запроса ленивая догрузка 130 крейтов reqwest не укладывается в таймаут resolve | В фоне: адаптер читает только индекс, задача Celery раз в минуту дозаполняет до 50 крейтов, добавленных или изменившихся с последнего чтения API (отметка `api_fetched_at` в `raw_metadata`, её стирает каждая синхронизация). Описание нового крейта появляется в течение минут (решение владельца, 2026-10-30) |
| Q23 | Резолвер Go: MVS без подрезки графа (как было в §9.5) или как Go 1.17+ | Как Go: подрезка графа модулей на `go >= 1.17`; эталон `go list -m all` совпадает на 100% (решение владельца, 2026-10-30) |
| Q24 | Попадают ли модули build list Go в каталог при resolve | Нет: каталог курируемый, добавление модуля дорогое (список версий и `.info` каждой). Транзитивные `go.mod` лежат в `source_documents`, в каталог попадают только корни resolve (решение владельца, 2026-10-30) |

### 16.2 Открыто

Открытых вопросов нет.

---

## 17. Валидация по опыту индустрии

Сравнили решения спеки с тем, как ту же задачу решают Pulp, Sonatype Nexus, JFrog Artifactory, deps.dev и socket.dev. Источники — документация проектов (ссылки ниже). Спорное место модели (§6.5) проверено на реальных данных.

### 17.1 Что подтверждается

| Наше решение | Как у них | Вывод |
|---|---|---|
| Плагин на экосистему + адаптер на протокол (§3) | Pulp: `pulpcore` + плагин на тип контента (rpm, python, npm, maven, …), у плагина свои Remote и sync. Nexus и Artifactory: формат/тип пакетов + тип репозитория (hosted/proxy/group, local/remote/virtual) | Совпадает с индустрией |
| Профиль источников (§6.2) | Group в Nexus, virtual в Artifactory: упорядоченный список членов, метаданные объединяются | Совпадает |
| Routing `exclusive` (§6.2) | Routing rules ALLOW/BLOCK в Nexus (regex по пути, на proxy-репозитории). Include/exclude patterns и **Priority Resolution** в Artifactory, рекомендованные именно против подмены внутренних пакетов | Совпадает по идее, но у нас не хватало режимов `block` и `priority` → **добавлены** |
| Отрицательный кэш при обнаружении (§6.6) | Not Found Cache TTL в Nexus — 1440 минут; Missed Retrieval Cache Period в Artifactory — 1800 с | Совпадает; TTL сделан настраиваемым, по умолчанию 1 час |
| Только метаданные, без файлов (§0.3) | Политика `on_demand` в Pulp: при sync скачиваются метаданные, файлы — по запросу | Совпадает |
| Пропуск RPM-источника по ревизии `repomd.xml` (§8.1) | `optimize` в Pulp: sync идёт, только если в remote есть изменения; пропуск не применяется при первом sync и после изменения конфига | Совпадает, но **не хватало условия про конфиг → добавлено** |
| libsolv + libmodulemd в отдельном образе на базе RH (§3.2, §9.8) | pulp_rpm требует libsolv (depsolving при копировании) и libmodulemd (модульные репозитории), обе в основном пакетируются для RH-семейства. В трекере Pulp — ошибка «libsolv берёт пакет не из того модуля», решение — исключать модульные RPM неактивных потоков | Совпадает, включая наш шаг «modular filtering» |
| Свой резолвер на экосистему, эмулирующий нативный инструмент (§9) | deps.dev: резолверы Maven (по 3.6.3), npm (по 6.14.12), PyPI (алгоритм pip); граф `GetDependencies` — для npm, Cargo, Maven, PyPI, «как на 64-битном Linux» | Совпадает. Взяли у них явную фиксацию эмулируемой версии и ошибки на узлах |
| Граф, где пакет может встречаться несколькими узлами (§9.1) | deps.dev: «в некоторых системах одна версия пакета может быть несколькими узлами» | Совпадает |
| Схемы версий и диапазонов по экосистемам (§5.5) | `deps.dev/util/semver`: отдельные правила сравнения и синтаксиса ограничений для npm, Cargo, Go, Maven, NuGet, PyPI | Совпадает |
| Лента изменений npm (§8.2) | socket.dev реплицирует реестры почти в реальном времени и анализирует каждую новую публикацию | Совпадает с нашим выбором `_changes` |

### 17.2 Где мы сознательно отличаемся

| Отличие | У них | Почему у нас так |
|---|---|---|
| **Слияние источников при записи** в одну строку пакета и версии | Nexus, Artifactory и Pulp хранят контент **в репозитории**, объединение — при чтении (group/virtual) или в версии репозитория (Pulp) | Каталогу нужны карточка, события и дайджест по пакету, а не по репозиторию. Принадлежность к источникам сохраняется в `package_registries` и `version_sources`, расходящиеся метаданные — вариантами (§6.5). Этим закрыт главный риск слияния при записи |
| **Плановое обнаружение изменений** (раз в сутки) | Прокси обновляют метаданные лениво, по запросу клиента, с TTL (metadata retrieval cache в Artifactory, maximum metadata age в Nexus) | Мы каталог, а не прокси: дайджест требует узнавать об изменениях без запроса клиента |
| **Общие таблицы + jsonb** на все экосистемы | В Pulp у каждого типа контента своя таблица с точным естественным ключом (у RPM — NEVRA + checksum) | Сквозной поиск, события и дайджест по всем экосистемам. Цена — дисциплина схем jsonb (§5.4) |
| **Нет истории содержимого источника** | Pulp хранит RepositoryVersion — снимок содержимого после каждого изменения | История изменений есть в журнале событий. Снимки содержимого источников каталогу не нужны |
| **Без файлов артефактов** | Socket анализирует содержимое пакетов (install-скрипты, сетевые вызовы), Pulp, Nexus и Artifactory хранят файлы | Метаданных хватает для каталога и resolve. Анализ содержимого — будущий классификатор (§0.3) |

### 17.3 Что исправлено по итогам

1. **Варианты зависимостей по источникам** (§5.1, §6.5). Найдено на реальных данных: `torch 2.9.1` на PyPI и в PyTorch-индексе. Схема: `dependencies.variant_registry_id`, проверено сценарием.
2. **Режимы routing `allow`, `block`, `priority`** (§6.2) — по Nexus и Artifactory.
3. **Ошибки источника не кэшируются как «нет пакета»**, TTL отрицательного кэша настраивается, circuit breaker на источник (§6.6) — по Artifactory и Nexus.
4. **Ревизия конфигурации в курсоре RPM** (§8.1) — по Pulp.
5. **Ошибки на узлах графа, `resolved_at`, фиксация эмулируемой версии инструмента** (§9.1) — по deps.dev.
6. **bundleDependencies npm** (§9.3) — явное ограничение.
7. **Схемы jsonb по экосистемам** (§5.4) — ответ на аргумент Pulp о типизированных таблицах.
8. **Хранилище файлов по sha256** для будущего классификатора (§0.3) — по Pulp.

### 17.4 Что можно взять готовым — и почему пока нет

- **`deps.dev/util/resolve`** (Go, Apache-2.0). Это резолверы npm, Maven и PyPI с интерфейсом клиента данных: `Version`, `Versions`, `Requirements`, `MatchingVersions`. Как замена нашим резолверам не подходит:
  - npm эмулируется по версии 6, без автоустановки peer-зависимостей, а нам нужна 10;
  - Maven — по 3.6.3;
  - Cargo, Go, NuGet и RPM не покрыты;
  - библиотека на Go, а стек у нас на Python.

  Полезна как справочная реализация при разработке (D8).
- **`deps.dev/util/semver`** подходит как дополнительная сверка схем версий при разработке. Эталоном она быть не может: поддержка Maven, по их же документации, неполная. Эталоны для `sort_key` Maven и NuGet — только нативные `maven-artifact` и `NuGet.Versioning` (§5.5).

### 17.5 Источники

- Pulp: [концепции](https://pulpproject.org/pulpcore/docs/user/learn/concepts/), [on-demand](https://pulpproject.org/pulpcore/docs/user/learn/on-demand-downloading/), [копирование RPM с depsolving](https://docs.pulpproject.org/pulp_rpm/workflows/copy.html), [параметры sync pulp_rpm](https://docs.pulpproject.org/pulp_rpm_client/RpmRepositorySyncURL/), [roadmap pulp_rpm (пропуск sync по repomd)](https://pulp.plan.io/projects/pulp/wiki/Pulp_RPM_Roadmap), [зачем pulp_rpm RH-дистрибутив](https://discourse.pulpproject.org/t/q-pulp-rpm-requires-rpm-based-distribution-why/501), [модули и libsolv](https://pulp.plan.io/issues/4962?tab=notes)
- Nexus: [routing rules](https://help.sonatype.com/en/routing-rules.html), [поля репозитория (Not Found Cache)](https://help.sonatype.com/en/configurable-repository-fields.html)
- Artifactory: [Other Settings (Priority Resolution)](https://jfrog.com/help/r/jfrog-artifactory-documentation/other-settings), [конфигурация репозиториев (missed retrieval cache)](https://docs.jfrog.com/installation/docs/repositories-configurations-in-artifactory-yaml), [429 как 404](https://jfrog.com/help/r/artifactory-429-rate-limits-from-maven-central-public-upstreams-surface-as-404s), [priority resolution против подмены](https://jfrog.com/de/blog/going-beyond-exclude-patterns-safe-repositories-with-priority-resolution/)
- deps.dev: [API v3](https://docs.deps.dev/api/v3/), [util/resolve](https://pkg.go.dev/deps.dev/util/resolve), [util/semver](https://pkg.go.dev/deps.dev/util/semver)
- socket.dev: [обзор](https://risky.biz/catalog/socket/), [политика краулеров npm](https://docs.npmjs.com/policies/crawlers)
