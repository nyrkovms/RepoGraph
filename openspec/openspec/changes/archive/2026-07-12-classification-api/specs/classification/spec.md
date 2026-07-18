# classification: выдача, курация, экспорт

## ADDED Requirements

### Requirement: Эффективные свойства
Эффективный набор свойств версии SHALL вычисляться как: машинные свойства текущей версии правил − пометки present=false + пометки present=true; кураторская пометка SHALL побеждать машинный факт при конфликте; пометка конкретной версии SHALL побеждать пометку «все версии» (version=''). Правило SHALL применяться во всех точках чтения: карточка, фильтры, статистика, экспорт. У каждого эффективного свойства SHALL быть origin: `structural` или `user`.

#### Scenario: Куратор снимает ложное свойство
- **WHEN** у версии машинное свойство `importable-api`, и куратор ставит пометку present=false
- **THEN** эффективный набор не содержит `importable-api`, фильтр `cap_none=importable-api` находит пакет

#### Scenario: Пометка конкретной версии побеждает общую
- **WHEN** пометка version='' снимает свойство, а пометка version=X добавляет его
- **THEN** для версии X свойство эффективно присутствует с origin user

### Requirement: Классификация в карточке пакета
Карточка пакета SHALL включать блок классификации latest-версии: статус с причиной, версию правил, фасеты (роль, уверенность, evidence, provisional), машинные свойства с evidence, эффективные свойства с origin, ручные пометки, связи пакета, а также список реестров присутствия с признаками primary/name_collision/checksum_mismatch. Отсутствие вердикта MUST NOT ломать карточку (блок null).

#### Scenario: Карточка классифицированного пакета
- **WHEN** запрошена карточка пакета с вердиктом
- **THEN** ответ содержит роли с evidence и эффективные свойства

### Requirement: Фасетные фильтры списка пакетов
Список пакетов SHALL поддерживать фильтры: `role` (роль присутствует у latest-версии), `status` (статус вердикта), `cap_all` (каждое из перечисленных свойств эффективно присутствует), `cap_none` (ни одно эффективно не присутствует).

#### Scenario: Поиск dev-tool без importable-api
- **WHEN** запрошен список с role=dev-tool и cap_none=importable-api
- **THEN** ruff в результатах (dev-tool, importable-api отсутствует эффективно)

### Requirement: Статистика классификации
`/stats` SHALL включать блок классификации: количество вердиктов текущей версии правил по статусам, счётчики пакетов по ролям и по эффективным свойствам; свойства с нулём пакетов MAY опускаться.

#### Scenario: Сводка датасета
- **WHEN** запрошен /stats после классификации
- **THEN** ответ содержит счётчики по ролям и статусам

### Requirement: Курация пометок под RBAC
`POST /api/v1/annotate` SHALL выполнять upsert пометки {ecosystem, name, capability, present, note, version?=''} и `POST /api/v1/annotate/delete` — удаление; оба SHALL требовать admin-токен; чтение пометок доступно viewer. Валидация SHALL отклонять неизвестные ecosystem/capability и пакеты вне каталога.

#### Scenario: Viewer не может аннотировать
- **WHEN** viewer-токен вызывает POST /annotate
- **THEN** ответ 403, пометка не создана

#### Scenario: Невалидное свойство
- **WHEN** admin передаёт capability вне известного списка
- **THEN** ответ 422 с перечнем допустимых значений

### Requirement: Экспорт и перенос
CLI SHALL поддерживать: `export-annotations`/`import-annotations [--replace]` в формате annotations.jsonl classifire (ecosystem, name, version, capability, present, note, created_at; импорт по умолчанию пропускает существующие) и `export-llm-input` (JSONL: не-ok вердикты текущей версии правил — ecosystem, name, version, status, status_reason, structural_roles, capabilities эффективные, user_annotations, summary, descriptions по origin с обрезкой --max-chars, exported_at).

#### Scenario: Перенос пометок из classifire
- **WHEN** выполнен import-annotations с файлом, выгруженным из classifire
- **THEN** пометки применяются к эффективным свойствам каталога; существующие не перезаписаны без --replace

#### Scenario: Вход LLM-слоя
- **WHEN** выполнен export-llm-input
- **THEN** каждый не-ok вердикт даёт JSONL-запись с описаниями версии (registry/wheel-metadata/tarball-readme)
