# PackageHub — Ежедневное обновление каталога и дайджест изменений

> Раз в сутки находим пакеты каталога, изменившиеся в PyPI, перекачиваем только их и записываем, что именно изменилось. На журнале изменений строится дайджест «что обновилось за день / неделю».

**Версия:** 0.2-draft
**Дата:** 2026-10-27
**Статус:** проект, к обсуждению.
**База:** `main` (MVP, PyPI-only). Заменяет решение [SPEC-MVP.md](./SPEC-MVP.md) «incremental sync = batch refresh by synced_at».

## 0. Требования

- Отслеживаем изменения **только пакетов, которые уже есть в БД**. Остальной PyPI не интересен.
- Достаточно обновления **раз в сутки**.
- Нужен дайджест: какие пакеты каталога обновились за день / неделю и что именно изменилось.
- Не зависеть от устаревающих API PyPI (XML-RPC).

Что сейчас не так в `main`: перекачка 500 самых давно синхронизированных пакетов раз в 6 часов. Задержка растёт с размером каталога (5,7K пакетов ≈ 3 дня), почти все запросы перекачивают неизменившиеся пакеты, а изменения нигде не сохраняются.

### 0.1 Какие вопросы закрывает спека

| Вопрос | Ответ | Где |
|---|---|---|
| Как узнать, какие пакеты каталога обновились в PyPI? | Раз в сутки сравниваем `_last-serial` из корневого Simple-индекса с `packages.last_serial` | §1, §2 |
| Откуда берётся `last_serial`? | Из индекса (`_last-serial`) и из JSON API пакета (`last_serial`); у нас — колонка `packages.last_serial`, начальное заполнение из `raw_metadata` | §1, §3.1 |
| Обновляем ли пакеты вне каталога? | Нет, только те, что уже в БД | §0, §2 |
| Что именно изменилось? | Сравнение метаданных до и после загрузки: новые версии, yank, удаления, уязвимости, недоступность пакета | §4 |
| Можно ли посмотреть обзор за день / неделю / любой период? | Да: журнал `package_events` хранится без срока, дайджест строится по любому окну — API `/updates/summary`, лента `/updates`, страница «Обновления» | §3.3, §5 |
| Насколько актуален обзор? | Каждый прогон пишется в `update_runs`; дайджест показывает время и статус последнего прогона и сколько пакетов не обновилось | §3.4, §5 |
| Не потеряем ли загруженные зависимости при обновлении? | Нет, исправлено: pipeline заменяет зависимости только у версий, для которых коннектор их прислал | §6 |
| Скачиваем ли wheel/sdist? | Нет. Используется только JSON API и Simple-индекс; в `artifacts` хранятся описания файлов | §2 |
| Зависим ли от устаревших API PyPI? | Нет, XML-RPC не используется | §1 |

## 1. Источник изменений: корневой индекс Simple API

`GET https://pypi.org/simple/` с `Accept: application/vnd.pypi.simple.v1+json` (PEP 691) возвращает все проекты PyPI с `_last-serial` у каждого:

```json
{
  "meta": {"_last-serial": 42823406, "api-version": "1.4"},
  "projects": [{"name": "flask", "_last-serial": 34432672}, ...]
}
```

`_last-serial` проекта — serial последнего события по нему (новый релиз, файл, yank, удаление). Тот же номер приходит в JSON API пакета (`last_serial`, заголовок `X-PyPI-Last-Serial`). Если serial в индексе больше сохранённого у нас — пакет изменился.

Замеры (2026-10-27):

| Параметр | Значение |
|---|---|
| Размер ответа | ≈10 МБ (gzip), < 1 с |
| Проектов в индексе | 924 578 |
| Кеш | `max-age=600`, есть `ETag` |
| Изменилось пакетов каталога за ≈22 ч | 143 из 5 707 (2,5%), 160 новых релизов |

Итого ежедневный прогон — 1 запрос индекса + ≈150 запросов JSON API.

