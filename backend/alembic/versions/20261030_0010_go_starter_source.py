"""Go starter source: proxy.golang.org and the default profile of the go ecosystem (IMPL R4, E3 H6)

Each stage creates the starter source of its ecosystem together with the plugin (R4): `proxy-golang-org` (goproxy,
priority 0) with the module index feed in `config.index_url` for the change detector (SPEC §8.1), and the default
go profile holding it. Idempotent; the downgrade removes both while no package lives in the source.

Revision ID: 20261030_0010
Revises: 20261030_0009
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261030_0010"
down_revision: Union[str, Sequence[str], None] = "20261030_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REGISTRY_ID = "md5('registry:go/proxy-golang-org')::uuid"
PROFILE_ID = "md5('profile:go/default')::uuid"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO registries (id, ecosystem, slug, name, base_url, protocol, config, priority, enabled, auth_type)
        VALUES ({REGISTRY_ID}, 'go', 'proxy-golang-org', 'Go module proxy', 'https://proxy.golang.org/', 'goproxy',
                '{{"index_url": "https://index.golang.org/index"}}'::jsonb, 0, true, 'none')
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profiles (id, ecosystem, slug, name, is_default)
        VALUES ({PROFILE_ID}, 'go', 'default', 'Go', true)
        ON CONFLICT (ecosystem, slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO source_profile_registries (profile_id, registry_id, ecosystem, position)
        SELECT p.id, r.id, 'go', 0 FROM source_profiles p, registries r
        WHERE p.ecosystem = 'go' AND p.slug = 'default' AND r.slug = 'proxy-golang-org'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM package_registries pr JOIN registries r ON r.id = pr.registry_id
                       WHERE r.slug = 'proxy-golang-org') THEN
                RAISE EXCEPTION 'proxy-golang-org has packages: disable it instead of downgrading';
            END IF;
        END $$
    """)
    op.execute("DELETE FROM source_profiles WHERE ecosystem = 'go' AND slug = 'default'")
    op.execute("DELETE FROM sync_states WHERE registry_id IN (SELECT id FROM registries WHERE slug = 'proxy-golang-org')")
    op.execute("DELETE FROM registries WHERE slug = 'proxy-golang-org'")
