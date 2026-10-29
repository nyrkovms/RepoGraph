from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.updates import SummaryEvent, aggregate_summary, compute_summary_window, compute_totals

T0 = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)


def ev(package, kind, minutes=0, version=None, details=None, id=0, ecosystem="pypi", namespace=""):
    return SummaryEvent(
        package=package,
        kind=kind,
        detected_at=T0 + timedelta(minutes=minutes),
        version=version,
        details=details or {},
        id=id,
        ecosystem=ecosystem,
        namespace=namespace,
    )


def release(package, version, previous, bump, minutes=0, id=0):
    return ev(package, "release_added", minutes, version, {"previous_version": previous, "bump": bump}, id)


def test_from_to_and_max_bump_across_releases() -> None:
    events = [
        release("flask", "3.2.0", "3.2.0rc1", "patch", minutes=2, id=3),
        release("flask", "3.2.0rc1", "3.1.1", "minor", minutes=1, id=2),
    ]
    (pkg,) = aggregate_summary(events)
    assert pkg.name == "flask"
    assert pkg.from_version == "3.1.1"
    assert pkg.to_version == "3.2.0"
    assert pkg.releases == ["3.2.0rc1", "3.2.0"]
    assert pkg.max_bump == "minor"
    assert pkg.flags == []
    assert pkg.last_detected_at == T0 + timedelta(minutes=2)


def test_release_order_uses_id_as_tiebreaker() -> None:
    events = [release("a", "1.0.2", "1.0.1", "patch", id=2), release("a", "1.0.1", "1.0.0", "patch", id=1)]
    (pkg,) = aggregate_summary(events)
    assert pkg.releases == ["1.0.1", "1.0.2"]
    assert pkg.from_version == "1.0.0"
    assert pkg.to_version == "1.0.2"


@pytest.mark.parametrize(
    ("bumps", "expected"),
    [
        (["other", "pre"], "pre"),
        (["pre", "patch"], "patch"),
        (["patch", "minor", "pre"], "minor"),
        (["minor", "major", "patch"], "major"),
        (["other"], "other"),
    ],
)
def test_max_bump_order(bumps, expected) -> None:
    events = [release("x", f"1.{i}", None, b, minutes=i, id=i) for i, b in enumerate(bumps)]
    (pkg,) = aggregate_summary(events)
    assert pkg.max_bump == expected


def test_major_flag_and_missing_previous_version() -> None:
    (pkg,) = aggregate_summary([release("django", "6.0", None, "major")])
    assert pkg.from_version is None
    assert pkg.to_version == "6.0"
    assert pkg.flags == ["major"]


def test_flags_order_and_lists() -> None:
    events = [
        release("pkg", "2.0.0", "1.9.0", "major", minutes=1, id=1),
        ev("pkg", "release_yanked", 2, "1.8.0", id=2),
        ev("pkg", "release_removed", 3, "1.7.0", id=3),
        ev("pkg", "vulnerability_added", 4, None, {"id": "GHSA-1"}, id=4),
        ev("pkg", "project_unavailable", 5, id=5),
    ]
    (pkg,) = aggregate_summary(events)
    assert pkg.flags == ["unavailable", "vulnerability_added", "removed", "yanked", "major"]
    assert pkg.yanked == ["1.8.0"]
    assert pkg.removed == ["1.7.0"]
    assert pkg.vulnerabilities == ["GHSA-1"]


def test_tracking_started_is_ignored() -> None:
    events = [
        ev("new", "tracking_started", 0, details={"latest_version": "1.0"}),
        ev("old", "tracking_started", 0),
        release("old", "1.1", "1.0", "minor", minutes=1),
    ]
    packages = aggregate_summary(events)
    assert [p.name for p in packages] == ["old"]
    assert packages[0].last_detected_at == T0 + timedelta(minutes=1)


def test_accepts_enum_kinds() -> None:
    from app.models.enums import PackageEventKind

    events = [ev("a", PackageEventKind.release_yanked, 0, "1.0"), ev("a", PackageEventKind.tracking_started, 1)]
    (pkg,) = aggregate_summary(events)
    assert pkg.yanked == ["1.0"]
    assert pkg.last_detected_at == T0


def test_sorting_flags_first_by_severity_then_recency_then_name() -> None:
    events = [
        release("plain-new", "1.1", "1.0", "minor", minutes=50),
        release("plain-old", "1.1", "1.0", "minor", minutes=10),
        release("major-pkg", "2.0", "1.0", "major", minutes=5),
        ev("yanked-pkg", "release_yanked", 6, "1.0"),
        ev("vuln-old", "vulnerability_added", 1, None, {"id": "V1"}),
        ev("vuln-new", "vulnerability_added", 7, None, {"id": "V2"}),
        ev("gone", "project_unavailable", 0),
        release("b-tie", "1.1", "1.0", "minor", minutes=10),
    ]
    names = [p.name for p in aggregate_summary(events)]
    assert names == [
        "gone",  # unavailable
        "vuln-new",  # vulnerability, more recent first
        "vuln-old",
        "yanked-pkg",
        "major-pkg",
        "plain-new",  # unflagged, by recency
        "b-tie",  # same recency as plain-old, name order
        "plain-old",
    ]


def test_most_severe_flag_wins_for_ranking() -> None:
    events = [
        release("multi", "2.0", "1.0", "major", minutes=1),
        ev("multi", "vulnerability_added", 1, None, {"id": "V"}),
        ev("yank-only", "release_yanked", 100, "1.0"),
    ]
    assert [p.name for p in aggregate_summary(events)] == ["multi", "yank-only"]


