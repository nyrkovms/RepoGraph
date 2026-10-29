#!/usr/bin/env bash
# Expected answers of the resolver reference cases (SPEC §14.2) from the native tool, never by hand.
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
  while IFS= read -r dir; do CASES+=("$dir"); done < <(find backend/tests/reference/pypi -mindepth 1 -maxdepth 1 -type d | sort)
fi

for case_dir in "${CASES[@]}"; do
  echo "== ${case_dir}"
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
