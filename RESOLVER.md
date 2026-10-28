# PackageHub — Dependency Resolution Architecture

> Детальное описание алгоритмов резолвинга зависимостей per-ecosystem, расчёт ресурсов, входные данные, сравнительный анализ.

**Версия:** 1.0
**Дата:** 2026-03-04
**Связанный документ:** SPEC.md v0.4, раздел 12

> **2026-10-27:** in-memory индекс и расчёты ресурсов устарели (курируемый каталог). Алгоритмы остаются справочником; правила первой волны — [SPEC-MULTI-ECOSYSTEM.md](./SPEC-MULTI-ECOSYSTEM.md) §8.

---

## 1. Обзор проблемы

Dependency resolution — задача построения полного дерева (или графа) транзитивных зависимостей проекта с выбором конкретной версии каждого пакета. Сложность задачи фундаментально зависит от правил экосистемы.

### 1.1 Ключевой вопрос: сколько версий одного пакета допустимо?

```
Одна версия (Python, Java, Rust, Go, .NET):
  → Если A хочет X>=2 и B хочет X<2, это КОНФЛИКТ
  → Нужен constraint solver или эвристика разрешения конфликтов

Множественные версии (npm/JavaScript):
  → Если A хочет lodash@3, B хочет lodash@4 — ставим ОБЕ
  → Нет конфликтов*, задача сводится к обходу графа
  
  *Исключение: peerDependencies — создают real constraints
```

### 1.2 Классификация алгоритмов

```
Тип 1: SAT/CDCL (NP-complete, но быстро на практике)
  Экосистемы: PyPI (uv/PubGrub), crates.io (Cargo/PubGrub)
  Суть: формализация как задача удовлетворимости ограничений
  При конфликте: анализ причины, запись learned clause, targeted backjump
  
Тип 2: Greedy с эвристикой (линейный)
  Экосистемы: Maven (nearest-wins), Gradle/NuGet (highest-wins), Go (MVS)
  Суть: детерминированный обход, конфликт = одна сторона молча проигрывает
  При конфликте: нет backtracking, просто выбрать победителя по правилу

Тип 3: Tree builder (линейный, без конфликтов)
  Экосистемы: npm (множественные версии)
  Суть: для каждого constraint — выбрать latest, рекурсивно обойти
  При конфликте: нет конфликтов (кроме peer deps)
```

---

## 2. PyPI (Python)

### 2.1 Правила экосистемы

- Одна версия пакета per-environment
- Version specifiers: PEP 440 (`>=1.0,<2.0`, `~=1.4`, `!=1.5`, `==1.0.*`)
- Environment markers: `python_version`, `sys_platform`, `os_name`, `implementation_name`
- Extras: optional dependency groups (`requests[security]`)
- Метаданные: в wheel metadata (METADATA файл) или setup.py/pyproject.toml

### 2.2 Алгоритм: PubGrub (CDCL SAT)

PubGrub — алгоритм из экосистемы Dart, адаптированный uv (Rust) и pubgrub-rs.

```
Основной цикл:
1. Partial solution = {root: 1.0}
2. Unit propagation: из root достать dependencies
3. Decision: выбрать пакет без версии → назначить latest compatible
4. Если конфликт:
   a. Conflict analysis: найти корневую причину
   b. Записать incompatibility: {A>=2, B>=3} = невозможно
   c. Backjump к decision level где можно изменить выбор
5. Повторять до: все пакеты resolved ИЛИ root = impossible (UNSAT)
```

**Пример конфликта и его решение:**

```
root → A >=1.0
root → B >=1.0
A 2.0 → C >=2.0
B 2.0 → C <2.0

pip (старый resolvelib):
  1. Выбрать A 2.0 → добавить C >=2.0
  2. Выбрать C 2.1
  3. Выбрать B 2.0 → добавить C <2.0 → КОНФЛИКТ
  4. Backtrack: попробовать C 2.0 → всё ещё <2.0 конфликт
  5. Backtrack: попробовать C 1.9, 1.8, 1.7... → все не подходят
  6. Backtrack на B → попробовать B 1.0
  → O(versions_A × versions_C × versions_B) в worst case

PubGrub:
  1. Выбрать A 2.0 → добавить C >=2.0
  2. Выбрать B 2.0 → добавить C <2.0 → КОНФЛИКТ
  3. Conflict analysis: причина = {A 2.0, B 2.0}
  4. Record incompatibility: "A >=2.0 AND B >=2.0 → impossible"
  5. Backjump на B → попробовать B 1.0 (skip all B 2.x)
  → O(N) typical case
```

### 2.3 Входные данные для резолвера

