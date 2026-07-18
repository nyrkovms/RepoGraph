# Tasks: fetcher-redesign

Реализация — в репозитории PackageHub (`/Users/mikhail/Documents/Work/repograph`), backend.

## 1. Схема и модели

- [x] 1.1 Registry: поля `protocol` (string, not null) и `enumerate_enabled` (bool, default false) + alembic-миграция с backfill `protocol='pypi-json'` для существующей строки pypi
- [x] 1.2 Модель `DistProbe` (`version_id` FK, `registry_slug`, `artifact_filename`, `is_representative`, `entry_points_txt`, `top_level_txt`, `file_facts` JSONB, `readme_extracted`, `error`, `fetched_at`; UNIQUE(version_id, artifact_filename)) + миграция
- [x] 1.3 Таблица описаний `version_description` (`version_id` FK, `origin`, `content`; UNIQUE(version_id, origin)) для tarball-readme + миграция
- [x] 1.4 PackageRegistry: флаги `name_collision` и `checksum_mismatch` (bool, default false) + миграция

## 2. Транспорт и доступ к реестрам

- [x] 2.1 Общий `httpx.AsyncClient` на процесс (lifespan в API, инициализация в Celery worker) вместо клиента на запрос
- [x] 2.2 `RegistryClient`: auth-заголовки из `registry.auth_config` (bearer/basic), инвариант `headers_for` — Authorization только на хост реестра, редиректы обрабатываются вручную без переноса заголовков; юнит-тесты на CDN-редирект
- [x] 2.3 Троттлер per-registry: минимальный интервал из `get_rate_limit()` применяется реально
- [x] 2.4 Фикс `_pick_latest_version`: сортировка через `packaging.Version`, не лексикографическая; юнит-тест «9.0 < 10.0»
- [x] 2.5 `LazyFetcher.ensure_package` различает not-found / upstream-error в возвращаемом типе; ошибка auth (401/403) — отдельный класс `RegistryAuthError`
- [x] 2.6 (найдено по ходу) Регистрация Celery-задач: `autodiscover_tasks(["app.tasks"])` не находил ни одной задачи — worker дропал batch refresh/retry с момента создания проекта; заменено явным `include` в Celery()
- [x] 2.7 (найдено по ходу) Loop-affinity asyncpg в worker: пул движка привязывался к первому event loop форка, второй таск в том же процессе падал; в worker_process_init движок подменяется на NullPool

## 3. Probe-контур

- [x] 3.1 Селектор представительного дистрибутива PyPI (порт эвристики classifire: только wheel ≤ лимита, предпочтение `none-any`, permanent-причины `no wheel available` / `all wheels exceed size limit`); юнит-тесты
- [x] 3.2 Извлечение фактов из wheel in-memory (entry_points.txt, top_level.txt, file_facts: total/ext_counts/scripts/platform_wheel, README ≤ 500K; без extractall, лимиты размеров); юнит-тесты на синтетических wheel
- [x] 3.3 Celery-задача `probe_version`: Redis-лок по (version_id, filename), INSERT ON CONFLICT DO NOTHING, artifact-unusable → строка с `error`, retryable → celery-retry с backoff, после исчерпания — `sync_failures`
- [x] 3.4 Enqueue probe представительного дистрибутива latest-версии после успешного sync (hook в pipeline)
- [x] 3.5 Admin endpoint: probe конкретной версии/дистрибутива; удаление probe-строки (re-probe)
- [x] 3.6 Описание из дистрибутива → `version_description`: для pypi — тело `*.dist-info/METADATA` (origin `wheel-metadata`); origin `tarball-readme` заработает с npm-probe

## 4. Per-registry контуры и мультиреестры

- [x] 4.1 Выбор коннектора по `registry.protocol`; реестр передаётся в коннектор (base_url/auth), захардкоженный pypi.org уходит
- [x] 4.2 Batch refresh и beat итерируют реестры с `sync_enabled = true`, состояние per registry
- [x] 4.3 Коннектор `pypi-simple` (PEP 503/691 + PEP 658 при наличии): имена, версии, файлы, hashes, requires-python, yanked → NormalizedPackage с пометкой полноты
- [x] 4.4 Правило слияния: поля из бедного протокола не затирают поля из богатого; метаданные пакета — только из primary-реестра
- [x] 4.5 Задача enumeration: полный листинг реестра (`enumerate_enabled = true`), батчи, курсор в SyncState, enqueue недостающих; интеграционный тест на фикстуре simple index
- [x] 4.6 Детект коллизий: имя в internal hosted + public → `name_collision`; одинаковый version+filename с разными checksums → `checksum_mismatch`
- [x] 4.7 Admin API: регистрация/правка реестра (protocol, enumerate_enabled, auth_config), test connection

## 5. Наблюдаемость и приёмка

- [x] 5.1 Метрики: probe duration/outcome, enumeration progress, ошибки по классам — в разрезе registry
- [x] 5.2 Интеграционный сценарий: два pypi-реестра (json + simple-фикстура а-ля PyTorch index), версии объединяются, артефакты помнят реестр
- [x] 5.3 Acceptance smoke: probe latest после seed, admin probe, enumeration внутреннего hosted
- [x] 5.4 README/runbook: регистрация реестров, Nexus hosted-only, разбор probe-ошибок
