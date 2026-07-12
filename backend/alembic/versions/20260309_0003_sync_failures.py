"""add sync_failures queue for retryable package sync errors

Revision ID: 20260309_0003
Revises: 20260309_0002
Create Date: 2026-03-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260309_0003"
down_revision: Union[str, Sequence[str], None] = "20260309_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_failures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("package_name", sa.String(length=255), nullable=False),
        sa.Column("error_class", sa.String(length=255), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("operation", "package_name", name="uq_sync_failures_operation_package"),
    )
    op.create_index("idx_sync_failures_retry_due", "sync_failures", ["retryable", "next_retry_at"])
    op.create_index("idx_sync_failures_last_seen", "sync_failures", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("idx_sync_failures_last_seen", table_name="sync_failures")
    op.drop_index("idx_sync_failures_retry_due", table_name="sync_failures")
    op.drop_table("sync_failures")