```
PyPI ResolveRequest:
  dependencies:
    - name: "flask"
      version_spec: ">=3.0"          # PEP 440
      extras: ["async"]              # optional
    - name: "sqlalchemy"
      version_spec: ">=2.0,<3.0"

  environment:                        # для фильтрации markers
    python_version: "3.12"
    sys_platform: "linux"
    os_name: "posix"
    implementation_name: "cpython"
    
  options:
    include_dev: false
    strategy: "pubgrub"               # единственная для PyPI
```

### 2.4 Что хранить в индексе

Per version entry:
```
PackageName (interned u32)
Version (interned u32, PEP 440 parsed)
Dependencies: Vec<{
    name: u32,
    version_spec: PEP440Spec,    # parsed, not string
    extras: Option<Vec<u32>>,
    markers: Option<MarkerTree>  # pre-parsed marker expression
}>
Extras defined: Vec<u32>         # какие extras этот пакет объявляет
Yanked: bool
```

### 2.5 Расчёт памяти

```
Пакетов:               ~550K
Версий (reachable):    ~2.5M (из 4M total)
Зависимостей:          ~12M
Avg deps per version:  ~5

Per version: ~120 байт (name + version + deps vec header + yanked)
Per dependency: ~32 байт (name + spec + extras_ptr + markers_ptr)

2.5M × 120 = 300 MB
12M × 32 = 384 MB
String interning: ~15 MB
Parsed markers cache: ~100 MB
HashMap overhead: ~200 MB
────────────────────
Total: ~1.0 GB
```

### 2.6 Производительность

```
Типичный проект (Django app, ~30 direct, ~200 transitive):
  Resolve: 1-5 ms (все данные в RAM, нет network I/O)

Тяжёлый проект (data science, numpy+scipy+pandas+torch, ~500 transitive):
  Resolve: 5-20 ms (markers фильтрация, больше candidates)

Патологический (конфликтный граф, lots of backtracking):
  Resolve: 50-200 ms (PubGrub с learned clauses)
  
С Redis cache (85% hit rate): effective p50 < 1 ms
```

### 2.7 Уникальные сложности

**Forking resolver (multi-environment):** Один и тот же пакет может иметь разные зависимости для разных environment markers. uv решает это "forking" — разветвление resolution на ветки per marker combination. Для PackageHub V3 — позволит отвечать "покажи дерево flask для ВСЕХ Python-версий" одним запросом.

**Metadata discovery:** PyPI исторически не имел способа получить зависимости без скачивания wheel. PEP 658 (metadata in Simple API) решает это для новых пакетов. Для старых — fallback на скачивание и парсинг. В PackageHub мы имеем ВСЕ зависимости в индексе, поэтому эта проблема не актуальна.

---

## 3. npm (JavaScript)

### 3.1 Правила экосистемы

- **Множественные версии одного пакета допустимы**
- Semver ranges: `^1.2.3` (>=1.2.3, <2.0.0), `~1.2.3` (>=1.2.3, <1.3.0), `>=1.0.0 <3.0.0`
- Dependency types: dependencies, devDependencies, peerDependencies, optionalDependencies
- peerDependencies: должны быть на том же уровне (нельзя nest) → создают real constraints

### 3.2 Алгоритм: Greedy DFS (tree builder)

npm НЕ решает SAT-задачу. Поскольку множественные версии допустимы, нет "конфликтов" в классическом смысле.

```
fn resolve_npm(root_deps: Vec<Dep>) -> Tree {
    let mut seen: HashMap<(PackageName, Version), NodeId> = {};
    let mut tree = Tree::new();
    
    for dep in root_deps {
        resolve_recursive(dep, &mut tree, &mut seen, depth=0);
    }
    return tree;
}

fn resolve_recursive(dep, tree, seen, depth) {
    // 1. Найти latest version удовлетворяющую constraint
    let version = find_latest(dep.name, dep.range);
    
    // 2. Cycle detection: если уже видели этот (name, version)
    if seen.contains((dep.name, version)) {
        tree.add_ref(dep.name, version);  // ссылка, не новый узел
        return;
    }
    seen.insert((dep.name, version));
    
    // 3. Добавить в дерево
    let node = tree.add_node(dep.name, version);
    
    // 4. Рекурсивно обойти зависимости этой версии
    let subdeps = index.get_deps(dep.name, version);
    for subdep in subdeps {
        if subdep.type == "dev" && depth > 0 { continue; }  // dev deps только для root
        resolve_recursive(subdep, tree, seen, depth + 1);
    }
}
```

**Ключевое отличие от Maven/PyPI:** если два разных поддерева хотят разные версии lodash — обе попадают в дерево. Это НЕ конфликт.

### 3.3 peerDependencies — единственный source of real conflicts

```
react-dom@18.0 peerDependency: react@^18.0
some-old-lib peerDependency: react@^16.0

Здесь конфликт: react не может быть и 18.x и 16.x на одном уровне.
npm v7+: просто выдаёт warning/error, НЕ пытается solve.
```

