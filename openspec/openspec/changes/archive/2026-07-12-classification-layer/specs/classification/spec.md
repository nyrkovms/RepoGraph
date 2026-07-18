# classification: структурный классификатор версий

## ADDED Requirements

### Requirement: Фасетная модель вердикта
Классификатор SHALL записывать для версии пакета: роли (фасеты) с уверенностью high/low, evidence и признаком provisional; машинные свойства (capabilities) с уверенностью и evidence; статус `ok` / `needs-data` / `undecidable-by-spec` с причиной. Роли и свойства SHALL соответствовать модели classifire v2.4.0 (10 ролей, 16 свойств).

#### Scenario: Классификация ruff
- **WHEN** классифицируется pypi-пакет с платформенным wheel, bin-скриптом мимо console_scripts и ≤3 py-файлами
- **THEN** свойства содержат `cli` и `binary-payload` (high) без `importable-api`, роль — `dev-tool`

#### Scenario: Плагин eslint
- **WHEN** классифицируется npm-пакет с peerDependency на eslint
- **THEN** свойство `plugin` high, связь `plugin-of -> eslint`, роль `buildtime-component`

### Requirement: Версионирование правил
Каждый вердикт SHALL нести `classifier_version`; повторная классификация SHALL заменять только строки текущей версии правил, история прошлых версий SHALL сохраняться. Уникальность — по паре (версия пакета, версия правил).

#### Scenario: Reclassify после смены правил
- **WHEN** CLASSIFIER_VERSION поднята и выполнен reclassify
- **THEN** появляются вердикты новой версии правил, вердикты старой версии остаются для сравнения

### Requirement: Кураторские реестры правил в БД
Реестры `role_overrides` (терминальные наборы ролей: эвристики не доклеивают роли поверх) и `entry_group_rules` (таксономия entry-point групп PyPI: pattern с `*`-префиксом, group_type, role_effect, host) SHALL храниться в БД и правиться без изменения кода. Первичное наполнение SHALL быть идемпотентным и MUST NOT перетирать локальные правки.

#### Scenario: Override терминален
- **WHEN** классифицируется пакет с кураторским набором ролей
- **THEN** роли равны набору из override, статус ok, эвристики ролей не применяются

#### Scenario: Неизвестная entry-point группа
- **WHEN** у pypi-пакета есть entry-point группа вне реестра
- **THEN** статус вердикта `needs-data` с именем группы в причине

### Requirement: Событийная и батчевая классификация
Завершённый probe версии (включая permanent-ошибку — недоступность дистрибутива тоже вход) SHALL ставить задачу классификации этой версии. Батч-команда SHALL классифицировать все версии с представительным probe без вердикта текущей версии правил; `--reclassify` SHALL пересчитывать вердикты текущей версии правил.

#### Scenario: Автоматическая классификация после sync
- **WHEN** пакет синхронизирован и probe его latest-версии завершился
- **THEN** без ручных действий появляется вердикт классификатора для этой версии

### Requirement: Связи пакетов
Классификация SHALL перезаписывать связи `plugin-of` пакета (пересоздаются при каждом прогоне) и дозаписывать `platform-payload` (npm optionalDependencies с платформенным суффиксом); связи SHALL быть доступны как факты пакета.

#### Scenario: platform-payload
- **WHEN** классифицируется npm-пакет с optionalDependency `opencode-linux-x64`
- **THEN** появляется связь `platform-payload -> opencode-linux-x64`

### Requirement: Неприкосновенность кураторских пометок
Таблица `user_capabilities` (ручные пометки свойств: present/absent, note, version=''—все версии) MUST NOT изменяться классификатором ни при каком прогоне, включая reclassify.

#### Scenario: Reclassify не трогает пометки
- **WHEN** выполнен reclassify при существующих ручных пометках
- **THEN** строки user_capabilities не изменены
