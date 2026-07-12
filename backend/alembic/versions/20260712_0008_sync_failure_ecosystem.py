"""sync_failures carry the ecosystem so retries use the right connector

Revision ID: 20260712_0008
Revises: 20260712_0007
Create Date: 2026-07-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260712_0008"
down_revision: Union[str, Sequence[str], None] = "20260712_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sync_failures",
        sa.Column("ecosystem", sa.String(length=16), nullable=False, server_default="pypi"),
    )


def downgrade() -> None:
    op.drop_column("sync_failures", "ecosystem")
