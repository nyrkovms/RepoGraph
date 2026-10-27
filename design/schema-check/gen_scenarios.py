"""Generate scenario SQL for design/schema-target.sql: positive data per ecosystem + negative checks."""
import sys

sys.path.insert(0, sys.argv[1])
from sortkey_proto import pep440_key, rpm_key, semver_key  # noqa: E402

out = []
w = out.append


def uid(kind, key):
    return f"md5('{kind}:{key}')::uuid"


def sk(fn, v):
    return f"decode('{fn(v).hex()}', 'hex')"


def expect_error(label, sql, sqlstate):
    w(f"""DO $$ BEGIN
  BEGIN {sql};
  RAISE EXCEPTION 'NEGATIVE CHECK PASSED UNEXPECTEDLY: {label}';
  EXCEPTION WHEN SQLSTATE '{sqlstate}' THEN RAISE NOTICE 'ok (rejected): {label}';
  END;
END $$;""")


def check(label, cond_sql):
    w(f"DO $$ BEGIN IF NOT ({cond_sql}) THEN RAISE EXCEPTION 'CHECK FAILED: {label}'; END IF; "
      f"RAISE NOTICE 'ok: {label}'; END $$;")


# --- registries -------------------------------------------------------------------------------
regs = [
    ("pypi", "pypi", "pypi-warehouse", "https://pypi.org/", "NULL", 10, "none", "NULL", "{}"),
    ("pytorch-cu121", "pypi", "pypi-simple", "https://download.pytorch.org/whl/cu121/",
     """'{"mode":"exclusive","packages":["torch","torchvision","torchaudio"]}'""", 5, "none", "NULL", "{}"),
    ("npmjs", "npm", "npm-registry", "https://registry.npmjs.org/", "NULL", 10, "none", "NULL",
     '{"changes_url":"https://replicate.npmjs.com/registry/_changes"}'),
    ("corp-npm", "npm", "npm-registry", "https://npm.corp.example/", """'{"mode":"exclusive","scopes":["corp"]}'""",
     5, "bearer", "'CORP_NPM_TOKEN'", "{}"),
    ("maven-central", "maven", "maven2", "https://repo1.maven.org/maven2/", "NULL", 10, "none", "NULL", "{}"),
    ("google-maven", "maven", "maven2", "https://dl.google.com/android/maven2/",
     """'{"group_patterns":["androidx.*","com.android.*","com.google.android.*"]}'""", 20, "none", "NULL", "{}"),
    ("gradle-plugins", "maven", "maven2", "https://plugins.gradle.org/m2/", "NULL", 30, "none", "NULL", "{}"),
    ("rhel-9-baseos", "rpm", "rpm-repodata", "https://cdn.redhat.com/content/dist/rhel9/$releasever/$basearch/baseos/os",
     "NULL", 10, "tls_client_cert", "'RHEL_ENTITLEMENT'", '{"namespace":"rhel-9","release":"9","arch":["x86_64","aarch64"]}'),
    ("rhel-9-appstream", "rpm", "rpm-repodata", "https://cdn.redhat.com/content/dist/rhel9/$releasever/$basearch/appstream/os",
     "NULL", 20, "tls_client_cert", "'RHEL_ENTITLEMENT'", '{"namespace":"rhel-9","release":"9","arch":["x86_64","aarch64"]}'),
    ("rhel-8-appstream", "rpm", "rpm-repodata", "https://cdn.redhat.com/content/dist/rhel8/$releasever/$basearch/appstream/os",
     "NULL", 20, "tls_client_cert", "'RHEL_ENTITLEMENT'", '{"namespace":"rhel-8","release":"8","modular":true}'),
    ("rocky-9-baseos", "rpm", "rpm-repodata", "https://dl.rockylinux.org/pub/rocky/9/BaseOS/$basearch/os/",
     "NULL", 10, "none", "NULL", '{"namespace":"rocky-9","release":"9"}'),
    ("goproxy", "go", "goproxy", "https://proxy.golang.org/", "NULL", 10, "none", "NULL",
     '{"index_url":"https://index.golang.org/index"}'),
    ("crates-io", "crates", "cargo-sparse", "https://index.crates.io/", "NULL", 10, "none", "NULL", "{}"),
    ("nuget-org", "nuget", "nuget-v3", "https://api.nuget.org/v3/index.json", "NULL", 10, "none", "NULL", "{}"),
]
for slug, eco, proto, url, routing, prio, auth, cred, cfg in regs:
    w(f"INSERT INTO registries (id, ecosystem, slug, name, protocol, base_url, routing, priority, auth_type, credentials_ref, config) "
      f"VALUES ({uid('reg', slug)}, '{eco}', '{slug}', '{slug}', '{proto}', '{url}', {routing}, {prio}, '{auth}', {cred}, '{cfg}');")
    w(f"INSERT INTO sync_states (registry_id) VALUES ({uid('reg', slug)});")

