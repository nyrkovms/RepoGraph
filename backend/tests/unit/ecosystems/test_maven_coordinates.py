"""Maven coordinates (WP J2): GA identity, validation, purls and repository paths, the `group_patterns` matcher."""

import pytest

from app.ecosystems.base import PackageRef, RegistryConfig
from app.ecosystems.maven.coordinates import (
    MavenCoordinates,
    file_path,
    group_path,
    metadata_path,
    pom_path,
    purl_qualifiers,
)
from app.ecosystems.maven.routing import group_matches
from app.ecosystems.routing import route

COORDINATES = MavenCoordinates()


def _ref(group: str, artifact: str) -> PackageRef:
    return PackageRef(ecosystem="maven", namespace=group, name=artifact)


SPRING = _ref("org.springframework", "spring-core")


def test_parse_and_display_round_trip() -> None:
    assert COORDINATES.parse("org.springframework:spring-core") == SPRING
    assert COORDINATES.parse("  org.springframework:spring-core\n") == SPRING
    assert COORDINATES.display(SPRING) == "org.springframework:spring-core"
    assert COORDINATES.parse(COORDINATES.display(SPRING)) == SPRING


def test_case_matters() -> None:
    assert COORDINATES.parse("Org.Foo:Bar") == _ref("Org.Foo", "Bar")
    assert COORDINATES.parse("Org.Foo:Bar") != COORDINATES.parse("org.foo:bar")
    assert COORDINATES.display(_ref("Org.Foo", "Bar")) == "Org.Foo:Bar"


@pytest.mark.parametrize("raw", [
    "a:b", "com.google.guava:guava", "io.netty:netty-all", "org.apache.logging.log4j:log4j-api", "a_b:c_d",
    "a-b.c:d.e-f", "javax.servlet:javax.servlet-api", "1:2", "a:.b", "a:b.",
])
def test_valid_coordinates(raw: str) -> None:
    group, artifact = raw.split(":")
    assert COORDINATES.parse(raw) == _ref(group, artifact)


