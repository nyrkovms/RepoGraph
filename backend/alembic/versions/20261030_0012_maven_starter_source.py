"""Maven starter source: Maven Central and the default profile of the maven ecosystem (IMPL R4, E5 J7)

Each stage creates the starter source of its ecosystem together with the plugin (R4): `maven-central` (maven2,
priority 0; Maven Central has no change feed, each GA's maven-metadata.xml is checked by ETag, SPEC §8.1), and the
default maven profile holding it. Google Maven and the Gradle Plugin Portal are examples in registries.example.yaml.
Idempotent; the downgrade removes both while no package lives in the source.

Revision ID: 20261030_0012
Revises: 20261030_0011
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261030_0012"
down_revision: Union[str, Sequence[str], None] = "20261030_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REGISTRY_ID = "md5('registry:maven/maven-central')::uuid"
PROFILE_ID = "md5('profile:maven/default')::uuid"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO registries (id, ecosystem, slug, name, base_url, protocol, config, priority, enabled, auth_type)
        VALUES ({REGISTRY_ID}, 'maven', 'maven-central', 'Maven Central', 'https://repo1.maven.org/maven2/', 'maven2',
                '{{}}'::jsonb, 0, true, 'none')
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
        VALUES ({PROFILE_ID}, 'maven', 'default', 'Maven', true)
        ON CONFLICT (ecosystem, slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
        SELECT p.id, r.id, 'maven', 0 FROM source_profiles p, registries r
        WHERE p.ecosystem = 'maven' AND p.slug = 'default' AND r.slug = 'maven-central'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM package_registries pr JOIN registries r ON r.id = pr.registry_id
                       WHERE r.slug = 'maven-central') THEN
                RAISE EXCEPTION 'maven-central has packages: disable it instead of downgrading';
            END IF;
        END $$
    """)
    op.execute("DELETE FROM source_profiles WHERE ecosystem = 'maven' AND slug = 'default'")
    op.execute("DELETE FROM sync_states WHERE registry_id IN (SELECT id FROM registries WHERE slug = 'maven-central')")
    op.execute("DELETE FROM registries WHERE slug = 'maven-central'")