Для PackageHub: при обнаружении конфликтных peer deps — помечаем в response:
```json
{"conflicts": [{"package": "react", "required": ["^18.0", "^16.0"], "type": "peer"}]}
```

### 3.4 Входные данные

```
npm ResolveRequest:
  dependencies:
    - name: "react"
      range: "^18.0.0"
    - name: "next"
      range: "^14.0.0"
  
  options:
    include_dev: false
    include_optional: true
    include_peer: true
    strategy: "greedy_dfs"      # единственная для npm
```

Никаких BOM'ов, parent chains, properties. npm — самый простой по входным данным.

### 3.5 Расчёт памяти

```
Пакетов:               ~2.5M
Версий (hot tier):     ~8M (из 60M total, top-100K packages + reachable)
Зависимостей:          ~60M в hot tier
Avg deps per version:  ~7.5

Per version: ~80 байт
Per dependency: ~24 байт (name + range + type)

8M × 80 = 640 MB
60M × 24 = 1,440 MB
String interning: ~50 MB (много длинных scoped names: @angular/core)
HashMap overhead: ~800 MB
Semver ranges parsed: ~500 MB
────────────────────
Total: ~3.5 GB (hot tier)
```

### 3.6 Производительность

```
Simple (express app, ~100 transitive):     3-10 ms
Medium (gatsby, ~500 transitive):          20-80 ms
Heavy (React app, ~1500 transitive):       50-200 ms
Extreme (Next.js, ~2500 transitive):       200-800 ms

Bottleneck: tree depth (10-15 levels) × breadth (avg 7.5 deps)
Нет backtracking, но очень большие деревья.
```

### 3.7 Почему npm медленнее PyPI при более простом алгоритме

1. **Деревья в 5-10× больше:** npm ~1500 nodes typical, PyPI ~200 nodes typical
2. **Больше version candidates:** npm packages часто имеют 100+ версий (types/node: 3000+)
3. **Semver ranges шире:** `^1.0.0` = любая 1.x → больше candidates для find_latest
4. **Нет deduplication в дереве:** если 50 пакетов зависят от `tslib` — 50 узлов (с разными versions)

### 3.8 Почему НЕ нужен PubGrub для npm

npm позволяет множественные версии → нет SAT-задачи → нет необходимости в backtracking → greedy DFS оптимален. PubGrub для npm был бы МЕДЛЕННЕЕ (overhead от clause learning при отсутствии реальных конфликтов).

Исключение: если PackageHub когда-нибудь захочет показать "deduplicated tree" (как npm install с hoisting) — потребуется более сложный алгоритм (Arborist-подобный). Для V3+.

---

## 4. Maven (Java)

### 4.1 Правила экосистемы

- Одна версия per groupId:artifactId (GA) в classpath
- Координаты: groupId:artifactId:version (GAV), плюс type, classifier, scope
- Scopes: compile, provided, runtime, test, system
- dependencyManagement: override версий для всего поддерева
- BOM (Bill of Materials): POM типа "pom" с dependencyManagement, импортируемый другими
- Parent POM: наследование dependencyManagement, properties, dependencies
- Properties: `${...}` подстановка в любых полях POM
- Version ranges: `[1.0,2.0)`, `[1.0]`, `(,2.0]` (используются редко, <5% POM)
- Exclusions: `<exclusions>` в dependency для исключения транзитивных

### 4.2 Алгоритм: Greedy BFS (Nearest-Wins)

Maven-алгоритм — BFS по графу зависимостей. Для каждого GA: первая встреченная при BFS версия — победитель.