@pytest.mark.parametrize("raw", [
    "",                                 # nothing
    "spring-core",                      # no group
    "org.springframework:",             # empty artifactId
    ":spring-core",                     # empty groupId
    ":",
    "org.springframework:spring-core:6.1.0",     # a version is not identity
    "org.springframework:spring-core:jar:6.1.0",
    ".org:a", "org.:a",                 # leading / trailing dot in groupId
    "org..springframework:a", "org:a..b",        # '..' in either part
    "..:a", "a:..", "a:.",
    "or g:a", "org:a b", "org/foo:a", "org:a/b", "org\\foo:a", "org:a\\b",
    "org@foo:a", "org:a@1", "org:a+b", "org:a*", "org,x:a", "org:a;b", "org:a\tb", "org:a\nb:c",
    "ünï.cøde:a", "org:ärtifact",
])
def test_invalid_coordinates(raw: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.parse(raw)


def test_group_dot_rules_do_not_apply_to_artifact_ids() -> None:
    # Only groupId must not start or end with a dot (task rule 2); artifactIds like `.b` are odd but legal in Maven.
    assert COORDINATES.parse("a:.b").name == ".b"
    assert COORDINATES.parse("a:b.").name == "b."


@pytest.mark.parametrize(("purl", "ref", "version"), [
    ("pkg:maven/org.springframework/spring-core", SPRING, None),
    ("pkg:maven/org.springframework/spring-core@6.1.4", SPRING, "6.1.4"),
    ("pkg:maven/com.google.guava/guava@33.0.0-jre", _ref("com.google.guava", "guava"), "33.0.0-jre"),
    ("pkg:maven/Org.Foo/Bar@1.0", _ref("Org.Foo", "Bar"), "1.0"),
])
def test_purls_in_both_directions(purl: str, ref: PackageRef, version: str | None) -> None:
    assert COORDINATES.from_purl(purl) == (ref, version)
    assert COORDINATES.to_purl(ref, version) == purl


def test_to_purl_has_no_qualifiers() -> None:
    assert "?" not in COORDINATES.to_purl(SPRING, "6.1.4")


def test_from_purl_ignores_qualifiers_for_identity() -> None:
    purl = "pkg:maven/org.apache.maven/maven-bom@3.9.6?type=pom&classifier=sources&repository_url=https://repo.example/"
    assert COORDINATES.from_purl(purl) == (_ref("org.apache.maven", "maven-bom"), "3.9.6")
    assert COORDINATES.from_purl("pkg:maven/org.springframework/spring-core@6.1.4?type=jar") == (SPRING, "6.1.4")


def test_purl_qualifiers_reads_type_and_classifier() -> None:
    assert purl_qualifiers("pkg:maven/g/a@1?type=pom") == {"type": "pom"}
    assert purl_qualifiers("pkg:maven/g/a@1?classifier=sources&type=jar") == {"type": "jar", "classifier": "sources"}
    assert purl_qualifiers("pkg:maven/g/a@1?classifier=tests") == {"classifier": "tests"}
    assert purl_qualifiers("pkg:maven/g/a@1") == {}
    assert purl_qualifiers("pkg:maven/g/a@1?repository_url=https://x.invalid/") == {}
    assert purl_qualifiers("pkg:maven/g/a@1?type=") == {}      # the purl grammar drops empty values


@pytest.mark.parametrize("purl", [
    "pkg:npm/lodash@4.17.21",                   # other type
    "pkg:pypi/flask",
    "pkg:maven/spring-core",                    # no namespace
    "pkg:maven/spring-core@1.0",
    "pkg:maven/org.springframework/spring-core/extra",     # two namespace segments: not a groupId
    "pkg:maven/.org/a", "pkg:maven/org./a", "pkg:maven/org..x/a",
    "pkg:maven/org/",                           # no name
    "pkg:maven/",
    "org.springframework:spring-core",          # not a purl
])
def test_purls_that_are_not_maven_coordinates(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


def test_purl_qualifiers_rejects_other_types() -> None:
    with pytest.raises(ValueError, match="not a maven purl"):
        purl_qualifiers("pkg:npm/lodash?type=pom")
    with pytest.raises(ValueError):
        purl_qualifiers("not a purl")


def test_repository_paths() -> None:
    assert group_path(SPRING) == "org/springframework"
    assert group_path(_ref("junit", "junit")) == "junit"
    assert metadata_path(SPRING) == "org/springframework/spring-core/maven-metadata.xml"
    assert pom_path(SPRING, "6.1.4") == "org/springframework/spring-core/6.1.4/spring-core-6.1.4.pom"
    assert file_path(SPRING, "6.1.4", "jar") == "org/springframework/spring-core/6.1.4/spring-core-6.1.4.jar"
    assert (
        file_path(SPRING, "6.1.4", "jar", "sources")
        == "org/springframework/spring-core/6.1.4/spring-core-6.1.4-sources.jar"
    )
    assert file_path(SPRING, "6.1.4", "module") == "org/springframework/spring-core/6.1.4/spring-core-6.1.4.module"
    assert file_path(SPRING, "6.1.4", "jar", None) == file_path(SPRING, "6.1.4", "jar")
    assert file_path(SPRING, "6.1.4", "pom.sha1") == pom_path(SPRING, "6.1.4") + ".sha1"


def test_snapshot_style_and_dotted_artifact_versions_are_kept_verbatim() -> None:
    assert pom_path(_ref("g", "a.b"), "1.0-SNAPSHOT") == "g/a.b/1.0-SNAPSHOT/a.b-1.0-SNAPSHOT.pom"


@pytest.mark.parametrize(("version", "extension", "classifier"), [
    ("", "jar", None), ("../1", "jar", None), ("1/2", "jar", None), ("1\\2", "jar", None), (" 1", "jar", None),
    ("1", "", None), ("1", "../jar", None), ("1", "a/b", None),
    ("1", "jar", "../x"), ("1", "jar", "a/b"),
])
def test_paths_reject_values_that_would_leave_the_artifact_directory(
    version: str, extension: str, classifier: str | None
) -> None:
    with pytest.raises(ValueError):
        file_path(SPRING, version, extension, classifier)


@pytest.mark.parametrize(("pattern", "group", "expected"), [
    ("com.android.tools", "com.android.tools", True),            # exact
    ("com.android.tools", "com.android.tools.build", False),     # exact is not a prefix
    ("com.android.tools", "com.android", False),
    ("com.android.tools", "com.android.toolsx", False),
    ("androidx.*", "androidx", True),                            # the group itself
    ("androidx.*", "androidx.core", True),
    ("androidx.*", "androidx.compose.ui", True),
    ("androidx.*", "androidxyz", False),
    ("androidx.*", "androidx-foo", False),
    ("androidx.*", "com.androidx", False),
    ("com.google.android.*", "com.google.android.gms", True),
    ("com.google.android.*", "com.google", False),
    ("com.google.android.*", "com.google.guava", False),
    ("*", "anything.at.all", True),
    ("*", "x", True),
    ("  androidx.*  ", "androidx.core", True),
    ("androidx.*", "AndroidX.core", False),                      # case matters
    ("Org.Foo", "org.foo", False),
    ("androidx*", "androidx.core", False),                       # only `.*` and `*` are wildcards
    ("androidx.core.*", "androidx", False),
    ("", "androidx", False),
    (".*", "androidx", False),
    ("*.core", "androidx.core", False),
    ("androidx.**", "androidx.core", False),
    ("androidx.*", "", False),                                   # no group (not a Maven package)
    ("*", "", False),
])
def test_group_matches(pattern: str, group: str, expected: bool) -> None:
    assert group_matches(_ref(group, "artifact"), pattern) is expected


def _registry(slug: str, routing: dict | None = None) -> RegistryConfig:
    return RegistryConfig(
        id=slug, slug=slug, ecosystem="maven", protocol="maven2", base_url=f"https://{slug}.invalid/",
        priority=100, config={}, routing=routing, auth_type="none", credentials_ref=None,
    )


GOOGLE = {"mode": "allow", "group_patterns": ["androidx.*", "com.android.*", "com.google.android.*"]}


def _slugs(raw: str, registries: list[RegistryConfig]) -> list[str]:
    ref = COORDINATES.parse(raw)
    return [r.slug for r in route(ref, registries, "group_patterns", group_matches)]


def test_group_patterns_route_like_google_maven() -> None:
    central = _registry("central")
    google = _registry("google", GOOGLE)
    registries = [central, google]

    assert _slugs("androidx.core:core-ktx", registries) == ["central", "google"]
    assert _slugs("androidx:annotation", registries) == ["central", "google"]
    assert _slugs("com.android.tools.build:gradle", registries) == ["central", "google"]
    assert _slugs("com.google.android.material:material", registries) == ["central", "google"]
    assert _slugs("com.google.guava:guava", registries) == ["central"]
    assert _slugs("com.google.android:android", registries) == ["central", "google"]
    assert _slugs("org.springframework:spring-core", registries) == ["central"]
    assert _slugs("androidxyz:a", registries) == ["central"]


def test_exclusive_and_block_group_patterns() -> None:
    central = _registry("central", {"mode": "block", "group_patterns": ["com.corp.*"]})
    corp = _registry("corp", {"mode": "exclusive", "group_patterns": ["com.corp.*", "org.fork"]})
    registries = [central, corp]

    assert _slugs("com.corp.tools:x", registries) == ["corp"]
    assert _slugs("org.fork:lib", registries) == ["corp"]
    assert _slugs("org.fork.sub:lib", registries) == ["central"]
    assert _slugs("org.springframework:spring-core", registries) == ["central"]


def test_wildcard_pattern_serves_every_package() -> None:
    registries = [_registry("all", {"mode": "allow", "group_patterns": ["*"]}), _registry("none", {"mode": "block", "group_patterns": ["*"]})]
    assert _slugs("org.springframework:spring-core", registries) == ["all"]
