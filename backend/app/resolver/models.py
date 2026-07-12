from dataclasses import dataclass, replace
from uuid import UUID

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from app.normalizers.names import normalize_package_name


@dataclass(frozen=True)
class ResolverRequirement:
    name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    dependency_type: str = "runtime"
    extra_name: str | None = None
    environment_markers: dict | None = None

    @classmethod
    def from_input(
        cls,
        name: str,
        version_spec: str,
        extras: list[str] | None = None,
        dependency_type: str = "runtime",
        extra_name: str | None = None,
        environment_markers: dict | None = None,
    ) -> "ResolverRequirement":
        return cls(
            name=normalize_package_name(name),
            specifier=SpecifierSet(version_spec if version_spec and version_spec != "*" else ""),
            extras=frozenset(extras or []),
            dependency_type=dependency_type,
            extra_name=extra_name,
            environment_markers=environment_markers,
        )


@dataclass(frozen=True)
class ResolverCandidate:
    name: str
    version: Version
    version_id: UUID
    requires_python: str | None
    extras: frozenset[str]

    @property
    def version_str(self) -> str:
        return str(self.version)

    def with_extras(self, extras: frozenset[str]) -> "ResolverCandidate":
        if extras == self.extras:
            return self
        return replace(self, extras=extras)
