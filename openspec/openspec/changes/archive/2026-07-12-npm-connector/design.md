# Design: npm-коннектор

## Context

Протокол npm един для npmjs.org, Verdaccio и Nexus npm hosted (D11 fetcher-redesign). classifire уже содержит отлаженные npm-механики: versioned endpoint вместо полного packument для одной версии, tarball-probe с лимитом по `unpackedSize`, README по regex из tarball. PackageHub-pipeline после fetcher-redesign реестро-осознан, но экосистемно — нет (хардкод pypi в зависимостях и resolve).

## Goals / Non-Goals

**Goals:** npm-пакеты (включая scoped и внутренние реестры) в каталоге с версиями, зависимостями latest, dist-tags, deprecated, артефактами с integrity; npm-probe для классификатора; pipeline без pypi-хардкодов.

**Non-Goals:** node-semver матчинг в Level-1 resolve (берём latest stable); npm в resolver Level 2 (`/resolve` остаётся pypi); downloads-статистика (отдельный API npm); OSV-уязвимости для npm.

## Decisions

### D1. Метаданные — полный packument, гидратация исторических версий — versioned endpoint

Полный packument (`GET {base}/{encoded_name}`) — единственный способ получить все версии+dist-tags за один запрос; abbreviated-форма не отдаёт description/maintainers/deprecated-сообщения. Зависимости сохраняются только для latest (консистентно с pypi); исторические версии гидратируются лениво через лёгкий `GET {base}/{name}/{version}` (у pypi аналогично — `{name}/{version}/json`). `raw_metadata` пакета — packument без `versions` (мегабайты у популярных пакетов; экономия — правило D9 fetcher-redesign).

### D2. Ecosystem-aware нормализация имён

`normalize_name(ecosystem, name)`: pypi — PEP 503 canonicalize; npm — lowercase, scope сохраняется, точки/подчёркивания НЕ трогаются (`lodash.merge` ≠ `lodash-merge`). Существующий `normalize_package_name` остаётся как pypi-алиас для обратной совместимости вызовов.

### D3. Level-1 resolve для npm — latest stable, без constraint-матчинга

`_find_best_version_id` использует PEP 440 (`packaging`); node-semver диапазоны (`^`, `~`, `||`, `1.x`) им не парсятся. Честное поведение: для npm-зависимостей резолвится latest stable версия целевого пакета, constraint игнорируется (как и было для нераспарсенных ограничений). Полный node-semver — отдельный чейндж вместе с npm-стратегией резолвера (greedy DFS). Альтернатива «подтянуть node-semver библиотеку» отклонена: полуправильный Level-1 хуже задокументированного приближения, а Level 2 всё равно потребует полной модели.

### D4. Probe per-ecosystem: селектор и извлечение диспетчеризуются по экосистеме

npm: единственный tgz-артефакт версии; лимит — `dist.unpackedSize` (до скачивания) и размер payload (после); извлечение:总 файлов, ext_counts, `has_index_js` (package/index.{js,cjs,mjs,node}), README (`package/readme*`, регистронезависимо, ≤500K) → `version_descriptions` origin `tarball-readme`. tarfile — только чтение известных имён в памяти, без extractall (инвариант fetch-probe). package.json из tarball не парсится — манифест уже есть в раw_metadata версии.

### D5. dist-tags и deprecated

`NormalizedPackage.dist_tags: dict[str, str]` — pipeline пишет все теги (upsert по (package, tag)); для pypi заполняется `{"latest": ...}`. npm `versions[v].deprecated` (строка) → status `deprecated` версии; deprecated самого пакета (latest deprecated) → `package.deprecated` + message (поля уже в модели).

### D6. Checksums

npm `dist.shasum` — SHA-1, `dist.integrity` — SRI (sha512-...). Колонки `checksum_sha1`, `checksum_integrity` добавляются в artifacts (из полной SPEC); sha256 у npm отсутствует — колонка остаётся NULL, checksum-конфликт для npm детектится по integrity (расширение `find_checksum_conflicts`: сравнение по первому непустому из sha256/integrity/sha1).

## Risks / Trade-offs

- [Полный packument популярного пакета — мегабайты] → одна загрузка на sync, в БД не дублируется (versions отрезаются), abbreviated не подходит по полноте.
- [Level-1 resolve npm игнорирует constraint] → задокументировано в спеке; деревья в UI помечаются resolve_strategy=latest_stable (существующее поле контракта).
- [npm-имена регистрозависимы исторически (старые пакеты с заглавными)] → normalized_name lowercase может склеить legacy-дубли; принято: новые имена npm — lowercase-only с 2017, коллизии крайне редки и видны по name.

## Migration Plan

1. Миграция: `artifacts.checksum_sha1`, `artifacts.checksum_integrity` (+ индексы не нужны).
2. Нормализация/сериализация: `normalize_name(ecosystem, ...)`, NormalizedPackage.namespace/dist_tags.
3. Pipeline ecosystem-aware (deps, resolve, namespace, dist-tags loop).
4. Коннектор npm + фабрика + seed npm-реестра (slug `npm`, protocol `npm`).
5. Probe: селектор/извлечение per-ecosystem.
6. Тесты, приёмка. Rollback: npm-реестр `sync_enabled=false` возвращает pypi-only поведение.

## Open Questions

- Downloads (api.npmjs.org) и OSV для npm — отдельные чейнджи слоя обогащения.
