# fetch-metadata: npm-протокол

## ADDED Requirements

### Requirement: Коннектор протокола npm
Реестры с протоколом `npm` (npmjs.org, Verdaccio, Nexus npm hosted) SHALL обслуживаться одним коннектором: метаданные пакета — полный packument (версии, dist-tags, deprecated, maintainers, license, readme), зависимости latest-версии — из packument, исторических версий — лениво через versioned endpoint. `raw_metadata` пакета MUST NOT содержать массив `versions` packument-а.

#### Scenario: Sync npm-пакета
- **WHEN** синхронизируется npm-пакет с 50 версиями и dist-tags latest/next
- **THEN** в каталоге появляются все 50 версий с артефактами, зависимости заполнены у версии тега latest, оба dist-tag сохранены

#### Scenario: Внутренний Verdaccio hosted
- **WHEN** зарегистрирован corporate-реестр с протоколом `npm` и auth
- **THEN** его пакеты синхронизируются тем же коннектором с auth-заголовком только на хост реестра

### Requirement: Scoped-имена и ecosystem-aware нормализация
Для npm `@scope/pkg` SHALL сохраняться `namespace = scope` и `normalized_name` = полное имя в lowercase. Нормализация имён SHALL зависеть от экосистемы: правило PEP 503 (схлопывание `.`/`_`/`-`) MUST NOT применяться к npm-именам.

#### Scenario: Пакеты с точкой в имени
- **WHEN** синхронизированы npm-пакеты `lodash.merge` и `lodash-merge`
- **THEN** это два разных пакета каталога

#### Scenario: Scoped-пакет
- **WHEN** синхронизируется `@types/node`
- **THEN** пакет имеет namespace `types`, находится поиском по `@types/node`

### Requirement: Ecosystem-aware зависимости и Level-1 resolve
Строка зависимости SHALL нести экосистему пакета-владельца; Level-1 resolve SHALL нормализовать имя зависимости по правилам её экосистемы. Для npm-зависимостей Level-1 SHALL выбирать latest stable версию целевого пакета без применения node-semver constraint (ограничение зафиксировано до появления npm-стратегии полного резолвера); dev/peer/optional типы npm SHALL маппиться в enum `dependency_type`.

#### Scenario: npm-зависимость на пакет каталога
- **WHEN** latest-версия npm-пакета зависит от `lodash@^4.17.0` и lodash есть в каталоге
- **THEN** зависимость получает resolved-ссылку на latest stable версию lodash, тип `runtime`, raw_spec `^4.17.0`

#### Scenario: peerDependencies
- **WHEN** плагин объявляет `peerDependencies: {"eslint": ">=8"}`
- **THEN** зависимость сохраняется с типом `peer`

### Requirement: dist-tags пакета
Все dist-tags npm-пакета SHALL сохраняться и обновляться при sync (upsert по паре пакет+тег). Для экосистем без тегов SHALL сохраняться тег `latest`.

#### Scenario: Смещение тега next
- **WHEN** при refresh у пакета dist-tag `next` указывает на новую версию
- **THEN** строка тега `next` обновлена, тег `latest` не затронут

### Requirement: Checksums артефактов npm
Артефакты npm SHALL сохранять `shasum` (SHA-1) и `integrity` (SRI) из packument; детект расхождения checksum между реестрами SHALL сравнивать первый доступный из sha256/integrity/sha1.

#### Scenario: Расхождение integrity между реестрами
- **WHEN** один filename npm-версии в двух реестрах имеет разные integrity
- **THEN** взводится флаг checksum_mismatch на связках пакет↔реестр
