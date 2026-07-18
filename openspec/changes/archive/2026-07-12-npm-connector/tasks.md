# Tasks: npm-connector

Реализация — PackageHub (`/Users/mikhail/Documents/Work/repograph`), ветка feature/unified-fetcher-classifier.

## 1. Схема и нормализация

- [ ] 1.1 Миграция: `artifacts.checksum_sha1`, `artifacts.checksum_integrity` + модель
- [ ] 1.2 `normalize_name(ecosystem, name)`: pypi — PEP 503, npm — lowercase с сохранением scope/точек; юнит-тесты (`lodash.merge` ≠ `lodash-merge`, `@Scope/Pkg` → lowercase)
- [ ] 1.3 `NormalizedPackage`: поля `namespace`, `dist_tags`; `NormalizedArtifact`: `checksum_sha1`, `checksum_integrity`; `NormalizedVersion.deprecation_message`

## 2. Pipeline ecosystem-aware

- [ ] 2.1 `_replace_dependencies`: экосистема зависимости от пакета (без хардкода pypi)
- [ ] 2.2 `_resolve_level1`: нормализация имени по экосистеме зависимости; npm — latest stable без constraint
- [ ] 2.3 `_upsert_package`: namespace из NormalizedPackage; deprecated/deprecation_message пакета
- [ ] 2.4 dist-tags: upsert всех тегов из `NormalizedPackage.dist_tags`
- [ ] 2.5 Источник checksum для `find_checksum_conflicts`: sha256 → integrity → sha1; юнит-тест

## 3. Коннектор npm

- [ ] 3.1 `connectors/npm.py`: packument → NormalizedPackage (versions, dist-tags, deprecated, maintainers, license, artifacts c shasum/integrity); scoped-URL-энкодинг; retry/auth через RegistryClient; `fetch_version_dependencies` через versioned endpoint
- [ ] 3.2 Маппинг зависимостей: dependencies/devDependencies/peerDependencies/optionalDependencies → dependency_type; юнит-тесты на packument-фикстуре (scoped, deprecated, dist-tags, integrity)
- [ ] 3.3 Фабрика: protocol `npm`; seed-строка реестра npm (slug `npm`, base_url registry.npmjs.org); `test_connection`
- [ ] 3.4 Ленивая гидратация зависимостей npm-версии (ensure_version_dependencies per-ecosystem коннектор)

## 4. npm-probe

- [ ] 4.1 `probe/extract_npm.py`: tarball → file_facts (total, ext_counts, has_index_js), README ≤500K; in-memory, без extractall; юнит-тесты на синтетических tgz
- [ ] 4.2 Селектор per-ecosystem: npm — tgz-артефакт, лимит по unpackedSize/размеру payload; юнит-тесты
- [ ] 4.3 Задача probe: диспетчеризация по экосистеме, README → version_descriptions origin `tarball-readme`

## 5. Проверка

- [ ] 5.1 Юнит-тесты зелёные, ruff чистый
- [ ] 5.2 Интеграционно (docker): sync реального npm-пакета (react или eslint) + probe → строка dist_probes + tarball-readme
- [ ] 5.3 Обновить README (npm в списке протоколов), архивировать чейндж, коммит этапа
