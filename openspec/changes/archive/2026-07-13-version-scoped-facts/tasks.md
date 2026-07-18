# Tasks: version-scoped-facts

> НЕ реализовано. Для Opus. Порядок — по зависимостям. Прогон после каждого
> блока: `docker compose run --rm --no-deps api pytest tests/unit -q` + ruff.

## 1. Коннектор: метаданные одной версии

- [x] 1.1 `BaseConnector.fetch_version_metadata(name, version) -> NormalizedVersion` (абстрактный, с дефолтным NotImplementedError как у list_package_names)
- [x] 1.2 pypi: реализовать через `GET {json_base_url}/{name}/{version}/json` → `info`+`urls` этой версии → NormalizedVersion (release_date, status по yanked, requires_python, raw_metadata={"info":..., "urls":...}, artifacts из urls); 404 → PackageNotFound
- [x] 1.3 npm: реализовать (манифест версии из packument versions[v], либо `GET {base}/{name}/{version}`) → NormalizedVersion с raw_metadata=манифест
- [x] 1.4 Юнит-тесты обоих на mock-транспорте (как test_pypi_connector / test_npm_connector)

## 2. Pipeline: ensure-version + версионный манифест

- [x] 2.1 `ensure_version_row(db, package, version, connector)`: найти строку версии; если нет — fetch_version_metadata → upsert строки + артефактов (переиспользовать _upsert_versions/_replace_artifacts на одну версию); вернуть PackageVersion
- [x] 2.2 pypi `_normalize_versions`: класть в version.raw_metadata полный `{info, urls}` для latest (для остальных info дозагружается в 2.1 при version-scoped-запросе) — либо оставить как есть и полагаться на 2.1; выбрать по D2
- [x] 2.3 **ФИКС БАГА** `build_manifest(package, version)`: pypi берёт `{info, urls}` из `version.raw_metadata`; фолбэк на package.raw_metadata только если version.raw_metadata пуст И version == latest
- [x] 2.4 Регресс-тест: latest-вердикт (flask/ruff/httpie) не изменился после 2.3

## 3. Сервис фактов + API

- [x] 3.1 `ensure_facts(db, ecosystem, name, version=None)`: version=None → как сейчас; version задан → ensure пакет → ensure_version_row → ensure probe(version) → ensure classify(version) → сборка ответа по этой версии
- [x] 3.2 API: версия как query `?version=` (рекомендация design D4 — однозначно для scoped npm); 404 при отсутствии версии в upstream, прочие коды как в on-demand-classification
- [x] 3.3 Интеграционные тесты: version-scoped холодный вызов (версия не в каталоге) собирает факты этой версии; не-latest версия не смещает latest; несуществующая версия → 404; контракт «без вердиктов» сохранён

## 4. Финал

- [x] 4.1 Полный прогон unit+integration+ruff, acceptance smoke, живая проверка (docker): факты по конкретной не-latest версии реального пакета
- [x] 4.2 README: сервис фактов принимает версию; архив чейнджа; коммит в ветку feature/unified-fetcher-classifier
