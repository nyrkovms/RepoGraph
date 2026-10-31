"""RPM coordinates (WP K2): identity, validation, purls, NEVRA helpers and the `packages` matcher."""

import pytest

from app.ecosystems.base import PackageRef, RegistryConfig
from app.ecosystems.purl import parse as parse_purl
from app.ecosystems.routing import route
from app.ecosystems.rpm.coordinates import (
    RpmCoordinates,
    distro_of,
    nevra,
    package_matches,
    parse_nevra,
    purl_qualifiers,
    vendor_of,
)

COORDINATES = RpmCoordinates()


def _ref(namespace: str, name: str) -> PackageRef:
    return PackageRef(ecosystem="rpm", namespace=namespace, name=name)


OPENSSL = _ref("rocky-9", "openssl")


def test_parse_and_display_round_trip() -> None:
    assert COORDINATES.parse("rocky-9/openssl") == OPENSSL
    assert COORDINATES.parse("  rocky-9/openssl\n") == OPENSSL
    assert COORDINATES.display(OPENSSL) == "rocky-9/openssl"
    assert COORDINATES.parse(COORDINATES.display(OPENSSL)) == OPENSSL


def test_distribution_is_part_of_identity() -> None:
    assert COORDINATES.parse("rhel-9/openssl") != COORDINATES.parse("rocky-9/openssl")


def test_names_are_case_sensitive_and_kept_as_given() -> None:
    assert COORDINATES.parse("rocky-9/NetworkManager") == _ref("rocky-9", "NetworkManager")
    assert COORDINATES.parse("rocky-9/NetworkManager") != COORDINATES.parse("rocky-9/networkmanager")
    assert COORDINATES.display(_ref("rocky-9", "NetworkManager")) == "rocky-9/NetworkManager"


@pytest.mark.parametrize("raw", [
    "rocky-9/openssl", "rhel-9.4/openssl", "fedora-41/kernel", "epel-9/htop", "alma-8/bash", "centos-7/glibc",
    "epel-next-9/foo", "rocky-9/python3.11", "rocky-9/libstdc++", "rocky-9/gcc-c++", "rocky-9/perl-Foo-Bar",
    "rocky-9/xorg-x11-server-Xorg", "rocky-9/_foo", "rocky-9/.foo", "rocky-9/a", "rocky-9/1",
])
def test_valid_coordinates(raw: str) -> None:
    namespace, name = raw.split("/")
    assert COORDINATES.parse(raw) == _ref(namespace, name)


