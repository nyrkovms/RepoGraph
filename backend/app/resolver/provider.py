import asyncio
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from resolvelib import AbstractProvider
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ecosystems.pypi.connector import PyPIConnector
from app.fetcher.pipeline import _merge_version_features
from app.models.dependency import Dependency
from app.models.enums import DependencyType, Ecosystem, PackageVersionStatus
from app.models.package import Package
from app.models.version import PackageVersion
from app.normalizers.names import normalize_package_name
from app.resolver.errors import ResolveInternalError, ResolveNotFoundError, ResolveUpstreamTimeoutError
from app.resolver.markers import marker_applies
from app.resolver.models import ResolverCandidate, ResolverRequirement

LazyFetchSync = Callable[[str], str]


class PyPIProvider(AbstractProvider):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        environment: dict[str, str],
        lazy_fetch: LazyFetchSync,
        include_dev: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.environment = environment
        self.lazy_fetch = lazy_fetch
        self.include_dev = include_dev
        self.connector = PyPIConnector()

        self._candidates_cache: dict[str, list[ResolverCandidate]] = {}
        self._deps_cache: dict[tuple[str, tuple[str, ...]], list[ResolverRequirement]] = {}
        self._existence_cache: dict[str, bool] = {}

    def identify(self, requirement_or_candidate: ResolverRequirement | ResolverCandidate) -> str:
        return requirement_or_candidate.name

    def get_preference(
        self,
        identifier: str,
        resolutions: Mapping[str, ResolverCandidate],
        candidates: Mapping[str, Iterable[ResolverCandidate]],
        information: Mapping[str, Iterator[Any]],
        backtrack_causes: Sequence[Any],
    ) -> int:
        return sum(1 for _ in candidates.get(identifier, []))

    def find_matches(
        self,
        identifier: str,
        requirements: Mapping[str, Iterable[ResolverRequirement]],
        incompatibilities: Mapping[str, Iterable[ResolverCandidate]],
    ) -> list[ResolverCandidate]:
        reqs = list(requirements.get(identifier, []))
        requested_extras = frozenset(extra for req in reqs for extra in req.extras)

        candidates = self._load_candidates(identifier)
        if not candidates:
            exists = self._package_exists(identifier)
            if not exists:
                status = self.lazy_fetch(identifier)
                if status == "fetched":
                    self._invalidate(identifier)
                    candidates = self._load_candidates(identifier)
                elif status == "not_found":
                    raise ResolveNotFoundError(identifier)
                elif status == "timeout":
                    raise ResolveUpstreamTimeoutError(identifier)
                else:
                    raise ResolveInternalError(f"Lazy fetch failed for {identifier}")

        blocked_versions = {candidate.version for candidate in incompatibilities.get(identifier, [])}

        matched: list[ResolverCandidate] = []
        for candidate in candidates:
            if candidate.version in blocked_versions:
                continue
            if self._candidate_satisfies_all(candidate, reqs):
                matched.append(candidate.with_extras(requested_extras))

        return sorted(matched, key=lambda item: item.version, reverse=True)

    def is_satisfied_by(self, requirement: ResolverRequirement, candidate: ResolverCandidate) -> bool:
        return requirement.specifier.contains(candidate.version, prereleases=True)

    def get_latest_compatible_version(self, identifier: str) -> str | None:
        candidates = sorted(self._load_candidates(identifier), key=lambda item: item.version, reverse=True)
        if not candidates:
            return None
        stable_candidates = [candidate for candidate in candidates if not candidate.version.is_prerelease]
        preferred = stable_candidates[0] if stable_candidates else candidates[0]
        return preferred.version_str

    def get_dependencies(self, candidate: ResolverCandidate) -> list[ResolverRequirement]:
        extras_key = tuple(sorted(candidate.extras))
        cache_key = (str(candidate.version_id), extras_key)
        if cache_key in self._deps_cache:
            return self._deps_cache[cache_key]

        self._ensure_candidate_dependencies(candidate)

        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(Dependency)
                    .where(Dependency.version_id == candidate.version_id)
                    .order_by(Dependency.dependency_name.asc())
                )
                .scalars()
                .all()
            )

        requirements: list[ResolverRequirement] = []
        for dep in rows:
            if dep.dependency_type == DependencyType.dev and not self.include_dev:
                continue
            if dep.dependency_type in {DependencyType.build, DependencyType.peer, DependencyType.provided}:
                continue
            if dep.dependency_ecosystem != Ecosystem.pypi:
                continue
            if dep.dependency_type == DependencyType.optional:
                # The marker names the parent's extra (`extra == "test"`); it applies if any requested extra matches.
                if not any(
                    marker_applies(dep.environment_markers, {**self.environment, "extra": extra})
                    for extra in candidate.extras
                ):
                    continue
            elif not marker_applies(dep.environment_markers, self.environment):
                continue

            requirements.append(
                ResolverRequirement.from_input(
                    name=dep.dependency_name,
                    version_spec=dep.version_constraint,
                    extras=_requested_extras(dep.raw_spec),
                    dependency_type=dep.dependency_type.value,
                    extra_name=dep.extra_name,
                    environment_markers=dep.environment_markers,
                )
            )

        self._deps_cache[cache_key] = requirements
        return requirements

    def _ensure_candidate_dependencies(self, candidate: ResolverCandidate) -> None:
        with self.session_factory() as session:
            row = session.execute(
                select(PackageVersion, Package.normalized_name)
                .join(Package, Package.id == PackageVersion.package_id)
                .where(PackageVersion.id == candidate.version_id)
            ).one_or_none()
            if not row:
                return

            version_row, package_name = row
            features = dict(version_row.features or {})
            if features.get("dependencies_hydrated") is True:
                return

            dep_count = session.execute(
                select(func.count(Dependency.id)).where(Dependency.version_id == version_row.id)
            ).scalar_one()
            if dep_count and features.get("dependencies_hydrated") is None:
                version_row.features = {**features, "dependencies_hydrated": True}
                session.commit()
                return

            dependencies, requires_python = asyncio.run(
                self.connector.fetch_version_dependencies(package_name, version_row.version)
            )
            session.execute(delete(Dependency).where(Dependency.version_id == version_row.id))
            for dep in dependencies:
                session.add(
                    Dependency(
                        version_id=version_row.id,
                        dependency_name=normalize_package_name(dep.dependency_name),  # T14
                        dependency_namespace="",
                        dependency_ecosystem=Ecosystem.pypi,
                        version_constraint=dep.version_constraint,
                        dependency_type=DependencyType(dep.dependency_type),
                        extra_name=dep.extra_name,
                        environment_markers=dep.environment_markers,
                        raw_spec=dep.raw_spec,
                    )
                )
            if requires_python:
                version_row.requires_python = requires_python
            version_row.features = _merge_version_features(features, True)
            session.commit()

    def _candidate_satisfies_all(self, candidate: ResolverCandidate, reqs: list[ResolverRequirement]) -> bool:
        for req in reqs:
            if not req.specifier.contains(candidate.version, prereleases=True):
                return False
        return True

    def _package_exists(self, identifier: str) -> bool:
        if identifier in self._existence_cache:
            return self._existence_cache[identifier]

        with self.session_factory() as session:
            exists = (
                session.execute(
                    select(Package.id).where(
                        Package.ecosystem == Ecosystem.pypi,
                        Package.normalized_name == normalize_package_name(identifier),
                    )
                ).scalar_one_or_none()
                is not None
            )
        self._existence_cache[identifier] = exists
        return exists

    def _load_candidates(self, identifier: str) -> list[ResolverCandidate]:
        normalized = normalize_package_name(identifier)
        if normalized in self._candidates_cache:
            return self._candidates_cache[normalized]

        with self.session_factory() as session:
            package = session.execute(
                select(Package).where(Package.ecosystem == Ecosystem.pypi, Package.normalized_name == normalized)
            ).scalar_one_or_none()
            if not package:
                self._candidates_cache[normalized] = []
                return []

            rows = (
                session.execute(
                    select(PackageVersion)
                    .where(
                        PackageVersion.package_id == package.id,
                        PackageVersion.status == PackageVersionStatus.active,
                    )
                )
                .scalars()
                .all()
            )

        candidates: list[ResolverCandidate] = []
        for row in rows:
            if not _requires_python_allows(row.requires_python, self.environment.get("python_version", "3.12")):
                continue
            if not _artifacts_allow_environment(row.raw_metadata, self.environment):
                continue
            try:
                parsed_version = Version(row.version)
            except InvalidVersion:
                continue
            candidates.append(
                ResolverCandidate(
                    name=normalized,
                    version=parsed_version,
                    version_id=row.id,
                    requires_python=row.requires_python,
                    extras=frozenset(),
                )
            )

        self._candidates_cache[normalized] = candidates
        return candidates

    def _invalidate(self, identifier: str) -> None:
        normalized = normalize_package_name(identifier)
        self._candidates_cache.pop(normalized, None)
        self._existence_cache.pop(normalized, None)


