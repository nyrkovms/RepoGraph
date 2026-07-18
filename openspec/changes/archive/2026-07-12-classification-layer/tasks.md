# Tasks: classification-layer

Реализация — PackageHub, ветка feature/unified-fetcher-classifier.

## 1. Схема

- [x] 1.1 Миграция 0007: classifications, classification_facets, classification_capabilities, user_capabilities, role_overrides, entry_group_rules, package_relations, llm_verdicts + модели SQLAlchemy

## 2. Порт классификатора

- [x] 2.1 `classifier/core.py`: Mark/Result/ProbeView/lower_blob/parse_entry_points/match_entry_group/PLATFORM_SUFFIX
- [x] 2.2 `classifier/npm.py`: classify_npm + finalize + platform_payload_relations (1:1 с classifire 2.4.0)
- [x] 2.3 `classifier/pypi.py`: classify_pypi (1:1)
- [x] 2.4 `classifier/registries_data.py`: ENTRY_GROUP_RULES (35) + ROLE_OVERRIDES (51)
- [x] 2.5 `classifier/service.py`: seed реестров (идемпотентно), адаптеры манифеста/probe, хранение вердикта, связи
- [x] 2.6 Фикс pypi raw_metadata: {info, urls} без releases (D9)

## 3. Запуск

- [x] 3.1 Задача classify_version + батч run_classification(--reclassify)
- [x] 3.2 Хук: probe (probed/unusable) → enqueue classify (4 точки выхода)
- [x] 3.3 Admin POST /classify, CLI classify --reclassify --limit

## 4. Проверка

- [x] 4.1 Юнит-тесты правил: 25 эталонных кейсов (ruff-паттерн, eslint-плагин, @types, override-терминальность, entry-группы, статусы undecidable/needs-data, platform-payload)
- [x] 4.2 Живая проверка (docker): 7 версий классифицированы, эталоны совпали (ruff → dev-tool+binary-payload без importable-api; flask → override; httpie → application); реестры засеяны (35+51)
- [x] 4.3 Полный прогон unit+integration+lint, коммит этапа
