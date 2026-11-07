"""What changed in the dependencies between two versions of a package (the Compare tab)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DepRow:
    ecosystem: str
    namespace: str
    name: str
    dependency_type: str
    constraint: str


@dataclass(frozen=True, slots=True)
class Added:
    name: str
    constraint: str
    dependency_type: str


@dataclass(frozen=True, slots=True)
class Changed:
    name: str
    dependency_type: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Comparison:
    added: list[Added]
    removed: list[Added]
    changed: list[Changed]
    unchanged: int


_Key = tuple[str, str, str, str]


def _constraints(rows: Iterable[DepRow]) -> dict[_Key, str]:
    """One constraint string per key: several rows (markers, variants) become their sorted distinct constraints."""
    grouped: dict[_Key, set[str]] = defaultdict(set)
    for row in rows:
        grouped[(row.ecosystem, row.namespace, row.name, row.dependency_type)].add(row.constraint)
    return {key: ", ".join(sorted(values)) for key, values in grouped.items()}


def _order(key: _Key) -> tuple[str, str, str, str]:
    ecosystem, namespace, name, dep_type = key
    return (dep_type, name, namespace, ecosystem)


def compare_dependencies(old: Iterable[DepRow], new: Iterable[DepRow]) -> Comparison:
    """Differences by key (ecosystem, namespace, name, type). A move between types is one removed and one added.
    Lists are sorted by type, then name, so the result does not depend on the input order."""
    before, after = _constraints(old), _constraints(new)
    added = [Added(k[2], after[k], k[3]) for k in sorted(after.keys() - before.keys(), key=_order)]
    removed = [Added(k[2], before[k], k[3]) for k in sorted(before.keys() - after.keys(), key=_order)]
    both = before.keys() & after.keys()
    changed = [Changed(k[2], k[3], before[k], after[k]) for k in sorted(both, key=_order) if before[k] != after[k]]
    return Comparison(added=added, removed=removed, changed=changed, unchanged=len(both) - len(changed))