def test_totals() -> None:
    events = [
        ev("ignored", "tracking_started"),
        release("a", "1.1", "1.0", "minor", minutes=1),
        release("a", "1.2", "1.1", "minor", minutes=2),
        release("b", "2.0", "1.0", "major", minutes=3),
        ev("b", "release_yanked", 4, "1.5"),
        ev("c", "release_removed", 5, "0.1"),
        ev("c", "vulnerability_added", 6, None, {"id": "V"}),
        ev("d", "project_unavailable", 7),
    ]
    packages = aggregate_summary(events)
    totals = compute_totals(events, packages)
    assert totals.model_dump() == {
        "packages": 4,
        "releases": 3,
        "yanked": 1,
        "removed": 1,
        "deprecated": 0,
        "sources_added": 0,
        "vulnerabilities": 1,
        "unavailable": 1,
    }


def test_same_name_in_two_ecosystems_is_two_packages() -> None:
    events = [
        ev("x", "release_yanked", 1, "1.0"),
        ev("x", "release_yanked", 2, "2.0", ecosystem="npm"),
        ev("x", "release_yanked", 3, "3.0", ecosystem="npm", namespace="corp"),   # stored without the @ (SPEC §4)
    ]
    packages = aggregate_summary(events)
    assert {(p.ecosystem, p.purl): p.yanked for p in packages} == {
        ("pypi", "pkg:pypi/x"): ["1.0"],
        ("npm", "pkg:npm/x"): ["2.0"],
        ("npm", "pkg:npm/%40corp/x"): ["3.0"],
    }
    assert compute_totals(events, packages).packages == 3


def test_deprecated_and_sources_added() -> None:
    events = [
        release("pkg", "2.0.0", "1.9.0", "major", minutes=1, id=1),
        ev("pkg", "release_yanked", 2, "1.8.0", id=2),
        ev("pkg", "release_deprecated", 3, "1.7.0", {"message": "old"}, id=3),
        ev("pkg", "source_added", 4, None, {"registry": "corp-pypi"}, id=4),
        ev("pkg", "source_added", 5, "2.0.0", {"registry": "corp-pypi"}, id=5),
        ev("pkg", "source_added", 6, "2.0.0", {"registry": "mirror"}, id=6),
        ev("other", "source_added", 7, "3.1", id=7),
    ]
    packages = aggregate_summary(events)
    pkg = next(p for p in packages if p.name == "pkg")
    assert pkg.flags == ["yanked", "deprecated", "major"]
    assert pkg.deprecated == ["1.7.0"]
    assert pkg.sources_added == ["corp-pypi", "mirror"]
    other = next(p for p in packages if p.name == "other")
    assert other.flags == [] and other.sources_added == ["3.1"]      # no registry in details: the version
    totals = compute_totals(events, packages)
    assert (totals.deprecated, totals.sources_added) == (1, 4)


def test_deprecated_ranks_between_yanked_and_major() -> None:
    events = [
        release("major-pkg", "2.0", "1.0", "major", minutes=5),
        ev("deprecated-pkg", "release_deprecated", 1, "1.0"),
        ev("yanked-pkg", "release_yanked", 1, "1.0"),
    ]
    assert [p.name for p in aggregate_summary(events)] == ["yanked-pkg", "deprecated-pkg", "major-pkg"]


# --- window computation

NOW = datetime(2026, 10, 27, 1, 30, tzinfo=timezone.utc)


def test_window_day_utc() -> None:
    since, until = compute_summary_window("day", "UTC", now=NOW)
    assert since == datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
    assert until == NOW


def test_window_week_utc_is_seven_calendar_days() -> None:
    since, until = compute_summary_window("week", "UTC", now=NOW)
    assert since == datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
    assert until == NOW


def test_window_day_respects_timezone() -> None:
    # 01:30 UTC is still Oct 26 in New York (UTC-4 until Nov 1), so "today" started Oct 26 04:00 UTC.
    since, _ = compute_summary_window("day", "America/New_York", now=NOW)
    assert since == datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
    # Ahead of UTC: 01:30 UTC is 10:30 Oct 27 in Tokyo, start of day = Oct 26 15:00 UTC.
    since, _ = compute_summary_window("day", "Asia/Tokyo", now=NOW)
    assert since == datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


def test_window_week_across_dst_change() -> None:
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)  # US DST ended Nov 1
    since, _ = compute_summary_window("week", "America/New_York", now=now)
    # Oct 28 00:00 EDT (UTC-4)
    assert since == datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)


def test_explicit_since_until_win_over_period_and_tz() -> None:
    s = datetime(2026, 10, 1, tzinfo=timezone.utc)
    u = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert compute_summary_window("week", "Asia/Tokyo", s, u, now=NOW) == (s, u)


def test_naive_bounds_are_treated_as_utc() -> None:
    s, u = compute_summary_window("day", "UTC", datetime(2026, 10, 1), datetime(2026, 10, 2), now=NOW)
    assert s.tzinfo is not None and s == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert u == datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_single_explicit_bound_uses_period_default_for_other() -> None:
    s = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert compute_summary_window("day", "UTC", since=s, now=NOW) == (s, NOW)
    u = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
    since, until = compute_summary_window("week", "UTC", until=u, now=NOW)
    assert (since, until) == (datetime(2026, 10, 21, tzinfo=timezone.utc), u)


def test_invalid_timezone_and_empty_window_raise() -> None:
    with pytest.raises(ValueError):
        compute_summary_window("day", "Mars/Olympus", now=NOW)
    with pytest.raises(ValueError):
        compute_summary_window("day", "../etc/passwd", now=NOW)
    with pytest.raises(ValueError):
        compute_summary_window("day", "UTC", NOW, NOW, now=NOW)
