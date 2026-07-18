# Tasks: classification-api

## 1. Эффективные свойства

- [x] 1.1 `classifier/effective.py`: merge_effective (чистая функция: machine − present=false + present=true, версия побеждает ''), загрузка машинных свойств и пометок; юнит-тесты
- [x] 1.2 SQL-предикат эффективного свойства для фильтров (correlated exists)

## 2. API

- [x] 2.1 Карточка пакета: блок classification (latest) + registries с флагами коллизий
- [x] 2.2 Фильтры /packages: role, status, cap_all, cap_none
- [x] 2.3 /stats: блок классификации (by_status, роли, эффективные свойства)
- [x] 2.4 POST /annotate + /annotate/delete (admin), валидация, чтение аннотаций в карточке

## 3. Экспорт

- [x] 3.1 CLI export-annotations / import-annotations [--replace] (формат classifire)
- [x] 3.2 CLI export-llm-input (--max-chars, descriptions по origin)

## 4. Проверка

- [x] 4.1 Интеграционные тесты: карточка с классификацией, RBAC аннотаций, эффект пометки на фильтр
- [x] 4.2 Полный прогон, живой сценарий (docker), архив чейнджа, коммит
