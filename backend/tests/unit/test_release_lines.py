from datetime import UTC, datetime

import pytest

from app.services.release_lines import ReleasedVersion, build_lines, fill_months, last_months, line_of


@pytest.mark.parametrize(
    ("ecosystem", "version", "line"),
    [
        ("pypi", "2.14.3", "2.14"),
        ("pypi", "6.1rc1", "6.1"),
        ("pypi", "2021", "2021.0"),
        ("npm", "1.2.3-beta.1", "1.2"),
        ("crates", "0.23.1", "0.23"),
        ("maven", "33.4.0-jre", "33.4"),
        ("nuget", "8.0.1", "8.0"),
        ("go", "v1.28.7", "v1"),
        ("go", "v2.8.10", "v2"),
        ("go", "v0.0.0-20261014143641-f0251eb4aa3e", "v0"),
        ("go", "v2.0.0+incompatible", "v2"),
        ("rpm", "2.4.0-1.el8_10", None),
        ("pypi", "abc", None),
        ("maven", "abc-1.0", None),
        ("go", "1.2.3", None),
        ("cobol", "1.2.3", None),
    ],
)
def test_line_of(ecosystem: str, version: str, line: str | None) -> None:
    assert line_of(ecosystem, version) == line


def test_last_months_spans_twelve_calendar_months_across_a_year_end() -> None:
    months = last_months(datetime(2026, 2, 28, 23, 59, tzinfo=UTC))
    assert months[0] == "2025-03" and months[-1] == "2026-02" and len(months) == 12
    assert months == sorted(months)


def test_fill_months_has_zero_months_and_ignores_months_outside_the_window() -> None:
    now = datetime(2026, 11, 7, tzinfo=UTC)
    series = fill_months({"2025-12": 2, "2026-11": 1, "2024-01": 9, "2026-12": 4}, now)
    assert series[0]["month"] == "2025-12"
    assert series[-1]["month"] == "2026-11"
    assert {item["month"]: item["releases"] for item in series if item["releases"]} == {"2025-12": 2, "2026-11": 1}
    assert len(series) == 12


def _release(version: str, key: int, day: int) -> ReleasedVersion:
    return ReleasedVersion(version, datetime(2026, 1, day, tzinfo=UTC), key.to_bytes(4, "big"))


def test_build_lines_orders_by_highest_version_and_counts_stable_releases() -> None:
    lines = build_lines("pypi", [
        _release("1.10.2", 10, 5), _release("2.14.0", 30, 20), _release("1.10.1", 9, 3),
        _release("2.14.3", 32, 25), _release("2.13.0", 20, 10), _release("junk", 99, 1),
    ])
    assert [line.line for line in lines] == ["2.14", "2.13", "1.10"]
    top = lines[0]
    assert (top.latest, top.stable_releases, top.released_at.day, top.started_at.day) == ("2.14.3", 2, 25, 20)
    assert lines[2].latest == "1.10.2" and lines[2].started_at.day == 3


def test_build_lines_of_rpm_is_empty() -> None:
    assert build_lines("rpm", [_release("2.4.0-1.el8_10", 1, 1)]) == []
