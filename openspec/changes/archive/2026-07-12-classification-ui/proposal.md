# Proposal: Классификация и мультиреестры в веб-интерфейсе

## Why

API отдаёт классификацию и мультиреестровые факты (classification-api), но веб-интерфейс — основной способ работы безопасника и куратора с датасетом — их не показывает.

## What Changes

- Карточка пакета: секция Classification — статус (needs-data/undecidable подсвечены), версия правил, роли (provisional/low помечены, evidence в tooltip), эффективные свойства (кураторские помечены), пометки кураторов, связи, реестры присутствия с бейджами name collision / checksum mismatch.
- Страница поиска: фасетные фильтры Role / Status / Ecosystem, бейдж экосистемы в результатах (каталог мультиэкосистемный).
- Backend: `/search` принимает те же фасетные параметры, что и `/packages`.

## Capabilities

### Modified Capabilities

- `classification`: требование выдачи в веб-интерфейсе.

## Impact

- frontend: PackagePage (секция), SearchPage (фильтры), тесты vitest.
- backend: passthrough фильтров в /search.
