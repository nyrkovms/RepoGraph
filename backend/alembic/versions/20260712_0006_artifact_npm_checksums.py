"""npm checksums on artifacts: sha1 (shasum) and SRI integrity

Revision ID: 20260712_0006
Revises: 20260712_0005
Create Date: 2026-07-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260712_0006"
down_revision: Union[str, Sequence[str], None] = "20260712_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("checksum_sha1", sa.String(length=128), nullable=True))
    op.add_column("artifacts", sa.Column("checksum_integrity", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("artifacts", "checksum_integrity")
    op.drop_column("artifacts", "checksum_sha1")
