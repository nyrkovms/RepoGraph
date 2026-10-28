"""Reference data for the plugin contract suite (SPEC-MULTI-ECOSYSTEM.md §14.1).

Every plugin adds an entry when it is registered; the suite fails for a registered plugin without samples.
`ordered_versions` must be strictly increasing in the ecosystem's own order (checked against the native
implementation when the entry is written, see design/schema-check).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Samples:
    # (native coordinates, purl without version)
    names: list[tuple[str, str]]
    ordered_versions: list[str]
    invalid_versions: list[str] = field(default_factory=list)
    prereleases: list[str] = field(default_factory=list)


SAMPLES: dict[str, Samples] = {}