```
fn resolve_maven(request: MavenResolveRequest) -> ResolvedTree {
    
    // ФАЗА 0: Подготовка
    // 0.1 Раскрыть parent chain → собрать наследованные properties
    let effective_props = resolve_parent_chain(request.parent, index);
    
    // 0.2 Раскрыть BOM imports рекурсивно
    //     spring-boot-dependencies → imports spring-framework-bom, jackson-bom, ...
    //     → плоская карта managed: HashMap<GA, ManagedEntry>
    let managed = expand_boms(request.managed_deps, effective_props, index);
    
    // 0.3 Подставить ${...} во все version/groupId/artifactId
    let deps = interpolate_all(request.deps, effective_props);

    // ФАЗА 1: BFS
    let mut queue: VecDeque<(GAV, depth, scope, Set<GA> exclusions)>;
    let mut resolved: HashMap<GA, ResolvedEntry>;
    
    // Seed: прямые зависимости
    for dep in deps {
        let version = managed.get(dep.ga).unwrap_or(dep.version);
        let version = resolve_range_if_needed(version, dep.ga, index);
        resolved.insert(dep.ga, ResolvedEntry { version, depth: 1, scope: dep.scope });
        queue.push_back((GAV(dep.ga, version), 1, dep.scope, dep.exclusions));
    }
    
    // BFS loop
    while let Some((gav, depth, parent_scope, exclusions)) = queue.pop_front() {
        let pom = index.get(gav);
        let pom_props = resolve_properties(pom, index);  // with parent inheritance
        
        for dep in pom.dependencies {
            let dep = interpolate(dep, pom_props);
            
            // Фильтры
            if exclusions.contains(dep.ga) { continue; }
            let eff_scope = propagate_scope(parent_scope, dep.scope);
            if !request.options.include_scopes.contains(eff_scope) { continue; }
            if dep.optional && !request.options.include_optional { continue; }
            
            // Версия: managed override → declared → range resolve
            let version = managed.get(dep.ga)
                .or(dep.version)
                .map(|v| resolve_range_if_needed(v, dep.ga, index));
            
            // NEAREST-WINS: если GA уже resolved — пропустить
            if resolved.contains_key(dep.ga) { continue; }
            
            resolved.insert(dep.ga, ResolvedEntry {
                version, depth: depth + 1, scope: eff_scope, from: gav
            });
            
            let merged_excl = exclusions.union(dep.exclusions);
            queue.push_back((GAV(dep.ga, version), depth+1, eff_scope, merged_excl));
        }
    }
    
    build_tree(resolved)
}
```

### 4.3 Scope Propagation Matrix

Транзитивный scope вычисляется из scope родителя и scope зависимости:

```
Parent →    compile     provided    runtime     test
Dep ↓
compile     compile     provided    runtime     test
provided    (skip)      (skip)      (skip)      (skip)
runtime     runtime     (skip)      runtime     test
test        (skip)      (skip)      (skip)      (skip)
```

`(skip)` = зависимость не включается в resolve. provided и test НЕ транзитивны.

### 4.4 BOM Expansion

BOM — самая сложная часть Maven resolve. Пример цепочки:

```
Пользователь объявляет:
  <dependencyManagement>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-dependencies</artifactId>
      <version>3.2.0</version>
      <type>pom</type>
      <scope>import</scope>
    </dependency>
  </dependencyManagement>

spring-boot-dependencies:3.2.0 содержит:
  dependencyManagement:
    - com.fasterxml.jackson:jackson-bom:2.16.0 (import)
    - io.netty:netty-bom:4.1.101 (import)
    - io.micrometer:micrometer-bom:1.12.0 (import)
    - ... ещё ~30 BOM imports
    - org.springframework:spring-core:6.1.1
    - org.springframework:spring-web:6.1.1
    - ... ещё ~350 direct managed entries

jackson-bom:2.16.0 содержит:
    - com.fasterxml.jackson.core:jackson-core:2.16.0
    - com.fasterxml.jackson.core:jackson-databind:2.16.0
    - ... ещё ~40 entries

Итого: один spring-boot-dependencies BOM раскрывается в ~500 managed entries
```

Алгоритм раскрытия:

```
fn expand_boms(managed_deps, props, index) -> HashMap<GA, Version> {
    let mut result = HashMap::new();
    let mut visited_boms = HashSet::new();  // cycle detection
    
    fn expand_recursive(ga, version, result, visited, index) {
        if !visited.insert((ga, version)) { return; }  // cycle
        
        let pom = index.get(GAV(ga, version));
        
        // Сначала рекурсивно раскрыть вложенные BOM imports
        for dep in pom.managed_deps.filter(|d| d.is_import) {
            expand_recursive(dep.ga, dep.version, result, visited, index);
        }
        
        // Затем добавить direct managed entries (override вложенных!)
        for dep in pom.managed_deps.filter(|d| !d.is_import) {
            result.insert(dep.ga, dep.version);  // later = wins (override)
        }
    }
    
    for dep in managed_deps {
        if dep.is_import {
            expand_recursive(dep.ga, dep.version, &mut result, &mut visited_boms, index);
        } else {
            result.insert(dep.ga, dep.version);  // direct entries override BOM
        }
    }
    
    result
}
```

### 4.5 Property Interpolation

```
POM properties:
  <properties>
    <spring.version>6.1.1</spring.version>
    <jackson.version>${jackson-bom.version}</jackson.version>
  </properties>
  
  <dependency>
    <groupId>org.springframework</groupId>
    <artifactId>spring-core</artifactId>
    <version>${spring.version}</version>
  </dependency>

Алгоритм:
1. Собрать properties из текущего POM
2. Merge с properties из parent POM (current overrides parent)
3. Рекурсивно раскрыть ${...} (max 10 уровней, cycle detection)
4. Built-in properties: ${project.version}, ${project.groupId}, ${project.artifactId}
```

