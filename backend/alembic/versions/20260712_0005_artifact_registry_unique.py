"""artifact uniqueness includes registry (multi-registry union of distributions)

Revision ID: 20260712_0005
Revises: 20260712_0004
Create Date: 2026-07-12
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_0005"
down_revision: Union[str, Sequence[str], None] = "20260712_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_artifact_version_filename", "artifacts", type_="unique")
    op.create_unique_constraint(
        "uq_artifact_version_registry_filename",
        "artifacts",
        ["version_id", "registry_id", "filename"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_artifact_version_registry_filename", "artifacts", type_="unique")
    op.create_unique_constraint("uq_artifact_version_filename", "artifacts", ["version_id", "filename"])
