"""expand packages.license_raw to text

Revision ID: 20260309_0002
Revises: 20260308_0001
Create Date: 2026-03-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260309_0002"
down_revision: Union[str, Sequence[str], None] = "20260308_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("packages", "license_raw", existing_type=sa.String(length=512), type_=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE packages SET license_raw = LEFT(license_raw, 512) WHERE license_raw IS NOT NULL")
    op.alter_column("packages", "license_raw", existing_type=sa.Text(), type_=sa.String(length=512), nullable=True)