expect_error("registry with auth but without credentials_ref",
             f"INSERT INTO registries (id, ecosystem, slug, name, protocol, base_url, auth_type) VALUES "
             f"({uid('reg', 'bad')}, 'npm', 'bad', 'bad', 'npm-registry', 'https://x/', 'basic')", "23514")

# --- profiles ---------------------------------------------------------------------------------
profiles = [("pypi", "default", True, ["pypi"]), ("pypi", "cuda121", False, ["pytorch-cu121", "pypi"]),
            ("maven", "default", True, ["maven-central", "google-maven", "gradle-plugins"]),
            ("npm", "default", True, ["corp-npm", "npmjs"])]
for eco, slug, default, members in profiles:
    pid = uid("prof", f"{eco}/{slug}")
    w(f"INSERT INTO source_profiles (id, ecosystem, slug, name, is_default) VALUES ({pid}, '{eco}', '{slug}', '{slug}', {default});")
    for pos, reg in enumerate(members):
        w(f"INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position) VALUES ({pid}, {uid('reg', reg)}, '{eco}', {pos});")

expect_error("maven registry inside a pypi profile",
             f"INSERT INTO source_profile_registries VALUES ({uid('prof', 'pypi/cuda121')}, {uid('reg', 'maven-central')}, 'pypi', 9, NULL)", "23503")
expect_error("same registry declared with a wrong ecosystem to sneak past the FK",
             f"INSERT INTO source_profile_registries VALUES ({uid('prof', 'pypi/cuda121')}, {uid('reg', 'maven-central')}, 'maven', 9, NULL)", "23503")
expect_error("second default profile for pypi",
             f"INSERT INTO source_profiles (id, ecosystem, slug, name, is_default) VALUES ({uid('prof', 'x')}, 'pypi', 'x', 'x', true)", "23505")

# --- packages ---------------------------------------------------------------------------------
def pkg(key, eco, ns, name, norm, primary, origin="seed", latest=None):
    lv = f"'{latest}'" if latest else "NULL"
    w(f"INSERT INTO packages (id, ecosystem, namespace, name, normalized_name, origin, primary_registry_id, latest_version, updated_at, synced_at) "
      f"VALUES ({uid('pkg', key)}, '{eco}', '{ns}', '{name}', '{norm}', '{origin}', {uid('reg', primary)}, {lv}, now(), now());")


def pkg_reg(key, eco, reg, revision="NULL"):
    w(f"INSERT INTO package_registries (package_id, registry_id, ecosystem, first_seen_at, upstream_revision) "
      f"VALUES ({uid('pkg', key)}, {uid('reg', reg)}, '{eco}', now(), {revision});")


def ver(key, version, fn, prerelease=False, status="active", raw="{}", metadata_reg=None, deps_state="hydrated"):
    mreg = uid("reg", metadata_reg) if metadata_reg else "NULL"
    w(f"INSERT INTO package_versions (id, package_id, version, sort_key, is_prerelease, status, raw_metadata, metadata_registry_id, dependencies_state) "
      f"VALUES ({uid('ver', key + '@' + version)}, {uid('pkg', key)}, '{version}', {sk(fn, version)}, {prerelease}, '{status}', '{raw}', {mreg}, '{deps_state}');")


def ver_src(key, version, reg, status="active"):
    w(f"INSERT INTO version_sources (version_id, registry_id, first_seen_at, status) "
      f"VALUES ({uid('ver', key + '@' + version)}, {uid('reg', reg)}, now(), '{status}');")