Имена в индексе не нормализованы (`0-._.-._.-0`), сравниваем по PEP 503.

`Decisions fixed`
- Основной и единственный источник — корневой Simple-индекс. XML-RPC (`changelog_since_serial`) не используется: deprecated, а при суточной частоте его преимущества (порядок и время событий) не нужны.
- Fallback, если корневой индекс недоступен: `GET /simple/{name}/` по каждому пакету каталога (`meta._last-serial`). Это 5,7K лёгких запросов, приемлемо раз в сутки.

## 2. Ежедневный прогон

Celery beat, задача `daily-update`, время запуска `DAILY_UPDATE_CRON` (по умолчанию `0 3 * * *` UTC).

1. **Создать запись прогона** в `update_runs` (`status = running`).
2. **Скачать индекс**, построить `normalized_name → serial`.
3. **Найти изменившиеся пакеты каталога:**
   - `index_serial > coalesce(packages.last_serial, 0)` → в список на обновление;
   - пакета нет в индексе → кандидат в недоступные (шаг 5).
4. **Обновить изменившиеся пакеты:** `fetch_package_metadata` + `run_sync_pipeline` с ограничением параллельности (`DAILY_UPDATE_CONCURRENCY`, 4). Pipeline сам пишет события (§4).
   - **Проверка CDN:** если `payload.last_serial < index_serial`, ответ устарел. Метаданные сохраняем, пакет ставим в повтор в конце прогона (одна попытка через `DAILY_UPDATE_STALE_RETRY_SECONDS`, 300). Если и тогда устарел — пакет попадёт в следующий прогон сам, потому что `last_serial` ещё меньше serial в индексе.
   - Сетевые ошибки — как сейчас: `sync_failures` + `retry-failed-sync`.
5. **Недоступные пакеты:** для пропавших из индекса запросить JSON. 404 → `packages.unavailable_at = now()`, событие `project_unavailable`. Пакет, который снова появился, — `unavailable_at = null`, событие `project_restored`. Насколько известно, PyPI убирает из индекса и удалённые, и помещённые в карантин проекты; различить их по публичному API нельзя, поэтому статус один. Проверить на реальном карантинном пакете при реализации.
6. **Закрыть прогон:** счётчики, `status = success | partial | failed`.

Прогон идемпотентен: повторный запуск в тот же день найдёт только то, что не обновилось в первый раз.

Batch refresh по `synced_at` отключается (`SYNC_REFRESH_ENABLED=false`).

## 3. Модель данных

### 3.1 `packages` — новые поля

| Поле | Тип | Смысл |
|---|---|---|
| `last_serial` | `bigint null` | serial PyPI на момент последнего успешного fetch (JSON `last_serial`) |
| `unavailable_at` | `timestamptz null` | пакет пропал из PyPI (удалён или в карантине) |

Миграция заполняет `last_serial` из `raw_metadata->>'last_serial'` (сейчас есть у 5704/5707 пакетов). Все пути загрузки (seed, lazy fetch, manual fetch, daily-update) обновляют `last_serial` из ответа.

### 3.2 `package_versions` — удалённые версии

- Новый статус `removed` в `PackageVersionStatus`. Строки не удаляем: на них ссылаются `dependencies.resolved_version_id` и журнал.
- Коннектор сейчас пропускает релизы без файлов (`if not files: continue` в `_normalize_versions`). Нужно передавать полный список версий из `releases`; релиз без файлов не устанавливается и получает `removed`.
- Версия, которой нет в свежем JSON или у которой нет файлов, → `removed`. Появилась снова → `active` / `yanked`.

### 3.3 `package_events` — журнал изменений

Только пакеты каталога. Объём ≈200 строк/сутки (≈75K/год), retention не нужен.

| Поле | Тип | Смысл |
|---|---|---|
| `id` | `bigserial pk` | |
| `package_id` | `uuid fk → packages` | |
| `version` | `varchar(128) null` | для событий версии |
| `kind` | `package_event_kind_enum` | см. ниже |
| `detected_at` | `timestamptz` | когда мы это обнаружили |
| `occurred_at` | `timestamptz null` | когда это произошло в PyPI, если известно |
| `run_id` | `uuid fk → update_runs null` | прогон, если событие из daily-update |
| `details` | `jsonb` | данные события, см. ниже |