### 4.6 Gradle-режим (Highest-Wins)

Gradle использует Maven Central, но другой алгоритм. PackageHub поддерживает оба:

```
strategy: "nearest"   → Maven BFS, первый побеждает
strategy: "highest"   → Gradle-стиль, наивысшая версия побеждает

Разница в resolve loop:
  Maven:  if resolved.contains_key(dep.ga) { continue; }
  Gradle: if resolved.get(dep.ga).version >= version { continue; }
          else { resolved.insert(dep.ga, new_entry); re-queue subtree; }
```

Gradle highest-wins может потребовать повторного обхода поддерева при upgrade:
если `jackson-core` upgrade с 2.15 на 2.16 → нужно заново раскрыть зависимости 2.16. Но на практике зависимости версий одного GA почти идентичны, так что overhead минимален.

### 4.7 Входные данные

```
Maven ResolveRequest:
  dependencies:
    - group_id: "org.springframework.boot"
      artifact_id: "spring-boot-starter-web"
      version: "3.2.0"
      scope: "compile"
      exclusions: []
    - group_id: "org.postgresql"
      artifact_id: "postgresql"
      version: "42.7.1"
      scope: "runtime"

  managed_dependencies:
    - group_id: "org.springframework.boot"
      artifact_id: "spring-boot-dependencies"
      version: "3.2.0"
      type: "pom"
      import: true

  parent:
    group_id: "org.springframework.boot"
    artifact_id: "spring-boot-starter-parent"
    version: "3.2.0"

  properties:
    "java.version": "17"
    "spring-security.version": "6.2.1"   # override BOM version

  options:
    strategy: "nearest"                    # Maven-стиль
    include_scopes: ["compile", "runtime"]
    include_optional: false
    max_depth: 50
```

### 4.8 Расчёт памяти

```
Уникальных GA:            ~700K
Версий (reachable GAV):   ~5M (из 14.5M total)
Зависимостей:             ~70M (без test scope из reachable GAV)
Managed deps:             ~3M (BOM entries)
Properties:               ~5M (resolve-relevant, не все)
Avg deps per GAV:         ~8.3

Per GAV entry:
  group_id (u32) + artifact_id (u32) + version (u32):    12 bytes
  parent GAV (Option<(u32,u32,u32)>):                    13 bytes
  Vec<Dep> header:                                       24 bytes
  Vec<ManagedDep> header:                                24 bytes
  Vec<Property> header:                                  24 bytes
  packaging + flags:                                     2 bytes
  ──────────────
  Fixed per GAV: ~99 bytes

Per dependency: ga(8) + version(4) + scope(1) + optional(1) + exclusions_len(2) = 16 bytes
  + avg 0.3 exclusions × 8 bytes = 2.4 bytes
  = ~18 bytes per dep
  
Per managed dep: ga(8) + version(4) + scope(1) + is_import(1) = 14 bytes
Per property: key(4) + value(4) = 8 bytes

Calculation:
  5M × 99 bytes (fixed)                   = 495 MB
  70M × 18 bytes (dependencies)           = 1,260 MB  (*)
  3M × 14 bytes (managed deps)            = 42 MB
  5M × 8 bytes (properties)               = 40 MB
  String interning (~800K unique strings)  = 20 MB
  HashMap overhead (5M entries)            = 400 MB
  ────────────────────────────────────────
  Total: ~2.3 GB

(*) Это доминирующая статья — 120M зависимостей в raw Central,
    после фильтрации reachable + no-test остаётся ~70M.

Pre-computed BOM cache (top-500 BOM'ов, flattened):
  500 × avg 200 entries × 14 bytes        = 1.4 MB (пренебрежимо)

Рекомендация: 4-8 GB RAM (with double buffer: 8 GB)
```

### 4.9 Производительность

```
Микросервис (10-20 direct deps, ~50-100 transitive):
  BOM expansion: 0.1-0.5 ms (pre-cached)
  BFS resolve: 0.2-0.5 ms
  Total: < 1 ms

Spring Boot web app (20-40 direct, ~200-400 transitive):
  BOM expansion: 0.5-1 ms (spring-boot-dependencies = ~500 entries)
  Property interpolation: 0.1-0.3 ms
  BFS resolve: 0.5-2 ms
  Total: 1-3 ms

Enterprise monolith (100+ direct, ~500-1500 transitive):
  Multiple BOM expansions: 1-3 ms
  BFS resolve: 2-8 ms
  Total: 3-10 ms

С Redis cache: effective p50 < 0.5 ms
```

### 4.10 Repository Tiers

Подробно описано в SPEC.md 3.8. Для резолвера это означает:

