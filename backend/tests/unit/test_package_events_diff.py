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
