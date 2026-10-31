"""Modularity (SPEC-MULTI-ECOSYSTEM.md §9.8.3): `modules.yaml` of EL8/EL9 repositories. Filled in by K5; for now the
streams a build belongs to, for the package card and events."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping


def artifact_streams(snapshots: Iterable[Any], builds: List[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """`{nevra: ["nodejs:18", ...]}` for builds that are artifacts of a module stream."""
    return {}
