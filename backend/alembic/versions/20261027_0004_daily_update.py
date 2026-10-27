"""daily update: package_events journal, update_runs, packages.last_serial/unavailable_at, removed version status

Revision ID: 20261027_0004
Revises: 20260309_0003
Create Date: 2026-10-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261027_0004"
down_revision: Union[str, Sequence[str], None] = "20260309_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG12+ allows ADD VALUE inside a transaction as long as the new value is not used in the same transaction.
    op.execute("ALTER TYPE package_version_status_enum ADD VALUE IF NOT EXISTS 'removed'")

    op.add_column("packages", sa.Column("last_serial", sa.BigInteger(), nullable=True))
    op.add_column("packages", sa.Column("unavailable_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE packages SET last_serial = (raw_metadata->>'last_serial')::bigint "
        "WHERE raw_metadata ? 'last_serial' AND jsonb_typeof(raw_metadata->'last_serial') = 'number'"
    )

    bind = op.get_bind()
    update_run_status_enum = postgresql.ENUM(
        "running", "success", "partial", "failed", name="update_run_status_enum", create_type=False
    )
    package_event_kind_enum = postgresql.ENUM(
        "tracking_started",
        "release_added",
        "release_yanked",
        "release_unyanked",
        "release_removed",
        "release_restored",
        "vulnerability_added",
        "vulnerability_withdrawn",
        "project_unavailable",
        "project_restored",
        name="package_event_kind_enum",
        create_type=False,
    )
    update_run_status_enum.create(bind, checkfirst=True)
    package_event_kind_enum.create(bind, checkfirst=True)

    op.create_table(
        "update_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", update_run_status_enum, nullable=False),
        sa.Column("index_serial", sa.BigInteger(), nullable=True),
        sa.Column("packages_checked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("packages_changed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("packages_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("packages_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("packages_unavailable", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "package_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=True),
        sa.Column("kind", package_event_kind_enum, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("update_runs.id"), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("idx_package_events_detected_at", "package_events", ["detected_at"])
    op.create_index("idx_package_events_package_detected", "package_events", ["package_id", "detected_at"])


def downgrade() -> None:
    op.drop_index("idx_package_events_package_detected", table_name="package_events")
    op.drop_index("idx_package_events_detected_at", table_name="package_events")
    op.drop_table("package_events")
    op.drop_table("update_runs")

    bind = op.get_bind()
    sa.Enum(name="package_event_kind_enum").drop(bind, checkfirst=True)
    sa.Enum(name="update_run_status_enum").drop(bind, checkfirst=True)

    op.drop_column("packages", "unavailable_at")
    op.drop_column("packages", "last_serial")

    # NOTE: the 'removed' value of package_version_status_enum is intentionally left in place:
    # PostgreSQL does not support removing a value from an enum type.
