#!/usr/bin/env bash
# Expected answers of the resolver reference cases (SPEC §14.2) from the native tool, never by hand.
# npm: `npm install --package-lock-only` of the npm bundled with node:20, reduced by scripts/reference_npm.js to what
# npm installs on the case's platform.
# PyPI: `pip install --dry-run --report` of the pinned pip version, run natively in a python image of the case's
# platform (`native.platform`, a docker platform): pip's --platform/--python-version flags change the wheel tags but
# not the environment markers, and narrow manylinux tags hide older-tagged wheels (nvidia-* are manylinux2014).
# Needs docker and the live network. Usage: scripts/generate_resolver_references.sh [case-dir ...]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PIP_VERSION="24.3.1"
IMAGE="python:3.12-slim"
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

for case_dir in "${CASES[@]}"; do
  echo "== ${case_dir}"
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
