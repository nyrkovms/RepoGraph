"""npm coordinates (WP F2): names, purls in both directions, the `scopes` routing matcher."""

import pytest

from app.ecosystems.base import PackageRef, RegistryConfig
from app.ecosystems.npm.coordinates import NpmCoordinates, split_name
from app.ecosystems.npm.routing import scope_matches
from app.ecosystems.routing import route

COORDINATES = NpmCoordinates()


def _ref(namespace: str, name: str) -> PackageRef:
    return PackageRef(ecosystem="npm", namespace=namespace, name=name)


# --- names ----------------------------------------------------------------------------------------------------


def test_parse_scoped_and_unscoped_names() -> None:
    assert COORDINATES.parse("@angular/core") == _ref("angular", "core")
    assert COORDINATES.parse("left-pad") == _ref("", "left-pad")
    assert COORDINATES.parse("  react-dom \n") == _ref("", "react-dom")
    assert COORDINATES.parse("lodash.merge") == _ref("", "lodash.merge")
    assert COORDINATES.parse("@types/node") == _ref("types", "node")
    assert COORDINATES.parse("@_internal/_tool") == _ref("_internal", "_tool")   # the leading rule is for the name only


def test_display_is_the_native_spelling() -> None:
    assert COORDINATES.display(_ref("angular", "core")) == "@angular/core"
    assert COORDINATES.display(_ref("", "left-pad")) == "left-pad"
    for raw in ("@angular/core", "left-pad", "@scope/a.b_c-d"):
        assert COORDINATES.display(COORDINATES.parse(raw)) == raw


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "Express",                    # capitals: not valid for new packages, and npm names are lowercase
    "@Angular/core",
    "@angular/Core",
    ".hidden",
    "_private",
    "has space",
    "tab\tname",
    "a/b",                        # a slash is only for the scope
    "@angular",                   # scope without a name
    "@angular/",
    "@/core",
    "@/",
    "@a/b/c",
    "@@a/b",
    "left pad",
    "naïve",
    "100%",
    "a#b",
    "a?b",
    "a:b",
    "node_modules",
    "favicon.ico",
    "a" * 215,
    "@" + "a" * 100 + "/" + "b" * 114,   # 1 + 100 + 1 + 114 = 216 with the scope and the slash
])
def test_parse_rejects_invalid_names(raw: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.parse(raw)


def test_length_limit_counts_the_scope() -> None:
    assert COORDINATES.parse("a" * 214).name == "a" * 214
    scoped = "@" + "a" * 100 + "/" + "b" * 112   # 214 in all
    assert len(scoped) == 214 and COORDINATES.parse(scoped).name == "b" * 112
    with pytest.raises(ValueError, match="214"):
        COORDINATES.parse(scoped + "b")


def test_url_safe_punctuation_that_npm_allows() -> None:
    for raw in ("a.b", "a_b", "a-b", "a~b", "a1", "1a", "a!b", "a'b", "a(b)", "a*b"):
        assert COORDINATES.parse(raw).name == raw


def test_core_module_names_are_accepted() -> None:
    # The registry holds `http`, `util`, `fs`...: a catalog of what exists must be able to name them.
    assert COORDINATES.parse("util") == _ref("", "util")


def test_split_name_checks_only_the_shape() -> None:
    assert split_name("JSONStream") == ("", "JSONStream")   # a legacy name with capitals
    assert split_name("@Scope/Name") == ("Scope", "Name")
    for raw in ("", "a/b", "@a", "@a/", "@/b", "@a/b/c"):
        with pytest.raises(ValueError):
            split_name(raw)


# --- purl -----------------------------------------------------------------------------------------------------


def test_to_purl_encodes_the_at_of_the_scope() -> None:
    assert COORDINATES.to_purl(_ref("angular", "core")) == "pkg:npm/%40angular/core"
    assert COORDINATES.to_purl(_ref("angular", "core"), "17.3.12") == "pkg:npm/%40angular/core@17.3.12"
    assert COORDINATES.to_purl(_ref("", "left-pad")) == "pkg:npm/left-pad"
    assert COORDINATES.to_purl(_ref("", "left-pad"), "1.3.0") == "pkg:npm/left-pad@1.3.0"
    assert COORDINATES.to_purl(_ref("", "react"), "19.0.0-rc.1+build.5") == "pkg:npm/react@19.0.0-rc.1%2Bbuild.5"


@pytest.mark.parametrize(("purl", "ref", "version"), [
    ("pkg:npm/%40angular/core", _ref("angular", "core"), None),
    ("pkg:npm/%40angular/core@17.3.12", _ref("angular", "core"), "17.3.12"),
    ("pkg:npm/@angular/core", _ref("angular", "core"), None),      # the unencoded @ is accepted
    ("pkg:npm/@angular/core@17.3.12", _ref("angular", "core"), "17.3.12"),
    ("pkg:npm/left-pad", _ref("", "left-pad"), None),
    ("pkg:npm/left-pad@1.3.0", _ref("", "left-pad"), "1.3.0"),
    ("pkg:npm/react@19.0.0-rc.1%2Bbuild.5", _ref("", "react"), "19.0.0-rc.1+build.5"),
    ("PKG:NPM/left-pad", _ref("", "left-pad"), None),
    ("pkg:npm/left-pad@1.3.0?repository_url=https:%2F%2Fnpm.corp.example#sub", _ref("", "left-pad"), "1.3.0"),
])
def test_from_purl(purl: str, ref: PackageRef | None, version: str | None) -> None:
    if ref is None:
        with pytest.raises(ValueError):
            COORDINATES.from_purl(purl)
    else:
        assert COORDINATES.from_purl(purl) == (ref, version)


@pytest.mark.parametrize("purl", [
    "pkg:pypi/left-pad",                 # another type
    "pkg:maven/%40angular/core",
    "pkg:npm/angular/core",              # a namespace is a scope, which has its @
    "pkg:npm/%40angular/core/extra",     # one scope only
    "pkg:npm/%40angular",                # the scope without a name is the name `@angular`: not a name
    "pkg:npm/@/core",
    "pkg:npm/foo%2Fbar",                 # a slash inside the name
    "pkg:npm/Express",
    "pkg:npm/%40Angular/core",
    "pkg:npm/",
    "npm/left-pad",
    "not a purl",
])
def test_from_purl_rejects(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


@pytest.mark.parametrize("raw", ["@angular/core", "left-pad", "@types/node", "@a/b.c", "lodash.merge"])
@pytest.mark.parametrize("version", [None, "1.0.0", "2.0.0-beta.1+sha.5114f85"])
def test_purl_round_trip(raw: str, version: str | None) -> None:
    ref = COORDINATES.parse(raw)
    assert COORDINATES.from_purl(COORDINATES.to_purl(ref, version)) == (ref, version)


# --- scopes routing -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("pattern", "ref", "expected"), [
    ("corp", _ref("corp", "ui"), True),
    ("@corp", _ref("corp", "ui"), True),
    ("@corp/*", _ref("corp", "ui"), True),
    ("corp", _ref("corp", "tool"), True),
    ("corp", _ref("corp-labs", "ui"), False),          # a scope matches as a whole
    ("corp", _ref("other", "corp"), False),
    ("corp", _ref("", "corp"), False),                   # unscoped `corp` is not in the scope `@corp`
    ("@corp/tool", _ref("corp", "tool"), True),
    ("corp/tool", _ref("corp", "tool"), True),
    ("@corp/tool", _ref("corp", "tool2"), False),        # a package pattern is that one package
    ("@corp/tool", _ref("corp", "ui"), False),
    ("@corp/tool", _ref("other", "tool"), False),
    ("@corp/tool", _ref("", "tool"), False),
    ("@corp/*", _ref("corp-labs", "ui"), False),
    ("  @corp  ", _ref("corp", "ui"), True),
    ("", _ref("corp", "ui"), False),
    ("@", _ref("corp", "ui"), False),
    ("@/ui", _ref("corp", "ui"), False),
    ("@corp/", _ref("corp", "ui"), False),
    ("Corp", _ref("corp", "ui"), False),                 # names are lowercase, patterns are not folded
])
def test_scope_matches(pattern: str, ref: PackageRef, expected: bool) -> None:
    assert scope_matches(ref, pattern) is expected


def _registry(slug: str, routing: dict | None = None) -> RegistryConfig:
    return RegistryConfig(
        id=slug, slug=slug, ecosystem="npm", protocol="npm-registry", base_url=f"https://{slug}.invalid/",
        priority=100, config={}, routing=routing, auth_type="none", credentials_ref=None,
    )


def test_scopes_work_as_the_routing_patterns_of_a_registry() -> None:
    npmjs = _registry("npmjs")
    corp = _registry("corp", {"mode": "exclusive", "scopes": ["corp", "@labs/tool"]})
    registries = [npmjs, corp]

    def slugs(raw: str) -> list[str]:
        return [r.slug for r in route(COORDINATES.parse(raw), registries, "scopes", scope_matches)]

    assert slugs("@corp/ui") == ["corp"]            # exclusive: only the corporate registry
    assert slugs("@labs/tool") == ["corp"]
    assert slugs("@labs/other") == ["npmjs"]
    assert slugs("react") == ["npmjs"]
