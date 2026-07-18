# Proposal: npm-коннектор и npm-probe

## Why

Фетчер (fetcher-redesign) поддерживает только pypi-протоколы; npm — вторая экосистема датасета classifire и обязательная для объединённого решения (внутренние Verdaccio/Nexus npm hosted говорят на том же протоколе, что npmjs.org — один коннектор закрывает и публичный, и внутренние реестры). Классификатору нужны npm-факты (tarball probe: has_index_js, bin-скрипты, README) — заготовки уже отлажены в classifire.

## What Changes

- Коннектор протокола `npm`: полный packument для метаданных пакета (все версии, dist-tags, deprecated, maintainers), versioned endpoint `{name}/{version}` для ленивой гидратации зависимостей исторических версий. Экономия хранения: `raw_metadata` пакета — без массива `versions`.
- Scoped-имена: `@scope/pkg` → `namespace = scope`, `normalized_name` = полное имя lowercase. Нормализация имён становится ecosystem-aware: правило PEP 503 (замена `._-`) НЕ применяется к npm (там `lodash.merge` и `lodash-merge` — разные пакеты).
- Pipeline становится ecosystem-aware: зависимость несёт экосистему пакета (был хардкод pypi), Level-1 resolve нормализует имя по экосистеме зависимости. **Ограничение (зафиксировано):** Level-1 resolve для npm выбирает latest stable без проверки node-semver constraint (полный semver-матчинг — вместе с npm-resolve Level 2, вне scope).
- Все dist-tags пакета сохраняются (npm: latest/next/beta; для pypi — только latest, как раньше).
- Artifact получает `checksum_sha1` (npm shasum) и `checksum_integrity` (SRI sha512) — колонки из полной SPEC.
- npm-probe: tarball ≤ лимита по `unpackedSize`, факты (total, ext_counts, has_index_js), README из tarball → `version_descriptions` (origin `tarball-readme`) — порт из classifire.
- Peer/dev/optional зависимости npm маппятся в существующий enum `dependency_type`.

## Capabilities

### New Capabilities

<!-- нет: npm ложится в существующие спеки -->

### Modified Capabilities

- `fetch-metadata`: добавляются требования npm-протокола (packument, scoped-имена, dist-tags, ecosystem-aware нормализация и resolve).
- `fetch-probe`: добавляется требование npm-probe (tarball → факты + README).

## Impact

- Код: `connectors/npm.py`, `normalizers/names.py` (ecosystem-aware), `fetcher/pipeline.py` (ecosystem-aware deps/resolve, dist-tags, namespace), `probe/` (tarball-извлечение, селектор per-ecosystem), фабрика протоколов, seed-строка npm-реестра.
- Схема: миграция — `artifacts.checksum_sha1`, `artifacts.checksum_integrity`.
- Не затрагивается: resolver Level 2 (остаётся pypi-only), UI.
