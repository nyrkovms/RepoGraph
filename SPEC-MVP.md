# PackageHub MVP — Каноническая спецификация

> PyPI-only вертикальный слайс: каталог, синхронизация и dependency resolve.

**Версия:** 1.3-mvp  
**Дата:** 2026-03-12  
**Статус:** Source of truth для продуктового и архитектурного контура MVP.

## 0. Decision Baseline

Фиксируем обязательные дефолты MVP:

- `scope = full MVP`: backend + infra + UI в одном релизе.
- `incremental sync = batch refresh by synced_at` каждые 6 часов.
- `lazy fetch failure в POST /api/v1/resolve = fail-fast` (без partial result).
- `MVP behavior wins`: при расхождениях с полной `SPEC.md` приоритет у этого документа.

`Decisions fixed`
- Все инженерные решения по умолчанию зафиксированы в этом разделе и не переопределяются в приложениях.

`Open questions`
- Нет.

## 1. Цель MVP

MVP доказывает работоспособность архитектуры на PyPI и закрывает полный пользовательский путь:

1. Найти пакет.
2. Посмотреть метаданные, версии, артефакты и уязвимости.
3. Построить дерево зависимостей (Level 1).
4. Выполнить full resolve (Level 2) с учётом environment markers.
5. Автоматически дозагрузить пакет из PyPI, если его нет в БД.

Целевой deployment-контур: Docker Compose на dev-машине.

`Decisions fixed`
- MVP ориентирован на валидацию подхода, а не на full PyPI index.

`Open questions`
- Нет.

## 2. Scope и границы

### 2.1 In Scope

- PyPI как единственный registry в MVP.
- Demand-driven синхронизация: seed + lazy fetch + batch refresh + retry failed sync.
- Каталожный REST API + admin API + служебные endpoints.
- Resolver module на Python (`resolvelib` + `packaging`) внутри FastAPI.
- Web UI: поиск, карточка пакета, дерево зависимостей, resolve-страница.
- Infra: PostgreSQL, Redis, Celery worker/beat, API, frontend в Docker Compose.

### 2.2 Out of Scope

- npm, Maven, NuGet, crates.io, Go.
- Rust resolver microservice.
- Full sync всех ~550K пакетов PyPI.
- SSO/OIDC.
- Reverse dependencies и графовая визуализация.
- HPA/multi-pod production deployment.

`Decisions fixed`
- Вне scope элементы могут быть упомянуты только как миграционный путь.

`Open questions`
- Нет.

## 3. PyPI-first rationale

PyPI выбран первым из-за минимальной интеграционной сложности:

- Один запрос `GET /pypi/{name}/json` даёт пакет, версии, файлы, зависимости, уязвимости.
- PEP 508/440 стандартизуют зависимости и версионирование.
- Demand-driven загрузка позволяет стартовать без full crawl.

`Decisions fixed`
- В MVP не используем глобальный поиск по всему PyPI; поиск ограничен локальной БД.

`Open questions`
- Нет.

## 4. Архитектурный обзор

Состав MVP:

- `FastAPI` — публичный REST API.
- `Resolver Module (in-process)` — Level 2 resolve.
- `Lazy Fetcher (in-process)` — on-demand загрузка пакетов.
- `Celery Worker/Beat` — seed, manual fetch, batch refresh, retry failed sync.
- `PostgreSQL` — основное хранилище.
- `Redis` — API/resolve cache + Celery broker.
- `React (Vite)` — UI.
- Host-mounted file logs — `api.log`, `worker.log`, `beat.log` для dev-разбора sync ошибок.

Детализация контрактов:

- API: [SPEC-MVP-API.md](./SPEC-MVP-API.md)
- БД и миграции: [SPEC-MVP-DB.md](./SPEC-MVP-DB.md)
- План поставки: [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md)

`Decisions fixed`
- Resolver остаётся in-process в MVP; внешний сервис не вводится.

`Open questions`
- Нет.

## 5. Синхронизация (обзор стратегии)

MVP использует четырёхконтурную стратегию:

1. `Seed` — загрузка стартового списка пакетов и рекурсивное дозаполнение зависимостей.
2. `Lazy fetch` — загрузка неизвестного пакета по факту обращения (API/resolver/admin).
3. `Batch refresh` — периодическое обновление oldest N пакетов по `synced_at`.
4. `Retry failed sync` — повторная обработка транзиентных ошибок из очереди `sync_failures`.

Дополнительные правила:

- Latest version пакета гидратируется целиком в основном pipeline.
- Исторические версии получают зависимости lazy-on-demand при запросе дерева или resolve.
- Worker не валит batch целиком из-за одной ошибки пакета; ошибка пишется в `sync_failures`, лог и `sync_state`.

Полный pipeline, upsert-правила и производные поля описаны в [SPEC-MVP-DB.md](./SPEC-MVP-DB.md).

`Decisions fixed`
- Incremental sync в MVP строится только на batch refresh, не на changelog.
- Автоповтор применяется только к retryable upstream/network ошибкам; `not_found` не ретраится.

`Open questions`
- Нет.

## 6. Resolver (обзор стратегии)

Уровни резолвинга:

- `Level 1` — best-effort `resolved_version_id` при sync, для быстрых деревьев в UI.
- `Level 2` — `POST /api/v1/resolve` через `resolvelib` с backtracking.

Политика ошибок в resolve:

