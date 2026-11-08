# Экосистемы: адресация и resolve

Как адресовать пакеты и запускать resolve в каждой экосистеме (PyPI, npm, crates.io, Go, NuGet, Maven, RPM), с примерами `curl`.
Полный контракт API: [`SPEC-MVP-API.md`](../SPEC-MVP-API.md).

## API: адресация по purl и resolve

Полный контракт: [`SPEC-MVP-API.md`](../SPEC-MVP-API.md).

Пакет адресуется экосистемой и координатами (`/packages/{ecosystem}/{coordinates}/-/…`) или purl; маршруты `/packages/pypi/{name}/…` остаются deprecated-алиасами до конца E7. Фильтры `ecosystem`, `registry`, `origin` есть у `/packages`, `/search` и (кроме `origin`) у `/updates`.

```bash
curl http://localhost:8000/api/v1/packages/pypi/flask/-/                # карточка: purl, origin, sources
curl http://localhost:8000/api/v1/packages/pypi/flask/-/versions/3.1.3  # версия: engines, sources
curl -i http://localhost:8000/api/v1/purl/pkg:pypi/flask                # 307 на канонический путь
```

npm работает так же; scope пакета пишется с `@`, в purl он закодирован как `%40`:

```bash
curl http://localhost:8000/api/v1/packages/npm/@angular/core/-/                                      # карточка: purl, namespace
curl 'http://localhost:8000/api/v1/packages/npm/left-pad/-/versions/1.3.0/dependencies?os=linux'      # дерево зависимостей
curl 'http://localhost:8000/api/v1/search?q=react&ecosystem=npm'                                       # поиск только по npm
```

`POST /resolve` отвечает графом: узлы с purl и источником версии, рёбра, конфликты. Сбой всего графа приходит в поле `error`
ответа `200`. Старый вложенный формат (`dependencies` вместо `roots`) работает как алиас до конца E7.

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"roots": [{"purl": "pkg:pypi/flask", "constraint": ">=3.0"}], "options": {"timeout_s": 30}}'
```

Для npm добавьте `"ecosystem": "npm"` и окружение (`node_version`, `os`, `cpu`); граф строит стратегия `npm-arborist`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "npm", "roots": [{"purl": "pkg:npm/express", "constraint": "^4.18.0"}], "environment": {"node_version": "20.11.0", "os": "linux", "cpu": "x64"}}'
```

crates.io: слаг экосистемы `crates`, тип purl `cargo` (`pkg:cargo/serde_json`), имя крейта пишется как опубликовано, без свёртки
регистра и `-`/`_` (`serde_json`, `Inflector`). Окружение дерева: `target` (целевая тройка Rust, по умолчанию
`x86_64-unknown-linux-gnu`), `features` (через запятую) и `default_features`:

```bash
curl http://localhost:8000/api/v1/packages/crates/serde_json/-/                                      # карточка: purl, ссылки
curl 'http://localhost:8000/api/v1/packages/crates/serde_json/-/versions/1.0.140/dependencies?target=x86_64-pc-windows-msvc&features=std'
curl 'http://localhost:8000/api/v1/search?q=serde&ecosystem=crates'                                  # поиск только по crates.io
```

Для resolve укажите `"ecosystem": "crates"`; features корня идут в `extras`, окружение — `target` (`"*"` — все платформы, как в
`Cargo.lock`), `features`, `default_features`. Граф строит стратегия `cargo-resolver`, один крейт может быть в нескольких версиях
разных классов совместимости (`rand 0.7` и `rand 0.8`); в `nodes[].details.features` лежат features, с которыми крейт попал в граф:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "crates", "roots": [{"purl": "pkg:cargo/serde_json", "constraint": "^1.0", "extras": ["preserve_order"]}], "environment": {"target": "*"}}'
```

Добавить крейт в каталог можно по purl через admin API. Ввод в другом написании приводится к опубликованному имени
(`pkg:cargo/serde-json` сохранится как `serde_json`); остальные маршруты ждут точное имя:

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:cargo/serde_json", "pkg:cargo/tokio"]}'
```

