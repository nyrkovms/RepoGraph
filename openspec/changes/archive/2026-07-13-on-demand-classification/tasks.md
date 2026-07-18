# Tasks: on-demand-classification

- [x] 1.1 Настройка `facts_timeout_seconds`; оркестратор ensure_facts (lazy fetch → inline probe latest → inline classify → сборка ответа), переиспользование корутин probe/classify
- [x] 1.2 Эндпоинт GET /api/v1/facts/{ecosystem}/{name} (+refresh=true), схема ответа без полей вердикта, маппинг ошибок 404/502/503/504
- [x] 2.1 Интеграционные тесты: холодный вызов (пакет не в каталоге) собирает факты за один запрос; повторный — из каталога; refresh; контракт «без вердиктов»; RBAC viewer
- [x] 2.2 README: позиционирование «только факты», блок «режим обновления — требует проработки» со ссылкой на open questions
- [x] 2.3 Полный прогон тестов, живая проверка (docker), архив чейнджа, коммит
