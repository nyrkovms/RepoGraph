"""Shared classifier primitives (ported verbatim from classifire.classify)."""

import re
from dataclasses import dataclass, field
from typing import Any

PLATFORM_SUFFIX = re.compile(
    r"-(linux|darwin|win32|windows|freebsd|openbsd|netbsd|android|alpine|musl|sunos|aix)"
    r"(-(x64|arm64|arm|ia32|ppc64|riscv64|s390x|loong64|mips64el|universal|baseline|musl|gnu|msvc|eabi))*$"
)


@dataclass
class Mark:
    name: str
    confidence: str
    evidence: str
    provisional: bool = False


@dataclass
class ProbeView:
    """Classifier-facing view of a dist_probes row (or its absence)."""

    entry_points_txt: str | None = None
    top_level_txt: str | None = None
    file_facts: dict | None = None
    error: str | None = None
    available: bool = False


@dataclass
class Result:
    facets: list[Mark] = field(default_factory=list)
    capabilities: list[Mark] = field(default_factory=list)
    relations: list = field(default_factory=list)  # (relation, related_name)
    status: str = "ok"
    status_reason: str = ""
    summary: str = ""

    def add_facet(self, role: str, confidence: str, evidence: str, provisional: bool = False) -> None:
        for facet in self.facets:
            if facet.name == role:
                if confidence == "high" and facet.confidence == "low":
                    facet.confidence = "high"
                    facet.evidence = evidence
                return
        self.facets.append(Mark(role, confidence, evidence, provisional))

    def add_capability(self, capability: str, confidence: str, evidence: str) -> None:
        for cap in self.capabilities:
            if cap.name == capability:
                if confidence == "high" and cap.confidence == "low":
                    cap.confidence = "high"
                    cap.evidence = evidence
                return
        self.capabilities.append(Mark(capability, confidence, evidence))

    @property
    def roles(self) -> set[str]:
        return {facet.name for facet in self.facets}


def lower_blob(*parts: Any) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            chunks.extend(str(item) for item in part.keys())
        elif isinstance(part, (list, tuple, set)):
            chunks.extend(str(item) for item in part)
        elif part:
            chunks.append(str(part))
    return "\n".join(chunks).lower()


def has_any(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def parse_entry_points(raw: str | None) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    if not raw:
        return groups
    current: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
        elif current and line and not line.startswith("#") and "=" in line:
            groups.setdefault(current, []).append(line.split("=", 1)[0].strip())
    return {group: names for group, names in groups.items() if names}  # только непустые группы


def match_entry_group(rules: list[dict], group: str) -> dict | None:
    """Паттерн с '*' на конце — префикс, иначе точное совпадение."""
    for rule in rules:
        pattern = rule["pattern"]
        if pattern.endswith("*"):
            if group.startswith(pattern[:-1]):
                return rule
        elif group == pattern:
            return rule
    return None