Go: слаг экосистемы `go`, тип purl `golang`. Идентичность модуля — его путь как написан, регистр значим
(`github.com/BurntSushi/toml`); путь без последнего элемента — namespace purl (`pkg:golang/github.com/gin-gonic/gin`), а суффикс
мажорной версии `/v2` и выше делает другой модуль (`pkg:golang/github.com/redis/go-redis/v9`). Слэши пути остаются в URL как есть.
Версии — `v1.10.0`, псевдоверсии (`v0.0.0-20261014143641-f0251eb4aa3e`, это prerelease) и `+incompatible` (в пути кодируется как `%2B`);
отозванная в `go.mod` версия приходит со статусом `yanked`. У Go нет описания, лицензии и авторов, а у дерева зависимостей нет
окружения: каждое `require` из `go.mod` — ребро `runtime`:

```bash
curl http://localhost:8000/api/v1/packages/go/github.com/gin-gonic/gin/-/                                  # карточка: purl
curl http://localhost:8000/api/v1/packages/go/github.com/BurntSushi/toml/-/versions                         # версии с датами
curl 'http://localhost:8000/api/v1/packages/go/github.com/gin-gonic/gin/-/versions/v1.10.0/dependencies'   # дерево require
curl 'http://localhost:8000/api/v1/search?q=gin&ecosystem=go'                                              # поиск только по Go
```

Для resolve укажите `"ecosystem": "go"`; `constraint` корня — версия, с которой начинает Minimal Version Selection (`*` — последняя).
Окружение: `go_version` (по умолчанию `1.23`), `replace` (`[{"old": "путь[@версия]", "new": "путь@версия"}]`) и `exclude`
(`["путь@версия"]`) главного модуля. Граф строит стратегия `go-mvs` (`Go 1.23 (MVS with module graph pruning)`); множество
`(путь, версия)` в ответе равно `go list -m all`. Модуль, которому нужен `go` новее `go_version`, попадает в `warnings` с кодом `go_version`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "go", "roots": [{"purl": "pkg:golang/github.com/gin-gonic/gin", "constraint": "v1.9.1"}], "environment": {"go_version": "1.23", "exclude": ["golang.org/x/sys@v0.17.0"]}}'
```

Добавить модуль в каталог можно по purl через admin API; путь пишется ровно как у модуля, с регистром и `/vN`. Новые версии известных
модулей ежедневное обновление находит по ленте index.golang.org (источник `proxy-golang-org`):

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:golang/github.com/gin-gonic/gin", "pkg:golang/github.com/redis/go-redis/v9"]}'
```

В Web UI Go выбирается в переключателе экосистем на странице поиска; страница модуля — `/package/go/<путь модуля>`: purl, версии с датами
(псевдоверсии и отозванные версии с причиной отзыва помечены, `+incompatible` показывается как есть), баннер deprecation и
пометка `indirect` у зависимостей в дереве. Окружения у дерева нет.

В Web UI crates.io выбирается в переключателе экосистем на странице поиска; на странице крейта окружение дерева — target, features и
default features, а выбранной версии показываются её features.

NuGet: слаг экосистемы `nuget`, тип purl `nuget` (`pkg:nuget/newtonsoft.json`). Id не различает регистр: каталог хранит и адресует
его строчными (`newtonsoft.json`), а написание реестра (`Newtonsoft.Json`) отдаёт как `name`. Версии — три или четыре части
(`13.0.3`, `12.0.3.1`), скрытая автором версия приходит со статусом `unlisted`, а объявленный deprecated пакет несёт `deprecation`.
Зависимости версии собраны по платформам, окружение дерева — `target_framework` (`net5.0`…`net11.0` и `netstandard1.0`…`netstandard2.1`,
по умолчанию `net8.0`); ребро помечено группой, из которой взято (`environment_markers.dotnet_target_framework`):

