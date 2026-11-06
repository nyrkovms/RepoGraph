from datetime import UTC, datetime

import pytest

from app.connectors.base import NormalizedPackage, NormalizedVersion, NormalizedVulnerability
from app.ecosystems.merge import merge_by_priority
from app.ecosystems.pypi.adapters import to_contract
from app.ecosystems.pypi.versions import PyPIVersionScheme
from app.fetcher.events import PackageSnapshot, classify_bump
from app.fetcher.events import diff_package as _diff
from app.models.enums import PackageEventKind, PackageVersionStatus

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


def _version(version: str, status: str = "active", yanked_reason: str | None = None) -> NormalizedVersion:
    return NormalizedVersion(
        version=version,
        release_date=NOW if status != "removed" else None,
        status=status,
        requires_python=None,
        raw_metadata={},
        yanked_reason=yanked_reason,
    )


def _vuln(source_id: str, withdrawn_at: datetime | None = None) -> NormalizedVulnerability:
    return NormalizedVulnerability(
        source="pypi_native",
        source_id=source_id,
        aliases=["CVE-2026-0001"],
        summary="bad",
        details=None,
        severity=None,
        fixed_in=["3.2.1"],
        published_at=NOW,
        withdrawn_at=withdrawn_at,
        link="https://example.org",
    )


def _package(versions: list[NormalizedVersion], vulns: list[NormalizedVulnerability] | None = None) -> NormalizedPackage:
    return NormalizedPackage(
        ecosystem="pypi",
        name="flask",
        normalized_name="flask",
        description=None,
        homepage_url=None,
        repository_url=None,
        documentation_url=None,
        license_raw=None,
        license_spdx=None,
        keywords=None,
        latest_version=versions[-1].version,
        downloads_total=None,
        downloads_recent=None,
        created_at=NOW,
        updated_at=NOW,
        raw_metadata={},
        versions=versions,
        vulnerabilities=vulns or [],
    )


def _snapshot(statuses: dict[str, str], latest: str, vulns: dict | None = None, unavailable: bool = False):
    return PackageSnapshot(
        latest_version=latest,
        version_statuses={name: PackageVersionStatus(status) for name, status in statuses.items()},
        vulnerability_withdrawn=vulns or {},
        unavailable=unavailable,
    )


def diff_package(before, after: NormalizedPackage):
    """The legacy connector output, as the pipeline sees it: one source, merged."""
    return _diff(before, merge_by_priority([to_contract(after, "reg-pypi")]), PyPIVersionScheme(), after.latest_version)


def _kinds(events) -> list[tuple[PackageEventKind, str | None]]:
    return [(event.kind, event.version) for event in events]


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("3.1.1", "3.1.2", "patch"),
        ("3.1.1", "3.2.0", "minor"),
        ("3.1.1", "4.0.0", "major"),
        ("3.1.1", "3.2.0rc1", "pre"),
        ("3.1.1", "3.2.0.dev1", "pre"),
        ("3.1.1", "3.0.9", "other"),
        ("2", "2.1", "minor"),
        ("2.8.0", "2.8.0+cpu", "other"),
        ("not-a-version", "1.0", "other"),
        (None, "1.0", None),
    ],
)
def test_classify_bump(previous, current, expected) -> None:
    assert classify_bump(previous, current) == expected


def test_first_fetch_only_starts_tracking() -> None:
    events = diff_package(None, _package([_version("1.0"), _version("2.0")]))

    assert _kinds(events) == [(PackageEventKind.tracking_started, None)]
    assert events[0].details == {"latest_version": "2.0"}


def test_unchanged_package_produces_no_events() -> None:
    before = _snapshot({"1.0": "active", "2.0": "yanked"}, latest="1.0", vulns={"PYSEC-1": None})
    after = _package([_version("1.0"), _version("2.0", "yanked")], [_vuln("PYSEC-1")])

    assert diff_package(before, after) == []


