#!/usr/bin/env bash
# Expected answers of rpm itself for the RPM version scheme tests, from rockylinux:9 (rpm 4.16, python3-rpm):
# rpm.labelCompare for every pair of the version inputs, and for every constraint input the answer of rpmdsCompare
# (rpm.ds.Compare of a requirement with flags and EVR against a provide of the candidate EVR) for every candidate, plus the
# highest satisfying candidate of several pools. Needs docker and the network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/rpm/versions"
IMAGE="rockylinux:9"

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

cat > "$SRC/vectors.py" <<'PY'
import functools
import json
import re
import subprocess

import rpm

FLAGS = {
    "=": "EQ", "==": "EQ", "EQ": "EQ", "<": "LT", "LT": "LT", "<=": "LE", "=<": "LE", "LE": "LE",
    ">": "GT", "GT": "GT", ">=": "GE", "=>": "GE", "GE": "GE",
}
SENSE = {
    "EQ": rpm.RPMSENSE_EQUAL, "LT": rpm.RPMSENSE_LESS, "LE": rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL,
    "GT": rpm.RPMSENSE_GREATER, "GE": rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL,
}


def split(evr):
    """rpmEVRparse: the epoch is a run of digits before `:`, the release is what follows the last `-`."""
    epoch = None
    m = re.match(r"(\d+):", evr)
    if m:
        epoch, evr = m[1], evr[m.end():]
    version, dash, release = evr.rpartition("-")
    if not dash:
        version, release = evr, None
    return epoch, version, release


def label(evr):
    return split(evr)


def compare(a, b):
    return rpm.labelCompare(label(a), label(b))


def sign(n):
    return "<" if n < 0 else ">" if n > 0 else "="


def parse_constraint(raw):
    raw = raw.strip()
    if raw in ("", "*"):
        return None, None
    m = re.fullmatch(r"(<=|>=|==|=<|=>|=|<|>)\s*(\S+)|(EQ|LT|LE|GT|GE)\s+(\S+)", raw)
    assert m, raw
    return FLAGS[m[1] or m[3]], m[2] or m[4]


inputs = json.load(open("/work/inputs.json"))
versions = inputs["versions"]
candidates = inputs["candidates"]

provides = {c: rpm.ds(("foo", rpm.RPMSENSE_EQUAL, c), "provides") for c in candidates}
for pool in inputs["pools"]:
    for c in pool:
        provides.setdefault(c, rpm.ds(("foo", rpm.RPMSENSE_EQUAL, c), "provides"))


def satisfies(flag, evr, candidate):
    requirement = rpm.ds(("foo", 0 if flag is None else SENSE[flag], evr or ""), "requires")
    return bool(requirement.Compare(provides[candidate]))


constraints = {}
for raw in inputs["constraints"]:
    flag, evr = parse_constraint(raw)
    entry = {"flag": flag, "evr": evr, "contains": {c: satisfies(flag, evr, c) for c in candidates}, "best_match": []}
    for pool in inputs["pools"]:
        best = None
        for i, c in enumerate(pool):
            if satisfies(flag, evr, c) and (best is None or compare(c, pool[best]) > 0):
                best = i
        entry["best_match"].append(best)
    constraints[raw] = entry

out = {
    "tool": "rpm " + rpm.__version__ + " (python3-rpm, rockylinux:9)",
    "rpm_version": rpm.__version__,
    "compare_versions": versions,
    "compare": ["".join(sign(compare(a, b)) for b in versions) for a in versions],
    "constraints": constraints,
}
json.dump(out, open("/work/expected.json", "w"), indent=1, ensure_ascii=False)
print(f"rpm {rpm.__version__}: {len(versions)} versions, {len(constraints)} constraints x {len(candidates)} candidates")
PY

docker run --rm --platform linux/amd64 -v "$DIR:/work" -v "$SRC:/src:ro" -w /tmp "$IMAGE" sh -c '
  set -e
  dnf -y -q install python3-rpm >/dev/null
  python3 /src/vectors.py
'