- При конфликте ограничений возвращается конфликтный ответ.
- При upstream timeout/ошибке lazy fetch — fail-fast ответ с ошибкой.
- Partial result для `/resolve` не поддерживается.

Детальный контракт — в [SPEC-MVP-API.md](./SPEC-MVP-API.md).

`Decisions fixed`
- Для MVP производительность resolver оптимизируется кэшем и warm-up, без смены backend-языка.

`Open questions`
- Нет.

## 7. Единые SLA и целевые метрики MVP

Этот раздел — единственный источник SLA/производительности для всех приложений.

| Область | Цель |
|---|---|
| Seed | 100 seed-пакетов + транзитивные зависимости (итого ~2-3K пакетов) за `<10 мин` |
| Lazy fetch через API | типовой ответ `<500 ms` |
| Поиск `GET /api/v1/packages` / `GET /api/v1/search` | `p95 < 200 ms` |
| Дерево зависимостей (Level 1, depth<=5) | `<500 ms` |
| Resolve (после прогрева и/или cache hit) | типично `10-50 ms` |
| Повторный resolve из Redis cache | `<10 ms` |
| Warm-up resolver cache при старте API | `<10 сек` |

`Decisions fixed`
- Все приложения ссылаются на этот раздел и не дублируют SLA числа.

`Open questions`
- Нет.

## 8. Acceptance Criteria (канонические)

Идентификаторы используются в execution-плане без дублирования формулировок.

### 8.1 Sync

- `AC-SYNC-1`: Seed загружает 100 пакетов + рекурсивные зависимости за `<10 мин`.
- `AC-SYNC-2`: Lazy fetch неизвестного пакета через API работает автоматически.
- `AC-SYNC-3`: Lazy fetch из resolver работает и не создаёт дублей при конкуренции.
- `AC-SYNC-4`: Batch refresh по `synced_at` выполняется по расписанию.
- `AC-SYNC-5`: Сохраняются метаданные, версии, зависимости, артефакты, уязвимости.
- `AC-SYNC-6`: PEP 508 parser корректно обрабатывает версии/extras/markers.
- `AC-SYNC-7`: SPDX нормализация покрывает минимум 70% пакетов.
- `AC-SYNC-8`: Транзиентные sync-ошибки попадают в `sync_failures`, логируются в файл и автоматически ретраятся beat-ом.

### 8.2 API

- `AC-API-1`: Эндпоинты каталога возвращают все требуемые поля модели.
- `AC-API-2`: Фильтры и cursor pagination работают стабильно.
- `AC-API-3`: Bearer-token auth покрывает viewer/admin сценарии.
- `AC-API-4`: Контракты ошибок единообразны для всех endpoint-ов.

### 8.3 Resolver

- `AC-RES-1`: Типовые кейсы (`flask`, `django`) проходят с корректным деревом.
- `AC-RES-2`: Учитываются environment markers и extras.
- `AC-RES-3`: Конфликт/UNSAT возвращается с понятным объяснением.
- `AC-RES-4`: Fail-fast при сбое lazy fetch в `/resolve`.
- `AC-RES-5`: Повторный запрос использует cache и укладывается в SLA.

### 8.4 UI

- `AC-UI-1`: Поиск пакетов с автодополнением, keyboard navigation и переходом в карточку пакета.
- `AC-UI-2`: Страница пакета: метаданные, версии, уязвимости, environment-aware дерево зависимостей и нормализованное описание пакета.
- `AC-UI-3`: Визуализация дерева зависимостей: compact tree, badges условий применения, collapse/expand по узлам и для всего дерева.
- `AC-UI-4`: Страница resolve: ввод зависимостей, выбор environment, режимы `requirements`/`pip freeze`, вывод итогового дерева и ошибок.

### 8.5 Infra

- `AC-INFRA-1`: Docker Compose поднимает весь стек одной командой.
- `AC-INFRA-2`: Health checks присутствуют для `postgres`, `redis`, `api`; `worker/beat/frontend` зависят от healthy upstream.
- `AC-INFRA-3`: Метрики: sync duration, API/resolve latency, cache hit rate.
- `AC-INFRA-4`: Dev-логи API/worker/beat пишутся в host-mounted файлы.
- `AC-INFRA-5`: README содержит полный сценарий локального запуска и разбора sync ошибок.

`Decisions fixed`
- Acceptance criteria считаются единственным критерием готовности MVP.

`Open questions`
- Нет.

## 9. Roadmap (управленческий уровень)

Этапы остаются прежними: Setup → Schema → Connector → Sync → API → Resolver → UI → Polish.

Детальная нарезка задач по 0.5-1 дню, DoD, required tests и артефакты вынесены в [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md).

`Decisions fixed`
- Этот раздел содержит только этапы и порядок; операционные детали — только в execution-приложении.

`Open questions`
- Нет.

## 10. Документы и владение контрактами

- Product/architecture governance: этот документ.
- API контракты: [SPEC-MVP-API.md](./SPEC-MVP-API.md).
- DB контракты, миграции и upsert semantics: [SPEC-MVP-DB.md](./SPEC-MVP-DB.md).
- Delivery plan и runbook реализации: [SPEC-MVP-EXECUTION.md](./SPEC-MVP-EXECUTION.md).

`Decisions fixed`
- У каждого типа утверждений один primary owner-документ.

`Open questions`
- Нет.
