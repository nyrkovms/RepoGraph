import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from app.connectors.base import NormalizedDependency, NormalizedPackage, NormalizedVersion
from app.fetcher import pipeline


class _FakeSession:
    async def commit(self) -> None:
        pass

    async def refresh(self, _obj) -> None:
        pass


def _make_package() -> NormalizedPackage:
    now = datetime.now(UTC)
    dependency = NormalizedDependency(
        dependency_name="click",
        version_constraint=">=8",
        dependency_type="runtime",
        extra_name=None,
        environment_markers=None,
        raw_spec="click>=8",
    )
    return NormalizedPackage(
        ecosystem="pypi",
        name="flask",
        normalized_name="flask",
        description=None,
        homepage_url=None,
        repository_url=None,
        documentation_url=None,
        license_raw=None,
        license_spdx=None,
        keywords=None,
        latest_version="3.0.0",
        downloads_total=None,
        downloads_recent=None,
        created_at=now,
        updated_at=now,
        raw_metadata={},
        versions=[
            NormalizedVersion(
                version="3.0.0",
                release_date=now,
                status="active",
                requires_python=None,
                raw_metadata={},
                dependencies_hydrated=True,
                dependencies=[dependency],
            ),
            NormalizedVersion(
                version="2.0.0",
                release_date=now,
                status="active",
                requires_python=None,
                raw_metadata={},
                dependencies_hydrated=False,
            ),
        ],
    )


def test_refresh_keeps_dependencies_of_non_hydrated_versions(monkeypatch) -> None:
    replaced: list[tuple[str, list]] = []

    async def noop(*_args, **_kwargs):
        return None

    async def fake_registry(_db):
        return SimpleNamespace(id="registry")

    async def fake_upsert_package(*_args, **_kwargs):
        return SimpleNamespace(id="package")

    async def fake_upsert_versions(_db, _package_id, versions):
        return {version.version: f"id-{version.version}" for version in versions}

    async def fake_replace_dependencies(_db, version_id, dependencies):
        replaced.append((version_id, dependencies))

    monkeypatch.setattr(pipeline, "_get_or_create_pypi_registry", fake_registry)
    monkeypatch.setattr(pipeline, "find_package", noop)
    monkeypatch.setattr(pipeline, "_mark_missing_versions_removed", noop)
    monkeypatch.setattr(pipeline, "_link_dependents", noop)
    monkeypatch.setattr(pipeline, "_record_events", noop)
    monkeypatch.setattr(pipeline, "_upsert_package", fake_upsert_package)
    monkeypatch.setattr(pipeline, "_upsert_package_registry", noop)
    monkeypatch.setattr(pipeline, "_upsert_versions", fake_upsert_versions)
    monkeypatch.setattr(pipeline, "_replace_dependencies", fake_replace_dependencies)
    monkeypatch.setattr(pipeline, "_replace_artifacts", noop)
    monkeypatch.setattr(pipeline, "_upsert_dist_tag", noop)
    monkeypatch.setattr(pipeline, "_upsert_authors", noop)
    monkeypatch.setattr(pipeline, "_upsert_vulnerabilities", noop)
    monkeypatch.setattr(pipeline, "_resolve_level1", noop)
    monkeypatch.setattr(pipeline, "_upsert_sync_state", noop)

    asyncio.run(pipeline.run_sync_pipeline(_make_package(), _FakeSession()))

    assert [version_id for version_id, _ in replaced] == ["id-3.0.0"]
    assert [dep.dependency_name for dep in replaced[0][1]] == ["click"]


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, _statement) -> None:
        pass

    async def flush(self) -> None:
        pass


def test_new_package_is_written_with_empty_namespace(monkeypatch) -> None:
    async def not_found(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "find_package", not_found)
    session = _RecordingSession()

    package = asyncio.run(pipeline._upsert_package(session, "registry", _make_package(), datetime.now(UTC)))

    assert package.namespace == ""
    assert session.added == [package]


def test_dependencies_are_written_with_empty_namespace() -> None:
    session = _RecordingSession()
    dependencies = _make_package().versions[0].dependencies

    asyncio.run(pipeline._replace_dependencies(session, "version", dependencies))

    assert [dep.dependency_namespace for dep in session.added] == [""]


def test_dependency_names_are_written_normalized() -> None:
    # IMPL T14: dependency_name is the PEP 503 name; the original spelling stays in raw_spec.
    session = _RecordingSession()
    dependency = replace(_make_package().versions[0].dependencies[0], dependency_name="Typing_Extensions",
                         raw_spec="Typing_Extensions>=4")

    asyncio.run(pipeline._replace_dependencies(session, "version", [dependency]))

    assert [(dep.dependency_name, dep.raw_spec) for dep in session.added] == [("typing-extensions", "Typing_Extensions>=4")]
