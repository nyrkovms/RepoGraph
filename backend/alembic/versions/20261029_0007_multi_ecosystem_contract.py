"""multi-ecosystem contract: drop what the code no longer reads, NOT NULL and VALIDATE (IMPL-MULTI-ECOSYSTEM.md §2)

Irreversible: columns and update_runs are dropped. Take a backup first (`scripts/stand.sh migrate` does, and stops the
worker and beat).

Two phases:
1. Autocommit, resumable, nothing that blocks writers for long:
   - backfill() of 0006 again (loaded by path), for rows the pre-E0-B code wrote after 0006;
   - re-sync of values that code changed only in the old columns (copied where old and new differ);
   - sync_failures keyed by (operation, ecosystem, registry_id, package_ref), package_events.run_id against runs;
   - NOT NULL prepared as validated CHECK constraints and FKs validated (SHARE UPDATE EXCLUSIVE, writers continue),
     the new sync_failures key built CONCURRENTLY.
2. One short transaction under lock_timeout: SET NOT NULL (no scan thanks to the checks), drops, dist_tags key.

If phase 1 fails, rerun the upgrade: every step only touches what is not done yet. Downgrade is not possible.

Revision ID: 20261029_0007
Revises: 20261028_0006
Create Date: 2026-10-29
"""

import importlib.util
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261029_0007"
down_revision: Union[str, Sequence[str], None] = "20261028_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_TIMEOUT = "5s"
BATCH = 5000

# (table, column) made NOT NULL; the large tables go through a validated CHECK first.
_NOT_NULL_CHECKED = (
    ("packages", "namespace"),
    ("package_registries", "ecosystem"),
    ("package_versions", "sort_key"),
    ("package_versions", "is_prerelease"),
    ("dependencies", "dependency_namespace"),
)
_NOT_NULL_SMALL = (("registries", "protocol"), ("sync_failures", "ecosystem"), ("sync_failures", "package_ref"))
_VALIDATE = (
    ("package_registries", "fk_pkgreg_package_ecosystem"),
    ("package_registries", "fk_pkgreg_registry_ecosystem"),
    ("package_versions", "package_versions_metadata_registry_id_fkey"),
    ("dependencies", "dependencies_variant_registry_id_fkey"),
)
_PURL_TYPES = {"pypi": "pypi", "npm": "npm", "maven": "maven", "nuget": "nuget", "golang": "go", "cargo": "crates",
               "rpm": "rpm"}


def _load_0006():
    path = Path(__file__).with_name("20261028_0006_multi_ecosystem_expand.py")
    spec = importlib.util.spec_from_file_location("migration_0006", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column_exists(conn: sa.Connection, table: str, column: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).scalar())


