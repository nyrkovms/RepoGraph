#!/usr/bin/env bash
# Expected answers of node-semver (as npm 10 bundles it) for the npm version scheme tests: comparison of every pair of
# versions, validity, and satisfies(version, range) for every range. Needs docker and the network (npm install).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/npm/semver"
SEMVER_VERSION="7.6.3"

docker run --rm --platform linux/amd64 -v "$DIR:/work" -w /tmp node:20-slim sh -c "
  npm install --silent --no-audit --no-fund semver@${SEMVER_VERSION} >/dev/null 2>&1
  node -e '
    const semver = require(\"semver\");
    const input = require(\"/work/inputs.json\");
    const valid = input.versions.filter(v => semver.valid(v) !== null);
    const out = {
      tool: \"node-semver \" + require(\"semver/package.json\").version,
      valid: Object.fromEntries([...input.versions, ...input.invalid_versions].map(v => [v, semver.valid(v) !== null])),
      compare: valid.map(a => valid.map(b => semver.compare(a, b))),
      compare_versions: valid,
      ranges: Object.fromEntries(input.ranges.map(r => [r, semver.validRange(r) === null ? null
        : Object.fromEntries(valid.map(v => [v, semver.satisfies(v, r)]))])),
    };
    require(\"fs\").writeFileSync(\"/work/expected.json\", JSON.stringify(out, null, 1) + \"\\n\");
    console.log(out.tool, valid.length, \"versions\", input.ranges.length, \"ranges\");
  '
"
