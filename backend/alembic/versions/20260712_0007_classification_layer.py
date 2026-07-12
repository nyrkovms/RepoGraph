"""classification layer: classifications, facets, capabilities, curation, rule registries, llm verdicts

Revision ID: 20260712_0007
Revises: 20260712_0006
Create Date: 2026-07-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260712_0007"
down_revision: Union[str, Sequence[str], None] = "20260712_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "classifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("classifier_version", sa.String(length=32), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("version_id", "classifier_version", name="uq_classification_version_rules"),
    )
    op.create_index("idx_classifications_version", "classifications", ["version_id"])
    op.create_index("idx_classifications_status", "classifications", ["classifier_version", "status"])

    op.create_table(
        "classification_facets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "classification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("classifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("idx_facets_classification", "classification_facets", ["classification_id"])
    op.create_index("idx_facets_role", "classification_facets", ["role"])

    op.create_table(
        "classification_capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "classification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("classifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
    )
    op.create_index("idx_caps_classification", "classification_capabilities", ["classification_id"])
    op.create_index("idx_caps_capability", "classification_capabilities", ["capability"])

    op.create_table(
        "user_capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ecosystem", "name", "version", "capability", name="uq_user_capability"),
    )
    op.create_index("idx_user_caps_pkg", "user_capabilities", ["ecosystem", "name"])

    op.create_table(
        "role_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("roles", postgresql.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("ecosystem", "name", name="uq_role_override"),
    )

    op.create_table(
        "entry_group_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("pattern", sa.String(length=128), nullable=False, unique=True),
        sa.Column("group_type", sa.String(length=32), nullable=False),
        sa.Column("role_effect", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("host", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "package_relations",
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), primary_key=True),
        sa.Column("related_name", sa.String(length=255), primary_key=True),
        sa.Column("relation", sa.String(length=32), primary_key=True),
    )

    op.create_table(
        "llm_verdicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("package_versions.id"), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("verdict", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("version_id", "model", "prompt_version", name="uq_llm_verdict"),
    )


def downgrade() -> None:
    for table in (
        "llm_verdicts",
        "package_relations",
        "entry_group_rules",
        "role_overrides",
        "user_capabilities",
        "classification_capabilities",
        "classification_facets",
        "classifications",
    ):
        op.drop_table(table)
