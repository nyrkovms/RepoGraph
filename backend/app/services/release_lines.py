"""Release lines and release activity of a package, as the card shows them (SPEC-CATALOG-PAGES.md §5).

A line is the series a patch release belongs to: `major.minor` (pydantic 2.14), the major for Go (`v1`). It is a
property of the page layer, not of the plugin contract, so it is derived here from the version string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# A PEP 440 epoch (`1!2.0`) orders versions, it is not part of the series: the line of `1!2.0` is `2.0`.
_MAJOR_MINOR = re.compile(r"^v?(?:\d+!)?(\d+)(?:\.(\d+))?")
_GO_MAJOR = re.compile(r"^(v\d+)\.")
_MAJOR_MINOR_ECOSYSTEMS = frozenset({"pypi", "npm", "crates", "nuget", "maven"})


class _Named(Protocol):
    version: str


def line_of(ecosystem: str, version: str) -> str | None:
    """`2.14` for `2.14.3`, `6.1` for `6.1rc1`, `33.4` for `33.4.0-jre`, `v1` for Go's `v1.28.7`; None where there is
    no line: RPM builds, and a version with no leading number. A missing minor reads as 0, as PEP 440 equates `5`
    with `5.0`; an epoch is dropped (`1!2.0` is `2.0`)."""
    if ecosystem == "go":
        match = _GO_MAJOR.match(version)
        return match.group(1) if match else None
    if ecosystem not in _MAJOR_MINOR_ECOSYSTEMS:
        return None
    found = _MAJOR_MINOR.match(version)
    return f"{found.group(1)}.{found.group(2) or '0'}" if found else None


def filter_line[T: _Named](ecosystem: str, rows: Iterable[T], line: str) -> list[T]:
    """The rows (anything with a `version`) that belong to `line`, in the order given."""
    return [row for row in rows if line_of(ecosystem, row.version) == line]


@dataclass(frozen=True, slots=True)
class ReleasedVersion:
    version: str
    released_at: datetime | None     # None for a version the source gives no date
    sort_key: bytes


@dataclass(frozen=True, slots=True)
class ReleaseLine:
    line: str
    latest: str
    released_at: datetime | None   # of `latest`; None if it has no date
    stable_releases: int
    started_at: datetime | None    # the earliest dated release of the line; None if none has a date


def build_lines(ecosystem: str, versions: Iterable[ReleasedVersion]) -> list[ReleaseLine]:
    """Lines of the given stable versions, the line with the highest version first. A version with no line is left out;
    one with no date counts as a release and leaves the dates empty where it is the only evidence."""
    grouped: dict[str, list[ReleasedVersion]] = {}
    for item in versions:
        line = line_of(ecosystem, item.version)
        if line is not None:
            grouped.setdefault(line, []).append(item)
    lines = []
    for line, items in grouped.items():
        newest = max(items, key=lambda item: item.sort_key)
        lines.append((newest.sort_key, ReleaseLine(
            line=line,
            latest=newest.version,
            released_at=newest.released_at,
            stable_releases=len(items),
            started_at=min((item.released_at for item in items if item.released_at is not None), default=None),
        )))
    return [line for _, line in sorted(lines, key=lambda pair: pair[0], reverse=True)]


def last_months(now: datetime, count: int = 12) -> list[str]:
    """`YYYY-MM` of the last `count` calendar months in UTC, the current one included, oldest first."""
    year, month = now.year, now.month
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return months[::-1]


def fill_months(counts: Mapping[str, int], now: datetime, count: int = 12) -> list[dict[str, int | str]]:
    """The activity series: one entry per month of the window, zero where nothing was released."""
    return [{"month": month, "releases": counts.get(month, 0)} for month in last_months(now, count)]
