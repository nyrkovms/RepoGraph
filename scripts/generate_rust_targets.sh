#!/usr/bin/env bash
# The cfg set of each Rust target the crates environment accepts (`rustc --print cfg --target <t>`, the set Cargo
# evaluates `[target.'cfg(...)']` against), written to backend/app/ecosystems/crates/targets.json. Needs docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/backend/app/ecosystems/crates/targets.json"
RUST_IMAGE="rust:1.86-slim"
TARGETS="x86_64-unknown-linux-gnu x86_64-unknown-linux-musl aarch64-unknown-linux-gnu aarch64-unknown-linux-musl
i686-unknown-linux-gnu armv7-unknown-linux-gnueabihf riscv64gc-unknown-linux-gnu powerpc64le-unknown-linux-gnu
s390x-unknown-linux-gnu x86_64-pc-windows-msvc x86_64-pc-windows-gnu i686-pc-windows-msvc aarch64-pc-windows-msvc
x86_64-apple-darwin aarch64-apple-darwin aarch64-apple-ios aarch64-linux-android x86_64-unknown-freebsd
wasm32-unknown-unknown wasm32-wasip1 wasm32-wasip2"

docker run --rm --platform linux/amd64 "$RUST_IMAGE" sh -c "
  echo \"{\\\"tool\\\": \\\"\$(rustc --version)\\\", \\\"targets\\\": {\"
  first=1
  for t in $(echo $TARGETS); do
    [ \$first = 1 ] || echo ','
    first=0
    printf '  \"%s\": [' \"\$t\"
    rustc --print cfg --target \"\$t\" | sed 's/\"/\\\\\"/g; s/^/\"/; s/\$/\"/' | paste -sd, - | tr -d '\n'
    printf ']'
  done
  echo
  echo '}}'
" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d, indent=1, sort_keys=False))' > "$OUT"
python3 -c "import json; d=json.load(open('$OUT')); print(d['tool'], len(d['targets']), 'targets')"