def _constraint(conn: sa.Connection, name: str) -> tuple[bool, bool]:
    """(exists, validated)."""
    row = conn.execute(sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"), {"n": name}).first()
    return (row is not None, bool(row and row[0]))


# --- phase 1 --------------------------------------------------------------------------------------------------


def _resync(conn: sa.Connection, batches) -> None:
    """IMPL §2: values the 0005-era code changed only in the old columns after 0006. The code that ships with 0007
    cannot run on the 0006 schema, and the E0-B code wrote the old and new columns together, so wherever they differ
    the old column is the newer one."""
    for ids in batches(conn, "package_versions", "dependencies_state = 'pending' AND (features ->> 'dependencies_hydrated')::boolean IS TRUE"):
        conn.execute(sa.text("UPDATE package_versions SET dependencies_state = 'hydrated' WHERE id = ANY(:ids)"), {"ids": ids})
    if _column_exists(conn, "package_versions", "requires_python"):
        for ids in batches(conn, "package_versions", "requires_python IS NOT NULL AND (engines ->> 'python') IS DISTINCT FROM requires_python"):
            conn.execute(sa.text("""
                UPDATE package_versions SET engines = coalesce(engines, '{}'::jsonb) || jsonb_build_object('python', requires_python)
                WHERE id = ANY(:ids)
            """), {"ids": ids})
    if _column_exists(conn, "packages", "last_serial"):
        for ids in batches(conn, "packages", "last_serial IS NOT NULL"):
            conn.execute(sa.text("""
                UPDATE package_registries pr SET upstream_revision = p.last_serial::text
                FROM packages p
                WHERE p.id = pr.package_id AND pr.registry_id = p.primary_registry_id AND p.id = ANY(:ids)
                  AND pr.upstream_revision IS DISTINCT FROM p.last_serial::text
            """), {"ids": ids})
    # A yank or removal by the 0005-era code changed package_versions.status only. Where the version has one source
    # (every catalog before E0-B), that source's status is the version's; visibility and resolve read version_sources.
    for ids in batches(conn, "package_versions", """
            (SELECT count(*) FROM version_sources vs WHERE vs.version_id = package_versions.id) = 1
            AND EXISTS (SELECT 1 FROM version_sources vs WHERE vs.version_id = package_versions.id
                        AND vs.status IS DISTINCT FROM package_versions.status)"""):
        conn.execute(sa.text("""
            UPDATE version_sources vs SET status = v.status FROM package_versions v
            WHERE v.id = vs.version_id AND v.id = ANY(:ids)
        """), {"ids": ids})

    if conn.execute(sa.text("SELECT to_regclass('update_runs')")).scalar():
        # A run still `running` at 0006 was copied unfinished; runs written by the current code stay as they are.
        conn.execute(sa.text("""
            INSERT INTO runs (id, operation, ecosystem, status, started_at, finished_at, packages_checked,
                              packages_changed, packages_updated, packages_failed, packages_unavailable,
                              events_created, per_registry, error)
            SELECT id, 'daily_update', 'pypi', status::text, started_at, finished_at, packages_checked, packages_changed,
                   packages_updated, packages_failed, packages_unavailable, events_created,
                   CASE WHEN index_serial IS NULL THEN '{}'::jsonb
                        ELSE jsonb_build_object('pypi', jsonb_build_object('index_serial', index_serial)) END,
                   error
            FROM update_runs
            ON CONFLICT (id) DO UPDATE SET
                status = excluded.status, finished_at = excluded.finished_at,
                packages_checked = excluded.packages_checked, packages_changed = excluded.packages_changed,
                packages_updated = excluded.packages_updated, packages_failed = excluded.packages_failed,
                packages_unavailable = excluded.packages_unavailable, events_created = excluded.events_created,
                error = excluded.error
            WHERE runs.status = 'running' AND excluded.status <> 'running'
        """))
    conn.execute(sa.text("""
        UPDATE package_events e SET run_id = NULL
        WHERE run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.id = e.run_id)
    """))


def _sync_failures(conn: sa.Connection) -> None:
    """New key (operation, ecosystem, registry_id, package_ref): fill the ecosystem from the purl, drop rows that
    cannot be keyed (failures are transient: the package is retried or failed again), keep the newest duplicate."""
    cases = " ".join(f"WHEN '{purl_type}' THEN '{ecosystem}'" for purl_type, ecosystem in _PURL_TYPES.items())
    conn.execute(sa.text(f"""
        UPDATE sync_failures SET ecosystem = (CASE split_part(substr(package_ref, 5), '/', 1) {cases} END)::ecosystem_enum
        WHERE ecosystem IS NULL AND package_ref LIKE 'pkg:%'
    """))
    conn.execute(sa.text("DELETE FROM sync_failures WHERE package_ref IS NULL OR ecosystem IS NULL"))
    conn.execute(sa.text("""
        DELETE FROM sync_failures f USING sync_failures g
        WHERE f.operation = g.operation AND f.ecosystem = g.ecosystem AND f.package_ref = g.package_ref
          AND f.registry_id IS NOT DISTINCT FROM g.registry_id AND (f.last_seen_at, f.id) < (g.last_seen_at, g.id)
    """))


def _preflight(conn: sa.Connection) -> None:
    missing = conn.execute(sa.text("SELECT slug FROM registries WHERE protocol IS NULL ORDER BY slug")).scalars().all()
    if missing:
        raise RuntimeError(
            f"registries without protocol: {', '.join(missing)}. Set registries.protocol (or delete the unused source) "
            "and run the upgrade again; no column was dropped and no row deleted."
        )


def _prepare_constraints(conn: sa.Connection) -> None:
    conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    for table, column in _NOT_NULL_CHECKED:
        name = f"ck_0007_{table}_{column}_not_null"
        exists, validated = _constraint(conn, name)
        nullable = conn.execute(sa.text(
            "SELECT is_nullable = 'YES' FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
        ), {"t": table, "c": column}).scalar()
        if not nullable:
            continue
        if not exists:
            conn.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IS NOT NULL) NOT VALID"))
        if not validated:
            conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))
    for table, name in _VALIDATE:
        exists, validated = _constraint(conn, name)
        if exists and not validated:
            conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))

    exists, validated = _constraint(conn, "package_events_run_id_fkey")
    if not exists:
        conn.execute(sa.text(
            "ALTER TABLE package_events ADD CONSTRAINT package_events_run_id_fkey "
            "FOREIGN KEY (run_id) REFERENCES runs (id) NOT VALID"
        ))
    if not validated:
        conn.execute(sa.text("ALTER TABLE package_events VALIDATE CONSTRAINT package_events_run_id_fkey"))

    invalid = conn.execute(sa.text("""
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'uq_sync_failures_key' AND NOT i.indisvalid
    """)).scalar()
    if invalid:
        conn.execute(sa.text("DROP INDEX CONCURRENTLY IF EXISTS uq_sync_failures_key"))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_sync_failures_key
        ON sync_failures (operation, ecosystem, registry_id, package_ref) NULLS NOT DISTINCT
    """))
    conn.execute(sa.text("RESET lock_timeout"))


# --- phase 2 --------------------------------------------------------------------------------------------------


def _contract() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for table, column in (*_NOT_NULL_CHECKED, *_NOT_NULL_SMALL):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
    for table, column in _NOT_NULL_CHECKED:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_0007_{table}_{column}_not_null")

    op.execute("""
        ALTER TABLE packages DROP COLUMN IF EXISTS deprecated, DROP COLUMN IF EXISTS deprecation_message,
            DROP COLUMN IF EXISTS deprecation_alternative, DROP COLUMN IF EXISTS last_serial
    """)
    op.execute("ALTER TABLE package_registries DROP COLUMN IF EXISTS is_primary")
    # idx_ver_needed goes with its column; idx_ver_pkg_ver duplicates uq_package_versions_pkg_version.
    op.execute("ALTER TABLE package_versions DROP COLUMN IF EXISTS requires_python, DROP COLUMN IF EXISTS needed_in_index")
    op.execute("DROP INDEX IF EXISTS idx_ver_pkg_ver")
    op.execute("""
        ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS uq_artifact_version_filename,
            DROP COLUMN IF EXISTS requires_dist_metadata
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'dist_tags' AND column_name = 'id') THEN
                ALTER TABLE dist_tags DROP CONSTRAINT dist_tags_pkey, DROP CONSTRAINT IF EXISTS uq_dist_tags_package_tag,
                    DROP COLUMN id, ADD CONSTRAINT dist_tags_pkey PRIMARY KEY (package_id, tag);
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE registries DROP COLUMN IF EXISTS tier, DROP COLUMN IF EXISTS registry_type,
            DROP COLUMN IF EXISTS api_url, DROP COLUMN IF EXISTS url_pattern, DROP COLUMN IF EXISTS auth_required,
            DROP COLUMN IF EXISTS auth_config, DROP COLUMN IF EXISTS sync_enabled, DROP COLUMN IF EXISTS sync_priority
    """)
    op.execute("""
        ALTER TABLE sync_states DROP COLUMN IF EXISTS sync_type, DROP COLUMN IF EXISTS packages_synced,
            DROP COLUMN IF EXISTS packages_total
    """)
    op.execute("""
        ALTER TABLE sync_failures DROP CONSTRAINT IF EXISTS uq_sync_failures_operation_package,
            DROP COLUMN IF EXISTS package_name
    """)
    op.execute("DROP TABLE IF EXISTS update_runs")
    for name in ("update_run_status_enum", "sync_type_enum", "registry_type_enum", "tier_enum"):
        op.execute(f"DROP TYPE IF EXISTS {name}")


def upgrade() -> None:
    conn = op.get_bind()
    migration_0006 = _load_0006()
    with op.get_context().autocommit_block():
        migration_0006.backfill(conn)
        _preflight(conn)
        _resync(conn, migration_0006._batches)
        _sync_failures(conn)
        _prepare_constraints(conn)
    _contract()


def downgrade() -> None:
    raise RuntimeError("0007 is irreversible (it drops columns and update_runs); restore the backup taken before it")
