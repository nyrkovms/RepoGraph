"""The SourceLookup of a sync: parent POMs and BOMs are looked for beyond the sources of the artifact itself."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.ecosystems.base import PackageRef, RegistryConfig, SourceProfile, VersionDependencies
from app.fetcher import pipeline
from app.fetcher.pipeline import _ProfileLookup, plugin_for
from app.models.enums import DependenciesState, Ecosystem


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


def test_a_lazy_load_looks_up_parents_in_the_profile_of_the_reader(monkeypatch) -> None:
    """Regression (#28): a version loaded during a resolve read its parent POMs through the default profile."""
    central = _registry("central")
    internal = _registry("internal")
    asked = SourceProfile(ecosystem="maven", slug="corp", registries=[central, internal])
    default = SourceProfile(ecosystem="maven", slug="default", registries=[central])
    lookups: list[_ProfileLookup] = []

    class Adapter:
        def __init__(self, registry: RegistryConfig) -> None:
            self.registry = registry

        async def fetch_version_dependencies(self, ref, version, lookup) -> VersionDependencies:
            lookups.append(lookup)
            return VersionDependencies(dependencies=[], available=False)

    plugin = plugin_for("maven")
    monkeypatch.setattr(type(plugin), "adapter", lambda self, registry: Adapter(registry))
    monkeypatch.setattr(pipeline, "_dependencies_source", AsyncMock(return_value=SimpleNamespace(id="id-central")))
    monkeypatch.setattr(pipeline.sources, "registry_config", lambda row: central)
    monkeypatch.setattr(pipeline.sources, "default_profile", AsyncMock(return_value=default))
    monkeypatch.setattr(pipeline, "store_documents", AsyncMock())
    package = SimpleNamespace(ecosystem=Ecosystem.maven, namespace="com.corp", normalized_name="app")

    async def load(profile: SourceProfile | None) -> _ProfileLookup:
        version_row = SimpleNamespace(dependencies_state=DependenciesState.pending, version="1.0",
                                      metadata_registry_id=None, features=None)
        await pipeline.ensure_version_dependencies(AsyncMock(), package, version_row, profile)
        return lookups[-1]

    # The resolve's profile reaches the lookup, so a parent only in `internal` is found; without one, the default.
    assert [a.registry.slug for a in asyncio.run(load(asked)).adapters_for(PackageRef("maven", "com.corp", "parent"))] \
        == ["central", "internal"]
    assert [a.registry.slug for a in asyncio.run(load(None)).adapters_for(PackageRef("maven", "com.corp", "parent"))] \
        == ["central"]


def test_the_resolve_catalog_hands_its_profile_to_lazy_loads(monkeypatch) -> None:
    from app.resolver import catalog as catalog_module

    profile = SourceProfile(ecosystem="maven", slug="corp", registries=[_registry("central")])
    seen: list[object] = []

    async def source_dependencies(db, package, version_row, registry_id, profile=None):
        seen.append(profile)
        return [], False

    monkeypatch.setattr(catalog_module, "find_package", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(catalog_module, "source_dependencies", source_dependencies)
    db = AsyncMock()
    db.scalar.return_value = SimpleNamespace(id=2)
    catalog = catalog_module.DbCatalog(db, plugin_for("maven"), profile)
    assert asyncio.run(catalog.dependencies(PackageRef("maven", "com.corp", "app"), "1.0", "id-central")) == []
    assert seen == [profile]