```bash
curl http://localhost:8000/api/v1/packages/nuget/newtonsoft.json/-/                                    # карточка: purl, лицензия
curl http://localhost:8000/api/v1/packages/nuget/newtonsoft.json/-/versions                             # версии с датами, unlisted отмечены
curl 'http://localhost:8000/api/v1/packages/nuget/newtonsoft.json/-/versions/13.0.3/dependencies?target_framework=netstandard2.0'
curl 'http://localhost:8000/api/v1/search?q=serilog&ecosystem=nuget'                                   # поиск только по NuGet
```

Для resolve укажите `"ecosystem": "nuget"`; `constraint` корня — диапазон NuGet (`1.0` значит `>= 1.0`, `[1.0,2.0)`, плавающий `1.*`),
окружение — `target_framework`. Граф строит стратегия `nuget-nearest-wins` (`.NET SDK 8 (NuGet 6.x)`): берётся самая низкая подходящая
версия, побеждает ближайшая к проекту ссылка (более глубокая, которой нужна версия выше, даёт предупреждение `NU1605`), родственные
ссылки берут самую низкую версию, которую допускают все диапазоны, иначе ошибка `NU1107`; результат равен `dotnet restore`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "nuget", "roots": [{"purl": "pkg:nuget/newtonsoft.json", "constraint": "[12.0.1,14.0)"}], "environment": {"target_framework": "net8.0"}}'
```

Добавить пакет в каталог можно по purl через admin API (регистр id не важен). Новые версии известных пакетов с nuget.org ежедневное
обновление находит по ленте Catalog (источник `nuget-org`, адрес ленты в `config.catalog_url`); другие фиды NuGet проверяются по ETag
Registration:

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:nuget/newtonsoft.json", "pkg:nuget/serilog"]}'
```

В Web UI NuGet выбирается в переключателе экосистем на странице поиска; страница пакета — `/package/nuget/<id>`: purl, лицензия, версии
с датами (скрытые автором версии помечены `unlisted`, четырёхчастные версии показываются как есть), баннер deprecation пакета и
пометка deprecated у версии, если API её отдаёт. Окружение дерева — выбор платформы (`target_framework`, по умолчанию `net8.0`), у
каждого ребра показана его группа платформ.

Maven: слаг экосистемы `maven`, тип purl `maven` (`pkg:maven/org.springframework/spring-core`: `groupId` — namespace, `artifactId` — имя).
Пакет — пара `groupId:artifactId`, регистр значим; в путях, в `name` и в поиске она пишется через двоеточие
(`org.springframework:spring-core`). Версии сравниваются как Maven ComparableVersion (`6.1.14`, `33.3.1-jre`, `6.2.0-RC1`), все они
`active`: релизы Maven не удаляют, дату версии даёт `Last-Modified` её POM (новейшей — сразу, остальным — фоновая задача). Если новейший POM переносит артефакт
(`<relocation>`), пакет несёт `deprecation.alternative` (`<groupId>:<artifactId>`, с `:<версия>`, если перенос её называет; resolve ставит целевой артефакт на место перенесённого). Зависимости версии берутся из effective model
(родительские POM, `dependencyManagement` и BOM применены); окружение дерева — `scopes` (по умолчанию `compile,runtime`; ещё `provided`,
`system`, `test`), у ребра в `details` бывают `exclusions`, `type` и `classifier`:

```bash
curl http://localhost:8000/api/v1/packages/maven/org.springframework:spring-core/-/                          # карточка: purl, лицензия, релокация
curl http://localhost:8000/api/v1/packages/maven/org.springframework:spring-core/-/versions                  # версии с датами
curl 'http://localhost:8000/api/v1/packages/maven/org.springframework:spring-core/-/versions/6.1.14/dependencies?scopes=compile,runtime,test'
curl 'http://localhost:8000/api/v1/search?q=org.springframework:spring&ecosystem=maven'                      # поиск group:artifact
```

