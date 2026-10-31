#!/usr/bin/env bash
# Expected answers of the resolver reference cases (SPEC §14.2) from the native tool, never by hand.
# npm: `npm install --package-lock-only` of the npm bundled with node:20, reduced by scripts/reference_npm.js to what
# npm installs on the case's platform.
# PyPI: `pip install --dry-run --report` of the pinned pip version, run natively in a python image of the case's
# platform (`native.platform`, a docker platform): pip's --platform/--python-version flags change the wheel tags but
# not the environment markers, and narrow manylinux tags hide older-tagged wheels (nvidia-* are manylinux2014).
# crates: `cargo generate-lockfile` of a project depending on the roots (every platform, as Cargo.lock is) and
# `cargo tree --target <environment.target> -e normal,build` (what is built there), in the rust image below.
# go: `go list -m all` of a main module (go 1.23) that requires the roots, with module graph pruning as Go does it,
# in the golang image below.
# nuget: `dotnet restore` of a net8.0 project (.NET SDK 8) referencing the roots, packages from project.assets.json.
# maven: `mvn dependency:list -DincludeScope=runtime` of a project depending on the roots (Maven 3.9, JDK 21); a case
# with `strategy: gradle` instead lists the runtimeClasspath of a Gradle 8 `java` project with `implementation` of the
# roots, reading POMs only (`metadataSources { mavenPom(); ignoreGradleMetadataRedirection() }`: without the second, a POM
# marked `published-with-gradle-metadata` sends Gradle to its `.module`): our gradle strategy does not read Gradle Module Metadata
# (SPEC §9.7, Q15).
# rpm: dnf 4's Python API (the resolve of `dnf install --assumeno --installroot`) in the case's Rocky Linux image, over
# frozen vault snapshots; the module streams of the environment enabled first.
# Needs docker and the live network. Usage: scripts/generate_resolver_references.sh [case-dir ...]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PIP_VERSION="24.3.1"
IMAGE="python:3.12-slim"
RUST_IMAGE="rust:1.86-slim"
GO_IMAGE="golang:1.23"
DOTNET_IMAGE="mcr.microsoft.com/dotnet/sdk:8.0"
MAVEN_IMAGE="maven:3.9-eclipse-temurin-21"
GRADLE_IMAGE="gradle:8.10-jdk21"
CASES=("$@")
if [[ ${#CASES[@]} -eq 0 ]]; then
  # No mapfile: macOS ships bash 3.
  while IFS= read -r dir; do CASES+=("$dir"); done < <(find backend/tests/reference -mindepth 2 -maxdepth 2 -type d | sort)
fi

npm_case() {
  local case_dir="$1" platform="$2"
  docker run --rm --platform "${platform:-linux/amd64}" -v "$ROOT/${case_dir}:/case" \
    -v "$ROOT/scripts/reference_npm.js:/reduce.js:ro" -w /work node:20-slim sh -c "
    set -e
    npm install --silent --no-audit --no-fund --prefix /tools js-yaml@4.1.0 >/dev/null 2>&1
    NODE_PATH=/tools/node_modules node -e '
      const yaml = require(\"js-yaml\"), fs = require(\"fs\");
      const c = yaml.load(fs.readFileSync(\"/case/case.yaml\", \"utf8\"));
      const deps = Object.fromEntries(c.roots.map(r => [decodeURIComponent(r.purl.slice(8).split(\"@\").length > 2
        ? \"@\" + r.purl.slice(8).split(\"@\")[1] : r.purl.slice(8).replace(/%40/g, \"@\")), r.constraint || \"*\"]));
      fs.writeFileSync(\"package.json\", JSON.stringify({name: \"reference-case\", version: \"1.0.0\", dependencies: deps}));
    '
    npm install --package-lock-only --ignore-scripts --no-audit --no-fund >/dev/null 2>&1
    node -e '
      const { execSync } = require(\"child_process\");
      const install = JSON.parse(execSync(\"node /reduce.js\").toString());
      const out = {tool: \"npm \" + execSync(\"npm --version\").toString().trim(),
                   command: [\"npm\", \"install\", \"--package-lock-only\"], generated_at: new Date().toISOString(),
                   install};
      require(\"fs\").writeFileSync(\"/case/expected.json\", JSON.stringify(out, null, 2) + \"\\n\");
      console.log(install.length, \"packages\");
    '
  "
}

crates_case() {
  local case_dir="$1" platform="$2" work
  work="$(mktemp -d)"
  # The project: each root under a name of its own, so two classes of one crate can both be roots.
  python3 - "$case_dir/case.yaml" "$work" <<'PY'
import sys, yaml
case = yaml.safe_load(open(sys.argv[1]))
env = case.get("environment") or {}
default = str(env.get("default_features", True)).lower() not in ("false", "0", "no")
lines = ['[package]', 'name = "reference-case"', 'version = "0.0.0"', 'edition = "2021"', '', '[dependencies]']
for i, root in enumerate(case["roots"]):
    name = root["purl"].split("/", 1)[1].split("@")[0]
    features = list(root.get("extras") or []) + list(env.get("features") or [])
    lines.append(f'root{i} = {{ package = "{name}", version = "{root.get("constraint") or "*"}", '
                 f'features = {features!r}, default-features = {str(default).lower()} }}'.replace("'", '"'))
open(f"{sys.argv[2]}/Cargo.toml", "w").write("\n".join(lines) + "\n")
open(f"{sys.argv[2]}/target", "w").write(env.get("target") or "x86_64-unknown-linux-gnu")
PY
  mkdir -p "$work/src" && echo "fn main() {}" > "$work/src/main.rs"
  docker run --rm --platform "${platform:-linux/amd64}" -v "$work:/w" -w /w "$RUST_IMAGE" sh -c '
    set -e
    cargo generate-lockfile -q
    cargo tree -q --target "$(cat target)" -e normal,build --prefix none --format "{p}" > tree.txt
    cargo --version > tool.txt
  '
  python3 - "$work" "$case_dir/expected.json" <<'PY'
import datetime, json, re, sys
work, out = sys.argv[1], sys.argv[2]
lock = re.findall(r'name = "([^"]+)"\nversion = "([^"]+)"', open(f"{work}/Cargo.lock").read())
tree = {tuple(m.groups()) for line in open(f"{work}/tree.txt") if (m := re.match(r"(\S+) v(\S+)", line))}
def rows(pairs):
    return [{"name": n, "version": v} for n, v in sorted(set(pairs)) if n != "reference-case"]
target = open(f"{work}/target").read()
json.dump({"tool": open(f"{work}/tool.txt").read().split(" (")[0],
           "command": ["cargo", "tree", "--target", target, "-e", "normal,build"],
           "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "install": rows(tree), "lockfile": rows(lock)}, open(out, "w"), indent=2)
open(out, "a").write("\n")
print(len(rows(tree)), "packages on", target, "/", len(rows(lock)), "in Cargo.lock")
PY
  rm -rf "$work"
}

go_case() {
  local case_dir="$1" platform="$2" requires
  requires="$(python3 -c '
import sys, yaml
case = yaml.safe_load(open(sys.argv[1]))
for root in case["roots"]:
    path = root["purl"].split("/", 1)[1].rsplit("@", 1)[0]
    print("-require=" + path + "@" + root["constraint"])
' "$case_dir/case.yaml" | tr '\n' ' ')"
  docker run --rm --platform "${platform:-linux/amd64}" -v "$ROOT/${case_dir}:/case" -w /work -e GOFLAGS=-mod=mod \
    "$GO_IMAGE" sh -c "
    set -e
    go mod init reference.case >/dev/null 2>&1
    go mod edit -go=1.23 $requires
    go list -m all > /tmp/all.txt
    go version > /tmp/tool.txt
    tail -n +2 /tmp/all.txt > /case/.all.txt
    cp /tmp/tool.txt /case/.tool.txt
  "
  python3 - "$case_dir" <<'PY'
import datetime, json, sys
from pathlib import Path
case = Path(sys.argv[1])
rows = [line.split()[:2] for line in (case / ".all.txt").read_text().splitlines() if line.strip()]
tool = " ".join((case / ".tool.txt").read_text().split()[:3])
out = {"tool": tool, "command": ["go", "list", "-m", "all"],
       "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
       "install": [{"name": name, "version": version} for name, version in sorted(rows)]}
(case / "expected.json").write_text(json.dumps(out, indent=2) + "\n")
(case / ".all.txt").unlink(); (case / ".tool.txt").unlink()
print(len(rows), "modules")
PY
}

nuget_case() {
  local case_dir="$1" platform="$2" work
  work="$(mktemp -d)"
  python3 - "$case_dir/case.yaml" "$work" <<'PY'
import sys, yaml
case = yaml.safe_load(open(sys.argv[1]))
tfm = (case.get("environment") or {}).get("target_framework") or "net8.0"
refs = "".join(
    '    <PackageReference Include="{}" Version="{}" />\n'.format(r["purl"].split("/", 1)[1].split("@")[0], r["constraint"])
    for r in case["roots"])
open(f"{sys.argv[2]}/case.csproj", "w").write(
    f'<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup><TargetFramework>{tfm}</TargetFramework></PropertyGroup>\n'
    f'  <ItemGroup>\n{refs}  </ItemGroup>\n</Project>\n')
PY
  docker run --rm --platform "${platform:-linux/amd64}" -v "$work:/w" -w /w -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    -e DOTNET_NOLOGO=1 "$DOTNET_IMAGE" sh -c 'dotnet restore -v q >/dev/null; dotnet --version > tool.txt'
  python3 - "$work" "$case_dir/expected.json" <<'PY'
import datetime, json, sys
work, out = sys.argv[1], sys.argv[2]
assets = json.load(open(f"{work}/obj/project.assets.json"))
rows = sorted(({"name": key.split("/")[0], "version": key.split("/")[1]}
               for key, lib in assets["libraries"].items() if lib.get("type") == "package"),
              key=lambda r: r["name"].lower())
# A restore that fails (NU1605, NU1107) still writes the assets file; its error codes are part of the answer.
errors = sorted({log["code"] for log in assets.get("logs") or [] if log.get("level") == "Error"})
json.dump({"tool": "dotnet " + open(f"{work}/tool.txt").read().strip(), "command": ["dotnet", "restore"],
           "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "install": rows, **({"errors": errors} if errors else {})}, open(out, "w"), indent=2)
open(out, "a").write("\n")
print(len(rows), "packages", errors or "")
PY
  rm -rf "$work"
}

maven_case() {
  local case_dir="$1" platform="$2" work
  work="$(mktemp -d)"
  python3 - "$case_dir/case.yaml" "$work" <<'PY'
import sys, yaml
case = yaml.safe_load(open(sys.argv[1]))
def coordinates(item):
    coords, _, version = item["purl"].split("/", 1)[1].partition("@")
    group, artifact = coords.split("/")
    version, _, qualifiers = version.partition("?")
    constraint = item.get("constraint") or version
    return group, artifact, "" if constraint == "*" else constraint, "type=pom" in qualifiers

deps = managed = ""
for r in case["roots"]:
    group, artifact, version, _ = coordinates(r)
    version = f"<version>{version}</version>" if version else ""   # versionless: from dependencyManagement
    deps += f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>{version}</dependency>"
for m in case.get("managed") or []:
    group, artifact, version, bom = coordinates(m)
    extra = "<type>pom</type><scope>import</scope>" if bom else ""
    managed += f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>{extra}</dependency>"
open(f"{sys.argv[2]}/pom.xml", "w").write(
    '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
    '<groupId>reference</groupId><artifactId>case</artifactId><version>0</version>'
    f'<dependencyManagement><dependencies>{managed}</dependencies></dependencyManagement>'
    f'<dependencies>{deps}</dependencies></project>')
PY
  docker run --rm --platform "${platform:-linux/amd64}" -v "$work:/w" -w /w "$MAVEN_IMAGE" sh -c \
    'mvn -q -B dependency:list -DincludeScope=runtime -DoutputFile=deps.txt -DoutputAbsoluteArtifactFilename=false >/dev/null && mvn -v | head -1 > tool.txt'
  python3 - "$work" "$case_dir/expected.json" <<'PY'
import datetime, json, re, sys
work, out = sys.argv[1], sys.argv[2]
rows = []
for line in open(f"{work}/deps.txt"):
    parts = line.strip().split(":")
    if len(parts) >= 5 and not line.startswith("The following"):
        group, artifact, version = parts[0], parts[1], parts[-2] if len(parts) == 5 else parts[-2]
        rows.append({"name": f"{group}:{artifact}", "version": parts[3] if len(parts) == 5 else parts[4]})
rows = sorted({(r["name"], r["version"]) for r in rows})
tool = re.sub(r"\s*\(.*", "", open(f"{work}/tool.txt").read().strip())
json.dump({"tool": tool, "command": ["mvn", "dependency:list", "-DincludeScope=runtime"],
           "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "install": [{"name": n, "version": v} for n, v in rows]}, open(out, "w"), indent=2)
open(out, "a").write("\n")
print(len(rows), "artifacts")
PY
  rm -rf "$work"
}

gradle_case() {
  local case_dir="$1" platform="$2" work
  work="$(mktemp -d)"
  python3 - "$case_dir/case.yaml" "$work" <<'PY'
import sys, yaml
case = yaml.safe_load(open(sys.argv[1]))
deps = ""
for r in case["roots"]:
    coords, _, version = r["purl"].split("/", 1)[1].partition("@")
    group, artifact = coords.split("/")
    deps += f"    implementation '{group}:{artifact}:{r['constraint'] or version}'\n"
open(f"{sys.argv[2]}/settings.gradle", "w").write("rootProject.name = 'reference'\n")
open(f"{sys.argv[2]}/build.gradle", "w").write(f"""plugins {{ id 'java' }}
repositories {{ mavenCentral {{ metadataSources {{ mavenPom(); artifact(); ignoreGradleMetadataRedirection() }} }} }}
dependencies {{
{deps}}}
tasks.register('referenceList') {{
    def result = configurations.runtimeClasspath.incoming.resolutionResult.rootComponent
    doLast {{
        def seen = [] as Set
        def walk
        walk = {{ component ->
            if (!seen.add(component.id)) return
            if (component.id instanceof org.gradle.api.artifacts.component.ModuleComponentIdentifier) {{
                println "REF " + component.moduleVersion.group + ":" + component.moduleVersion.name + ":" + component.moduleVersion.version
            }}
            component.dependencies.findAll {{ it instanceof org.gradle.api.artifacts.result.ResolvedDependencyResult }}
                .each {{ walk(it.selected) }}
        }}
        walk(result.get())
    }}
}}
""")
PY
  docker run --rm --platform "${platform:-linux/amd64}" -v "$work:/w" -w /w "$GRADLE_IMAGE" sh -c \
    'gradle -q --no-daemon referenceList > deps.txt && gradle --version | sed -n "s/^Gradle /Gradle /p" > tool.txt'
  python3 - "$work" "$case_dir/expected.json" <<'PY'
import datetime, json, sys
work, out = sys.argv[1], sys.argv[2]
rows = sorted({tuple(line.split()[1].rsplit(":", 1)) for line in open(f"{work}/deps.txt") if line.startswith("REF ")})
json.dump({"tool": open(f"{work}/tool.txt").read().strip(),
           "command": ["gradle", "dependencies", "--configuration", "runtimeClasspath"],
           "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "install": [{"name": n, "version": v} for n, v in rows]}, open(out, "w"), indent=2)
open(out, "a").write("\n")
print(len(rows), "artifacts")
PY
  rm -rf "$work"
}

rpm_case() {
  local case_dir="$1" platform="$2" image="$3" work
  work="$(mktemp -d)"
  cp "$case_dir/case.yaml" "$work/case.yaml"
  cat > "$work/resolve.py" <<'PY'
import json, sys
import dnf, dnf.module.module_base, yaml
case = yaml.safe_load(open("/w/case.yaml"))
env = case["environment"]
arch, distro = env.get("arch", "x86_64"), env["distro"]
major = distro.rsplit("-", 1)[1].split(".")[0]
base = dnf.Base()
base.conf.installroot = "/tmp/root"
base.conf.install_weak_deps = env.get("weak_deps", True)
base.conf.module_platform_id = "platform:el%s" % major
base.conf.substitutions["releasever"], base.conf.substitutions["basearch"] = major, arch
for source in case["sources"]:
    url = source["base_url"].replace("$basearch", arch).replace("$releasever", major)
    base.repos.add_new_repo(source["slug"], base.conf, baseurl=[url], gpgcheck=False)
base.fill_sack(load_system_repo=False)
streams = ["%s:%s" % item for item in (env.get("module_streams") or {}).items()]
if streams:
    dnf.module.module_base.ModuleBase(base).enable(streams)
for root in case["roots"]:
    name = root["purl"].split("/")[2].split("?")[0].split("@")[0]
    constraint = root.get("constraint") or "*"
    base.install(name if constraint == "*" else "%s %s" % (name, constraint))
base.resolve(allow_erasing=False)
rows = sorted({("%s/%s" % (distro, p.name), p.evr) for p in base.transaction.install_set})
json.dump({"tool": "dnf " + dnf.const.VERSION, "rows": rows}, open("/w/out.json", "w"))
PY
  docker run --rm --platform "${platform:-linux/amd64}" -v "$work:/w" "$image" sh -c \
    'dnf -y -q install python3-pyyaml >/dev/null 2>&1; /usr/libexec/platform-python /w/resolve.py'
  python3 - "$work/out.json" "$case_dir/expected.json" <<'PY'
import datetime, json, sys
out = json.load(open(sys.argv[1]))
json.dump({"tool": out["tool"], "command": ["dnf", "install", "--assumeno", "--installroot"],
           "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "install": [{"name": n, "version": v} for n, v in out["rows"]]}, open(sys.argv[2], "w"), indent=2)
open(sys.argv[2], "a").write("\n")
print(len(out["rows"]), "packages")
PY
  rm -rf "$work"
}

for case_dir in "${CASES[@]}"; do
  echo "== ${case_dir}"
  if [[ "$(basename "$(dirname "$case_dir")")" == "rpm" ]]; then
    rpm_case "$case_dir" "$(sed -n 's/^native: {platform: *\([^,]*\),.*/\1/p' "${case_dir}/case.yaml")" \
      "$(sed -n 's/.*image: *"\([^"]*\)".*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "maven" ]] && grep -q "strategy: gradle" "${case_dir}/case.yaml"; then
    gradle_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "maven" ]]; then
    maven_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "nuget" ]]; then
    nuget_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "go" ]]; then
    go_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "crates" ]]; then
    crates_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  if [[ "$(basename "$(dirname "$case_dir")")" == "npm" ]]; then
    npm_case "$case_dir" "$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
    continue
  fi
  platform="$(sed -n 's/^  platform: *\([^ #]*\).*/\1/p' "${case_dir}/case.yaml")"
  docker run --rm --platform "${platform:-linux/amd64}" -v "$ROOT/${case_dir}:/case" "$IMAGE" sh -c "
    set -e
    pip install -q 'pip==${PIP_VERSION}' pyyaml >/dev/null 2>&1
    python - <<'PY'
import json, subprocess, sys, datetime
import yaml

case = yaml.safe_load(open('/case/case.yaml'))
native = case['native']
requirements = []
for root in case['roots']:
    name = root['purl'].split('/', 1)[1].split('@')[0]
    extras = root.get('extras') or []
    spec = name + ('[' + ','.join(extras) + ']' if extras else '')
    constraint = root.get('constraint') or ''
    requirements.append(spec + ('' if constraint in ('', '*') else constraint))
command = ['pip', 'install', '--dry-run', '--quiet', '--ignore-installed', '--report', '/tmp/report.json',
           *native.get('args', []), *requirements]
subprocess.run(command, check=True)
report = json.load(open('/tmp/report.json'))
install = sorted(({'name': item['metadata']['name'], 'version': item['metadata']['version']}
                  for item in report['install']), key=lambda i: i['name'].lower())
out = {'tool': 'pip ' + subprocess.run(['pip', '--version'], capture_output=True, text=True).stdout.split()[1],
       'command': command, 'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
       'install': install}
json.dump(out, open('/case/expected.json', 'w'), indent=2)
open('/case/expected.json', 'a').write('\n')
print(len(install), 'packages')
PY
  "
done
