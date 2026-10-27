# PackageHub MVP — Production Checklist

Дата: 2026-03-09

## 1. Security baseline

- [x] API не стартует в `APP_ENV=production` с дефолтным admin-токеном.
- [ ] Read API и Web UI доступны только из внутренней сети, VPN или через OIDC-прокси. Read-эндпоинты работают без токена, а `POST /resolve` может пополнять каталог.
- [x] API не стартует в `APP_ENV=production` с wildcard CORS/Trusted Hosts.
- [x] Зафиксированы security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`).
- [x] Сравнение API токенов выполняется через constant-time compare.
- [ ] TLS termination (Nginx/Traefik/ALB) настроен перед API.
- [ ] Секреты вынесены из файлов в secret manager.

## 2. Runtime configuration

- [x] Добавлен production env-template: [`.env.prod.example`](/Users/mikhail/Documents/Work/repograph/.env.prod.example)
- [x] В проде можно отключить docs/openapi через `API_DOCS_ENABLED=false`.
- [x] CORS/TrustedHosts настраиваются через env (`CORS_ALLOW_ORIGINS`, `TRUSTED_HOSTS`).
- [ ] Для `frontend` используется production build + CDN/reverse proxy (не dev server).

## 3. Data durability

- [x] Добавлен backup script: [`scripts/db_backup.sh`](/Users/mikhail/Documents/Work/repograph/scripts/db_backup.sh)
- [x] Добавлен restore script: [`scripts/db_restore.sh`](/Users/mikhail/Documents/Work/repograph/scripts/db_restore.sh) (с рестартом `api/worker` после restore)
- [ ] Настроен регулярный backup cron и retention policy.
- [ ] Пройден restore drill на отдельном окружении.

## 4. Observability

- [x] Prometheus endpoint и базовые метрики доступны на `/api/v1/metrics`.
- [ ] Настроены алерты (latency/error-rate/sync-failures).
- [ ] Настроен сбор логов (Loki/ELK/Cloud logging) и дашборды.

## 5. Quality gates

- [x] CI lint gates (`backend-lint`, `frontend-lint`).
- [x] CI integration tests (`frontend-tests`, `backend-integration-tests`).
- [x] CI acceptance smoke.
- [ ] Периодический dependency/security scan (`pip-audit`, `npm audit`, image scan).

## 6. Go-live minimum

Перед go-live должны быть закрыты все пункты с `[ ]` в разделах 1-3.