```
Tier 1 (Central + Google): всё в in-memory индексе
Tier 2 (Gradle Plugins, Confluent): lazy load в LRU cache при первом запросе
Tier 3 (корпоративные Nexus/Artifactory): 
  - Пользователь передаёт repository_url в ResolveRequest
  - При miss в индексе: HTTP GET POM → parse → add to per-request cache
  - Latency: +50-200ms per unknown artifact (network I/O)
  - Для частых клиентов: background pre-sync их репо
```

---

## 5. crates.io (Rust)

### 5.1 Правила экосистемы

- Одна версия per crate
- Semver: `^1.2.3`, `~1.2.3`, `>=1.0.0, <2.0.0`, `=1.2.3`
- Dependency kinds: normal, dev, build
- Features: optional dependency groups (аналог PyPI extras)
- Target-specific deps: `[target.'cfg(unix)'.dependencies]`
- Rust edition и rust-version: минимальная версия компилятора

### 5.2 Алгоритм: PubGrub (как PyPI)

Cargo использует собственную реализацию PubGrub. Для PackageHub — pubgrub-rs (тот же алгоритм что для PyPI).

Отличия от PyPI resolve:
- Features создают дополнительные "виртуальные пакеты": `serde` + `serde/derive`
- target cfg() выражения аналогичны Python environment markers
- Build deps обрабатываются отдельно (не влияют на runtime classpath)

### 5.3 Расчёт памяти

```
Пакетов:               ~170K
Версий (reachable):    ~350K (из 500K total — crates.io молодая, мало мёртвых)
Зависимостей:          ~2.5M
Avg deps per version:  ~7

Per version: ~100 bytes
Per dependency: ~28 bytes (name + semver_spec + kind + features + target_cfg)

350K × 100 = 35 MB
2.5M × 28 = 70 MB
String interning: ~5 MB
HashMap overhead: ~30 MB
────────────────────
Total: ~140 MB

Рекомендация: 512 MB RAM (самая маленькая экосистема)
```

### 5.4 Производительность

```
Typical Rust project (~50-100 transitive): 1-3 ms
Heavy (tokio-based, ~200-300 transitive): 3-10 ms
With features resolution: +1-2 ms overhead
```

---

## 6. NuGet (.NET)

### 6.1 Правила экосистемы

- Одна версия per package per target framework
- Version ranges: `[1.0,2.0)`, `1.0.*`, `[1.0]`
- Target Framework Moniker (TFM): `net6.0`, `net8.0`, `netstandard2.0`
- Зависимости группируются по TFM: пакет может иметь разные deps для net6.0 и net8.0
- Resolution strategy: highest-wins (как Gradle)

### 6.2 Алгоритм: Highest-Wins + TFM Filter

```
fn resolve_nuget(request) -> ResolvedTree {
    // 1. Определить target TFM (из запроса или default net8.0)
    let tfm = request.target_framework;
    
    // 2. BFS с highest-wins (как Gradle)
    // Но при загрузке deps — фильтровать по TFM compatibility:
    //   net8.0 compatible with: net8.0, net7.0, net6.0, netstandard2.1, netstandard2.0
    //   Выбираем ближайший совместимый TFM group
    
    // 3. Если пакет имеет зависимости в group "net8.0" — берём их
    //    Если нет — fallback на "net6.0", затем "netstandard2.1", etc.
    
    // 4. Highest-wins: при конфликте побеждает наивысшая версия
}
```

### 6.3 Расчёт памяти

```
Пакетов:               ~400K
Версий (reachable):    ~1.5M (из 3M total)
Зависимостей:          ~8M (per TFM group)

Total index: ~600 MB
Рекомендация: 2 GB RAM (with double buffer)
```

---

## 7. Go

### 7.1 Правила экосистемы

- Одна версия per module
- **Minimal Version Selection (MVS):** уникальный алгоритм — выбирается МИНИМАЛЬНАЯ версия, удовлетворяющая все constraints (не максимальная!)
- go.mod: `require`, `replace`, `exclude` directives
- Indirect deps: помечены `// indirect`
- Semantic import versioning: `v2+` — другой import path (`github.com/foo/bar/v2`)

### 7.2 Алгоритм: MVS (Minimal Version Selection)

MVS — уникальный алгоритм, придуманный Russ Cox. Противоположен всем остальным: вместо "latest compatible" выбирает "minimum required".

```
fn resolve_go(request) -> ResolvedTree {
    // 1. Собрать ВСЕ require directives из всех go.mod транзитивно
    // 2. Для каждого module: выбрать МАКСИМУМ из всех запрошенных версий
    //    (это и есть "minimum" — минимальная версия удовлетворяющая ВСЕМ)
    //
    // Пример:
    //   A requires X >= 1.2
    //   B requires X >= 1.5
    //   Available: 1.0, 1.2, 1.5, 1.8, 2.0
    //   MVS выбирает: 1.5 (не 2.0!)
    //
    // 3. Apply replace/exclude directives
    // 4. Нет backtracking — один проход
}
```