Для resolve укажите `"ecosystem": "maven"`; `constraint` корня — версия (мягкая, как её берёт Maven), диапазон `[1.0,2.0)` или `*` (последний
релиз), версия в purl без `constraint` закрепляет её. `managed` — `dependencyManagement` проекта: purl с `?type=pom` — BOM, который
импортируется на своё место. Окружение — `scopes`, `strategy`, `plugins` и `java_version` (по умолчанию 21, решает jdk-профили POM). Стратегия `maven` (по умолчанию, ответ `maven-nearest-wins`,
`Maven 3.9`) берёт ближайшее объявление, множество `(groupId:artifactId, версия)` в ответе равно `mvn dependency:list -DincludeScope=runtime`;
стратегия `gradle` (`gradle-highest-wins`) при разных версиях берёт наибольшую. Gradle Module Metadata (`.module`) не читается, результат
`gradle` строится по POM и неполон там, где POM упрощён (Kotlin Multiplatform, часть AndroidX, platform через constraints); узлы несут
`details.scope`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "maven", "roots": [{"purl": "pkg:maven/org.springframework/spring-core", "constraint": "6.1.14"}], "managed": [{"purl": "pkg:maven/org.springframework.boot/spring-boot-dependencies?type=pom", "constraint": "3.3.5"}], "environment": {"scopes": ["compile", "runtime"], "strategy": "maven"}}'
```

Добавить артефакт в каталог можно по purl через admin API (с `groupId` в namespace; регистр значим). Стартовый источник — Maven Central
(`maven-central`); у него нет ленты изменений, поэтому ежедневное обновление условным запросом проверяет `maven-metadata.xml` каждого
известного артефакта по ETag. Другие репозитории (Google Maven, Gradle Plugin Portal, корпоративные Nexus и Artifactory) подключаются как
источники `maven2` через admin API или `registries.yaml` (пример в `backend/registries.example.yaml`), артефакты по источникам
раскладываются правилом `group_patterns`, например `androidx.*` для Google Maven:

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:maven/org.springframework/spring-core", "pkg:maven/com.google.guava/guava"]}'
```

В Web UI Maven выбирается в переключателе экосистем на странице поиска (ввод `group:artifact`); страница артефакта —
`/package/maven/<groupId>:<artifactId>`: purl, `groupId` и `artifactId`, лицензия, версии с датами, баннер переезда артефакта со ссылкой на
новое место, упаковка версии (`jar`, `pom`, `aar`), если API её отдаёт. Окружение дерева — выбор scopes (`compile` и `runtime` по умолчанию),
у ребра показаны его scope (`test` и `provided`; остальные, когда API их отдаёт), exclusions, classifier и тип. Ссылки из дайджеста ведут на страницу артефакта.

RPM: слаг экосистемы `rpm`, тип purl `rpm` (`pkg:rpm/rocky/openssl?distro=rocky-9`: `vendor` — издатель дистрибутива, qualifier `distro` — namespace пакета).
Namespace — дистрибутив и релиз (`rocky-9`, `rhel-9`), он часть идентичности: `openssl` в `rocky-9` и в `rhel-9` — два пакета. В путях,
в `name` и в поиске пакет пишется `<namespace>/<имя>` (`rocky-9/openssl`), регистр имени значим. Версия — EVR (`1:3.0.7-27.el9`, эпоха 0 не
пишется), сравнение как в `rpm`; сборка, у которой в источнике есть более новая сборка той же архитектуры, получает `status: "superseded"`
(она остаётся в каталоге, события для неё нет), `release_date` — время сборки, лицензия — тег License пакета, файлы версии — её `.rpm` по
архитектурам (`artifacts`). Дерево зависимостей показывает не пакеты, а возможности (capability: `libssl.so.3()(64bit)`, `/bin/sh`), которые версия
требует; пакет, который возможность предоставляет, — в `resolved_name`. Окружение дерева — `arch` (`x86_64` по умолчанию, ещё `aarch64`,
`ppc64le`, `s390x`), слабые зависимости (`recommends`, `suggests`, `supplements`, `enhances`) входят при `include_optional=true`:

