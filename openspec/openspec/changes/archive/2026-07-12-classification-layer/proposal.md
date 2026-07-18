# Proposal: Слой классификации (порт classifire L1)

## Why

Классификатор — дифференциатор продукта относительно socket.dev/deps.dev и вторая из двух ключевых задач объединённого решения. Правила classifire v2.4.0 отлажены на датасете; фетчер (probe + version_descriptions) уже собирает все нужные входы. Перенос в PackageHub делает классификацию событийной (после probe) и мультиреестровой вместо ручных прогонов по SQLite.

## What Changes

- Схема classifire переносится в PostgreSQL: `classifications` (версионируются по `classifier_version`, история правил сохраняется), `classification_facets` (роль+уверенность+evidence+provisional), `classification_capabilities`, `user_capabilities` (кураторские пометки — единственные невоспроизводимые данные, классификатор их не трогает), `role_overrides` и `entry_group_rules` (реестры правил, правятся в БД), `package_relations` (plugin-of, platform-payload), `llm_verdicts` (резерв L2).
- Правила classify_npm / classify_pypi портируются 1:1 (CLASSIFIER_VERSION 2.4.0 сохраняется — датасет сопоставим): 10 ролей, 16 capabilities, статусы ok / needs-data / undecidable-by-spec, терминальные override, entry-point-группы, плагинные конвенции.
- Классификация — событийная: probe (успешный ИЛИ unusable — «probe недоступен» тоже вход) ставит classify-задачу версии. Батч: `classify [--reclassify]` (CLI и admin API) — все версии с представительным probe.
- Адаптеры входов: npm — манифест версии (raw_metadata версии), pypi — {info, urls} пакета (соответствует latest, которую и классифицируем). Попутно исправлено хранение pypi raw_metadata: `releases` отрезаются (правило D9 fetcher-redesign, было упущено).
- Seed реестров правил идемпотентен (ON CONFLICT DO NOTHING) — локальные правки кураторов не перетираются.

## Capabilities

### New Capabilities

- `classification`: структурная классификация версий пакетов — роли, свойства, статусы, реестры правил, версионирование правил, событийный и батчевый запуск.

### Modified Capabilities

<!-- нет: фетчер-спеки не меняются, probe уже описывает вход -->

## Impact

- Код: `app/classifier/` (core, npm, pypi, registries_data, service), `tasks/classify.py`, хук в probe-задаче, admin `POST /classify`, CLI `classify`, миграция 0007.
- Не затрагивается: выдача классификации в каталожное API и курация аннотаций (следующий чейндж), резолвер, UI.
