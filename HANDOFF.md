# HANDOFF — объединённое решение PackageHub + classifire

Передача для следующей итерации (Opus). Составлено Fable 5, 2026-07-13.

## Что это

PackageHub (этот репозиторий) — **каталог фактов** о пакетах: мультиреестровый
фетчер + probe дистрибутивов + классификатор ролей/свойств (порт classifire
2.4.0). Позиционирование: решение выдаёт ТОЛЬКО факты; вердикты допуска/
блокировки принимает система проверок прокси-репозитория (вне решения).
classifire (`~/Claude/Projects/package-classifire`) — заморожен, остаётся
референсом методики и домом openspec-спек.

## Состояние (всё закоммичено, ничего не запушено)

- **repograph**: `main` = baseline MVP; ветка **`feature/unified-fetcher-classifier`**
  = вся работа (8 коммитов, миграции 0004–0008). Рабочее дерево чистое.
- **package-classifire**: ветка **`feature/openspec-sdd`** = спеки.
- Реализовано и заархивировано (openspec, 6 чейнджей): fetcher-redesign,
  npm-connector, classification-layer, classification-api, classification-ui,
  on-demand-classification.
- Приёмка на момент передачи: ruff чистый, **121 unit + 37 integration**
  (backend), **17 vitest** (frontend), **ACCEPTANCE_SMOKE: PASS (16/16)**.

## ПРИОРИТЕТ #1 — активный openspec-чейндж `version-scoped-facts`

Не реализован. Полный контракт: `package-classifire/openspec/changes/version-scoped-facts/`
(proposal + design + specs + tasks, 0/13). Суть: сервис фактов должен уметь
классифицировать КОНКРЕТНУЮ версию `foo@1.2.3` (её тянет установщик), которой
может не быть в каталоге. Внутри — **скрытый баг**: `classifier/service.py:79
build_manifest` для pypi всегда берёт манифест latest → классификация любой
не-latest pypi-версии сейчас неверна. Начинать отсюда:
`openspec show version-scoped-facts` → tasks.md.

## Прочие открытые вопросы (не решены, ждут решения продукта)

Из архива `on-demand-classification` (design.md → Open Questions), блок
«режим обновления» — пользователь явно пометил как требующий проработки:
1. Ре-классификация при изменении жизненного цикла (пакет стал deprecated/
   yanked ПОСЛЕ классификации) — вердикт с capability `deprecated` протухает.
2. Пер-версионная классификация массово: какие версии и по какому триггеру
   (новые при refresh / из resolve / access-log прокси / кураторский --all).
3. UI: привязать блок классификации к селектору версий (модель deps.dev:
   зона пакета + зона версии); сейчас блок прибит к latest.

## Как запускать (чтобы не переоткрывать)

```bash
cd ~/Documents/Work/repograph
# стек
docker compose up -d postgres redis api worker
docker compose run --rm api alembic upgrade head
# тесты в контейнере
docker compose run --rm --no-deps api ruff check app tests
docker compose run --rm --no-deps api pytest tests/unit -q
docker compose run --rm api pytest tests/integration -q      # нужны postgres+redis
docker compose run --rm api python -m app.cli fetch <pkg> [--ecosystem npm]
docker compose run --rm api python -m app.cli classify [--reclassify]
./scripts/acceptance_smoke.sh                                 # 16 блоков
# frontend (node:20 в контейнере, локального npm не требуется)
docker run --rm -u "$(id -u):$(id -g)" -e npm_config_cache=/tmp/.npm \
  -v "$PWD:/workspace" -w /workspace/frontend node:20-alpine \
  sh -lc "npm ci --silent && npm run test:run && npm run lint"
```
Локальный Python-venv 3.12 для быстрых unit-прогонов без docker:
`/private/tmp/.../scratchpad/phubenv` (может быть очищен — тогда пересоздать
`uv venv --python 3.12` + зависимости из backend/pyproject.toml). Быстрее просто
гонять unit в контейнере (`--no-deps`, без БД).

## Ключевые инварианты (не сломать)

- Классификатор портирован из classifire **1:1**, CLASSIFIER_VERSION 2.4.0 —
  датасеты сопоставимы. Любая правка правил = bump версии + отдельный чейндж.
  Эталоны: ruff → dev-tool + binary-payload БЕЗ importable-api; flask →
  кураторский override; httpie → application.
- `user_capabilities` классификатор НЕ трогает никогда (включая reclassify).
- `classifications`/`dist_probes` уже пер-версионные (FK на package_versions).
- probe immutable на (version, filename); Redis-лок + ON CONFLICT.
- Auth-заголовок реестра только на его хост (не на CDN) — `RegistryClient`.
- Celery: задачи регистрируются явным `include` в celery_app.py (autodiscover
  НЕ работал); asyncpg в worker — NullPool (loop-affinity), см. worker_process_init.

## Процесс

Каждая доработка — через openspec: `openspec new change <name>` →
proposal/design/specs/tasks → реализация → тесты → живая проверка →
`openspec archive <name> --yes` → коммит в ветку. Спеки живут в
package-classifire (`openspec/`), код — здесь.
