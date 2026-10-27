"""dependency extras: take extra_name from the `extra == "..."` marker instead of requested extras

Revision ID: 20261027_0005
Revises: 20261027_0004
Create Date: 2026-10-27
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261027_0005"
down_revision: Union[str, Sequence[str], None] = "20261027_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HAS_EXTRA_CONDITION = """environment_markers @> '{"conditions": [{"marker": "extra", "op": "=="}]}'"""


def upgrade() -> None:
    # Rows marked optional only because they request extras of the dependency (`requests[socks]`) are required.
    op.execute(
        "UPDATE dependencies SET dependency_type = 'runtime', extra_name = NULL "
        f"WHERE dependency_type = 'optional' AND NOT coalesce({_HAS_EXTRA_CONDITION}, false)"
    )
    # Rows guarded by `extra == "x"` belong to the parent's extra; normalize like packaging.utils.canonicalize_name.
    op.execute(
        f"""
        UPDATE dependencies AS d
        SET dependency_type = 'optional',
            extra_name = (
                SELECT lower(regexp_replace(c->>'value', '[-_.]+', '-', 'g'))
                FROM jsonb_array_elements(d.environment_markers->'conditions') WITH ORDINALITY AS t(c, n)
                WHERE c->>'marker' = 'extra' AND c->>'op' = '=='
                ORDER BY n
                LIMIT 1
            )
        WHERE d.dependency_type IN ('runtime', 'optional') AND d.{_HAS_EXTRA_CONDITION}
        """
    )


def downgrade() -> None:
    # The previous values were wrong (requested extras stored as the parent's extra); nothing to restore.
    pass
