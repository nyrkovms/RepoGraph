"""npm starter source: registry.npmjs.org and the default profile of the npm ecosystem (IMPL R4, E1 F6)

Each stage creates the starter source of its ecosystem together with the plugin (R4): `npmjs` (npm-registry, priority 0)
with the replication feed in `config.changes_url` for the `_changes` detector (SPEC §8.2), and the default npm profile
holding it. Idempotent; the downgrade removes both while no package lives in the source.

Revision ID: 20261029_0008
Revises: 20261029_0007
Create Date: 2026-10-29
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261029_0008"
down_revision: Union[str, Sequence[str], None] = "20261029_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REGISTRY_ID = "md5('registry:npm/npmjs')::uuid"
PROFILE_ID = "md5('profile:npm/default')::uuid"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO registries (id, ecosystem, slug, name, base_url, protocol, config, priority, enabled, auth_type)
        VALUES ({REGISTRY_ID}, 'npm', 'npmjs', 'npm registry', 'https://registry.npmjs.org/', 'npm-registry',
                '{{"changes_url": "https://replicate.npmjs.com/registry/"}}'::jsonb, 0, true, 'none')
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
        VALUES ({PROFILE_ID}, 'npm', 'default', 'npm', true)
        ON CONFLICT (ecosystem, slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
        SELECT p.id, r.id, 'npm', 0 FROM source_profiles p, registries r
        WHERE p.ecosystem = 'npm' AND p.slug = 'default' AND r.slug = 'npmjs'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM package_registries pr JOIN registries r ON r.id = pr.registry_id
                       WHERE r.slug = 'npmjs') THEN
                RAISE EXCEPTION 'npmjs has packages: disable it instead of downgrading';
            END IF;
        END $$
    """)
    op.execute("DELETE FROM source_profiles WHERE ecosystem = 'npm' AND slug = 'default'")
    op.execute("DELETE FROM sync_states WHERE registry_id IN (SELECT id FROM registries WHERE slug = 'npmjs')")
    op.execute("DELETE FROM registries WHERE slug = 'npmjs'")