def dep(key, version, eco, ns, name, constraint, dtype="runtime", scope="NULL", alias="NULL", extra="NULL", markers="NULL", details="NULL", variant=None):
    var = uid("reg", variant) if variant else "NULL"
    w(f"INSERT INTO dependencies (id, version_id, dependency_ecosystem, dependency_namespace, dependency_name, alias, version_constraint, "
      f"dependency_type, scope, extra_name, environment_markers, details, raw_spec, variant_registry_id) VALUES (gen_random_uuid(), {uid('ver', key + '@' + version)}, "
      f"'{eco}', '{ns}', '{name}', {alias}, '{constraint}', '{dtype}', {scope}, {extra}, {markers}, {details}, '{name} {constraint}', {var});")


# PyPI + PyTorch index: one identity, versions from two sources
pkg("pypi/torch", "pypi", "", "torch", "torch", "pypi", latest="2.5.1")
pkg_reg("pypi/torch", "pypi", "pypi", "'25000000'")
pkg_reg("pypi/torch", "pypi", "pytorch-cu121", "'\"etag-abc\"'")
for v, srcs in [("2.4.0", ["pypi"]), ("2.5.1", ["pypi", "pytorch-cu121"]), ("2.5.1+cu121", ["pytorch-cu121"]), ("2.6.0rc1", ["pypi"])]:
    ver("pypi/torch", v, pep440_key, prerelease=v.endswith("rc1"), metadata_reg=srcs[0])
    for s in srcs:
        ver_src("pypi/torch", v, s)
ver_src("pypi/torch", "2.4.0", "pytorch-cu121", status="removed")  # gone from the CUDA index, still on PyPI
dep("pypi/torch", "2.5.1+cu121", "pypi", "", "nvidia-cublas-cu12", "==12.1.3.1", markers="""'{"type":"python_environment","conditions":[{"marker":"platform_system","op":"==","value":"Linux"}]}'""")

# torch 2.9.1: PyPI wheel requires nvidia-* (canonical, PyPI has priority), the PyTorch-index wheel requires only filelock
# (measured 2026-10-28: 16 nvidia-* requirements exist only on PyPI).
w(f"INSERT INTO registries (id, ecosystem, slug, name, protocol, base_url, priority) VALUES "
  f"({uid('reg', 'pytorch-cpu')}, 'pypi', 'pytorch-cpu', 'pytorch-cpu', 'pypi-simple', 'https://download.pytorch.org/whl/cpu/', 20);")
ver("pypi/torch", "2.9.1", pep440_key, metadata_reg="pypi")
ver_src("pypi/torch", "2.9.1", "pypi")
ver_src("pypi/torch", "2.9.1", "pytorch-cpu")
dep("pypi/torch", "2.9.1", "pypi", "", "filelock", "*")
dep("pypi/torch", "2.9.1", "pypi", "", "nvidia-cublas-cu12", "==12.8.4.1")
dep("pypi/torch", "2.9.1", "pypi", "", "filelock", "*", variant="pytorch-cpu")

expect_error("duplicate pypi package with empty namespace",
             f"INSERT INTO packages (id, ecosystem, name, normalized_name, origin, primary_registry_id, updated_at, synced_at) "
             f"VALUES ({uid('pkg', 'dup')}, 'pypi', 'Torch', 'torch', 'manual', {uid('reg', 'pypi')}, now(), now())", "23505")
expect_error("pypi package whose primary source is a maven registry",
             f"INSERT INTO packages (id, ecosystem, name, normalized_name, origin, primary_registry_id, updated_at, synced_at) "
             f"VALUES ({uid('pkg', 'bad')}, 'pypi', 'x', 'x', 'manual', {uid('reg', 'maven-central')}, now(), now())", "23503")
expect_error("pypi package linked to the npm registry",
             f"INSERT INTO package_registries (package_id, registry_id, ecosystem, first_seen_at) "
             f"VALUES ({uid('pkg', 'pypi/torch')}, {uid('reg', 'npmjs')}, 'npm', now())", "23503")

