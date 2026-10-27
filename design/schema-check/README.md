# Schema check

Checks for [schema-target.sql](../schema-target.sql) and the `sort_key` prototypes (SPEC-MULTI-ECOSYSTEM.md §5.4–5.5).
Needs the test stand to be running (`scripts/stand.sh up`).

Schema scenarios. The scratch database is created and dropped afterwards:

```bash
cd backend && uv run --python 3.12 --with packaging -- python ../design/schema-check/gen_scenarios.py ../design/schema-check > /tmp/scenarios.sql
docker exec -i packagehub-stand-postgres-1 psql -U packagehub -d packagehub -c "CREATE DATABASE schema_check"
docker exec -i packagehub-stand-postgres-1 psql -U packagehub -d schema_check -v ON_ERROR_STOP=1 -q < ../design/schema-target.sql
docker exec -i packagehub-stand-postgres-1 psql -U packagehub -d schema_check -v ON_ERROR_STOP=1 -q < /tmp/scenarios.sql
docker exec -i packagehub-stand-postgres-1 psql -U packagehub -d packagehub -c "DROP DATABASE schema_check"
```

Every check prints `NOTICE: ok ...`; a failed check aborts with `CHECK FAILED` or `NEGATIVE CHECK PASSED UNEXPECTEDLY`.

`sort_key` against reference implementations:

- SemVer: `node cmp_semver.js <path-to>/node_modules/semver semver_versions.txt` gives the reference matrix; compare it with `semver_key`.
- RPM: `rpm.labelCompare` in a `rockylinux:9` container (`dnf install python3-rpm`) over `rpm_versions.txt`.
- PEP 440: `packaging.version.Version` against `pep440_key`.
