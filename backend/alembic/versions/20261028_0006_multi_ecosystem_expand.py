"""multi-ecosystem expand: sources, profiles, version sources, sort keys, runs journal (IMPL-MULTI-ECOSYSTEM.md §2)

Expand-only: adds and copies, never drops. Columns that 0007 removes lose NOT NULL or get a DEFAULT so the new
code can write without them, while the old code keeps working on this schema.

The data part runs in committed batches and only touches rows that are not filled yet, so an interrupted run can
be resumed; the DDL part is written with IF NOT EXISTS for the same reason.

Revision ID: 20261028_0006
Revises: 20261027_0005
Create Date: 2026-10-28
"""

import json
import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from packaging.version import InvalidVersion, Version

# revision identifiers, used by Alembic.
revision: str = "20261028_0006"
down_revision: Union[str, Sequence[str], None] = "20261027_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 5000
# T12: packages from SEED_PACKAGES are `seed`, everything else `dependency`. Default copied from app.config at the time
# of writing; the migration must not import application code.
DEFAULT_SEED_PACKAGES = "flask,django,fastapi,requests,numpy,pandas"


# --- vendored PEP 440 sort key (design/schema-check/sortkey_proto.py, verified against packaging) ---------------


def _num(digits: str) -> bytes:
    digits = digits.lstrip("0") or "0"
    return bytes([len(digits)]) + digits.encode()


def _pep440_key(raw: str) -> tuple[bytes, bool] | None:
    try:
        pv = Version(raw)
    except InvalidVersion:
        return None
    release = list(pv.release)
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    out = _num(str(pv.epoch))
    for part in release:
        out += b"\x02" + _num(str(part))
    out += b"\x01"
    if pv.pre is None and pv.post is None and pv.dev is not None:
        out += b"\x00"
    elif pv.pre is None:
        out += b"\x02"
    else:
        out += b"\x01" + bytes([{"a": 1, "b": 2, "rc": 3}[pv.pre[0]]]) + _num(str(pv.pre[1]))
    out += b"\x00" if pv.post is None else b"\x01" + _num(str(pv.post))
    out += b"\x02" if pv.dev is None else b"\x01" + _num(str(pv.dev))
    if pv.local is None:
        out += b"\x00"
    else:
        out += b"\x01"
        for seg in pv.local.split("."):
            out += (b"\x02" + _num(seg)) if seg.isdigit() else (b"\x01" + seg.lower().encode() + b"\x00")
        out += b"\x00"
    return out, pv.is_prerelease or pv.is_devrelease


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- DDL ----------------------------------------------------------------------------------------------------------