# npm: scoped package from a corporate registry, alias dependency
pkg("npm/react", "npm", "", "react", "react", "npmjs", latest="18.3.1")
pkg_reg("npm/react", "npm", "npmjs")
pkg("npm/@corp/ui", "npm", "corp", "ui", "ui", "corp-npm")
pkg_reg("npm/@corp/ui", "npm", "corp-npm")
for v in ["18.3.1", "19.0.0-rc.1", "18.2.0"]:
    ver("npm/react", v, semver_key, prerelease="-" in v, metadata_reg="npmjs")
    ver_src("npm/react", v, "npmjs")
ver("npm/@corp/ui", "1.0.0", semver_key, metadata_reg="corp-npm")
ver_src("npm/@corp/ui", "1.0.0", "corp-npm")
dep("npm/@corp/ui", "1.0.0", "npm", "", "react", "^18 || ^19", dtype="peer", scope="'peerDependencies'")
dep("npm/@corp/ui", "1.0.0", "npm", "", "string-width", "^4.2.0", alias="'string-width-cjs'")

# Maven: same GA in two repositories, Gradle plugin marker
pkg("maven/org.springframework:spring-core", "maven", "org.springframework", "spring-core", "spring-core", "maven-central", latest="6.1.14")
pkg_reg("maven/org.springframework:spring-core", "maven", "maven-central")
pkg_reg("maven/org.springframework:spring-core", "maven", "gradle-plugins")
ver("maven/org.springframework:spring-core", "6.1.14", semver_key, metadata_reg="maven-central")
ver_src("maven/org.springframework:spring-core", "6.1.14", "maven-central")
ver_src("maven/org.springframework:spring-core", "6.1.14", "gradle-plugins")
pkg("maven/marker", "maven", "org.springframework.boot", "org.springframework.boot.gradle.plugin",
    "org.springframework.boot.gradle.plugin", "gradle-plugins", latest="3.3.5")
pkg_reg("maven/marker", "maven", "gradle-plugins")
ver("maven/marker", "3.3.5", semver_key, metadata_reg="gradle-plugins")
ver_src("maven/marker", "3.3.5", "gradle-plugins")
dep("maven/marker", "3.3.5", "maven", "org.springframework.boot", "spring-boot-gradle-plugin", "3.3.5", scope="'compile'",
    details="""'{"exclusions":[{"group":"org.slf4j","artifact":"*"}]}'""")

# RPM: same name in two distributions = two packages; modular stream versions; arch-specific dependency
for ns, reg in [("rhel-9", "rhel-9-baseos"), ("rocky-9", "rocky-9-baseos")]:
    key = f"rpm/{ns}/openssl"
    pkg(key, "rpm", ns, "openssl", "openssl", reg, latest="1:3.0.7-28.el9")
    pkg_reg(key, "rpm", reg)
    for v, st in [("1:3.0.7-27.el9", "superseded"), ("1:3.0.7-28.el9", "active")]:
        ver(key, v, rpm_key, status=st, metadata_reg=reg)
        ver_src(key, v, reg, status=st)
dep("rpm/rhel-9/openssl", "1:3.0.7-28.el9", "rpm", "rhel-9", "libcrypto.so.3()(64bit)", "", details="""'{"kind":"requires"}'""",
    markers="""'{"type":"rpm_arch","arch":["x86_64","aarch64"]}'""")
dep("rpm/rhel-9/openssl", "1:3.0.7-28.el9", "rpm", "rhel-9", "openssl-libs(x86-64)", "= 1:3.0.7-28.el9",
    details="""'{"kind":"requires","flags":"EQ","evr":"1:3.0.7-28.el9"}'""", markers="""'{"type":"rpm_arch","arch":["x86_64"]}'""")
pkg("rpm/rhel-8/nodejs", "rpm", "rhel-8", "nodejs", "nodejs", "rhel-8-appstream", latest="1:20.18.0-1.module+el8.10.0+22000+aaaa")
pkg_reg("rpm/rhel-8/nodejs", "rpm", "rhel-8-appstream")
for v, stream in [("1:18.20.4-1.module+el8.10.0+21900+bbbb", "nodejs:18"), ("1:20.18.0-1.module+el8.10.0+22000+aaaa", "nodejs:20")]:
    ver("rpm/rhel-8/nodejs", v, rpm_key, raw=f'{{"module":"{stream}:8100020241001:a1b2c3d4"}}', metadata_reg="rhel-8-appstream")
    ver_src("rpm/rhel-8/nodejs", v, "rhel-8-appstream")

