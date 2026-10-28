"""Merge of several sources into one catalog row (SPEC-MULTI-ECOSYSTEM.md §6.5). Plugins call it from
`EcosystemPlugin.merge`; an ecosystem with other rules overrides that method.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import fields, replace

from app.ecosystems.base import (
    MergedPackage,
    MergedVersion,
    NormalizedDependency,
    NormalizedPackage,
    NormalizedVersion,
    VersionStatus,
)

# Package-level fields taken from the first source (by priority) that fills them.
_FILLED_FIELDS = tuple(
    f.name for f in fields(NormalizedPackage) if f.name not in {"ref", "registry_id", "display_name", "revision", "versions",
                                                                "raw_metadata"}
)


def dependencies_digest(dependencies: Sequence[NormalizedDependency]) -> bytes:
    """Order-independent digest of a dependency set: equal sets from two sources are the same metadata."""
    rows = sorted(
        json.dumps(
            [d.target.ecosystem, d.target.namespace, d.target.name, d.constraint, d.dependency_type, d.scope, d.alias,
             d.extra_name, d.environment_markers, d.details],
            sort_keys=True, default=str,
        )
        for d in dependencies
    )
    return hashlib.sha256("\n".join(rows).encode()).digest()


def merge_by_priority(per_source: Sequence[NormalizedPackage]) -> MergedPackage:
    """`per_source` is ordered by global source priority, highest first; the first one is the primary source.

    - package fields: first source where the field is not None (None = the source does not provide it);
      `raw_metadata` and the display name come from the primary source (IMPL T8);
    - versions: union; a version's metadata comes from the first source publishing it (`metadata_registry_id`),
      its aggregate status is the status there;
    - a source whose known dependencies differ from the canonical set keeps them as a variant (Q17).
    """
    if not per_source:
        raise ValueError("merge needs at least one source")
    primary = per_source[0]

    package = replace(primary, versions=[])
    for name in _FILLED_FIELDS:
        setattr(package, name, next((v for p in per_source if (v := getattr(p, name)) is not None), None))

    by_version: dict[str, list[tuple[str, NormalizedVersion]]] = {}
    for source in per_source:
        for version in source.versions:
            by_version.setdefault(version.version, []).append((source.registry_id, version))

    versions: list[MergedVersion] = []
    for publishers in by_version.values():
        metadata_registry_id, canonical = publishers[0]
        canonical_digest = dependencies_digest(canonical.dependencies) if canonical.dependencies is not None else None
        variants = {
            registry_id: version.dependencies
            for registry_id, version in publishers[1:]
            if version.dependencies is not None and dependencies_digest(version.dependencies) != canonical_digest
        }
        sources: dict[str, VersionStatus] = {registry_id: version.status for registry_id, version in publishers}
        versions.append(MergedVersion(
            version=canonical, metadata_registry_id=metadata_registry_id, sources=sources,
            dependency_variants=variants,
        ))

    return MergedPackage(
        package=package,
        primary_registry_id=primary.registry_id,
        versions=versions,
        registry_revisions={source.registry_id: source.revision for source in per_source},
    )
