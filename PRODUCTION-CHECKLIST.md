# PackageHub MVP — Production Checklist

Дата: 2026-11-07

## 1. Security baseline

- [x] API не стартует в `APP_ENV=production` с дефолтным admin-токеном.
- [ ] Read API и Web UI доступны только из внутренней сети, VPN или через OIDC-прокси. Read-эндпоинты работают без токена, а `POST /resolve` может пополнять каталог.
- [x] API не стартует в `APP_ENV=production` с wildcard CORS/Trusted Hosts.
- [x] Зафиксированы security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`).
- [x] Сравнение API токенов выполняется через constant-time compare.
- [ ] TLS termination (Nginx/Traefik/ALB) настроен перед API.
- [ ] Секреты вынесены из файлов в secret manager.
- [ ] Каталог `secrets/` (`PACKAGEHUB_SECRETS_DIR`, монтируется в `rpm-worker` только на чтение): клиентские сертификаты (`<имя>.crt`/`.key`) и учётные данные закрытых источников лежат вне git, права ограничены.

## 2. Runtime configuration

- [x] Добавлен production env-template: [`.env.prod.example`](.env.prod.example)
- [x] В проде можно отключить docs/openapi через `API_DOCS_ENABLED=false`.
- [x] CORS/TrustedHosts настраиваются через env (`CORS_ALLOW_ORIGINS`, `TRUSTED_HOSTS`).
- [ ] Контейнер `rpm-worker` запущен в проде (очередь `rpm`, volume кэша repodata `rpm_cache`).
- [ ] Для `frontend` используется production build + CDN/reverse proxy (не dev server).

## 3. Data durability

- [x] Добавлен backup script: [`scripts/db_backup.sh`](scripts/db_backup.sh)
- [x] Добавлен restore script: [`scripts/db_restore.sh`](scripts/db_restore.sh) (с рестартом `api/worker` после restore)
- [ ] Настроен регулярный backup cron и retention policy.
- [ ] Пройден restore drill на отдельном окружении.

## 4. Observability

- [x] Prometheus endpoint и базовые метрики доступны на `/api/v1/metrics`.
- [ ] Настроены алерты (latency/error-rate/sync-failures).
- [ ] Алерт на `packagehub_source_degraded == 1` (источник за circuit breaker).
- [ ] Алерт на устаревший `packagehub_daily_update_last_success_timestamp` (ежедневный прогон не проходил более суток).
- [ ] Расписания проверены: daily update (`DAILY_UPDATE_HOUR_UTC`, по умолчанию 03:00 UTC) и еженедельный presence probe (`PRESENCE_PROBE_*`, воскресенье 04:00 UTC) работают на `beat`.
- [ ] Настроен сбор логов (Loki/ELK/Cloud logging) и дашборды.

## 5. Quality gates

- [x] CI lint и типы (`typecheck`, `backend-lint`, `frontend-lint`).
- [x] CI unit-тесты (`backend-unit-tests`, `frontend-tests`, `rpm-worker-tests`) и миграции (`migrations`).
- [x] CI integration tests (`backend-integration-tests`).
- [x] CI acceptance smoke.
- [ ] Периодический dependency/security scan (`pip-audit`, `npm audit`, image scan).

## 6. Go-live minimum

Перед go-live должны быть закрыты все пункты с `[ ]` в разделах 1-3.
