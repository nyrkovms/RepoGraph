# Design: version-scoped-facts

> PROPOSAL — не реализовано. Продолжает on-demand-classification.

## Context

Схема с самого начала пер-версионная (`classifications`, `dist_probes` → FK на `package_versions`). Одноверсионность classifire осталась только в политике «классифицируем latest». Сервис фактов (on-demand-classification) достраивает latest inline. Проверке прокси нужна произвольная версия — включая отсутствующую в каталоге.

## Goals / Non-Goals

**Goals:** факты по (ecosystem, name, version), достраивание отсутствующей версии inline; версионно-корректный манифест классификатора (фикс бага). **Non-Goals:** массовая пер-версионная классификация «на всякий случай» (остаётся demand-driven); UI-привязка к селектору версий (open question on-demand-classification); ре-классификация по lifecycle-событиям.

## Decisions

### D1. `fetch_version_metadata` — новый метод контракта коннектора

Возвращает `NormalizedVersion` одной версии (+ минимум полей пакета для upsert, если пакета ещё нет). pypi: `GET {base}/{name}/{version}/json` — там `info` этой версии (classifiers, requires_dist, summary, yanked) + `urls` её файлов. npm: манифест версии уже в packument; для точечного случая — `GET {base}/{name}/{version}`. 404 upstream → `PackageNotFound` → фактам 404.

### D2. Version-scoped manifest — источник классификации (ФИКС БАГА)

`build_manifest(package, version)` для pypi собирает `{info, urls}` **из `version.raw_metadata`**, а не из `package.raw_metadata`. Значит `_normalize_versions` (pypi) должен класть в `version.raw_metadata` полный `info`+`urls` этой версии, а не только `files`. Компромисс объёма: pypi JSON API отдаёт `info` только для запрошенной версии — при обычном sync пакета `info` есть лишь для latest; для остальных версий `info` дозагружается лениво в момент, когда версию действительно классифицируют (тот же ленивый паттерн, что у зависимостей). Т.е.: latest — `info` сразу; не-latest — `raw_metadata` пополняется `info` при первом version-scoped-запросе через `fetch_version_metadata`.

Проверка на регрессию: после фикса классификация latest даёт ТОТ ЖЕ вердикт, что сейчас (эталоны ruff/flask/httpie/eslint не должны сдвинуться) — закрепить тестом.

### D3. Ensure-version в оркестраторе фактов

`ensure_facts(db, ecosystem, name, version=None)`:
1. version=None → текущее поведение (latest).
2. version задан → ensure пакет (как сейчас); найти строку версии; если нет — `fetch_version_metadata` → upsert строки версии + артефактов (переиспользовать `_upsert_versions`/`_replace_artifacts` из pipeline, передав одну версию); затем ensure probe(version) → ensure classify(version).
3. Классифицируемая версия ≠ latest — нормально; вердикт привязан к version_id, latest не трогается.

### D4. Маршрут — `?version=` (РЕШЕНО)

`GET /api/v1/facts/{ecosystem}/{name}?version=1.2.3`. Решение зафиксировано
(пользователь, 2026-07-13): версия — query-параметр, не сегмент пути. Причина —
scoped npm-имена (`@scope/pkg`) уже содержат `/`, и путь `{name}/{version}`
неоднозначен; query-параметр разрешает это без спецобработки. `version`
опционален: отсутствует → latest (обратная совместимость с on-demand-classification).

## Risks / Trade-offs

- [pypi: ленивая дозагрузка `info` не-latest версий — лишний GET на версию] → один раз на версию (потом в raw_metadata), demand-driven — только реально запрошенные версии.
- [Фикс build_manifest меняет путь latest] → покрыть регресс-тестом «latest-вердикт не изменился»; логика та же, меняется только ИСТОЧНИК манифеста (для latest info идентичен).
- [yanked-версия конкретно] → `fetch_version_metadata` вернёт `info.yanked`; статус версии `yanked` — это факт, классификация выполняется (capability `deprecated`/`sdist-only` по правилам).

## Open Questions

- Кэш-политика version-scoped фактов: версия immutable → вердикт можно считать вечным (в отличие от latest, где lifecycle меняется). Возможно, для не-latest версий вообще не нужен `refresh=true`. Уточнить при реализации.
- Связь с блоком «режим обновления» (on-demand-classification): version-scoped facts — это pull по требованию; массовое пополнение истории версий остаётся открытым.