def test_new_releases_chain_previous_versions() -> None:
    before = _snapshot({"3.1.1": "active"}, latest="3.1.1")
    after = _package([_version("3.1.1"), _version("3.2.0"), _version("3.2.0rc1")])

    events = diff_package(before, after)

    assert _kinds(events) == [
        (PackageEventKind.release_added, "3.2.0rc1"),
        (PackageEventKind.release_added, "3.2.0"),
    ]
    assert events[0].details == {"previous_version": "3.1.1", "bump": "pre", "prerelease": True}
    assert events[1].details == {"previous_version": "3.2.0rc1", "bump": "minor", "prerelease": False}
    assert events[1].occurred_at == NOW


def test_new_release_without_files_is_ignored() -> None:
    before = _snapshot({"1.0": "active"}, latest="1.0")

    assert diff_package(before, _package([_version("1.0"), _version("1.1", "removed")])) == []


def test_status_transitions() -> None:
    before = _snapshot(
        {"1.0": "active", "1.1": "yanked", "1.2": "active", "1.3": "removed", "1.4": "active"},
        latest="1.4",
    )
    after = _package(
        [
            _version("1.0", "yanked", yanked_reason="broken wheel"),
            _version("1.1"),
            _version("1.2", "removed"),
            _version("1.3"),
        ]
    )

    events = diff_package(before, after)

    assert _kinds(events) == [
        (PackageEventKind.release_yanked, "1.0"),
        (PackageEventKind.release_unyanked, "1.1"),
        (PackageEventKind.release_removed, "1.2"),
        (PackageEventKind.release_restored, "1.3"),
        (PackageEventKind.release_removed, "1.4"),
    ]
    assert events[0].details == {"reason": "broken wheel"}


def test_vulnerability_added_and_withdrawn() -> None:
    before = _snapshot({"1.0": "active"}, latest="1.0", vulns={"PYSEC-1": None})
    after = _package([_version("1.0")], [_vuln("PYSEC-1", withdrawn_at=NOW), _vuln("GHSA-2")])

    events = diff_package(before, after)

    assert [event.kind for event in events] == [
        PackageEventKind.vulnerability_withdrawn,
        PackageEventKind.vulnerability_added,
    ]
    assert events[1].details["id"] == "GHSA-2"
    assert events[1].occurred_at == NOW


def test_unavailable_package_is_restored() -> None:
    before = _snapshot({"1.0": "active"}, latest="1.0", unavailable=True)

    events = diff_package(before, _package([_version("1.0")]))

    assert _kinds(events) == [(PackageEventKind.project_restored, None)]


def test_a_vanished_rpm_build_is_superseded_without_an_event_and_a_modular_release_names_its_stream() -> None:
    before = _snapshot({"1.0": "active", "2.0": "active"}, latest="2.0")
    merged = merge_by_priority([to_contract(_package([_version("2.0")]), "reg-rpm")])
    # Default: a version gone from every source is removed, with an event.
    assert _kinds(_diff(before, merged, PyPIVersionScheme(), "2.0")) == [(PackageEventKind.release_removed, "1.0")]
    # RPM (Q3, §8.3): superseded, no event.
    assert _diff(before, merged, PyPIVersionScheme(), "2.0", vanished="superseded") == []
    modular = _version("3.0")
    modular.raw_metadata = {"modules": ["nodejs:18"]}
    added = _diff(before, merge_by_priority([to_contract(_package([_version("1.0"), _version("2.0"), modular]),
                                                         "reg-rpm")]), PyPIVersionScheme(), "3.0")
    assert added[0].kind == PackageEventKind.release_added and added[0].details["modules"] == ["nodejs:18"]


def test_a_tagless_go_module_s_older_pseudo_version_is_superseded_without_an_event() -> None:
    """#19: the proxy lists only `@latest` for a module without tags; the previous pseudo-version is still served."""
    from app.ecosystems.go.plugin import PLUGIN as GO

    old, new = "v0.0.0-20261001000000-aaaaaaaaaaaa", "v0.0.0-20261030000000-bbbbbbbbbbbb"
    before = _snapshot({old: "active", "v1.0.0": "active"}, latest=old)
    merged = merge_by_priority([to_contract(_package([_version(new)]), "reg-proxy")])
    events = _diff(before, merged, PyPIVersionScheme(), new, vanished=GO.vanished_status)
    # The vanished tag is still a removal; the vanished pseudo-version is not.
    assert [(e.kind, e.version) for e in events if e.kind != PackageEventKind.release_added] == [
        (PackageEventKind.release_removed, "v1.0.0")]
    assert GO.vanished_status(old) == "superseded" and GO.vanished_status("v1.0.0") == "removed"


