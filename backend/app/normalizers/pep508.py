import re
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement

_MARKER_PATTERN = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=|>=|<=|>|<)\s*['\"]([^'\"]+)['\"]")


@dataclass(slots=True)
class ParsedRequirement:
    dependency_name: str
    version_constraint: str
    dependency_type: str
    extra_name: str | None
    environment_markers: dict | None
    raw_spec: str


def _parse_marker_conditions(marker_str: str) -> list[dict[str, str]]:
    conditions: list[dict[str, str]] = []
    for marker, op, value in _MARKER_PATTERN.findall(marker_str):
        conditions.append({"marker": marker, "op": op, "value": value})
    return conditions


def parse_pep508(requirement_str: str) -> ParsedRequirement:
    try:
        req = Requirement(requirement_str)
    except InvalidRequirement as exc:
        raise ValueError(f"Invalid PEP 508 requirement: {requirement_str}") from exc

    marker_payload: dict | None = None
    if req.marker is not None:
        marker_str = str(req.marker)
        marker_payload = {
            "type": "python_environment",
            "expression": marker_str,
            "conditions": _parse_marker_conditions(marker_str),
        }

    extras = sorted(req.extras)
    extra_name = extras[0] if extras else None
    dep_type = "optional" if extra_name else "runtime"

    return ParsedRequirement(
        dependency_name=req.name,
        version_constraint=str(req.specifier) if str(req.specifier) else "*",
        dependency_type=dep_type,
        extra_name=extra_name,
        environment_markers=marker_payload,
        raw_spec=requirement_str,
    )
