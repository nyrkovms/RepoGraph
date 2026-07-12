"""multi-registry fetcher: registry protocol/enumeration, dist probes, version descriptions, collision flags

Revision ID: 20260712_0004
Revises: 20260309_0003
Create Date: 2026-07-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260712_0004"
down_revision: Union[str, Sequence[str], None] = "20260309_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "registries",
        sa.Column("protocol", sa.String(length=32), nullable=False, server_default="pypi-json"),
    )
    op.add_column(
        "registries",
        sa.Column("enumerate_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column(
        "package_registries",
        sa.Column("name_collision", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "package_registries",
        sa.Column("checksum_mismatch", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "dist_probes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("registry_slug", sa.String(length=255), nullable=False),
        sa.Column("artifact_filename", sa.String(length=512), nullable=False),
        sa.Column("is_representative", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("entry_points_txt", sa.Text(), nullable=True),
        sa.Column("top_level_txt", sa.Text(), nullable=True),
        sa.Column("file_facts", postgresql.JSONB(), nullable=True),
        sa.Column("readme_extracted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("version_id", "artifact_filename", name="uq_dist_probe_version_filename"),
    )
    op.create_index("idx_dist_probes_version", "dist_probes", ["version_id"])

    op.create_table(
        "version_descriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("version_id", "origin", name="uq_version_description_origin"),
    )
    op.create_index("idx_version_descriptions_version", "version_descriptions", ["version_id"])


def downgrade() -> None:
    op.drop_index("idx_version_descriptions_version", table_name="version_descriptions")
    op.drop_table("version_descriptions")
    op.drop_index("idx_dist_probes_version", table_name="dist_probes")
    op.drop_table("dist_probes")
    op.drop_column("package_registries", "checksum_mismatch")
    op.drop_column("package_registries", "name_collision")
    op.drop_column("registries", "enumerate_enabled")
    op.drop_column("registries", "protocol")