@pytest.mark.parametrize("raw", [
    "",                         # nothing
    "openssl",                  # no namespace
    "rocky-9/",                 # empty name
    "/openssl",                 # empty namespace
    "/",
    "rocky-9/open ssl", "rocky-9/open\tssl", "rocky-9/open\nssl",
    "rocky-9/-openssl",         # leading '-'
    "rocky-9/open/ssl",         # '/' in the name: split on the first '/' only
    "rocky-9/openssl@1", "rocky-9/open*", "rocky-9/open:ssl", "rocky-9/open,ssl", "rocky-9/open\\ssl",
    "rocky-9/öpenssl",
    "rocky/openssl",            # no release
    "9/openssl", "-9/openssl", "rocky-/openssl", "rocky--9/openssl",
    "Rocky-9/openssl", "ROCKY-9/openssl",   # namespaces are lowercase
    "rocky_9/openssl", "rocky-9.x/openssl", "rocky-9./openssl", "rocky-.9/openssl", "rocky-9..4/openssl",
    "rocky 9/openssl", "rocky-9 /openssl",
    "rocky-9/openssl-3.0.7-27.el9.x86_64/extra",
])
def test_invalid_coordinates(raw: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.parse(raw)


def test_error_messages_say_what_is_wrong() -> None:
    with pytest.raises(ValueError, match="expected '<distro>-<release>/<package>'"):
        COORDINATES.parse("openssl")
    with pytest.raises(ValueError, match="not an RPM namespace"):
        COORDINATES.parse("rocky/openssl")
    with pytest.raises(ValueError, match="not an RPM package name"):
        COORDINATES.parse("rocky-9/-openssl")
    with pytest.raises(ValueError, match="empty"):
        COORDINATES.parse("rocky-9/")


@pytest.mark.parametrize(("namespace", "distro", "release"), [
    ("rocky-9", "rocky", "9"), ("rhel-9.4", "rhel", "9.4"), ("fedora-41", "fedora", "41"),
    ("epel-8", "epel", "8"), ("epel-next-9", "epel-next", "9"),
])
def test_distro_of(namespace: str, distro: str, release: str) -> None:
    assert distro_of(namespace) == (distro, release)


@pytest.mark.parametrize("namespace", ["rocky", "rocky-", "-9", "", "Rocky-9", "rocky-9/x"])
def test_distro_of_rejects_namespaces_without_a_release(namespace: str) -> None:
    with pytest.raises(ValueError):
        distro_of(namespace)


@pytest.mark.parametrize(("namespace", "vendor"), [
    ("rocky-9", "rocky"), ("rhel-9", "redhat"), ("rhel-9.4", "redhat"), ("alma-9", "almalinux"),
    ("fedora-41", "fedora"), ("epel-9", "fedora"), ("centos-7", "centos"), ("oracle-8", "oracle"),
    ("epel-next-9", "epel-next"),
])
def test_vendor_of(namespace: str, vendor: str) -> None:
    assert vendor_of(namespace) == vendor


@pytest.mark.parametrize(("purl", "ref", "version"), [
    ("pkg:rpm/rocky/openssl?distro=rocky-9", OPENSSL, None),
    ("pkg:rpm/rocky/openssl@1:3.0.7-27.el9?distro=rocky-9", OPENSSL, "1:3.0.7-27.el9"),
    ("pkg:rpm/redhat/openssl?distro=rhel-9", _ref("rhel-9", "openssl"), None),
    ("pkg:rpm/redhat/openssl@3.0.7-27.el9?distro=rhel-9.4", _ref("rhel-9.4", "openssl"), "3.0.7-27.el9"),
    ("pkg:rpm/almalinux/bash?distro=alma-8", _ref("alma-8", "bash"), None),
    ("pkg:rpm/fedora/kernel@6.11.4-300.fc41?distro=fedora-41", _ref("fedora-41", "kernel"), "6.11.4-300.fc41"),
    ("pkg:rpm/fedora/htop@3.3.0-1.el9?distro=epel-9", _ref("epel-9", "htop"), "3.3.0-1.el9"),
    ("pkg:rpm/centos/glibc?distro=centos-7", _ref("centos-7", "glibc"), None),
    ("pkg:rpm/rocky/NetworkManager?distro=rocky-9", _ref("rocky-9", "NetworkManager"), None),
    ("pkg:rpm/rocky/libstdc%2B%2B?distro=rocky-9", _ref("rocky-9", "libstdc++"), None),
])
def test_purls_in_both_directions(purl: str, ref: PackageRef, version: str | None) -> None:
    assert COORDINATES.from_purl(purl) == (ref, version)
    assert COORDINATES.to_purl(ref, version) == purl


def test_to_purl_has_only_the_distro_qualifier() -> None:
    parsed = parse_purl(COORDINATES.to_purl(OPENSSL, "3.0.7-27.el9"))
    assert dict(parsed.qualifiers) == {"distro": "rocky-9"}
    assert parsed.namespace == "rocky"
    assert parsed.version == "3.0.7-27.el9"


def test_from_purl_ignores_arch_and_epoch_for_identity() -> None:
    purl = "pkg:rpm/rocky/openssl@3.0.7-27.el9?arch=x86_64&distro=rocky-9&epoch=1"
    assert COORDINATES.from_purl(purl) == (OPENSSL, "3.0.7-27.el9")


def test_from_purl_does_not_check_the_vendor_against_the_distro() -> None:
    assert COORDINATES.from_purl("pkg:rpm/centos/openssl?distro=rhel-9") == (_ref("rhel-9", "openssl"), None)


def test_purl_qualifiers_reads_arch() -> None:
    assert purl_qualifiers("pkg:rpm/rocky/openssl@1?arch=x86_64&distro=rocky-9") == {"arch": "x86_64"}
    assert purl_qualifiers("pkg:rpm/rocky/openssl@1?distro=rocky-9") == {}
    assert purl_qualifiers("pkg:rpm/rocky/openssl?arch=&distro=rocky-9") == {}    # empty values are dropped
    assert purl_qualifiers("pkg:rpm/rocky/openssl?arch=noarch") == {"arch": "noarch"}


@pytest.mark.parametrize("purl", [
    "pkg:npm/lodash@4.17.21",                       # other type
    "pkg:pypi/flask?distro=rocky-9",
    "pkg:rpm/rocky/openssl",                        # no distro qualifier
    "pkg:rpm/rocky/openssl@3.0.7-27.el9",
    "pkg:rpm/rocky/openssl?arch=x86_64",
    "pkg:rpm/rocky/openssl?distro=",                # empty qualifier is dropped
    "pkg:rpm/rocky/openssl?distro=rocky",           # no release
    "pkg:rpm/rocky/openssl?distro=Rocky-9",
    "pkg:rpm/rocky/extra/openssl?distro=rocky-9",   # two vendor segments
    "pkg:rpm/rocky/-openssl?distro=rocky-9",
    "pkg:rpm/rocky/?distro=rocky-9",                # no name: the vendor would be read as one
    "pkg:rpm/openssl?distro=rocky-9",               # no vendor
    "pkg:rpm/",
    "rocky-9/openssl",                              # not a purl
])
def test_purls_that_are_not_rpm_coordinates(purl: str) -> None:
    with pytest.raises(ValueError):
        COORDINATES.from_purl(purl)


def test_purl_qualifiers_rejects_other_types() -> None:
    with pytest.raises(ValueError, match="not an rpm purl"):
        purl_qualifiers("pkg:npm/lodash?arch=x86_64")
    with pytest.raises(ValueError):
        purl_qualifiers("not a purl")


def test_to_purl_rejects_a_ref_with_a_bad_namespace() -> None:
    with pytest.raises(ValueError):
        COORDINATES.to_purl(_ref("", "openssl"))


@pytest.mark.parametrize(("args", "text"), [
    (("openssl", 1, "3.0.7", "27.el9", "x86_64"), "openssl-1:3.0.7-27.el9.x86_64"),
    (("openssl", 0, "3.0.7", "27.el9", "x86_64"), "openssl-3.0.7-27.el9.x86_64"),
    (("openssl", None, "3.0.7", "27.el9", "x86_64"), "openssl-3.0.7-27.el9.x86_64"),
    (("openssl", "0", "3.0.7", "27.el9", "x86_64"), "openssl-3.0.7-27.el9.x86_64"),
    (("openssl", "2", "3.0.7", "27.el9", "noarch"), "openssl-2:3.0.7-27.el9.noarch"),
    (("python3-libs", 0, "3.9.18", "3.el9_4.1", "aarch64"), "python3-libs-3.9.18-3.el9_4.1.aarch64"),
    (("perl-Foo-Bar", 1, "1.02~rc1", "1.fc41", "src"), "perl-Foo-Bar-1:1.02~rc1-1.fc41.src"),
    (("gcc-c++", 0, "11.4.1", "3.el9", "ppc64le"), "gcc-c++-11.4.1-3.el9.ppc64le"),
])
def test_nevra_and_parse_nevra(args: tuple[str, int | str | None, str, str, str], text: str) -> None:
    assert nevra(*args) == text
    name, epoch, version, release, arch = parse_nevra(text)
    assert (name, version, release, arch) == (args[0], args[2], args[3], args[4])
    assert epoch == int(args[1] or 0)
    assert nevra(name, epoch, version, release, arch) == text


def test_parse_nevra_splits_from_the_right() -> None:
    assert parse_nevra("xorg-x11-server-Xorg-1.20.11-24.el9.x86_64") == (
        "xorg-x11-server-Xorg", 0, "1.20.11", "24.el9", "x86_64"
    )
    assert parse_nevra("  a-b-c-d-1:2-3.noarch ") == ("a-b-c-d", 1, "2", "3", "noarch")
    assert parse_nevra("gcc-c++-0:11.4.1-3.el9.x86_64") == ("gcc-c++", 0, "11.4.1", "3.el9", "x86_64")


@pytest.mark.parametrize("text", [
    "",
    "openssl",
    "openssl-3.0.7-27.el9",             # no arch: '.el9' is not one
    "openssl-3.0.7-27.el9.sparc",       # unknown arch
    "openssl-3.0.7.x86_64",             # no release
    "openssl.x86_64",
    "3.0.7-27.el9.x86_64",              # no name
    "-3.0.7-27.el9.x86_64",
    "openssl--27.el9.x86_64",           # empty version
    "openssl-3.0.7-.x86_64",            # empty release
    "openssl-x:3.0.7-27.el9.x86_64",    # epoch is not a number
    "openssl-:3.0.7-27.el9.x86_64",
    "open ssl-3.0.7-27.el9.x86_64",
])
def test_parse_nevra_rejects_malformed_text(text: str) -> None:
    with pytest.raises(ValueError):
        parse_nevra(text)


@pytest.mark.parametrize("args", [
    ("", 0, "1", "1", "x86_64"),
    ("-a", 0, "1", "1", "x86_64"),
    ("a", 0, "", "1", "x86_64"),
    ("a", 0, "1", "", "x86_64"),
    ("a", 0, "1-2", "1", "x86_64"),
    ("a", 0, "1", "1-2", "x86_64"),
    ("a", 0, "1:2", "1", "x86_64"),
    ("a", 0, "1", "1", ""),
    ("a", 0, "1", "1", "x86.64"),
    ("a", "x", "1", "1", "x86_64"),
    ("a", -1, "1", "1", "x86_64"),
])
def test_nevra_rejects_invalid_parts(args: tuple[str, int | str | None, str, str, str]) -> None:
    with pytest.raises(ValueError):
        nevra(*args)


@pytest.mark.parametrize(("name", "pattern", "expected"), [
    ("openssl", "openssl", True),
    ("openssl", "openssl-libs", False),
    ("openssl-libs", "openssl", False),
    ("python3-requests", "python3-*", True),
    ("python3-", "python3-*", True),
    ("python3", "python3-*", False),
    ("python3.11-devel", "python3-*", False),
    ("kernel-core", "kernel*", True),
    ("kernel", "kernel*", True),
    ("kernel-modules-core", "*-core", True),
    ("kernel-core-devel", "*-core", False),
    ("kernel-core", "*-core", True),
    ("perl-Foo-Bar", "perl-*-Bar", True),
    ("perl-Foo-Baz", "perl-*-Bar", False),
    ("anything", "*", True),
    ("NetworkManager", "networkmanager", False),    # case-sensitive
    ("NetworkManager", "Network*", True),
    ("networkmanager", "Network*", False),
    ("libstdc++", "libstdc++", True),
    ("libstdc", "libstdc++", False),
    ("libstdcxx", "libstdc+*", False),
    ("a.b", "a.b", True),
    ("axb", "a.b", False),                          # '.' is literal
    ("ab", "a?", False),                            # '?' is literal, not a glob
    ("a", "[a]", False),
    ("openssl", "  openssl ", True),
    ("openssl", "", False),
    ("openssl", "   ", False),
])
def test_package_matches(name: str, pattern: str, expected: bool) -> None:
    assert package_matches(_ref("rocky-9", name), pattern) is expected


def test_package_matches_ignores_the_namespace() -> None:
    assert package_matches(_ref("rhel-9", "openssl"), "openssl")
    assert not package_matches(_ref("rhel-9", "openssl"), "rhel-9/openssl")


def _registry(slug: str, routing: dict[str, object] | None) -> RegistryConfig:
    return RegistryConfig(
        id=slug, slug=slug, ecosystem="rpm", protocol="rpm-repodata", base_url=f"https://{slug}.invalid/",
        priority=100, config={"namespace": "rocky-9"}, routing=routing, auth_type="none", credentials_ref=None,
    )


def test_route_with_packages_patterns() -> None:
    plain = _registry("plain", None)
    only_python = _registry("py", {"mode": "allow", "packages": ["python3-*", "openssl"]})
    not_kernel = _registry("nk", {"mode": "block", "packages": ["kernel*"]})
    registries = [plain, only_python, not_kernel]

    def slugs(name: str) -> list[str]:
        return [r.slug for r in route(_ref("rocky-9", name), registries, "packages", package_matches)]

    assert slugs("python3-requests") == ["plain", "py", "nk"]
    assert slugs("openssl") == ["plain", "py", "nk"]
    assert slugs("bash") == ["plain", "nk"]
    assert slugs("kernel-core") == ["plain"]


def test_route_exclusive_packages() -> None:
    plain = _registry("plain", None)
    exclusive = _registry("ex", {"mode": "exclusive", "packages": ["openssl*"]})
    got = route(_ref("rocky-9", "openssl-libs"), [plain, exclusive], "packages", package_matches)
    assert [r.slug for r in got] == ["ex"]
    got = route(_ref("rocky-9", "bash"), [plain, exclusive], "packages", package_matches)
    assert [r.slug for r in got] == ["plain"]