MVS гарантирует **полную воспроизводимость** без lock-файла (go.sum — checksums, не versions).

### 7.3 Расчёт памяти

```
Модулей:               ~200K
Версий (reachable):    ~600K (из 1M total)
Зависимостей:          ~3M

Total index: ~250 MB
Рекомендация: 1 GB RAM
```

### 7.4 Производительность

```
Typical Go project (~30-80 transitive): 0.3-1 ms
Heavy (Kubernetes-scale, ~200+ transitive): 1-3 ms

MVS — самый быстрый алгоритм: один проход, O(N), нет backtracking,
нет conflict resolution. Просто max() по версиям.
```

---

## 8. Сводная таблица

### 8.1 Алгоритмы

```
Экосистема   Алгоритм         Конфликты          Backtracking  Multi-version
─────────────────────────────────────────────────────────────────────────────
PyPI         PubGrub (CDCL)   SAT solving        Да            Нет
npm          Greedy DFS       Нет (кроме peer)   Нет           Да
Maven        Greedy BFS       Nearest wins       Нет           Нет
Gradle       Greedy BFS       Highest wins       Нет           Нет
crates.io    PubGrub (CDCL)   SAT solving        Да            Нет
NuGet        Greedy BFS       Highest wins       Нет           Нет (per TFM)
Go           MVS              Min version         Нет           Нет
```

### 8.2 Ресурсы (in-memory индекс, needed packages only)

```
Экосистема   Пакетов  Версий       Deps     Index RAM  Total RAM*
─────────────────────────────────────────────────────────────────
PyPI         400K     3.2M         15M      1.2 GB     4 GB
npm          1M       28M (needed) 160M     5.0 GB     16 GB
Maven        550K     10M (needed) 100M     2.4 GB     8 GB
NuGet        280K     2.2M         12M      0.8 GB     3 GB
crates.io    140K     420K         3M       0.15 GB    1 GB
Go           150K     750K         4M       0.3 GB     1 GB
─────────────────────────────────────────────────────────────────
TOTAL        2.5M     44M          294M     ~10 GB     ~32 GB

* Total RAM = index + double buffer + overhead + LRU cache
  Рекомендация для all-in-one: 32 GB
  Рекомендация для 3 группы подов: Heavy 16GB, Medium 8GB, Light 4GB
```

### 8.3 Производительность

```
Экосистема   p50      p95       p99       Throughput (4 cores)
────────────────────────────────────────────────────────────────
PyPI         3 ms     15 ms     50 ms     1,500 req/s
npm          5 ms     40 ms     200 ms    800 req/s
Maven        1 ms     5 ms      20 ms     5,000 req/s
NuGet        1 ms     5 ms      15 ms     5,000 req/s
crates.io    2 ms     10 ms     30 ms     3,000 req/s
Go           0.5 ms   3 ms      10 ms     8,000 req/s
────────────────────────────────────────────────────────────────
Mixed avg    2 ms     13 ms     55 ms     ~4,000 req/s

С Redis cache (85% hit): effective throughput ×5-10
```

### 8.4 Сложность входных данных

```
Экосистема   Входные данные помимо зависимостей
────────────────────────────────────────────────────────────────
PyPI         environment markers (python_version, os, platform)
             extras
             
npm          (ничего дополнительного)

Maven        dependencyManagement + BOM imports (рекурсивные!)
             parent POM chain (наследование)
             properties (${...} interpolation)
             scopes (compile/provided/runtime/test)
             exclusions per dependency
             version ranges [1.0,2.0)

crates.io    features (optional deps)
             target cfg() expressions

NuGet        target framework moniker (TFM)
             dependency groups per TFM

Go           replace directives
             exclude directives
             semantic import versioning (v2+)
```

Maven — самая сложная экосистема по входным данным. npm — самая простая.

### 8.5 Стоимость инфраструктуры

```
Конфигурация                     RAM      CPU     Cost/мес  Cost HA (×2)
────────────────────────────────────────────────────────────────────────
Deployment: 3 группы подов (рекомендуемый)
  "Heavy" (npm)                  16 GB    4 cores $200      $400 (2-4 replicas)
  "Medium" (PyPI + Maven)        8 GB     4 cores $100      $200 (2-3 replicas)
  "Light" (NuGet + crates + Go)  4 GB     2 cores $30       $60  (2 replicas)

Redis cache (shared)             4 GB     2 cores $50       $100
PostgreSQL (source of truth)     256 GB   4 cores $150      $300
────────────────────────────────────────────────────────────────────────
Baseline (all ecosystems): ~$530-660/мес
Peak (scaled up): ~$810-1060/мес

Один бинарник, флаг --ecosystems определяет какие экосистемы загружать.
Масштабирование по группам: HPA на CPU utilization.
```

