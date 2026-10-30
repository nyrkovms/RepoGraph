#!/usr/bin/env bash
# Expected answers of the `semver` crate (the one Cargo uses) for the crates version scheme tests: precedence of every
# pair of versions, validity, and VersionReq::matches(version) for every requirement. Needs docker and the network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/crates/semver"
SEMVER_VERSION="1.0.26"
RUST_IMAGE="rust:1.86-slim"

docker run --rm --platform linux/amd64 -v "$DIR:/work" -w /tmp "$RUST_IMAGE" sh -c "
  cargo new -q --bin vectors && cd vectors
  printf 'semver = \"=${SEMVER_VERSION}\"\nserde_json = { version = \"1\", features = [\"preserve_order\"] }\n' >> Cargo.toml
  cat > src/main.rs <<'RS'
use semver::{BuildMetadata, Version, VersionReq};
use serde_json::{json, Map, Value};

fn main() {
    let input: Value = serde_json::from_str(&std::fs::read_to_string(\"/work/inputs.json\").unwrap()).unwrap();
    let strings = |key: &str| -> Vec<String> {
        input[key].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect()
    };
    let (versions, invalid, ranges) = (strings(\"versions\"), strings(\"invalid_versions\"), strings(\"ranges\"));
    let mut valid_map = Map::new();
    for v in versions.iter().chain(invalid.iter()) {
        valid_map.insert(v.clone(), json!(Version::parse(v).is_ok()));
    }
    let valid: Vec<(String, Version)> =
        versions.iter().filter_map(|v| Version::parse(v).ok().map(|p| (v.clone(), p))).collect();
    // Precedence: build metadata does not order versions (crates.io refuses versions that differ only in it).
    let bare = |v: &Version| { let mut v = v.clone(); v.build = BuildMetadata::EMPTY; v };
    let compare: Vec<Value> = valid.iter().map(|(_, a)| {
        json!(valid.iter().map(|(_, b)| bare(a).cmp(&bare(b)) as i8).collect::<Vec<_>>())
    }).collect();
    let mut range_map = Map::new();
    for r in &ranges {
        let value = match VersionReq::parse(r) {
            Err(_) => Value::Null,
            Ok(req) => {
                let mut m = Map::new();
                for (raw, v) in &valid { m.insert(raw.clone(), json!(req.matches(v))); }
                Value::Object(m)
            }
        };
        range_map.insert(r.clone(), value);
    }
    let out = json!({
        \"tool\": \"semver ${SEMVER_VERSION}\",
        \"valid\": valid_map,
        \"compare\": compare,
        \"compare_versions\": valid.iter().map(|(r, _)| r.clone()).collect::<Vec<_>>(),
        \"ranges\": range_map,
    });
    std::fs::write(\"/work/expected.json\", serde_json::to_string_pretty(&out).unwrap() + \"\n\").unwrap();
    println!(\"semver ${SEMVER_VERSION}: {} versions, {} ranges\", valid.len(), ranges.len());
}
RS
  cargo run -q --release
"
