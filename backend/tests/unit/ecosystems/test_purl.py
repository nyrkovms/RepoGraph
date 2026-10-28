import json
from pathlib import Path

import pytest

from app.ecosystems.purl import Purl, ecosystem_for, parse, purl_type_for

SPEC_CASES = json.loads((Path(__file__).parent / "purl_spec_cases.json").read_text())["tests"]

# SPEC-MULTI-ECOSYSTEM.md section 4.
SPEC_EXAMPLES = [
    "pkg:pypi/flask",
    "pkg:npm/%40angular/core",
    "pkg:maven/org.springframework/spring-core",
    "pkg:nuget/Newtonsoft.Json",
    "pkg:golang/github.com/redis/go-redis/v9",
    "pkg:cargo/serde",
    "pkg:rpm/redhat/openssl?distro=rhel-9",
    "pkg:pypi/torch@2.5.1%2Bcu121?repository_url=https:%2F%2Fdownload.pytorch.org%2Fwhl%2Fcu121",
]


@pytest.mark.parametrize("text", SPEC_EXAMPLES)
def test_spec_examples_round_trip(text: str) -> None:
    assert parse(text).to_string() == text


def test_components_are_decoded() -> None:
    purl = parse("pkg:pypi/torch@2.5.1%2Bcu121?repository_url=https://download.pytorch.org/whl/cu121")  # unencoded input is accepted

    assert purl == Purl(
        type="pypi",
        name="torch",
        version="2.5.1+cu121",
        qualifiers={"repository_url": "https://download.pytorch.org/whl/cu121"},
    )


def test_namespace_segments() -> None:
    npm = parse("pkg:npm/%40angular/core")
    go = parse("pkg:golang/github.com/redis/go-redis/v9")

    assert (npm.namespace, npm.name) == ("@angular", "core")
    assert (go.namespace, go.name) == ("github.com/redis/go-redis", "v9")


def test_unencoded_scope_is_not_taken_for_version() -> None:
    purl = parse("pkg:npm/@babel/core@7.0.0")

    assert (purl.namespace, purl.name, purl.version) == ("@babel", "core", "7.0.0")
    assert purl.to_string() == "pkg:npm/%40babel/core@7.0.0"


def test_build_encodes_special_characters() -> None:
    purl = Purl(type="npm", namespace="@angular", name="core", version="1.0.0+build@x")

    assert purl.to_string() == "pkg:npm/%40angular/core@1.0.0%2Bbuild%40x"


def test_type_is_lowercased_and_scheme_is_case_insensitive() -> None:
    assert parse("PKG:PyPI/Flask") == Purl(type="pypi", name="Flask")
    assert parse("pkg://pypi/flask") == Purl(type="pypi", name="flask")


def test_qualifiers_keys_lowercased_sorted_and_empty_values_dropped() -> None:
    purl = parse("pkg:rpm/fedora/curl@7.50.3?Distro=fedora-25&arch=i386&epoch=")

    assert dict(purl.qualifiers) == {"arch": "i386", "distro": "fedora-25"}
    assert purl.to_string() == "pkg:rpm/fedora/curl@7.50.3?arch=i386&distro=fedora-25"


def test_qualifiers_are_sorted_when_built_from_a_dict() -> None:
    purl = Purl(type="maven", namespace="g", name="a", qualifiers={"type": "zip", "classifier": "dist"})

    assert purl.to_string() == "pkg:maven/g/a?classifier=dist&type=zip"


def test_qualifier_value_is_encoded() -> None:
    purl = Purl(type="npm", name="a", qualifiers={"vcs_url": "git://h/r.git@abc", "tag": "a b&c"})

    assert purl.to_string() == "pkg:npm/a?tag=a%20b%26c&vcs_url=git:%2F%2Fh%2Fr.git%40abc"
    assert parse(purl.to_string()) == purl


def test_subpath_is_cleaned() -> None:
    purl = parse("pkg:npm/@babel/core#/a/./b/../c/")

    assert purl.subpath == "a/b/c"
    assert purl.to_string() == "pkg:npm/%40babel/core#a/b/c"


def test_empty_optional_parts_become_none() -> None:
    purl = Purl(type="pypi", name="flask", namespace="", version="", subpath="")

    assert (purl.namespace, purl.version, purl.subpath) == (None, None, None)
    assert purl.to_string() == "pkg:pypi/flask"


def test_purl_is_hashable_and_qualifiers_are_immutable() -> None:
    a = parse("pkg:rpm/redhat/openssl?distro=rhel-9")
    b = Purl(type="rpm", namespace="redhat", name="openssl", qualifiers={"distro": "rhel-9"})

    assert a == b
    assert len({a, b}) == 1
    with pytest.raises(TypeError):
        a.qualifiers["x"] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("flask", "pkg:"),
        ("pypi/flask", "pkg:"),
        ("pkg:", "type is missing"),
        ("pkg:/flask", "name is missing"),
        ("pkg:pypi", "name is missing"),
        ("pkg:pypi/", "name is missing"),
        ("pkg:maven/@1.3.4", "name is missing"),
        ("pkg:3nginx/nginx", "invalid purl type"),
        ("pkg:n&g/nginx", "invalid purl type"),
        ("pkg:nginx:a/nginx", "invalid purl type"),
        ("pkg:npm/a?in%20production=true", "qualifier key"),
        ("pkg:npm/a?novalue", "no '='"),
        ("pkg:npm/a?x=1&X=2", "duplicate qualifier"),
        ("pkg:npm/a%FF", "percent-encoding"),
    ],
)
def test_malformed_input_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(text)


def test_building_without_name_or_with_bad_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="name is required"):
        Purl(type="npm", name="")
    with pytest.raises(ValueError, match="invalid purl type"):
        Purl(type="", name="a")
    with pytest.raises(ValueError, match="qualifier key"):
        Purl(type="npm", name="a", qualifiers={"in production": "true"})


@pytest.mark.parametrize("case", SPEC_CASES, ids=lambda c: f"{c['test_type']}-{c['description'][:40]}")
def test_purl_spec_cases(case: dict) -> None:
    kind, data = case["test_type"], case["input"]

    def run() -> object:
        if kind == "parse":
            purl = parse(data)
            return {
                "type": purl.type,
                "namespace": purl.namespace,
                "name": purl.name,
                "version": purl.version,
                "qualifiers": dict(purl.qualifiers) or None,
                "subpath": purl.subpath,
            }
        return Purl(**{**data, "qualifiers": data["qualifiers"] or {}}).to_string()

    if case["expected_failure"]:
        with pytest.raises(ValueError):
            run()
    else:
        assert run() == case["expected_output"]


@pytest.mark.parametrize(
    ("ecosystem", "purl_type"),
    [
        ("pypi", "pypi"),
        ("npm", "npm"),
        ("maven", "maven"),
        ("nuget", "nuget"),
        ("go", "golang"),
        ("crates", "cargo"),
        ("rpm", "rpm"),
    ],
)
def test_ecosystem_purl_type_mapping(ecosystem: str, purl_type: str) -> None:
    assert purl_type_for(ecosystem) == purl_type
    assert ecosystem_for(purl_type) == ecosystem


def test_unknown_mapping_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown ecosystem"):
        purl_type_for("golang")
    with pytest.raises(ValueError, match="no ecosystem"):
        ecosystem_for("gem")
