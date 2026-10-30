#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000/api/v1}"
ADMIN_TOKEN="${ADMIN_TOKEN:-phub_admin_dev}"

ADMIN_HEADER="Authorization: Bearer ${ADMIN_TOKEN}"
TOTAL=19

echo "[1/${TOTAL}] health"
curl -fsS "${BASE_URL}/health" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="ok"; print(d)'

echo "[2/${TOTAL}] stats"
curl -fsS "${BASE_URL}/stats" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["ecosystem"]=="pypi" and d["packages"]>=1
assert isinstance(d["ecosystems"],list)
pypi=next(e for e in d["ecosystems"] if e["ecosystem"]=="pypi")
assert isinstance(pypi["registries"],list) and len(pypi["registries"])>=1
print({k:d[k] for k in ("packages","versions","dependencies")}, {"registries":[r["slug"] for r in pypi["registries"]]})'

echo "[3/${TOTAL}] packages list"
curl -fsS "${BASE_URL}/packages?limit=5&sort=name" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["data"],list) and len(d["data"])>0; print({"count":len(d["data"]),"has_more":d["has_more"]})'

echo "[4/${TOTAL}] packages list, ecosystem=pypi"
curl -fsS "${BASE_URL}/packages?ecosystem=pypi&limit=5&sort=name" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert len(d["data"])>0
assert all(p["purl"].startswith("pkg:pypi/") and p["origin"] for p in d["data"])
print([p["purl"] for p in d["data"][:3]])'

echo "[5/${TOTAL}] search"
curl -fsS "${BASE_URL}/search?q=flask&limit=5" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert any("flask" in p["normalized_name"] for p in d["data"]); print([p["normalized_name"] for p in d["data"][:3]])'

echo "[6/${TOTAL}] package detail by coordinates"
curl -fsS "${BASE_URL}/packages/pypi/flask/-/" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["normalized_name"]=="flask" and d["purl"]=="pkg:pypi/flask"
assert len(d["sources"])>0
print({"purl":d["purl"],"latest_version":d["latest_version"],"sources":[s["registry"] for s in d["sources"]]})'

echo "[7/${TOTAL}] package detail, deprecated alias"
curl -fsS "${BASE_URL}/packages/pypi/flask" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["normalized_name"]=="flask" and d["purl"]=="pkg:pypi/flask"; print({"latest_version":d["latest_version"]})'

VER="$(curl -fsS "${BASE_URL}/packages/pypi/flask/versions?limit=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["version"])')"
echo "[8/${TOTAL}] version detail flask@${VER}, deprecated alias"
curl -fsS "${BASE_URL}/packages/pypi/flask/versions/${VER}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["version"]; print({"requires_python":d["requires_python"]})'

echo "[9/${TOTAL}] version detail by coordinates"
curl -fsS "${BASE_URL}/packages/pypi/flask/-/versions/${VER}" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["requires_python"]==(d["engines"] or {}).get("python")
assert len(d["sources"])>0
print({"purl":d["purl"],"requires_python":d["requires_python"],"sources":[s["registry"] for s in d["sources"]]})'

echo "[10/${TOTAL}] dependency tree"
curl -fsS "${BASE_URL}/packages/pypi/flask/versions/${VER}/dependencies?depth=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["package"]=="flask"; print({"deps":len(d["dependencies"])})'

echo "[11/${TOTAL}] artifacts"
curl -fsS "${BASE_URL}/packages/pypi/flask/versions/${VER}/artifacts" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["data"],list); print({"artifacts":len(d["data"])})'

echo "[12/${TOTAL}] vulnerabilities"
curl -fsS "${BASE_URL}/packages/pypi/flask/vulnerabilities" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["package"]=="flask"; print({"vulns":len(d["data"])})'

echo "[13/${TOTAL}] purl redirect"
REDIRECT="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${BASE_URL}/purl/pkg:pypi/flask")"
case "${REDIRECT}" in
  "307 "*"/packages/pypi/flask/-/") echo "${REDIRECT}" ;;
  *) echo "expected 307 to .../packages/pypi/flask/-/, got: ${REDIRECT}"; exit 1 ;;
esac

echo "[14/${TOTAL}] resolve, graph format"
curl -fsS -H "Content-Type: application/json" \
  -d '{"roots":[{"purl":"pkg:pypi/flask","constraint":">=3.0"}]}' \
  "${BASE_URL}/resolve" \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["error"] is None
assert any(n["name"]=="flask" and n["root"] for n in d["nodes"])
assert d["emulates"]
print({"nodes":len(d["nodes"]),"emulates":d["emulates"],"time_ms":d["resolve_time_ms"]})'

echo "[15/${TOTAL}] resolve, nested format (alias)"
curl -fsS -H "Content-Type: application/json" \
  -d '{"ecosystem":"pypi","dependencies":[{"name":"flask","version_spec":">=3.0"}],"environment":{"python_version":"3.12","sys_platform":"linux","os_name":"posix","platform_machine":"x86_64","platform_system":"Linux","implementation_name":"cpython"},"options":{"include_dev":false,"max_depth":30,"max_rounds":200}}' \
  "${BASE_URL}/resolve" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["resolved"]; print({"pkgs":d["total_packages"],"time_ms":d["resolve_time_ms"]})'

echo "[16/${TOTAL}] admin auth"
CODE="$(curl -s -o /tmp/packagehub_rbac.json -w '%{http_code}' "${BASE_URL}/admin/sync/status")"
if [[ "${CODE}" != "401" ]]; then
  echo "expected 401 without token on /admin/sync/status, got ${CODE}"
  cat /tmp/packagehub_rbac.json
  exit 1
fi
echo "anonymous rejected: ok"

echo "[17/${TOTAL}] sync status"
curl -fsS -H "${ADMIN_HEADER}" "${BASE_URL}/admin/sync/status" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert "status" in d and "sync_type" in d
assert isinstance(d["registries"],list) and isinstance(d["recent_runs"],list)
print({"status":d["status"],"sync_type":d["sync_type"],"registries":len(d["registries"]),"recent_runs":len(d["recent_runs"])})'

echo "[18/${TOTAL}] updates summary"
curl -fsS "${BASE_URL}/updates/summary" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["last_runs"],list); print({"last_runs":len(d["last_runs"]),"packages":d["totals"]["packages"]})'

echo "[19/${TOTAL}] metrics"
METRICS="$(curl -fsS "${BASE_URL}/metrics")"
# A here-string, not `echo | grep -q`: grep -q exits at the first match and, under pipefail, a still-writing echo
# would fail the step with a broken pipe once the metrics outgrow the pipe buffer.
grep -q "packagehub_api_latency_seconds" <<< "${METRICS}"
grep -q "packagehub_resolve_latency_seconds" <<< "${METRICS}"
grep -q "packagehub_sync_duration_seconds" <<< "${METRICS}"
echo "metrics keys present"

echo "ACCEPTANCE_SMOKE: PASS"
