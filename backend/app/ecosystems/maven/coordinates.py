"""Maven coordinates: `groupId:artifactId`, case-sensitive and stored as written (SPEC-MULTI-ECOSYSTEM.md §4).

The identity of a package is its GA pair. Versions, packaging (`type`) and `classifier` are not part of it; the purl
reader keeps the last two apart for callers that care (BOM roots in the resolver are `type=pom`).

The path helpers build the paths of the Maven 2 repository layout, relative to a repository root.
"""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

_CHARS = re.compile(r"[A-Za-z0-9_.-]+")
_QUALIFIERS = ("type", "classifier")


def validate_group_id(raw: str) -> None:
    _validate_part("groupId", raw)
    if raw.startswith(".") or raw.endswith("."):
        raise ValueError(f"groupId must not start or end with a dot: {raw!r}")


def validate_artifact_id(raw: str) -> None:
    _validate_part("artifactId", raw)
    if raw == ".":
        raise ValueError(f"not an artifactId: {raw!r}")


def _validate_part(label: str, raw: str) -> None:
    if not raw:
        raise ValueError(f"{label} is empty")
    if not _CHARS.fullmatch(raw):
        raise ValueError(f"not a {label} (letters, digits, '_', '.' and '-' only): {raw!r}")
    if ".." in raw:
        raise ValueError(f"{label} must not contain '..': {raw!r}")


def _validate_segment(label: str, raw: str) -> None:
    """Version, extension and classifier end up in a repository path: keep them to one safe path segment."""
    if not raw or "/" in raw or "\\" in raw or ".." in raw or raw.strip() != raw:
        raise ValueError(f"not a valid {label} for a repository path: {raw!r}")


def group_path(ref: PackageRef) -> str:
    return ref.namespace.replace(".", "/")


def metadata_path(ref: PackageRef) -> str:
    return f"{group_path(ref)}/{ref.name}/maven-metadata.xml"


def pom_path(ref: PackageRef, version: str) -> str:
    return file_path(ref, version, "pom")


def file_path(ref: PackageRef, version: str, extension: str, classifier: str | None = None) -> str:
    """`<group path>/<artifact>/<version>/<artifact>-<version>[-<classifier>].<extension>`."""
    _validate_segment("version", version)
    _validate_segment("extension", extension)
    if classifier is not None:
        _validate_segment("classifier", classifier)
    suffix = f"-{classifier}" if classifier else ""
    return f"{group_path(ref)}/{ref.name}/{version}/{ref.name}-{version}{suffix}.{extension}"


def purl_qualifiers(purl: str) -> dict[str, str]:
    """The `type` and `classifier` qualifiers of a maven purl, those present only."""
    parsed = _parse_maven_purl(purl)
    return {key: parsed.qualifiers[key] for key in _QUALIFIERS if key in parsed.qualifiers}


def _parse_maven_purl(purl: str) -> Purl:
    parsed = parse(purl)
    if parsed.type != "maven":
        raise ValueError(f"not a maven purl: {purl}")
    return parsed


class MavenCoordinates:
    def parse(self, raw: str) -> PackageRef:
        """`group:artifact`. A version or any further part is an error: versions are not part of identity."""
        parts = raw.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"expected 'groupId:artifactId': {raw!r}")
        group_id, artifact_id = parts
        return self._ref(group_id, artifact_id)

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        """Qualifiers (`type`, `classifier`, `repository_url`) do not change identity; read them with `purl_qualifiers`."""
        parsed = _parse_maven_purl(purl)
        if not parsed.namespace:
            raise ValueError(f"maven purls need a namespace (the groupId): {purl}")
        return self._ref(parsed.namespace, parsed.name), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        return Purl(type="maven", namespace=ref.namespace, name=ref.name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return f"{ref.namespace}:{ref.name}"

    @staticmethod
    def _ref(group_id: str, artifact_id: str) -> PackageRef:
        validate_group_id(group_id)
        validate_artifact_id(artifact_id)
        return PackageRef(ecosystem="maven", namespace=group_id, name=artifact_id)


__all__ = [
    "MavenCoordinates",
    "file_path",
    "group_path",
    "metadata_path",
    "pom_path",
    "purl_qualifiers",
    "validate_artifact_id",
    "validate_group_id",
]