# Go: major version suffix makes another module; pseudo-versions
pkg("go/redis/v8", "go", "", "github.com/go-redis/redis/v8", "github.com/go-redis/redis/v8", "goproxy", latest="v8.11.5")
pkg("go/redis/v9", "go", "", "github.com/redis/go-redis/v9", "github.com/redis/go-redis/v9", "goproxy", latest="v9.7.0")
for v in ["v8.11.5", "v8.11.4", "v0.0.0-20191109021931-daa7c04131f5"]:
    ver("go/redis/v8", v, semver_key, prerelease=v.startswith("v0.0.0-"), metadata_reg="goproxy")

# NuGet: case-insensitive id, unlisted version
pkg("nuget/newtonsoft.json", "nuget", "", "Newtonsoft.Json", "newtonsoft.json", "nuget-org", latest="13.0.3")
ver("nuget/newtonsoft.json", "13.0.3", semver_key, metadata_reg="nuget-org")
ver("nuget/newtonsoft.json", "13.0.2", semver_key, status="unlisted", metadata_reg="nuget-org")
dep("nuget/newtonsoft.json", "13.0.3", "nuget", "", "system.runtime", "[4.3.0, )", markers="""'{"type":"dotnet_target_framework","framework":"netstandard1.0"}'""")

# crates: renamed dependency and a feature-gated optional one
pkg("crates/reqwest", "crates", "", "reqwest", "reqwest", "crates-io", latest="0.12.9")
ver("crates/reqwest", "0.12.9", semver_key, metadata_reg="crates-io")
dep("crates/reqwest", "0.12.9", "crates", "", "rustls", "^0.23", dtype="optional", extra="'rustls-tls'",
    details="""'{"default_features":false,"features":["ring"]}'""")
dep("crates/reqwest", "0.12.9", "crates", "", "http-body-util", "^0.1", alias="'body_util'")

# --- checks -----------------------------------------------------------------------------------
check("naive profile join (no routing) leaks PyPI builds of torch into the cuda121 profile",
      f"""(SELECT array_agg(v.version ORDER BY v.sort_key DESC) FROM package_versions v
           WHERE v.package_id = {uid('pkg', 'pypi/torch')} AND NOT v.is_prerelease AND EXISTS (
             SELECT 1 FROM version_sources s JOIN source_profile_registries p ON p.registry_id = s.registry_id
             WHERE s.version_id = v.id AND s.status = 'active' AND p.profile_id = {uid('prof', 'pypi/cuda121')}))
         = ARRAY['2.9.1', '2.5.1+cu121', '2.5.1', '2.4.0']""")
# Routing is evaluated by the plugin: for torch in profile cuda121 the exclusive rule leaves only pytorch-cu121.
check("with routing applied (registry set from the plugin) cuda121 sees only CUDA-index builds; 2.4.0 removed there",
      f"""(SELECT array_agg(v.version ORDER BY v.sort_key DESC) FROM package_versions v
           WHERE v.package_id = {uid('pkg', 'pypi/torch')} AND NOT v.is_prerelease AND EXISTS (
             SELECT 1 FROM version_sources s WHERE s.version_id = v.id AND s.status = 'active'
             AND s.registry_id = ANY (ARRAY[{uid('reg', 'pytorch-cu121')}])))
         = ARRAY['2.5.1+cu121', '2.5.1']""")
check("default pypi profile does not see the CUDA build",
      f"""NOT EXISTS (SELECT 1 FROM package_versions v JOIN version_sources s ON s.version_id = v.id
           JOIN source_profile_registries p ON p.registry_id = s.registry_id
           WHERE p.profile_id = {uid('prof', 'pypi/default')} AND v.version = '2.5.1+cu121')""")
