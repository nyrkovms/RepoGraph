"""multi-ecosystem expand: sources, profiles, version sources, sort keys, runs journal (IMPL-MULTI-ECOSYSTEM.md §2)

Expand-only: adds and copies, never drops. Columns that 0007 removes keep working for the old code; new columns stay
nullable until 0007 (the old code does not write them).

Three phases, so the api can keep serving while it runs:
1. One short transaction with fast DDL only (new columns without defaults that rewrite, new empty tables), under
   lock_timeout so it fails fast instead of queueing behind long api transactions.
2. backfill(): data in keyset batches over the primary key, one statement per batch (autocommit). Every step only
   touches rows that are not filled yet, so a crashed run is resumed by running the upgrade again. 0007 calls
   backfill() again before NOT NULL/VALIDATE to catch rows the old code wrote after 0006 (IMPL §2).
3. Indexes CREATE ... CONCURRENTLY after the data, unique constraints attached with USING INDEX, composite FKs
   NOT VALID (validated in 0007).

If phase 2 or 3 fails, alembic_version stays at 0005 with the phase-1 DDL committed; rerun the upgrade (the DDL is
idempotent). Do not downgrade in that state.

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
LOCK_TIMEOUT = "5s"
# T12: packages from SEED_PACKAGES are `seed`, everything else `dependency`. Default copied from app.config at the time
# of writing; the migration must not import application code.
DEFAULT_SEED_PACKAGES = "flask,django,fastapi,requests,numpy,pandas"


# --- vendored PEP 440 sort key (design/schema-check/sortkey_proto.py, same as app.ecosystems.pypi.versions) ------


def _num(digits: str) -> bytes:
    digits = digits.lstrip("0") or "0"
    if len(digits) > 255:
        raise ValueError("number too long to encode")
    return bytes([len(digits)]) + digits.encode()


def _pep440_key(raw: str) -> tuple[bytes, bool] | None:
    try:
        pv = Version(raw)
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
    except (InvalidVersion, ValueError):
        return None
    return out, pv.is_prerelease or pv.is_devrelease


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- phase 1: fast DDL --------------------------------------------------------------------------------------------


def _create_type(name: str, values: list[str]) -> None:
    labels = ", ".join(f"'{v}'" for v in values)
    op.execute(f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({labels}); EXCEPTION WHEN duplicate_object THEN NULL; END $$")


def _add_constraint(table: str, name: str, definition: str) -> None:
    op.execute(
        f"DO $$ BEGIN ALTER TABLE {table} ADD CONSTRAINT {name} {definition}; "
        f"EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$"
    )


def _schema() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
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

    # Constant defaults do not rewrite tables (PG >= 11); varchar -> text is binary compatible and rebuilds nothing.
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
    _add_constraint(
        "registries", "ck_registries_auth_credentials", "CHECK (auth_type = 'none' OR credentials_ref IS NOT NULL)"
    )
    _add_constraint(
        "registries", "ck_registries_auth_type", "CHECK (auth_type IN ('none', 'basic', 'bearer', 'tls_client_cert'))"
    )

    op.execute("""
        ALTER TABLE packages
            ADD COLUMN IF NOT EXISTS origin package_origin_enum NOT NULL DEFAULT 'dependency',
            ADD COLUMN IF NOT EXISTS deprecation jsonb,
            ALTER COLUMN namespace SET DEFAULT '',
            ALTER COLUMN latest_version DROP NOT NULL,
            ALTER COLUMN name TYPE text,
            ALTER COLUMN normalized_name TYPE text
    """)
    op.execute("""
        ALTER TABLE package_registries
            ADD COLUMN IF NOT EXISTS ecosystem ecosystem_enum,
            ADD COLUMN IF NOT EXISTS upstream_revision text,
            ADD COLUMN IF NOT EXISTS etag text,
            ADD COLUMN IF NOT EXISTS last_modified text,
            ADD COLUMN IF NOT EXISTS checked_at timestamptz,
            ADD COLUMN IF NOT EXISTS missing_since timestamptz
    """)
    # FK columns are added without the constraint; constraints come NOT VALID in phase 3 (no scan, short lock).
    op.execute("""
        ALTER TABLE package_versions
            ADD COLUMN IF NOT EXISTS sort_key bytea,
            ADD COLUMN IF NOT EXISTS is_prerelease boolean,
            ADD COLUMN IF NOT EXISTS status_reason text,
            ADD COLUMN IF NOT EXISTS deprecation jsonb,
            ADD COLUMN IF NOT EXISTS engines jsonb,
            ADD COLUMN IF NOT EXISTS metadata_registry_id uuid,
            ADD COLUMN IF NOT EXISTS dependencies_state dependencies_state_enum NOT NULL DEFAULT 'pending',
            ALTER COLUMN version TYPE text
    """)
    op.execute("""
        ALTER TABLE dependencies
            ADD COLUMN IF NOT EXISTS alias text,
            ADD COLUMN IF NOT EXISTS scope varchar(32),
            ADD COLUMN IF NOT EXISTS details jsonb,
            ADD COLUMN IF NOT EXISTS variant_registry_id uuid,
            ALTER COLUMN dependency_namespace SET DEFAULT '',
            ALTER COLUMN dependency_name TYPE text,
            ALTER COLUMN version_constraint TYPE text
    """)
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
    op.execute("""
        ALTER TABLE sync_states
            ADD COLUMN IF NOT EXISTS degraded_until timestamptz,
            ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0,
            ALTER COLUMN sync_type SET DEFAULT 'incremental'
    """)
    # New code keeps writing package_name until 0007 (the old unique key needs it); the new key arrives in 0007.
    op.execute("""
        ALTER TABLE sync_failures
            ADD COLUMN IF NOT EXISTS ecosystem ecosystem_enum,
            ADD COLUMN IF NOT EXISTS registry_id uuid REFERENCES registries (id),
            ADD COLUMN IF NOT EXISTS package_ref text
    """)
    # package_events.run_id points to update_runs; dropped here, re-created against runs in 0007 after the last
    # update_runs rows are copied (IMPL §2).
    op.execute("ALTER TABLE package_events DROP CONSTRAINT IF EXISTS package_events_run_id_fkey")
    op.execute("ALTER TABLE package_events ALTER COLUMN version TYPE text")

    # New tables are empty: their indexes cost nothing here.
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


def _preflight(conn: sa.Connection) -> None:
    # T16: '' and NULL namespaces become one key.
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
    # Secrets are not copied into the database (credentials_ref names a secret); a registry with auth needs a manual step.
    with_auth = conn.execute(sa.text("SELECT slug FROM registries WHERE auth_required ORDER BY slug")).scalars().all()
    if with_auth:
        raise RuntimeError(
            f"migration 0006 stopped: registries with auth_required: {', '.join(with_auth)}. Move their secrets out of "
            "auth_config, set auth_required=false, rerun, then set auth_type/credentials_ref through the admin API."
        )


# --- phase 2: data ------------------------------------------------------------------------------------------------


def _seed_names() -> list[str]:
    names = []
    for item in os.environ.get("SEED_PACKAGES", DEFAULT_SEED_PACKAGES).split(","):
        item = item.strip()
        if item.startswith("pkg:pypi/"):  # T9: purls are accepted too
            item = item[len("pkg:pypi/"):].split("@", 1)[0]
        if item and ":" not in item:
            names.append(_normalize(item))
    return names


def _batches(conn: sa.Connection, table: str, condition: str):
    """Keyset over the primary key: yields id lists of rows matching `condition`, without rescanning done ones."""
    last = None
    while True:
        rows = conn.execute(
            sa.text(
                f"SELECT id FROM {table} WHERE ({condition}) AND (CAST(:last AS uuid) IS NULL OR id > :last) "
                f"ORDER BY id LIMIT :n"
            ),
            {"last": last, "n": BATCH},
        ).scalars().all()
        if not rows:
            return
        yield list(rows)
        last = rows[-1]


def _backfill_small_tables(conn: sa.Connection) -> None:
    conn.execute(sa.text("""
        UPDATE registries SET
            protocol = 'pypi-warehouse', priority = sync_priority, enabled = sync_enabled,
            config = config || jsonb_strip_nulls(jsonb_build_object('api_url', api_url, 'url_pattern', url_pattern))
        WHERE protocol IS NULL AND ecosystem = 'pypi'
    """))
    conn.execute(
        sa.text("UPDATE packages SET origin = 'seed' WHERE ecosystem = 'pypi' AND normalized_name = ANY(:names) AND origin <> 'seed'"),
        {"names": _seed_names()},
    )
    conn.execute(sa.text("""
        UPDATE packages SET deprecation = jsonb_strip_nulls(jsonb_build_object(
            'message', deprecation_message, 'alternative', deprecation_alternative))
        WHERE deprecated AND deprecation IS NULL
    """))
    pypi = conn.execute(sa.text("SELECT id FROM registries WHERE slug = 'pypi'")).scalar()
    if pypi is not None:
        conn.execute(sa.text("""
            INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
            VALUES (md5('profile:pypi/default')::uuid, 'pypi', 'default', 'PyPI', true)
            ON CONFLICT (ecosystem, slug) DO NOTHING
        """))
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


def _backfill_packages(conn: sa.Connection) -> None:
    for ids in _batches(conn, "packages", "namespace IS NULL"):
        conn.execute(sa.text("UPDATE packages SET namespace = '' WHERE id = ANY(:ids)"), {"ids": ids})
    # package_registries has a composite key; batch by package.
    for ids in _batches(conn, "packages", "EXISTS (SELECT 1 FROM package_registries pr WHERE pr.package_id = packages.id AND pr.ecosystem IS NULL)"):
        conn.execute(sa.text("""
            UPDATE package_registries pr SET
                ecosystem = p.ecosystem,
                upstream_revision = CASE WHEN pr.registry_id = p.primary_registry_id AND p.last_serial IS NOT NULL
                                         THEN p.last_serial::text ELSE pr.upstream_revision END
            FROM packages p WHERE p.id = pr.package_id AND pr.ecosystem IS NULL AND p.id = ANY(:ids)
        """), {"ids": ids})


def _backfill_versions(conn: sa.Connection) -> None:
    # One UPDATE per batch fills every new column of package_versions (a single rewrite of each row), then the
    # version_sources rows of the same batch.
    for ids in _batches(conn, "package_versions", "sort_key IS NULL"):
        rows = conn.execute(sa.text("""
            SELECT v.id, v.version, v.requires_python, p.primary_registry_id,
                   CASE WHEN (v.features ->> 'dependencies_hydrated')::boolean IS TRUE THEN true
                        WHEN coalesce(v.features, '{}'::jsonb) ? 'dependencies_hydrated' THEN false
                        ELSE EXISTS (SELECT 1 FROM dependencies d WHERE d.version_id = v.id) END AS hydrated
            FROM package_versions v JOIN packages p ON p.id = v.package_id
            WHERE v.id = ANY(:ids)
        """), {"ids": ids}).fetchall()
        version_ids, keys, prereleases, invalid, states, engines, registries = [], [], [], [], [], [], []
        for version_id, raw, requires_python, registry_id, hydrated in rows:
            encoded = _pep440_key(raw)
            version_ids.append(version_id)
            if encoded is None:
                # T15: keep the row, sort it below every valid version, mark it.
                keys.append(b"\x00" + raw.encode())
                prereleases.append(False)
                invalid.append(True)
            else:
                keys.append(encoded[0])
                prereleases.append(encoded[1])
                invalid.append(False)
            # T13: hydrated if the flag says so, or no flag but dependencies exist.
            states.append("hydrated" if hydrated else "pending")
            engines.append(json.dumps({"python": requires_python}) if requires_python else None)
            registries.append(registry_id)
        conn.execute(sa.text("""
            UPDATE package_versions v SET
                sort_key = b.sort_key,
                is_prerelease = b.is_prerelease,
                dependencies_state = CASE WHEN v.dependencies_state = 'pending'
                                          THEN b.state::dependencies_state_enum ELSE v.dependencies_state END,
                engines = coalesce(v.engines, CAST(b.engines AS jsonb)),
                metadata_registry_id = coalesce(v.metadata_registry_id, b.registry_id),
                raw_metadata = CASE WHEN b.invalid THEN v.raw_metadata || '{"invalid_version": true}'::jsonb
                                    ELSE v.raw_metadata END
            FROM unnest(CAST(:ids AS uuid[]), CAST(:keys AS bytea[]), CAST(:pres AS boolean[]),
                        CAST(:invalid AS boolean[]), CAST(:states AS text[]), CAST(:engines AS text[]),
                        CAST(:registries AS uuid[]))
                 AS b(id, sort_key, is_prerelease, invalid, state, engines, registry_id)
            WHERE v.id = b.id
        """), {
            "ids": version_ids, "keys": keys, "pres": prereleases, "invalid": invalid,
            "states": states, "engines": engines, "registries": registries,
        })
        conn.execute(sa.text("""
            INSERT INTO version_sources (version_id, registry_id, first_seen_at, status)
            SELECT v.id, p.primary_registry_id, coalesce(v.release_date, now()), v.status
            FROM package_versions v JOIN packages p ON p.id = v.package_id
            WHERE v.id = ANY(:ids)
            ON CONFLICT DO NOTHING
        """), {"ids": version_ids})


def _backfill_dependencies(conn: sa.Connection) -> None:
    # T14: store the normalized name (PyPI); the original spelling stays in raw_spec.
    condition = (
        "dependency_namespace IS NULL OR (dependency_ecosystem = 'pypi' AND "
        "dependency_name <> lower(regexp_replace(dependency_name, '[-_.]+', '-', 'g')))"
    )
    for ids in _batches(conn, "dependencies", condition):
        conn.execute(sa.text("""
            UPDATE dependencies SET
                dependency_namespace = coalesce(dependency_namespace, ''),
                dependency_name = CASE WHEN dependency_ecosystem = 'pypi'
                                       THEN lower(regexp_replace(dependency_name, '[-_.]+', '-', 'g'))
                                       ELSE dependency_name END
            WHERE id = ANY(:ids)
        """), {"ids": ids})
    for ids in _batches(conn, "artifacts", "requires_dist_metadata AND NOT has_metadata_file"):
        conn.execute(sa.text("UPDATE artifacts SET has_metadata_file = true WHERE id = ANY(:ids)"), {"ids": ids})


def backfill(conn: sa.Connection) -> None:
    """Fill the 0006 columns for rows that do not have them yet. Idempotent; 0007 runs it again (IMPL §2)."""
    _backfill_small_tables(conn)
    _backfill_packages(conn)
    _backfill_versions(conn)
    _backfill_dependencies(conn)


# --- phase 3: indexes, unique constraints, composite FKs ----------------------------------------------------------

_INDEXES = (
    ("idx_ver_pkg_sort", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ver_pkg_sort ON package_versions (package_id, sort_key DESC)"),
    ("idx_dep_target", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dep_target ON dependencies (dependency_ecosystem, dependency_namespace, dependency_name)"),
    ("idx_dep_version_variant", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dep_version_variant ON dependencies (version_id, variant_registry_id)"),
    ("uq_artifacts_version_registry_filename", "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_artifacts_version_registry_filename ON artifacts (version_id, registry_id, filename)"),
    ("uq_packages_id_ecosystem", "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_packages_id_ecosystem ON packages (id, ecosystem)"),
    ("uq_registries_id_ecosystem", "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_registries_id_ecosystem ON registries (id, ecosystem)"),
)


def _indexes_and_constraints(conn: sa.Connection) -> None:
    conn.execute(sa.text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    for name, ddl in _INDEXES:
        # An interrupted CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would keep: drop it first.
        invalid = conn.execute(sa.text("""
            SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name AND NOT i.indisvalid
        """), {"name": name}).scalar()
        if invalid:
            conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        conn.execute(sa.text(ddl))

    def constraint(table: str, name: str, definition: str) -> None:
        exists = conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).scalar()
        if not exists:
            conn.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}"))

    constraint("packages", "uq_packages_id_ecosystem", "UNIQUE USING INDEX uq_packages_id_ecosystem")
    constraint("registries", "uq_registries_id_ecosystem", "UNIQUE USING INDEX uq_registries_id_ecosystem")
    # NOT VALID: no scan; validated by 0007 once every row has its ecosystem.
    constraint("package_registries", "fk_pkgreg_package_ecosystem",
               "FOREIGN KEY (package_id, ecosystem) REFERENCES packages (id, ecosystem) NOT VALID")
    constraint("package_registries", "fk_pkgreg_registry_ecosystem",
               "FOREIGN KEY (registry_id, ecosystem) REFERENCES registries (id, ecosystem) NOT VALID")
    constraint("package_versions", "package_versions_metadata_registry_id_fkey",
               "FOREIGN KEY (metadata_registry_id) REFERENCES registries (id) NOT VALID")
    constraint("dependencies", "dependencies_variant_registry_id_fkey",
               "FOREIGN KEY (variant_registry_id) REFERENCES registries (id) NOT VALID")

    conn.execute(sa.text("""
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
    """))
    conn.execute(sa.text("""
        INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
        SELECT md5('profile:pypi/default')::uuid, r.id, 'pypi', 0 FROM registries r
        WHERE r.slug = 'pypi' AND EXISTS (SELECT 1 FROM source_profiles WHERE id = md5('profile:pypi/default')::uuid)
        ON CONFLICT DO NOTHING
    """))
    conn.execute(sa.text("RESET lock_timeout"))


def upgrade() -> None:
    conn = op.get_bind()
    _preflight(conn)
    _schema()
    with op.get_context().autocommit_block():
        backfill(conn)
        _indexes_and_constraints(conn)


def downgrade() -> None:
    # Removes what 0006 added; the 0005 code works on the result. Left behind on purpose: enum values added to
    # existing types (PostgreSQL cannot drop them), varchar -> text column types, nullable artifacts.download_url,
    # normalized dependency names and '' namespaces (the 0005 code reads all of these).
    op.execute("""
        ALTER TABLE package_events
            ADD CONSTRAINT package_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES update_runs (id) NOT VALID
    """)
    for table in ("runs", "source_documents", "source_profile_registries", "source_profiles", "version_sources"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("""
        ALTER TABLE sync_failures DROP COLUMN IF EXISTS ecosystem, DROP COLUMN IF EXISTS registry_id,
            DROP COLUMN IF EXISTS package_ref
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
