"""Contract suite run for every registered plugin (SPEC-MULTI-ECOSYSTEM.md §14.1)."""

import pytest

from app.ecosystems.base import InvalidVersion
from app.ecosystems.registry import load_builtin_plugins
from tests.unit.ecosystems.contract_samples import SAMPLES

PLUGINS = load_builtin_plugins()
IDS = [plugin.ecosystem for plugin in PLUGINS]


@pytest.mark.parametrize("plugin", PLUGINS, ids=IDS)
def test_plugin_has_samples(plugin) -> None:
    assert plugin.ecosystem in SAMPLES, f"add contract samples for {plugin.ecosystem}"


@pytest.mark.parametrize("plugin", PLUGINS, ids=IDS)
def test_purl_round_trip(plugin) -> None:
    for native, purl in SAMPLES[plugin.ecosystem].names:
        ref = plugin.coordinates.parse(native)
        assert plugin.coordinates.to_purl(ref) == purl
        assert plugin.coordinates.from_purl(purl) == (ref, None)


@pytest.mark.parametrize("plugin", PLUGINS, ids=IDS)
def test_sort_key_follows_ecosystem_order(plugin) -> None:
    ordered = SAMPLES[plugin.ecosystem].ordered_versions
    keys = [plugin.versions.sort_key(version) for version in ordered]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("plugin", PLUGINS, ids=IDS)
def test_invalid_versions_are_rejected(plugin) -> None:
    for raw in SAMPLES[plugin.ecosystem].invalid_versions:
        with pytest.raises(InvalidVersion):
            plugin.versions.validate(raw)


@pytest.mark.parametrize("plugin", PLUGINS, ids=IDS)
def test_prerelease_detection(plugin) -> None:
    samples = SAMPLES[plugin.ecosystem]
    for raw in samples.ordered_versions:
        assert plugin.versions.is_prerelease(raw) == (raw in samples.prereleases)