```bash
curl http://localhost:8000/api/v1/packages/rpm/rocky-9/openssl/-/                                         # карточка: purl, лицензия, источники
curl http://localhost:8000/api/v1/packages/rpm/rocky-9/openssl/-/versions                                 # EVR с датами, superseded помечены
curl 'http://localhost:8000/api/v1/packages/rpm/rocky-9/openssl/-/versions/1%3A3.0.7-27.el9/dependencies?arch=aarch64&include_optional=true'
curl 'http://localhost:8000/api/v1/search?q=rocky-9/openssl&ecosystem=rpm'                                # поиск distro/name
curl -i 'http://localhost:8000/api/v1/purl/pkg:rpm/rocky/openssl?distro=rocky-9'                          # 307 на каноническую адресацию
```

Для resolve укажите `"ecosystem": "rpm"`; `constraint` корня — флаг RPM (`>= 3.0`, `= 1:3.0.7-27.el9`) или `*`. Окружение — `distro`, `arch`,
`repos` (какие репозитории дистрибутива использовать), `weak_deps`, `module_streams` (`{"nodejs": "18"}`), `disabled_modules` и `module_install`
(`["nodejs:18/common"]`). Задачу решает libsolv в rpm-worker (стратегия `libsolv`, ответ `emulates: "dnf 4"`), модульность учитывается как в
`dnf`: множество пакетов в ответе равно `dnf install --assumeno --installroot` с пустым корнем. Активные потоки модулей приходят в `details.modules`
ответа, ошибки — `unsolvable`, `module_conflict` и `not_found`, причины — в `error.problems[]`. Пакеты, которые resolve нашёл и которых нет в каталоге,
добавляются в фоне как `discovered`:

```bash
curl -H 'Content-Type: application/json' http://localhost:8000/api/v1/resolve \
  -d '{"ecosystem": "rpm", "roots": [{"purl": "pkg:rpm/rocky/nodejs?distro=rocky-8", "constraint": "*"}], "environment": {"arch": "x86_64", "module_streams": {"nodejs": "18"}}}'
```

Стартового источника у RPM нет: миграция создаёт только пустой профиль `default`, репозитории подключают сами. Готовый набор — Rocky Linux 8 и 9
(BaseOS, AppStream, CRB), AlmaLinux 9, EPEL 9 и Fedora 41 — лежит в `backend/registries.rpm.yaml`; один источник — один репозиторий одного релиза
(`config.namespace`, `config.arch`, `$basearch` и `$releasever` в `base_url`, `modular: true` у AppStream):

```bash
docker compose run --rm api python -m app.cli registries import registries.rpm.yaml
```

Репозитории читает rpm-worker, отдельный контейнер (Rocky Linux 9 с libsolv и libmodulemd), он слушает очередь Celery `rpm`; без него пакеты RPM не
читаются и не решаются. Закрытые репозитории (RHEL) подключаются с `auth_type: tls_client_cert` (`credentials_ref` — имя пары `<имя>.crt` и
`<имя>.key` в каталоге секретов, он монтируется в rpm-worker: `PACKAGEHUB_SECRETS_DIR`, по умолчанию `./secrets`) или `basic` (файл со строкой
`user:password`). Ежедневное обновление rpm-worker обновляет метаданные каждого репозитория (условный запрос `repomd.xml`) и сравнивает сборки
известных пакетов. Добавить пакет в каталог можно по purl через admin API (с `distro`, namespace входит в идентичность):

```bash
curl -H "Authorization: Bearer phub_admin_dev" -H 'Content-Type: application/json' http://localhost:8000/api/v1/admin/packages \
  -d '{"purls": ["pkg:rpm/rocky/openssl?distro=rocky-9"]}'
```

В Web UI RPM выбирается в переключателе экосистем на странице поиска (ввод `distro/name`, например `rocky-9/openssl`); страница пакета —
`/package/rpm/<namespace>/<имя>`: purl, дистрибутив и имя, лицензия, версии с датами и архитектурами их сборок, вытесненные сборки помечены
`superseded`. Окружение дерева — выбор архитектуры (`x86_64` по умолчанию), у каждого ребра показаны возможность, её флаг версии, вид зависимости
(если не `requires`) и ссылка на пакет, который её предоставляет, с его версией. Ссылки из дайджеста ведут на страницу пакета.

