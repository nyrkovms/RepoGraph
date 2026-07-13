"""build_manifest version-scoping (version-scoped-facts, task 2.3/2.4).

Guards the fixed bug: a non-latest pypi version must be classified from its
OWN info, not the package-level (latest) info.
"""

from dataclasses import dataclass, field

from app.classifier.service import build_manifest


@dataclass
class FakeEco:
    value: str


@dataclass
class FakePackage:
    ecosystem: FakeEco
    latest_version: str
    normalized_name: str = "pkg"
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class FakeVersion:
    version: str
    raw_metadata: dict = field(default_factory=dict)


def test_pypi_uses_version_scoped_info_when_present():
    package = FakePackage(
        ecosystem=FakeEco("pypi"),
        latest_version="2.0.0",
        raw_metadata={"info": {"summary": "LATEST summary"}, "urls": []},
    )
    version = FakeVersion(
        version="1.0.0",
        raw_metadata={"info": {"summary": "OLD summary", "classifiers": ["X"]}, "urls": [{"filename": "a.whl"}]},
    )
    manifest = build_manifest(package, version)
    # версия != latest, но берётся info ИМЕННО версии (фикс бага)
    assert manifest["info"]["summary"] == "OLD summary"
    assert manifest["urls"] == [{"filename": "a.whl"}]


def test_pypi_latest_still_uses_its_own_info():
    package = FakePackage(
        ecosystem=FakeEco("pypi"),
        latest_version="2.0.0",
        raw_metadata={"info": {"summary": "LATEST"}, "urls": [{"filename": "latest.whl"}]},
    )
    version = FakeVersion(version="2.0.0", raw_metadata={"info": {"summary": "LATEST"}, "urls": [{"filename": "latest.whl"}]})
    manifest = build_manifest(package, version)
    assert manifest["info"]["summary"] == "LATEST"


def test_pypi_fallback_to_package_for_latest_without_version_info():
    # latest-строка без своего info (легаси-состояние) → фолбэк на package, без варнинга
    package = FakePackage(
        ecosystem=FakeEco("pypi"),
        latest_version="2.0.0",
        raw_metadata={"info": {"summary": "PKG"}, "urls": []},
    )
    version = FakeVersion(version="2.0.0", raw_metadata={"files": []})
    manifest = build_manifest(package, version)
    assert manifest["info"]["summary"] == "PKG"


def test_npm_uses_version_manifest():
    package = FakePackage(ecosystem=FakeEco("npm"), latest_version="2.0.0", raw_metadata={"whole": "packument"})
    version = FakeVersion(version="1.0.0", raw_metadata={"version": "1.0.0", "bin": {"x": "cli.js"}})
    manifest = build_manifest(package, version)
    assert manifest["version"] == "1.0.0"
    assert manifest["bin"] == {"x": "cli.js"}