def _requires_python_allows(requires_python: str | None, python_version: str) -> bool:
    if not requires_python:
        return True
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return True
    return spec.contains(Version(python_version), prereleases=True)


def _artifacts_allow_environment(raw_metadata: dict | None, environment: dict[str, str]) -> bool:
    files = (raw_metadata or {}).get("files")
    if not isinstance(files, list) or not files:
        return True

    if any((file_payload or {}).get("packagetype") == "sdist" for file_payload in files):
        return True

    wheel_filenames = [
        str((file_payload or {}).get("filename") or "")
        for file_payload in files
        if (file_payload or {}).get("packagetype") == "bdist_wheel"
    ]
    if not wheel_filenames:
        return True

    if any(filename.endswith("none-any.whl") or filename.endswith("py3-none-any.whl") for filename in wheel_filenames):
        return True

    sys_platform = environment.get("sys_platform", "linux")
    return any(_wheel_filename_matches_platform(filename.lower(), sys_platform) for filename in wheel_filenames)


def _wheel_filename_matches_platform(filename: str, sys_platform: str) -> bool:
    if sys_platform == "win32":
        return "win32" in filename or "win_amd64" in filename or "win_arm64" in filename
    if sys_platform == "darwin":
        return "macosx" in filename
    if sys_platform == "linux":
        return "manylinux" in filename or "musllinux" in filename or "linux_" in filename
    return True


def _requested_extras(raw_spec: str) -> list[str]:
    # Extras the parent asks of this dependency, e.g. `requests[socks]`.
    try:
        return sorted(Requirement(raw_spec).extras)
    except InvalidRequirement:
        return []