Индексы: `(detected_at)`, `(package_id, detected_at)`.

| `kind` | Когда | `occurred_at` | `details` |
|---|---|---|---|
| `tracking_started` | пакет впервые попал в каталог | — | `{"latest_version": "3.1.2"}` |
| `release_added` | новая версия | `release_date` версии (max `upload_time` файлов) | `{"previous_version": "3.1.1", "bump": "minor", "prerelease": false}` |
| `release_yanked` | версия стала yanked | — | `{"reason": "..."}` (`yanked_reason` из JSON) |
| `release_unyanked` | yank снят | — | |
| `release_removed` | версия пропала или лишилась файлов | — | |
| `release_restored` | removed-версия вернулась | — | |
| `vulnerability_added` | новая запись в `vulnerabilities` пакета | `published` уязвимости | `{"id": "...", "aliases": [...], "fixed_in": [...]}` |
| `vulnerability_withdrawn` | у уязвимости появился `withdrawn` | `withdrawn` | `{"id": "..."}` |
| `project_unavailable` | пакет пропал из PyPI | — | |
| `project_restored` | пакет вернулся | — | |

`bump` — `major` / `minor` / `patch` / `pre` / `other` между `previous_version` и новой версией; считается через `packaging.version`. Если за один fetch появилось несколько версий, они сортируются по `packaging.version`: для первой `previous_version` — `latest_version` из снимка до fetch, для каждой следующей — предыдущая новая версия.

Для yank/remove точного времени PyPI не даёт, поэтому `occurred_at = null`, а точность — до суток (`detected_at`).

### 3.4 `update_runs` — прогоны

| Поле | Тип |
|---|---|
| `id` | `uuid pk` |
| `started_at`, `finished_at` | `timestamptz` |
| `status` | `running` / `success` / `partial` / `failed` |
| `index_serial` | `bigint` — `meta._last-serial` индекса |
| `packages_checked`, `packages_changed`, `packages_updated`, `packages_failed`, `packages_unavailable` | `int` |
| `events_created` | `int` |
| `error` | `text null` |

Нужен для статуса синхронизации и для дайджеста («данные актуальны на 03:12 UTC, 3 пакета не обновились»).

## 4. Генерация событий в pipeline

События рождаются из сравнения состояния **до** и **после** upsert в `run_sync_pipeline`, а не из внешнего источника. Поэтому они одинаковы для любого пути загрузки (daily-update, lazy fetch, manual fetch, seed).

1. Перед upsert загрузить снимок: `{version: status}` пакета, `latest_version`, `{vulnerability source_id: withdrawn_at}`.
2. Выполнить upsert как сейчас.
3. Сравнить и записать события по таблице §3.3.
4. Пакета не было в БД → только `tracking_started` (без события на каждую историческую версию).

Генерация событий — в той же транзакции, что и upsert: метаданные и журнал не расходятся.

## 5. Дайджест

### 5.1 API

`GET /api/v1/updates` — лента событий (без авторизации, см. SPEC-MVP-API §2.2):

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `since`, `until` | `now() - 1 day`, `now()` | окно по `detected_at` |
| `kind` | все | можно несколько |
| `package` | — | фильтр по пакету |
| `cursor`, `limit` | | keyset по `(detected_at, id)`, как в существующих списках |

`GET /api/v1/updates/summary` — агрегат по пакетам:

| Параметр | По умолчанию |
|---|---|
| `period` | `day` (`day` / `week`, или явные `since` / `until`) |
| `tz` | `UTC` — граница «сегодня» |

