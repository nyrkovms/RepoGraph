"""RPM default profile (IMPL E6 K7, R4, SPEC §6.3)

RPM has no starter source: repositories are added by hand (SPEC §6.3, Q16), one per repository of one release
(§6.4); `registries.rpm.yaml` holds Rocky, Alma, EPEL and Fedora examples. The migration only creates the default rpm
profile they are added to. Idempotent; the downgrade removes the profile while no source is in it.

Revision ID: 20261031_0013
Revises: 20261030_0012
Create Date: 2026-10-31
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261031_0013"
down_revision: Union[str, Sequence[str], None] = "20261030_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROFILE_ID = "md5('profile:rpm/default')::uuid"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
        VALUES ({PROFILE_ID}, 'rpm', 'default', 'RPM', true)
        ON CONFLICT (ecosystem, slug) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM source_profile_registries m JOIN source_profiles p ON p.id = m.profile_id
                       WHERE p.ecosystem = 'rpm' AND p.slug = 'default') THEN
                RAISE EXCEPTION 'the default rpm profile has sources: remove them before downgrading';
            END IF;
        END $$
    """)
    op.execute("DELETE FROM source_profiles WHERE ecosystem = 'rpm' AND slug = 'default'")
