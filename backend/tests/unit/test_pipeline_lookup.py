"""The SourceLookup of a sync: parent POMs and BOMs are looked for beyond the sources of the artifact itself."""

from app.ecosystems.base import PackageRef, RegistryConfig, SourceProfile
from app.fetcher.pipeline import _ProfileLookup, plugin_for


def _registry(slug: str, routing: dict | None = None) -> RegistryConfig:
    return RegistryConfig(id=f"id-{slug}", slug=slug, ecosystem="maven", protocol="maven2",
                          base_url=f"https://{slug}.example/", priority=0, config={}, routing=routing,
                          auth_type="none", credentials_ref=None)


def test_a_parent_is_looked_for_in_the_profile_sources_its_group_routes_to() -> None:
    google = _registry("google", {"mode": "allow", "group_patterns": ["androidx.*"]})
    central = _registry("central")
    profile = SourceProfile(ecosystem="maven", slug="default", registries=[google, central])
    lookup = _ProfileLookup(plugin_for("maven"), [google], profile)
    # An androidx artifact lives in Google Maven; a parent of another group comes from Central, where routing sends it.
    parent = PackageRef("maven", "org.jetbrains.kotlin", "kotlin-bom")
    assert [a.registry.slug for a in lookup.adapters_for(parent)] == ["central"]
    assert [a.registry.slug for a in lookup.adapters_for(PackageRef("maven", "androidx.core", "core"))] == \
        ["google", "central"]
    assert [a.registry.slug for a in _ProfileLookup(plugin_for("maven"), [central]).adapters_for(parent)] == ["central"]


def test_routing_keeps_a_parent_away_from_a_source_its_group_may_not_come_from() -> None:
    internal = _registry("internal", {"mode": "exclusive", "group_patterns": ["com.corp.*"]})
    central = _registry("central")
    profile = SourceProfile(ecosystem="maven", slug="default", registries=[internal, central])
    lookup = _ProfileLookup(plugin_for("maven"), [central], profile)
    # A com.corp parent comes only from the internal repository, even for an artifact synced from Central.
    assert [a.registry.slug for a in lookup.adapters_for(PackageRef("maven", "com.corp", "parent"))] == ["internal"]