def _create_type(name: str, values: list[str]) -> None:
    labels = ", ".join(f"'{v}'" for v in values)
    op.execute(f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({labels}); EXCEPTION WHEN duplicate_object THEN NULL; END $$")


def _add_constraint(table: str, name: str, definition: str) -> None:
    op.execute(
        f"DO $$ BEGIN ALTER TABLE {table} ADD CONSTRAINT {name} {definition}; "
        f"EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$"
    )


def _schema() -> None:
    # Enum values are added here and first used only by new code, as PostgreSQL requires within one transaction.
    op.execute("ALTER TYPE ecosystem_enum ADD VALUE IF NOT EXISTS 'rpm'")
    op.execute("ALTER TYPE package_version_status_enum ADD VALUE IF NOT EXISTS 'superseded'")
    op.execute("ALTER TYPE package_event_kind_enum ADD VALUE IF NOT EXISTS 'release_deprecated'")
    op.execute("ALTER TYPE package_event_kind_enum ADD VALUE IF NOT EXISTS 'source_added'")
    _create_type(
        "registry_protocol_enum",
        ["pypi-warehouse", "pypi-simple", "npm-registry", "maven2", "nuget-v3", "goproxy", "cargo-sparse", "rpm-repodata"],
    )
    _create_type("package_origin_enum", ["seed", "manual", "dependency", "discovered"])
    _create_type("dependencies_state_enum", ["pending", "hydrated", "unavailable"])

    # registries
    op.execute("""
        ALTER TABLE registries
            ADD COLUMN IF NOT EXISTS protocol registry_protocol_enum,
            ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS routing jsonb,
            ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100,
            ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true,
            ADD COLUMN IF NOT EXISTS auth_type varchar(32) NOT NULL DEFAULT 'none',
            ADD COLUMN IF NOT EXISTS credentials_ref text,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
            ALTER COLUMN tier SET DEFAULT 'tier1',
            ALTER COLUMN registry_type SET DEFAULT 'public'
    """)
    _add_constraint("registries", "uq_registries_id_ecosystem", "UNIQUE (id, ecosystem)")
    _add_constraint(
        "registries", "ck_registries_auth_credentials", "CHECK (auth_type = 'none' OR credentials_ref IS NOT NULL)"
    )
    _add_constraint(
        "registries", "ck_registries_auth_type", "CHECK (auth_type IN ('none', 'basic', 'bearer', 'tls_client_cert'))"
    )

    # packages
    op.execute("""
        ALTER TABLE packages
            ADD COLUMN IF NOT EXISTS origin package_origin_enum NOT NULL DEFAULT 'dependency',
            ADD COLUMN IF NOT EXISTS deprecation jsonb,
            ALTER COLUMN namespace SET DEFAULT '',
            ALTER COLUMN latest_version DROP NOT NULL,
            ALTER COLUMN name TYPE text,
            ALTER COLUMN normalized_name TYPE text
    """)
    _add_constraint("packages", "uq_packages_id_ecosystem", "UNIQUE (id, ecosystem)")

    # package_registries: per-source change-detection state
    op.execute("""
        ALTER TABLE package_registries
            ADD COLUMN IF NOT EXISTS ecosystem ecosystem_enum,
            ADD COLUMN IF NOT EXISTS upstream_revision text,
            ADD COLUMN IF NOT EXISTS etag text,
            ADD COLUMN IF NOT EXISTS last_modified text,
            ADD COLUMN IF NOT EXISTS checked_at timestamptz,
            ADD COLUMN IF NOT EXISTS missing_since timestamptz
    """)
    # NOT VALID: checked by 0007 once every row has the ecosystem; old code inserts rows without it until then.
    _add_constraint(
        "package_registries", "fk_pkgreg_package_ecosystem",
        "FOREIGN KEY (package_id, ecosystem) REFERENCES packages (id, ecosystem) NOT VALID",
    )
    _add_constraint(
        "package_registries", "fk_pkgreg_registry_ecosystem",
        "FOREIGN KEY (registry_id, ecosystem) REFERENCES registries (id, ecosystem) NOT VALID",
    )

    # package_versions
    op.execute("""
        ALTER TABLE package_versions
            ADD COLUMN IF NOT EXISTS sort_key bytea,
            ADD COLUMN IF NOT EXISTS is_prerelease boolean,
            ADD COLUMN IF NOT EXISTS status_reason text,
            ADD COLUMN IF NOT EXISTS deprecation jsonb,
            ADD COLUMN IF NOT EXISTS engines jsonb,
            ADD COLUMN IF NOT EXISTS metadata_registry_id uuid REFERENCES registries (id),
            ADD COLUMN IF NOT EXISTS dependencies_state dependencies_state_enum NOT NULL DEFAULT 'pending',
            ALTER COLUMN version TYPE text
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ver_pkg_sort ON package_versions (package_id, sort_key DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS version_sources (
            version_id      uuid NOT NULL REFERENCES package_versions (id) ON DELETE CASCADE,
            registry_id     uuid NOT NULL REFERENCES registries (id),
            first_seen_at   timestamptz NOT NULL,
            status          package_version_status_enum NOT NULL DEFAULT 'active',
            metadata_digest bytea,
            PRIMARY KEY (version_id, registry_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_versrc_registry ON version_sources (registry_id)")

    # dependencies
    op.execute("""
        ALTER TABLE dependencies
            ADD COLUMN IF NOT EXISTS alias text,
            ADD COLUMN IF NOT EXISTS scope varchar(32),
            ADD COLUMN IF NOT EXISTS details jsonb,
            ADD COLUMN IF NOT EXISTS variant_registry_id uuid REFERENCES registries (id),
            ALTER COLUMN dependency_namespace SET DEFAULT '',
            ALTER COLUMN dependency_name TYPE text,
            ALTER COLUMN version_constraint TYPE text
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dep_target ON dependencies (dependency_ecosystem, dependency_namespace, dependency_name)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_dep_version_variant ON dependencies (version_id, variant_registry_id)")

    # artifacts: files of a version per source
    op.execute("""
        ALTER TABLE artifacts
            ADD COLUMN IF NOT EXISTS arch varchar(32),
            ADD COLUMN IF NOT EXISTS checksum_sha1 varchar(40),
            ADD COLUMN IF NOT EXISTS checksum_sha512 varchar(128),
            ADD COLUMN IF NOT EXISTS checksum_integrity text,
            ADD COLUMN IF NOT EXISTS has_metadata_file boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS metadata_blob text,
            ALTER COLUMN download_url DROP NOT NULL,
            ALTER COLUMN filename TYPE text,
            ALTER COLUMN download_url TYPE text
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_version_registry_filename "
        "ON artifacts (version_id, registry_id, filename)"
    )

    # sources, profiles, documents
    op.execute("""
        CREATE TABLE IF NOT EXISTS source_profiles (
            id          uuid PRIMARY KEY,
            ecosystem   ecosystem_enum NOT NULL,
            slug        varchar(64) NOT NULL,
            name        text NOT NULL,
            is_default  boolean NOT NULL DEFAULT false,
            UNIQUE (ecosystem, slug),
            UNIQUE (id, ecosystem)
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_source_profiles_default ON source_profiles (ecosystem) WHERE is_default")
    op.execute("""
        CREATE TABLE IF NOT EXISTS source_profile_registries (
            profile_id       uuid NOT NULL,
            registry_id      uuid NOT NULL,
            ecosystem        ecosystem_enum NOT NULL,
            position         integer NOT NULL,
            routing_override jsonb,
            PRIMARY KEY (profile_id, registry_id),
            UNIQUE (profile_id, position),
            FOREIGN KEY (profile_id, ecosystem) REFERENCES source_profiles (id, ecosystem) ON DELETE CASCADE,
            FOREIGN KEY (registry_id, ecosystem) REFERENCES registries (id, ecosystem)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS source_documents (
            registry_id  uuid NOT NULL REFERENCES registries (id),
            path         text NOT NULL,
            content      bytea NOT NULL,
            content_type varchar(64) NOT NULL,
            sha256       varchar(64) NOT NULL,
            etag         text,
            fetched_at   timestamptz NOT NULL,
            PRIMARY KEY (registry_id, path)
        )
    """)

    # sync_states: cursor and health of a source (operation progress moves to runs, R1)
    op.execute("""
        ALTER TABLE sync_states
            ADD COLUMN IF NOT EXISTS degraded_until timestamptz,
            ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0,
            ALTER COLUMN sync_type SET DEFAULT 'incremental'
    """)

    # runs: journal of operations (R1). package_events.run_id points to update_runs today; the FK is dropped here and
    # re-created against runs in 0007, after the last update_runs rows are copied over.
    op.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id                   uuid PRIMARY KEY,
            operation            varchar(32) NOT NULL,
            ecosystem            ecosystem_enum,
            status               varchar(16) NOT NULL,
            started_at           timestamptz NOT NULL,
            finished_at          timestamptz,
            packages_total       integer NOT NULL DEFAULT 0,
            packages_checked     integer NOT NULL DEFAULT 0,
            packages_changed     integer NOT NULL DEFAULT 0,
            packages_updated     integer NOT NULL DEFAULT 0,
            packages_failed      integer NOT NULL DEFAULT 0,
            packages_unavailable integer NOT NULL DEFAULT 0,
            events_created       integer NOT NULL DEFAULT 0,
            per_registry         jsonb NOT NULL DEFAULT '{}'::jsonb,
            events_by_kind       jsonb NOT NULL DEFAULT '{}'::jsonb,
            error                text
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_runs_op_eco_started ON runs (operation, ecosystem, started_at DESC)")
    op.execute("ALTER TABLE package_events DROP CONSTRAINT IF EXISTS package_events_run_id_fkey")
    op.execute("ALTER TABLE package_events ALTER COLUMN version TYPE text")

    # sync_failures: keyed by ecosystem, source and purl (new unique key in 0007)
    op.execute("""
        ALTER TABLE sync_failures
            ADD COLUMN IF NOT EXISTS ecosystem ecosystem_enum,
            ADD COLUMN IF NOT EXISTS registry_id uuid REFERENCES registries (id),
            ADD COLUMN IF NOT EXISTS package_ref text,
            ALTER COLUMN package_name SET DEFAULT ''
    """)


# --- data -----------------------------------------------------------------------------------------------------------


def _check_duplicates(conn: sa.Connection) -> None:
    # T16: '' and NULL namespaces become one key; refuse to continue if that creates duplicates.
    duplicates = conn.execute(sa.text("""
        SELECT ecosystem::text, coalesce(namespace, ''), normalized_name, count(*)
        FROM packages GROUP BY 1, 2, 3 HAVING count(*) > 1 ORDER BY 1, 2, 3
    """)).fetchall()
    if duplicates:
        listing = ", ".join(f"{eco}:{ns}/{name} x{n}" for eco, ns, name, n in duplicates[:20])
        raise RuntimeError(
            f"migration 0006 stopped: {len(duplicates)} duplicate packages after namespace NULL -> '': {listing}. "
            "Merge them first (IMPL T16), then rerun."
        )


def _fill_registries_and_packages(conn: sa.Connection) -> None:
    conn.execute(sa.text("""
        UPDATE registries SET
            protocol = coalesce(protocol, 'pypi-warehouse'),
            priority = sync_priority,
            enabled = sync_enabled,
            config = config || jsonb_strip_nulls(jsonb_build_object('api_url', api_url, 'url_pattern', url_pattern))
        WHERE protocol IS NULL
    """))
    conn.execute(sa.text("ALTER TABLE registries ALTER COLUMN protocol SET NOT NULL"))

    conn.execute(sa.text("UPDATE packages SET namespace = '' WHERE namespace IS NULL"))
    seed = [_normalize(n) for n in os.environ.get("SEED_PACKAGES", DEFAULT_SEED_PACKAGES).split(",") if n.strip()]
    conn.execute(
        sa.text("UPDATE packages SET origin = 'seed' WHERE ecosystem = 'pypi' AND normalized_name = ANY(:names)"),
        {"names": seed},
    )
    conn.execute(sa.text("""
        UPDATE packages SET deprecation = jsonb_strip_nulls(jsonb_build_object(
            'message', deprecation_message, 'alternative', deprecation_alternative))
        WHERE deprecated AND deprecation IS NULL
    """))

    conn.execute(sa.text("""
        UPDATE package_registries pr SET ecosystem = p.ecosystem
        FROM packages p WHERE p.id = pr.package_id AND pr.ecosystem IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE package_registries pr SET upstream_revision = p.last_serial::text
        FROM packages p
        WHERE p.id = pr.package_id AND pr.registry_id = p.primary_registry_id
          AND p.last_serial IS NOT NULL AND pr.upstream_revision IS NULL
    """))

    pypi = conn.execute(sa.text("SELECT id FROM registries WHERE slug = 'pypi'")).scalar()
    if pypi is not None:
        conn.execute(sa.text("""
            INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
            VALUES (md5('profile:pypi/default')::uuid, 'pypi', 'default', 'PyPI', true)
            ON CONFLICT (ecosystem, slug) DO NOTHING
        """))
        conn.execute(sa.text("""
            INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
            VALUES (md5('profile:pypi/default')::uuid, :registry, 'pypi', 0)
            ON CONFLICT DO NOTHING
        """), {"registry": pypi})


def _fill_versions(conn: sa.Connection) -> None:
    # T13: hydrated if the flag says so, or no flag but dependencies exist (how the code reads it today).
    conn.execute(sa.text("""
        UPDATE package_versions v SET dependencies_state = 'hydrated'
        WHERE dependencies_state = 'pending' AND (
            (v.features ->> 'dependencies_hydrated')::boolean IS TRUE
            OR (NOT (coalesce(v.features, '{}'::jsonb) ? 'dependencies_hydrated')
                AND EXISTS (SELECT 1 FROM dependencies d WHERE d.version_id = v.id)))
    """))
    conn.execute(sa.text("""
        UPDATE package_versions SET engines = jsonb_build_object('python', requires_python)
        WHERE requires_python IS NOT NULL AND engines IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE package_versions v SET metadata_registry_id = p.primary_registry_id
        FROM packages p WHERE p.id = v.package_id AND v.metadata_registry_id IS NULL
    """))
    conn.execute(sa.text("""
        INSERT INTO version_sources (version_id, registry_id, first_seen_at, status)
        SELECT v.id, p.primary_registry_id, coalesce(v.release_date, now()), v.status
        FROM package_versions v JOIN packages p ON p.id = v.package_id
        ON CONFLICT DO NOTHING
    """))

    # sort_key/is_prerelease in committed batches (resumable: only rows still without a key).
    while True:
        rows = conn.execute(
            sa.text("SELECT id, version FROM package_versions WHERE sort_key IS NULL LIMIT :n"), {"n": BATCH}
        ).fetchall()
        if not rows:
            break
        for version_id, raw in rows:
            encoded = _pep440_key(raw)
            if encoded is None:
                # T15: keep the row, sort it below every valid version, mark it.
                conn.execute(sa.text("""
                    UPDATE package_versions SET sort_key = :key, is_prerelease = false,
                        raw_metadata = raw_metadata || '{"invalid_version": true}'::jsonb
                    WHERE id = :id
                """), {"key": b"\x00" + raw.encode(), "id": version_id})
            else:
                key, prerelease = encoded
                conn.execute(
                    sa.text("UPDATE package_versions SET sort_key = :key, is_prerelease = :pre WHERE id = :id"),
                    {"key": key, "pre": prerelease, "id": version_id},
                )


def _fill_dependencies_and_rest(conn: sa.Connection) -> None:
    conn.execute(sa.text("UPDATE dependencies SET dependency_namespace = '' WHERE dependency_namespace IS NULL"))
    # T14: store the normalized name; the original spelling stays in raw_spec.
    conn.execute(sa.text("""
        UPDATE dependencies SET dependency_name = lower(regexp_replace(dependency_name, '[-_.]+', '-', 'g'))
        WHERE dependency_ecosystem = 'pypi'
          AND dependency_name <> lower(regexp_replace(dependency_name, '[-_.]+', '-', 'g'))
    """))
    conn.execute(sa.text(
        "UPDATE artifacts SET has_metadata_file = requires_dist_metadata WHERE requires_dist_metadata AND NOT has_metadata_file"
    ))

    for run_id, serial in conn.execute(sa.text("""
        SELECT id, index_serial FROM update_runs u WHERE NOT EXISTS (SELECT 1 FROM runs r WHERE r.id = u.id)
    """)).fetchall():
        per_registry = {"pypi": {"index_serial": serial}} if serial is not None else {}
        conn.execute(sa.text("""
            INSERT INTO runs (id, operation, ecosystem, status, started_at, finished_at, packages_checked,
                              packages_changed, packages_updated, packages_failed, packages_unavailable,
                              events_created, per_registry, error)
            SELECT id, 'daily_update', 'pypi', status::text, started_at, finished_at, packages_checked,
                   packages_changed, packages_updated, packages_failed, packages_unavailable, events_created,
                   CAST(:per_registry AS jsonb), error
            FROM update_runs WHERE id = :id
            ON CONFLICT (id) DO NOTHING
        """), {"id": run_id, "per_registry": json.dumps(per_registry)})

    conn.execute(sa.text("""
        UPDATE sync_failures SET ecosystem = 'pypi',
            package_ref = 'pkg:pypi/' || lower(regexp_replace(package_name, '[-_.]+', '-', 'g'))
        WHERE package_ref IS NULL AND package_name <> ''
    """))


def upgrade() -> None:
    conn = op.get_bind()
    _check_duplicates(conn)
    _schema()
    # Commit the schema, then fill data in short transactions so long tables do not hold one big lock.
    with op.get_context().autocommit_block():
        _fill_registries_and_packages(conn)
        _fill_versions(conn)
        _fill_dependencies_and_rest(conn)


def downgrade() -> None:
    # Removes what 0006 added. Enum values added to existing types stay (PostgreSQL cannot drop them); normalized
    # dependency names and '' namespaces stay too (the old code reads both).
    op.execute("""
        ALTER TABLE package_events
            ADD CONSTRAINT package_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES update_runs (id) NOT VALID
    """)
    for table in ("runs", "source_documents", "source_profile_registries", "source_profiles", "version_sources"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("""
        ALTER TABLE sync_failures DROP COLUMN IF EXISTS ecosystem, DROP COLUMN IF EXISTS registry_id,
            DROP COLUMN IF EXISTS package_ref, ALTER COLUMN package_name DROP DEFAULT
    """)
    op.execute("""
        ALTER TABLE sync_states DROP COLUMN IF EXISTS degraded_until, DROP COLUMN IF EXISTS consecutive_failures,
            ALTER COLUMN sync_type DROP DEFAULT
    """)
    op.execute("DROP INDEX IF EXISTS uq_artifacts_version_registry_filename")
    op.execute("""
        ALTER TABLE artifacts DROP COLUMN IF EXISTS arch, DROP COLUMN IF EXISTS checksum_sha1,
            DROP COLUMN IF EXISTS checksum_sha512, DROP COLUMN IF EXISTS checksum_integrity,
            DROP COLUMN IF EXISTS has_metadata_file, DROP COLUMN IF EXISTS metadata_blob
    """)
    op.execute("DROP INDEX IF EXISTS idx_dep_target")
    op.execute("DROP INDEX IF EXISTS idx_dep_version_variant")
    op.execute("""
        ALTER TABLE dependencies DROP COLUMN IF EXISTS alias, DROP COLUMN IF EXISTS scope,
            DROP COLUMN IF EXISTS details, DROP COLUMN IF EXISTS variant_registry_id,
            ALTER COLUMN dependency_namespace DROP DEFAULT
    """)
    op.execute("DROP INDEX IF EXISTS idx_ver_pkg_sort")
    op.execute("""
        ALTER TABLE package_versions DROP COLUMN IF EXISTS sort_key, DROP COLUMN IF EXISTS is_prerelease,
            DROP COLUMN IF EXISTS status_reason, DROP COLUMN IF EXISTS deprecation, DROP COLUMN IF EXISTS engines,
            DROP COLUMN IF EXISTS metadata_registry_id, DROP COLUMN IF EXISTS dependencies_state
    """)
    op.execute("""
        ALTER TABLE package_registries DROP CONSTRAINT IF EXISTS fk_pkgreg_package_ecosystem,
            DROP CONSTRAINT IF EXISTS fk_pkgreg_registry_ecosystem,
            DROP COLUMN IF EXISTS ecosystem, DROP COLUMN IF EXISTS upstream_revision, DROP COLUMN IF EXISTS etag,
            DROP COLUMN IF EXISTS last_modified, DROP COLUMN IF EXISTS checked_at, DROP COLUMN IF EXISTS missing_since
    """)
    op.execute("ALTER TABLE packages DROP CONSTRAINT IF EXISTS uq_packages_id_ecosystem")
    op.execute("""
        ALTER TABLE packages DROP COLUMN IF EXISTS origin, DROP COLUMN IF EXISTS deprecation,
            ALTER COLUMN namespace DROP DEFAULT
    """)
    op.execute("UPDATE packages SET latest_version = '' WHERE latest_version IS NULL")
    op.execute("ALTER TABLE packages ALTER COLUMN latest_version SET NOT NULL")
    op.execute("""
        ALTER TABLE registries DROP CONSTRAINT IF EXISTS ck_registries_auth_credentials,
            DROP CONSTRAINT IF EXISTS ck_registries_auth_type, DROP CONSTRAINT IF EXISTS uq_registries_id_ecosystem,
            DROP COLUMN IF EXISTS protocol, DROP COLUMN IF EXISTS config, DROP COLUMN IF EXISTS routing,
            DROP COLUMN IF EXISTS priority, DROP COLUMN IF EXISTS enabled, DROP COLUMN IF EXISTS auth_type,
            DROP COLUMN IF EXISTS credentials_ref, DROP COLUMN IF EXISTS updated_at,
            ALTER COLUMN tier DROP DEFAULT, ALTER COLUMN registry_type DROP DEFAULT
    """)
    for name in ("dependencies_state_enum", "package_origin_enum", "registry_protocol_enum"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