def _release_details(events) -> dict[str, tuple[str | None, str | None]]:
    return {e.version: (e.details["previous_version"], e.details["bump"]) for e in events}


def test_backport_does_not_become_the_reference_of_the_current_line() -> None:
    before = _snapshot({"8.12.3": "active", "7.30.1": "active"}, latest="8.12.3")
    after = _package([_version("7.30.1"), _version("8.12.3"), _version("7.30.2"), _version("8.12.4")])

    assert _release_details(diff_package(before, after)) == {
        "7.30.2": ("7.30.1", "patch"),
        "8.12.4": ("8.12.3", "patch"),
    }


def test_patches_on_three_lines_are_measured_each_against_its_own_line() -> None:
    before = _snapshot({"6.1.2": "active", "6.0.9": "active", "5.2.18": "active"}, latest="6.1.2")
    after = _package([_version(v) for v in ("5.2.18", "6.0.9", "6.1.2", "5.2.19", "6.0.10", "6.1.3")])

    assert _release_details(diff_package(before, after)) == {
        "5.2.19": ("5.2.18", "patch"),
        "6.0.10": ("6.0.9", "patch"),
        "6.1.3": ("6.1.2", "patch"),
    }


def test_new_major_is_still_major() -> None:
    before = _snapshot({"3.0.0": "active"}, latest="3.0.0")

    events = diff_package(before, _package([_version("3.0.0"), _version("4.0.0")]))

    assert _release_details(events) == {"4.0.0": ("3.0.0", "major")}


def test_version_below_everything_known_falls_back_to_latest_and_is_not_major() -> None:
    before = _snapshot({"2.0.0": "active"}, latest="2.0.0")

    events = diff_package(before, _package([_version("2.0.0"), _version("1.0.0")]))

    assert _release_details(events) == {"1.0.0": ("2.0.0", "other")}


def _two_sources(primary: list[NormalizedVersion], added: list[NormalizedVersion]):
    """pypi (known) and a second index (new to the package), merged as the pipeline does."""
    return merge_by_priority([to_contract(_package(primary), "reg-pypi"), to_contract(_package(added), "reg-torch")])


def _with_known_source(statuses: dict[str, str], latest: str) -> PackageSnapshot:
    snapshot = _snapshot(statuses, latest)
    snapshot.registry_ids = {"reg-pypi"}
    return snapshot


def test_versions_that_arrive_only_with_a_new_source_are_backfill() -> None:
    before = _with_known_source({"2.0": "active"}, latest="2.0")
    merged = _two_sources([_version("2.0")], [_version("0.1.8"), _version("2.0")])

    events = _diff(before, merged, PyPIVersionScheme(), "2.0")

    releases = {e.version: e.details for e in events if e.kind == PackageEventKind.release_added}
    assert releases["0.1.8"]["backfill"] is True
    assert [e.kind for e in events].count(PackageEventKind.source_added) == 1


def test_a_version_in_a_new_and_a_known_source_is_not_backfill() -> None:
    before = _with_known_source({"2.0": "active"}, latest="2.0")
    merged = _two_sources([_version("2.0"), _version("2.1")], [_version("2.1")])

    events = _diff(before, merged, PyPIVersionScheme(), "2.1")

    (release,) = [e for e in events if e.kind == PackageEventKind.release_added]
    assert "backfill" not in release.details


def test_nothing_is_backfill_when_the_known_sources_are_not_loaded() -> None:
    before = _snapshot({"2.0": "active"}, latest="2.0")
    merged = _two_sources([_version("2.0")], [_version("0.1.8")])

    events = _diff(before, merged, PyPIVersionScheme(), "2.0")

    assert all("backfill" not in e.details for e in events)


def test_a_backfilled_yanked_version_gets_no_yank_event() -> None:
    before = _with_known_source({"2.0": "active"}, latest="2.0")
    merged = _two_sources([_version("2.0")], [_version("0.1.8", "yanked", "old"), _version("2.0")])

    events = _diff(before, merged, PyPIVersionScheme(), "2.0")

    assert PackageEventKind.release_yanked not in [e.kind for e in events]