---

## 9. Needed Packages Optimization

### 9.1 Проблема

Публичные registry содержат огромное количество мёртвых пакетов. Хранить ВСЁ в RAM дорого и бессмысленно.

Но обрезка на уровне **версий** некорректна: резолвер не знает заранее какую версию выберет. Если constraint `jackson-core >= 2.10, < 2.16` — нужны все версии jackson-core в диапазоне. Поэтому обрезка идёт на уровне **пакетов (GA)**: пакет либо нужен целиком (все версии), либо не нужен вообще.

### 9.2 Определение

Пакет (GA для Maven, name для остальных) **нужен в индексе** если:
1. Имеет хотя бы одну версию, опубликованную за последние 5 лет, ИЛИ
2. На него ссылается зависимость из другого нужного пакета (транзитивно)

Если пакет нужен — в индекс загружаются **все его версии**, кроме yanked и SNAPSHOT.

### 9.3 Вычисление

```sql
-- Запускается еженедельно после sync
-- Время: 10-30 минут per ecosystem

-- Шаг 1: Reset
UPDATE package_versions SET needed_in_index = FALSE;

-- Шаг 2: Найти нужные пакеты (рекурсивно)
WITH RECURSIVE needed_pkgs AS (
    -- Корни: пакеты с недавней публикацией
    SELECT DISTINCT p.id FROM packages p
    JOIN package_versions pv ON pv.package_id = p.id
    WHERE pv.release_date > now() - interval '5 years'

    UNION

    -- Транзитивно: пакеты, от которых зависят нужные
    SELECT DISTINCT d.resolved_package_id FROM dependencies d
    JOIN package_versions pv ON d.version_id = pv.id
    WHERE pv.package_id IN (SELECT id FROM needed_pkgs)
      AND d.resolved_package_id IS NOT NULL
)
-- Шаг 3: Пометить ВСЕ версии нужных пакетов (минус yanked/SNAPSHOT)
UPDATE package_versions SET needed_in_index = TRUE
WHERE package_id IN (SELECT id FROM needed_pkgs)
  AND status != 'yanked'
  AND version NOT LIKE '%-SNAPSHOT';
```

### 9.4 Результаты по экосистемам

```
             Total pkgs → Needed pkgs → Total vers → In index → Reduction
Maven        700K GA       550K          14.5M        10M        31%
npm          2.5M          1M            60M          28M        53%
PyPI         550K          400K          4M           3.2M       20%
NuGet        400K          280K          3M           2.2M       27%
crates.io    170K          140K          500K         420K       16%
Go           200K          150K          1M           750K       25%
```

npm выигрывает больше всех — огромное количество мёртвых пакетов. crates.io меньше всех — молодая экосистема.

Обрезка скромнее чем при подходе "reachable versions" (65%/87%), но корректная: резолвер гарантированно найдёт любую версию нужного пакета.

### 9.5 Fallback при промахе

```
1. Пакет не найден в in-memory индексе
2. Запрос в PostgreSQL — загрузить ВСЕ версии этого пакета
3. Добавить в LRU cache (100K entries, ~50 MB)
4. Пометить needed_in_index при следующем пересчёте
5. Если не найден в PostgreSQL: вернуть "unknown dependency"

Latency overhead: +5-20 ms per miss
Expected miss rate: <2% запросов
```

---

## 10. Phased Implementation

### Phase 0: Best-effort в PostgreSQL (MVP, этапы 2-6)

```
Что: resolved_version_id заполняется при sync (latest stable matching constraint)
Как: SQL при sync pipeline
Ограничения: не учитывает транзитивные конфликты, no BOM/parent resolution
Для кого: UI дерево зависимостей, простой API
```

### Phase 1: Resolver Microservice (этап 11)

```
Что: Rust сервис с PubGrub + greedy resolvers
Как: gRPC сервис, in-memory индекс из PostgreSQL
Объём: все 6 экосистем
RAM: 32 GB (all-in-one)
```

### Phase 2: Production hardening (этап 12)

```
Что: Redis cache, monitoring, rate limiting, Tier 3 proxy
Как: Redis layer перед resolver, Prometheus metrics
SLA: p95 < 50ms, 99.9% availability
```

### Phase 3: Advanced features (backlog)

```
Что:
  - Forking resolver для PyPI (multi-environment resolution)
  - npm deduplicated tree (Arborist-like hoisting)
  - Maven Gradle-mode (highest-wins) + mixed resolution
  - Conflict explanation (PubGrub derivation trees)
  - Pre-computed resolution for popular package combinations
```
