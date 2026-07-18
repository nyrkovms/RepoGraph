# Design: classification-layer

## Context

classifire v2.4.0: отлаженные правила classify_npm/classify_pypi поверх снапшота (манифест) + dist_probe, реестры правил в БД, инварианты (версионируемые вердикты, неприкосновенные user_capabilities, терминальные overrides). PackageHub после npm-connector собирает все входы: манифесты версий (npm), info+urls (pypi), dist_probes, version_descriptions.

## Goals / Non-Goals

**Goals:** порт правил 1:1 (сопоставимость датасета), событийная классификация, реестры правил в PostgreSQL, инварианты classifire сохранены. **Non-Goals:** выдача в каталожное API, курация/аннотации, экспорт llm_input (следующий чейндж), maven/go классификаторы, LLM-слой (llm_verdicts — резерв).

## Decisions

### D1. Порт 1:1 с сохранением CLASSIFIER_VERSION 2.4.0
Правила не «улучшаются» при переносе — иначе датасеты classifire и PackageHub разойдутся без возможности сверки. Любая правка правил — отдельный чейндж с bump версии. Кодовая структура: `classifier/core.py` (Mark/Result/parse_entry_points), `npm.py`, `pypi.py`, `registries_data.py` — тексты функций идентичны classifire с точностью до адаптеров входа.

### D2. Адаптер входа: ProbeView вместо sqlite Row
Классификатор не знает о SQLAlchemy: `ProbeView(entry_points_txt, top_level_txt, file_facts, error, available)` строится из представительной строки dist_probes. `probe_failed` = отсутствие строки ИЛИ error — семантика classifire сохранена.

### D3. Манифесты: npm per-version, pypi per-package
npm: манифест версии хранится в raw_metadata версии — классифицируется именно он. pypi: JSON API отдаёт info только для latest; классифицируем latest-версию (ту же, что probe), манифест = {info, urls} из raw_metadata пакета. Попутный фикс: pypi raw_metadata теперь режется до {info, urls} (D9 fetcher-redesign был задекларирован, но не реализован).

### D4. Триггер — завершённый probe, включая unusable
classifire классифицирует и без probe (probe_failed-ветки правил: PyPI-4 менеджеры пакетов, sdist-only). Поэтому classify ставится после ЛЮБОГО завершённого probe (probed/unusable), а не только успешного. Retryable-сбой probe классификацию не запускает — дождёмся ретрая.

### D5. Хранение вердикта
DELETE строк текущей classifier_version + INSERT (семантика classifire store_classification); CASCADE на фасеты/свойства. plugin-of пересоздаётся, platform-payload дозаписывается (ON CONFLICT DO NOTHING) — инвариант 6 classifire.

## Risks / Trade-offs

- [Правила писались под classifire-снапшоты; поля манифестов могли отличаться] → адаптеры выравнивают форму; эталонные кейсы (ruff, flask, httpie, eslint-plugin, @types) закреплены юнит-тестами и проверены на живых данных.
- [pypi классифицируется только latest] → соответствует classifire (снапшот = latest/pinned); классификация исторических версий потребует per-version манифестов (lazy fetch) — при необходимости отдельный чейндж.
- [セед реестров при каждом батче/таске] → ON CONFLICT DO NOTHING, 86 строк — копейки; проверка count не нужна.

## Migration Plan

Миграция 0007 (8 таблиц) аддитивна; rollback — downgrade. Классификация включается автоматически после деплоя (событийно); исторические версии с probe добираются `classify` батчем.

## Open Questions

- Перенос датасета classifire (SQLite): классификации воспроизводимы повторным прогоном — переносить нужно только user_capabilities (annotations.jsonl, следующий чейндж).
