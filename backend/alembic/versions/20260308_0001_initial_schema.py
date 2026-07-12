"""initial schema and pypi seed

Revision ID: 20260308_0001
Revises:
Create Date: 2026-03-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260308_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    ecosystem_enum = postgresql.ENUM(
        "pypi", "npm", "maven", "go", "crates", "nuget", name="ecosystem_enum", create_type=False
    )
    registry_type_enum = postgresql.ENUM(
        "public", "corporate", "mirror", name="registry_type_enum", create_type=False
    )
    tier_enum = postgresql.ENUM("tier1", "tier2", "tier3", name="tier_enum", create_type=False)
    package_version_status_enum = postgresql.ENUM(
        "active", "yanked", "deprecated", "unlisted", name="package_version_status_enum", create_type=False
    )
    dependency_type_enum = postgresql.ENUM(
        "runtime", "dev", "optional", "build", "peer", "provided", name="dependency_type_enum", create_type=False
    )
    vulnerability_source_enum = postgresql.ENUM(
        "osv", "pypi_native", name="vulnerability_source_enum", create_type=False
    )
    vulnerability_severity_enum = postgresql.ENUM(
        "unknown", "low", "medium", "high", "critical", name="vulnerability_severity_enum", create_type=False
    )
    sync_type_enum = postgresql.ENUM("full", "incremental", name="sync_type_enum", create_type=False)
    sync_status_enum = postgresql.ENUM("idle", "running", "failed", name="sync_status_enum", create_type=False)
    author_role_enum = postgresql.ENUM("author", "maintainer", name="author_role_enum", create_type=False)

    bind = op.get_bind()
    ecosystem_enum.create(bind, checkfirst=True)
    registry_type_enum.create(bind, checkfirst=True)
    tier_enum.create(bind, checkfirst=True)
    package_version_status_enum.create(bind, checkfirst=True)
    dependency_type_enum.create(bind, checkfirst=True)
    vulnerability_source_enum.create(bind, checkfirst=True)
    vulnerability_severity_enum.create(bind, checkfirst=True)
    sync_type_enum.create(bind, checkfirst=True)
    sync_status_enum.create(bind, checkfirst=True)
    author_role_enum.create(bind, checkfirst=True)

    op.create_table(
        "registries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("ecosystem", ecosystem_enum, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("registry_type", registry_type_enum, nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("api_url", sa.String(length=2048), nullable=True),
        sa.Column("url_pattern", sa.String(length=2048), nullable=True),
        sa.Column("auth_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auth_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tier", tier_enum, nullable=False),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("slug", name="uq_registries_slug"),
    )

    op.create_table(
        "packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("ecosystem", ecosystem_enum, nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("primary_registry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registries.id"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("homepage_url", sa.String(length=2048), nullable=True),
        sa.Column("repository_url", sa.String(length=2048), nullable=True),
        sa.Column("documentation_url", sa.String(length=2048), nullable=True),
        sa.Column("license_raw", sa.String(length=512), nullable=True),
        sa.Column("license_spdx", sa.String(length=128), nullable=True),
        sa.Column("keywords", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("latest_version", sa.String(length=128), nullable=False),
        sa.Column("downloads_total", sa.BigInteger(), nullable=True),
        sa.Column("downloads_recent", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deprecation_message", sa.Text(), nullable=True),
        sa.Column("deprecation_alternative", sa.String(length=255), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    op.create_table(
        "package_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("release_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", package_version_status_enum, nullable=False),
        sa.Column("requires_python", sa.String(length=128), nullable=True),
        sa.Column("download_count", sa.BigInteger(), nullable=True),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("needed_in_index", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("package_id", "version", name="uq_package_versions_pkg_version"),
    )

    op.create_table(
        "dependencies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("dependency_name", sa.String(length=255), nullable=False),
        sa.Column("dependency_namespace", sa.String(length=255), nullable=True),
        sa.Column("dependency_ecosystem", ecosystem_enum, nullable=False),
        sa.Column("version_constraint", sa.String(length=255), nullable=False),
        sa.Column("dependency_type", dependency_type_enum, nullable=False),
        sa.Column("extra_name", sa.String(length=128), nullable=True),
        sa.Column("environment_markers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_spec", sa.Text(), nullable=False),
        sa.Column("resolved_package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=True),
        sa.Column("resolved_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=True),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("registry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registries.id"), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("download_url", sa.String(length=4096), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=128), nullable=True),
        sa.Column("checksum_md5", sa.String(length=128), nullable=True),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("platform_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requires_dist_metadata", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("version_id", "filename", name="uq_artifact_version_filename"),
    )

    op.create_table(
        "package_registries",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("registry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registries.id"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("package_id", "registry_id", name="pk_package_registries"),
    )

    op.create_table(
        "dist_tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("package_id", "tag", name="uq_dist_tags_package_tag"),
    )

    op.create_table(
        "authors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
    )

    op.create_table(
        "package_authors",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("authors.id"), nullable=False),
        sa.Column("role", author_role_enum, nullable=False),
        sa.PrimaryKeyConstraint("package_id", "author_id", "role", name="pk_package_authors"),
    )

    op.create_table(
        "vulnerabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source", vulnerability_source_enum, nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("severity", vulnerability_severity_enum, nullable=True),
        sa.Column("fixed_in", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("link", sa.String(length=2048), nullable=False),
        sa.UniqueConstraint("source_id", name="uq_vulnerabilities_source_id"),
    )

    op.create_table(
        "package_vulnerabilities",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("vulnerability_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vulnerabilities.id"), nullable=False),
        sa.Column("affected_versions", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("package_id", "vulnerability_id", name="pk_package_vulnerabilities"),
    )

    op.create_table(
        "sync_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("registry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registries.id"), nullable=False),
        sa.Column("sync_type", sync_type_enum, nullable=False),
        sa.Column("status", sync_status_enum, nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("cursor_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("packages_synced", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("packages_total", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("registry_id", name="uq_sync_states_registry_id"),
    )

    op.create_index("idx_pkg_eco_ns_name", "packages", ["ecosystem", "namespace", "normalized_name"], unique=True)
    op.create_index("idx_pkg_name_trgm", "packages", ["normalized_name"], postgresql_using="gin", postgresql_ops={"normalized_name": "gin_trgm_ops"})
    op.create_index("idx_pkg_registry", "packages", ["primary_registry_id"])
    op.create_index("idx_pkg_license", "packages", ["license_spdx"], postgresql_where=sa.text("license_spdx IS NOT NULL"))
    op.create_index("idx_pkg_downloads", "packages", [sa.text("downloads_total DESC NULLS LAST")])

    op.create_index("idx_ver_pkg_ver", "package_versions", ["package_id", "version"], unique=True)
    op.create_index("idx_ver_release", "package_versions", ["release_date"])
    op.create_index("idx_ver_needed", "package_versions", ["needed_in_index"], postgresql_where=sa.text("needed_in_index = true"))

    op.create_index("idx_dep_version", "dependencies", ["version_id"])
    op.create_index("idx_dep_name", "dependencies", ["dependency_name", "dependency_ecosystem"])
    op.create_index("idx_dep_resolved", "dependencies", ["resolved_version_id"], postgresql_where=sa.text("resolved_version_id IS NOT NULL"))

    op.create_index("idx_art_version", "artifacts", ["version_id"])
    op.create_index("idx_art_type", "artifacts", ["version_id", "artifact_type"])

    op.create_index("idx_vuln_source", "vulnerabilities", ["source_id"], unique=True)
    op.create_index("idx_pkg_vuln", "package_vulnerabilities", ["package_id"])

    op.execute(
        """
        INSERT INTO registries (
            id, ecosystem, name, slug, registry_type, base_url, api_url,
            auth_required, tier, sync_enabled, sync_priority, created_at
        )
        VALUES (
            gen_random_uuid(), 'pypi', 'PyPI', 'pypi', 'public', 'https://pypi.org/', 'https://pypi.org/pypi/',
            false, 'tier1', true, 0, now()
        )
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("idx_pkg_vuln", table_name="package_vulnerabilities")
    op.drop_index("idx_vuln_source", table_name="vulnerabilities")
    op.drop_index("idx_art_type", table_name="artifacts")
    op.drop_index("idx_art_version", table_name="artifacts")
    op.drop_index("idx_dep_resolved", table_name="dependencies")
    op.drop_index("idx_dep_name", table_name="dependencies")
    op.drop_index("idx_dep_version", table_name="dependencies")
    op.drop_index("idx_ver_needed", table_name="package_versions")
    op.drop_index("idx_ver_release", table_name="package_versions")
    op.drop_index("idx_ver_pkg_ver", table_name="package_versions")
    op.drop_index("idx_pkg_downloads", table_name="packages")
    op.drop_index("idx_pkg_license", table_name="packages")
    op.drop_index("idx_pkg_registry", table_name="packages")
    op.drop_index("idx_pkg_name_trgm", table_name="packages")
    op.drop_index("idx_pkg_eco_ns_name", table_name="packages")

    op.drop_table("sync_states")
    op.drop_table("package_vulnerabilities")
    op.drop_table("vulnerabilities")
    op.drop_table("package_authors")
    op.drop_table("authors")
    op.drop_table("dist_tags")
    op.drop_table("package_registries")
    op.drop_table("artifacts")
    op.drop_table("dependencies")
    op.drop_table("package_versions")
    op.drop_table("packages")
    op.drop_table("registries")

    bind = op.get_bind()
    sa.Enum(name="author_role_enum").drop(bind, checkfirst=True)
    sa.Enum(name="sync_status_enum").drop(bind, checkfirst=True)
    sa.Enum(name="sync_type_enum").drop(bind, checkfirst=True)
    sa.Enum(name="vulnerability_severity_enum").drop(bind, checkfirst=True)
    sa.Enum(name="vulnerability_source_enum").drop(bind, checkfirst=True)
    sa.Enum(name="dependency_type_enum").drop(bind, checkfirst=True)
    sa.Enum(name="package_version_status_enum").drop(bind, checkfirst=True)
    sa.Enum(name="tier_enum").drop(bind, checkfirst=True)
    sa.Enum(name="registry_type_enum").drop(bind, checkfirst=True)
    sa.Enum(name="ecosystem_enum").drop(bind, checkfirst=True)
