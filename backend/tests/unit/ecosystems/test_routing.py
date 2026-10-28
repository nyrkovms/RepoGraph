import pytest

from app.ecosystems.base import PackageRef, RegistryConfig, SourceProfile
from app.ecosystems.pypi.plugin import PLUGIN
from app.ecosystems.routing import Rule, is_priority, parse_rule, route


def _registry(slug: str, routing: dict | None = None) -> RegistryConfig:
    return RegistryConfig(
        id=slug, slug=slug, ecosystem="pypi", protocol="pypi-warehouse", base_url=f"https://{slug}.invalid/",
        priority=100, config={}, routing=routing, auth_type="none", credentials_ref=None,
    )


def _ref(name: str) -> PackageRef:
    return PLUGIN.coordinates.parse(name)


def _slugs(name: str, *registries: RegistryConfig) -> list[str]:
    profile = SourceProfile(ecosystem="pypi", slug="test", registries=list(registries))
    return [r.slug for r in PLUGIN.sources_for(_ref(name), profile)]


PYPI = _registry("pypi")
CUDA = _registry("cu121", {"mode": "exclusive", "packages": ["torch", "torchvision"]})
CORP = _registry("corp", {"mode": "priority"})


def test_parse_rule() -> None:
    assert parse_rule(None, "packages") == Rule(None, ())
    assert parse_rule({}, "packages") == Rule(None, ())
    assert parse_rule({"packages": ["a"]}, "packages") == Rule("allow", ("a",))      # allow is the default
    assert parse_rule({"mode": "priority"}, "packages") == Rule("priority", ())
    assert parse_rule({"mode": "block", "packages": ["a"]}, "packages") == Rule("block", ("a",))


@pytest.mark.parametrize("routing", [
    {"mode": "deny", "packages": ["a"]},
    {"mode": "exclusive"},                     # nothing to pin
    {"mode": "allow", "packages": []},
    {"packages": "torch"},                     # not a list
    {"pakages": ["torch"]},                    # misspelt key: would otherwise serve everything
])
def test_parse_rule_rejects_bad_config(routing: dict) -> None:
    with pytest.raises(ValueError):
        parse_rule(routing, "packages")


def test_no_rule_serves_everything_in_profile_order() -> None:
    mirror = _registry("mirror")
    assert _slugs("requests", mirror, PYPI) == ["mirror", "pypi"]


def test_allow_serves_only_matching_packages() -> None:
    nvidia = _registry("nvidia", {"packages": ["tensorrt"]})
    assert _slugs("tensorrt", PYPI, nvidia) == ["pypi", "nvidia"]
    assert _slugs("requests", PYPI, nvidia) == ["pypi"]


def test_block_never_serves_matching_packages() -> None:
    mirror = _registry("mirror", {"mode": "block", "packages": ["internal-tool"]})
    assert _slugs("internal-tool", mirror, PYPI) == ["pypi"]
    assert _slugs("requests", mirror, PYPI) == ["mirror", "pypi"]


def test_exclusive_match_drops_every_other_source() -> None:
    assert _slugs("torch", PYPI, CUDA) == ["cu121"]
    # PEP 503 names: the rule and the package compare normalized.
    assert _slugs("TorchVision", PYPI, CUDA) == ["cu121"]


def test_exclusive_source_does_not_serve_other_packages() -> None:
    assert _slugs("numpy", PYPI, CUDA) == ["pypi"]


def test_exclusive_wins_over_a_blocking_or_priority_source() -> None:
    blocked = _registry("mirror", {"mode": "block", "packages": ["numpy"]})
    assert _slugs("torch", CORP, blocked, PYPI, CUDA) == ["cu121"]


def test_two_exclusive_sources_keep_profile_order() -> None:
    cu124 = _registry("cu124", {"mode": "exclusive", "packages": ["torch"]})
    assert _slugs("torch", cu124, PYPI, CUDA) == ["cu124", "cu121"]


def test_priority_without_patterns_serves_everything_and_is_flagged() -> None:
    assert _slugs("requests", PYPI, CORP) == ["pypi", "corp"]
    assert is_priority(CORP) and not is_priority(PYPI) and not is_priority(CUDA)


def test_priority_with_patterns_serves_only_matching_packages() -> None:
    corp = _registry("corp", {"mode": "priority", "packages": ["corp-lib"]})
    assert _slugs("corp-lib", PYPI, corp) == ["pypi", "corp"]
    assert _slugs("requests", PYPI, corp) == ["pypi"]


def test_profile_routing_override_is_what_counts() -> None:
    # Profiles pass routing overrides through RegistryConfig.routing (sources.registry_config).
    pinned_pypi = _registry("pypi", {"mode": "exclusive", "packages": ["torch"]})
    assert _slugs("torch", pinned_pypi, _registry("cu121")) == ["pypi"]


def test_route_with_another_patterns_key() -> None:
    corp = _registry("corp", {"mode": "exclusive", "scopes": ["corp"]})
    npm = _registry("npmjs")
    ref = PackageRef(ecosystem="npm", namespace="corp", name="ui")
    assert [r.slug for r in route(ref, [npm, corp], "scopes", lambda r, scope: r.namespace == scope)] == ["corp"]
