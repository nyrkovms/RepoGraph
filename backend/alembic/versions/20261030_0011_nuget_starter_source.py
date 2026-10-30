"""NuGet starter source: nuget.org and the default profile of the nuget ecosystem (IMPL R4, E4 I6)

Each stage creates the starter source of its ecosystem together with the plugin (R4): `nuget-org` (nuget-v3, its
service index as base_url, priority 0) with the catalog in `config.catalog_url` for the change detector (SPEC §8.1),
and the default nuget profile holding it. Idempotent; the downgrade removes both while no package lives in the source.

Revision ID: 20261030_0011
Revises: 20261030_0010
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261030_0011"
down_revision: Union[str, Sequence[str], None] = "20261030_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REGISTRY_ID = "md5('registry:nuget/nuget-org')::uuid"
PROFILE_ID = "md5('profile:nuget/default')::uuid"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO registries (id, ecosystem, slug, name, base_url, protocol, config, priority, enabled, auth_type)
        VALUES ({REGISTRY_ID}, 'nuget', 'nuget-org', 'nuget.org', 'https://api.nuget.org/v3/index.json', 'nuget-v3',
                '{{"catalog_url": "https://api.nuget.org/v3/catalog0/index.json"}}'::jsonb, 0, true, 'none')
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
        VALUES ({PROFILE_ID}, 'nuget', 'default', 'NuGet', true)
        ON CONFLICT (ecosystem, slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
        SELECT p.id, r.id, 'nuget', 0 FROM source_profiles p, registries r
        WHERE p.ecosystem = 'nuget' AND p.slug = 'default' AND r.slug = 'nuget-org'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM package_registries pr JOIN registries r ON r.id = pr.registry_id
                       WHERE r.slug = 'nuget-org') THEN
                RAISE EXCEPTION 'nuget-org has packages: disable it instead of downgrading';
            END IF;
        END $$
    """)
    op.execute("DELETE FROM source_profiles WHERE ecosystem = 'nuget' AND slug = 'default'")
    op.execute("DELETE FROM sync_states WHERE registry_id IN (SELECT id FROM registries WHERE slug = 'nuget-org')")
    op.execute("DELETE FROM registries WHERE slug = 'nuget-org'")