```json
{
  "since": "2026-10-20T00:00:00Z",
  "until": "2026-10-27T00:00:00Z",
  "last_run": {"finished_at": "2026-10-27T03:12:40Z", "status": "success", "packages_failed": 0},
  "totals": {"packages": 412, "releases": 655, "yanked": 3, "removed": 1, "vulnerabilities": 2, "unavailable": 0},
  "packages": [
    {
      "name": "flask",
      "from_version": "3.1.1",
      "to_version": "3.2.0",
      "max_bump": "minor",
      "releases": ["3.2.0rc1", "3.2.0"],
      "yanked": [],
      "removed": [],
      "vulnerabilities": [],
      "flags": [],
      "last_detected_at": "2026-10-27T03:05:11Z"
    }
  ]
}
```

- Сортировка: сначала пакеты с флагами риска (`unavailable`, `vulnerability_added`, `removed`, `yanked`, `major`), затем по `last_detected_at desc`.
- Окно считается по `detected_at`: при суточной частоте «сегодня» = «обнаружено сегодняшним прогоном». Для релизов в ответе также показывается точное `occurred_at`.

### 5.2 UI

Страница «Обновления»: переключатель «Сегодня / Неделя / Период», фильтр по типу, список пакетов из `summary` с раскрытием в ленту событий и ссылкой на карточку. Бейджи: `major`, `yanked`, `removed`, `vulnerability`, `unavailable`. Вверху — время и статус последнего прогона.

## 6. Попутные изменения

- **Зависимости при refresh** — уже исправлено: pipeline не стирает лениво загруженные зависимости (`test_pipeline_refresh_dependencies.py`).
- **Связи на новый пакет:** при первом появлении пакета в каталоге проставить `resolved_package_id` / `resolved_version_id` у уже существующих зависимостей, которые ссылаются на него по имени.

## 7. Наблюдаемость и управление

- `GET /api/v1/admin/sync/status` — последний прогон из `update_runs`.
- `POST /api/v1/admin/daily-update` и `python -m app.cli daily-update` — ручной запуск.
- Метрики: `packagehub_daily_update_duration_seconds`, `packagehub_daily_update_packages{result}`, `packagehub_package_events_total{kind}`, `packagehub_daily_update_last_success_timestamp` (для алерта «прогона не было > 36 ч»).

## 8. Этапы и исполнители

| # | Задача | Исполнитель |
|---|---|---|
| 1 | Миграция: `packages.last_serial` + backfill, `unavailable_at`, статус `removed`, `package_events`, `update_runs`; модели и enum-ы | Sonnet |
| 2 | Коннектор: полный список версий, `last_serial`, `yanked_reason` | Sonnet |
| 3 | Генерация событий в pipeline (§4), статус `removed` | Opus |
| 4 | Задача `daily-update`: индекс, сравнение, обновление, проверка CDN, недоступные пакеты, `update_runs` | Opus |
| 5 | API `/updates`, `/updates/summary` | Sonnet |
| 6 | Страница «Обновления» | Sonnet |
| 7 | Admin, CLI, метрики, отключение batch refresh | Sonnet |
| 8 | Ревью и интеграционный прогон | Opus |

Порядок: 1 → 2 → 3 → 4; 5–7 после 1 и параллельно с 3–4.

## 9. Тесты

- Pipeline: каждое событие из §3.3 на паре «снимок до / ответ после»; первый fetch даёт только `tracking_started`; повторный fetch без изменений событий не даёт.
- `bump`: major / minor / patch / pre / other, включая local-версии и пре-релизы.
- Daily-update: изменившийся / неизменившийся / пропавший пакет; устаревший ответ CDN; ошибка одного пакета не валит прогон; повторный запуск идемпотентен.
- Коннектор: релиз без файлов → `removed`.
- Summary: границы дня в разных `tz`, `from_version` / `to_version`, сортировка по риску.

## 10. Открытые вопросы

- Время ежедневного запуска и таймзона «сегодня» по умолчанию для UI.
- Нужно ли событие на изменение списка зависимостей у latest-версии (полезно для ревью, но шумно).
- Нужны ли уведомления по дайджесту (email / Slack) — вне этой спеки.
