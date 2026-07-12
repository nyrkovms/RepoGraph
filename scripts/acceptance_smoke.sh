#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000/api/v1}"
VIEWER_TOKEN="${VIEWER_TOKEN:-phub_viewer_dev}"
ADMIN_TOKEN="${ADMIN_TOKEN:-phub_admin_dev}"

VIEW_HEADER="Authorization: Bearer ${VIEWER_TOKEN}"
ADMIN_HEADER="Authorization: Bearer ${ADMIN_TOKEN}"

echo "[1/12] health"
curl -fsS "${BASE_URL}/health" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="ok"; print(d)'

echo "[2/12] stats(viewer)"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/stats" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ecosystem"]=="pypi" and d["packages"]>=1; print({k:d[k] for k in ("packages","versions","dependencies")})'

echo "[3/12] packages list"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages?limit=5&sort=name" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["data"],list) and len(d["data"])>0; print({"count":len(d["data"]),"has_more":d["has_more"]})'

echo "[4/12] search"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/search?q=flask&limit=5" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert any("flask" in p["normalized_name"] for p in d["data"]); print([p["normalized_name"] for p in d["data"][:3]])'

echo "[5/12] package detail"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["normalized_name"]=="flask"; print({"latest_version":d["latest_version"]})'

VER="$(curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask/versions?limit=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["version"])')"
echo "[6/12] version detail flask@${VER}"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask/versions/${VER}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["version"]; print({"requires_python":d["requires_python"]})'

echo "[7/12] dependency tree"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask/versions/${VER}/dependencies?depth=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["package"]=="flask"; print({"deps":len(d["dependencies"])})'

echo "[8/12] artifacts"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask/versions/${VER}/artifacts" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d["data"],list); print({"artifacts":len(d["data"])})'

echo "[9/12] vulnerabilities"
curl -fsS -H "${VIEW_HEADER}" "${BASE_URL}/packages/pypi/flask/vulnerabilities" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["package"]=="flask"; print({"vulns":len(d["data"])})'

echo "[10/12] resolve"
curl -fsS -H "${VIEW_HEADER}" -H "Content-Type: application/json" \
  -d '{"ecosystem":"pypi","dependencies":[{"name":"flask","version_spec":">=3.0"}],"environment":{"python_version":"3.12","sys_platform":"linux","os_name":"posix","platform_machine":"x86_64","platform_system":"Linux","implementation_name":"cpython"},"options":{"include_dev":false,"max_depth":30,"max_rounds":200}}' \
  "${BASE_URL}/resolve" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["resolved"]; print({"pkgs":d["total_packages"],"time_ms":d["resolve_time_ms"]})'

echo "[11/12] admin RBAC"
CODE="$(curl -s -o /tmp/packagehub_rbac.json -w '%{http_code}' -H "${VIEW_HEADER}" "${BASE_URL}/admin/sync/status")"
if [[ "${CODE}" != "403" ]]; then
  echo "expected 403 for viewer on /admin/sync/status, got ${CODE}"
  cat /tmp/packagehub_rbac.json
  exit 1
fi
echo "viewer forbidden: ok"
curl -fsS -H "${ADMIN_HEADER}" "${BASE_URL}/admin/sync/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "status" in d and "sync_type" in d; print({"status":d["status"],"sync_type":d["sync_type"]})'

echo "[12/12] metrics"
METRICS="$(curl -fsS "${BASE_URL}/metrics")"
echo "${METRICS}" | grep -q "packagehub_api_latency_seconds"
echo "${METRICS}" | grep -q "packagehub_resolve_latency_seconds"
echo "${METRICS}" | grep -q "packagehub_sync_duration_seconds"
echo "metrics keys present"

echo "ACCEPTANCE_SMOKE: PASS"
