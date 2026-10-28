from app.ecosystems.base import (
    NormalizedAuthor,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    NormalizedVulnerability,
    PackageRef,
)
from app.ecosystems.merge import dependencies_digest, merge_by_priority

TORCH = PackageRef(ecosystem="pypi", namespace="", name="torch")


def _dep(name: str, constraint: str = "*") -> NormalizedDependency:
    return NormalizedDependency(
        target=PackageRef(ecosystem="pypi", namespace="", name=name), constraint=constraint,
        dependency_type="runtime", raw_spec=f"{name}{constraint}",
    )


def _version(version: str, deps: list[NormalizedDependency] | None, status: str = "active") -> NormalizedVersion:
    return NormalizedVersion(version=version, status=status, dependencies=deps)  # type: ignore[arg-type]


def _source(registry_id: str, versions: list[NormalizedVersion], **fields) -> NormalizedPackage:
    return NormalizedPackage(ref=TORCH, registry_id=registry_id, display_name="torch", revision=f"rev-{registry_id}",
                             versions=versions, **fields)


def test_single_source_is_its_own_canonical_metadata() -> None:
    merged = merge_by_priority([_source("pypi", [_version("2.5.1", [_dep("filelock")])], description="Tensors")])

    assert merged.primary_registry_id == "pypi"
    assert merged.package.description == "Tensors"
    assert merged.registry_revisions == {"pypi": "rev-pypi"}
    [version] = merged.versions
    assert (version.metadata_registry_id, version.sources, version.dependency_variants) == ("pypi", {"pypi": "active"}, {})


def test_same_metadata_in_two_sources_makes_no_variant() -> None:
    merged = merge_by_priority([
        _source("pypi", [_version("2.5.1", [_dep("filelock"), _dep("sympy")])]),
        _source("cu121", [_version("2.5.1", [_dep("sympy"), _dep("filelock")])]),   # order does not matter
    ])

    [version] = merged.versions
    assert version.sources == {"pypi": "active", "cu121": "active"}
    assert version.dependency_variants == {}


def test_different_metadata_is_kept_as_a_variant_of_that_source() -> None:
    # torch 2.9.1 (2026-10-28): the PyPI wheel needs nvidia-*-cu12, the PyTorch index wheel does not.
    merged = merge_by_priority([
        _source("pypi", [_version("2.9.1", [_dep("filelock"), _dep("nvidia-cudnn-cu12")])]),
        _source("cu121", [_version("2.9.1", [_dep("filelock")])]),
    ])

    [version] = merged.versions
    assert version.metadata_registry_id == "pypi"
    assert [d.target.name for d in version.version.dependencies or []] == ["filelock", "nvidia-cudnn-cu12"]
    assert [d.target.name for d in version.dependency_variants["cu121"]] == ["filelock"]


def test_unknown_dependencies_are_not_a_variant() -> None:
    merged = merge_by_priority([
        _source("pypi", [_version("2.5.1", [_dep("filelock")])]),
        _source("cu121", [_version("2.5.1", None)]),    # PEP 503 file without PEP 658 metadata
    ])
    assert merged.versions[0].dependency_variants == {}


def test_version_only_in_the_second_source_takes_its_metadata_and_status() -> None:
    merged = merge_by_priority([
        _source("pypi", [_version("2.5.1", [])]),
        _source("cu121", [_version("2.5.1+cu121", [_dep("filelock")], status="yanked")]),
    ])

    by_version = {m.version.version: m for m in merged.versions}
    assert by_version["2.5.1+cu121"].metadata_registry_id == "cu121"
    assert by_version["2.5.1+cu121"].version.status == "yanked"
    assert by_version["2.5.1+cu121"].sources == {"cu121": "yanked"}


def test_aggregate_status_is_the_status_in_the_metadata_source() -> None:
    merged = merge_by_priority([
        _source("pypi", [_version("2.5.1", [], status="yanked")]),
        _source("cu121", [_version("2.5.1", [])]),
    ])
    assert merged.versions[0].version.status == "yanked"
    assert merged.versions[0].sources == {"pypi": "yanked", "cu121": "active"}


def test_package_fields_come_from_the_first_source_that_fills_them() -> None:
    author = NormalizedAuthor(name="PyTorch Team", email=None, url=None, role="author")
    vuln = NormalizedVulnerability(source="osv", source_id="PYSEC-1", aliases=[], summary="s", details=None,
                                   severity=None, fixed_in=None, published_at=None, withdrawn_at=None, link="x")
    merged = merge_by_priority([
        # A PEP 503 index as the primary source: files only, no package metadata.
        _source("cu121", [_version("2.5.1", None)], raw_metadata={"index": "cu121"}),
        _source("pypi", [_version("2.5.1", [])], description="Tensors", license_spdx="BSD-3-Clause",
                authors=[author], vulnerabilities=[vuln], raw_metadata={"info": {}}),
    ])

    assert merged.primary_registry_id == "cu121"
    assert (merged.package.description, merged.package.license_spdx) == ("Tensors", "BSD-3-Clause")
    assert merged.package.authors == [author] and merged.package.vulnerabilities == [vuln]
    assert merged.package.raw_metadata == {"index": "cu121"}   # primary source only (IMPL T8)
    assert merged.package.versions == []


def test_dependency_digest_ignores_order_and_sees_constraints() -> None:
    assert dependencies_digest([_dep("a"), _dep("b")]) == dependencies_digest([_dep("b"), _dep("a")])
    assert dependencies_digest([_dep("a", ">=1")]) != dependencies_digest([_dep("a", ">=2")])
    assert dependencies_digest([]) != dependencies_digest([_dep("a")])