# Dependencies of a version for the source the profile picked: its variant rows if any, else the canonical set.
for src, expected in [("pytorch-cpu", "ARRAY['filelock']"), ("pypi", "ARRAY['filelock', 'nvidia-cublas-cu12']")]:
    check(f"torch 2.9.1 dependencies as seen through {src}",
          f"""(SELECT array_agg(d.dependency_name ORDER BY d.dependency_name) FROM dependencies d
               WHERE d.version_id = {uid('ver', 'pypi/torch@2.9.1')}
                 AND d.variant_registry_id IS NOT DISTINCT FROM (
                   SELECT CASE WHEN EXISTS (SELECT 1 FROM dependencies x WHERE x.version_id = d.version_id
                                            AND x.variant_registry_id = {uid('reg', src)})
                               THEN {uid('reg', src)} END))
             = {expected}""")

check("latest stable npm react by sort_key is 18.3.1",
      f"""(SELECT version FROM package_versions WHERE package_id = {uid('pkg', 'npm/react')} AND NOT is_prerelease
           AND status = 'active' ORDER BY sort_key DESC LIMIT 1) = '18.3.1'""")
check("RPM newest openssl build in rhel-9 is -28, the -27 build is superseded",
      f"""(SELECT version || '/' || status FROM package_versions WHERE package_id = {uid('pkg', 'rpm/rhel-9/openssl')}
           ORDER BY sort_key DESC LIMIT 1) = '1:3.0.7-28.el9/active'""")
check("openssl exists as two packages: rhel-9 and rocky-9",
      "(SELECT count(*) FROM packages WHERE ecosystem = 'rpm' AND normalized_name = 'openssl') = 2")
check("nodejs streams order by EVR: 20 after 18",
      f"""(SELECT raw_metadata->>'module' FROM package_versions WHERE package_id = {uid('pkg', 'rpm/rhel-8/nodejs')}
           ORDER BY sort_key DESC LIMIT 1) LIKE 'nodejs:20:%'""")
check("Go pseudo-version sorts below tagged releases",
      f"""(SELECT array_agg(version ORDER BY sort_key) FROM package_versions WHERE package_id = {uid('pkg', 'go/redis/v8')})
         = ARRAY['v0.0.0-20191109021931-daa7c04131f5', 'v8.11.4', 'v8.11.5']""")
check("level-1 link: @corp/ui peer dependency on react finds the npm package by (ecosystem, namespace, name)",
      f"""(SELECT count(*) FROM dependencies d JOIN packages p ON p.ecosystem = d.dependency_ecosystem
           AND p.namespace = d.dependency_namespace AND p.normalized_name = d.dependency_name
           WHERE d.version_id = {uid('ver', 'npm/@corp/ui@1.0.0')}) = 1""")
check("spring-core 6.1.14 is published by two Maven sources",
      f"(SELECT count(*) FROM version_sources WHERE version_id = {uid('ver', 'maven/org.springframework:spring-core@6.1.14')}) = 2")

w(f"INSERT INTO sync_failures (id, operation, ecosystem, registry_id, package_ref, error_class, error_message) "
  f"VALUES (gen_random_uuid(), 'daily_update', 'pypi', NULL, 'pkg:pypi/torch', 'E', 'm');")
expect_error("second sync failure with the same key and NULL registry",
             "INSERT INTO sync_failures (id, operation, ecosystem, registry_id, package_ref, error_class, error_message) "
             "VALUES (gen_random_uuid(), 'daily_update', 'pypi', NULL, 'pkg:pypi/torch', 'E', 'm')", "23505")

w(f"DELETE FROM packages WHERE id = {uid('pkg', 'pypi/torch')};")
check("deleting a package cascades to versions, sources, registries links and dependencies",
      f"""NOT EXISTS (SELECT 1 FROM package_versions WHERE package_id = {uid('pkg', 'pypi/torch')})
          AND NOT EXISTS (SELECT 1 FROM package_registries WHERE package_id = {uid('pkg', 'pypi/torch')})
          AND NOT EXISTS (SELECT 1 FROM version_sources WHERE version_id = {uid('ver', 'pypi/torch@2.5.1')})
          AND NOT EXISTS (SELECT 1 FROM dependencies WHERE version_id = {uid('ver', 'pypi/torch@2.5.1+cu121')})""")

print("\n".join(out))
