"""The replay of the migration that repairs stored release events (#56, #58)."""

import importlib.util
from pathlib import Path

from app.ecosystems.pypi.versions import PyPIVersionScheme

_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "20261106_0014_repair_digest_events.py"
_SPEC = importlib.util.spec_from_file_location("repair_digest_events", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)

SCHEME = PyPIVersionScheme()


def versions(*names: str) -> dict[str, tuple[bytes, bool]]:
    return {name: (SCHEME.sort_key(name), SCHEME.is_prerelease(name)) for name in names}


def event(id: int, version: str, previous: str | None, bump: str | None, **extra) -> migration.StoredEvent:
    return migration.StoredEvent(id, version, {"previous_version": previous, "bump": bump, "prerelease": False, **extra})


def test_backport_stops_being_the_reference_of_the_current_line() -> None:
    stored = [event(1, "7.30.2", "8.12.3", "other"), event(2, "8.12.4", "7.30.2", "major")]

    changed = migration.replay(stored, versions("7.30.1", "8.12.3", "7.30.2", "8.12.4"), SCHEME.bump)

    assert changed[1]["previous_version"] == "7.30.1"
    assert changed[1]["bump"] == "patch"
    assert changed[2]["previous_version"] == "8.12.3"
    assert changed[2]["bump"] == "patch"


def test_three_lines_in_one_sync() -> None:
    stored = [event(1, "5.2.19", "6.1.2", "other"), event(2, "6.0.10", "5.2.19", "major"),
              event(3, "6.1.3", "6.0.10", "minor")]

    changed = migration.replay(stored, versions("5.2.18", "6.0.9", "6.1.2", "5.2.19", "6.0.10", "6.1.3"), SCHEME.bump)

    assert [changed[i]["bump"] for i in (1, 2, 3)] == ["patch", "patch", "patch"]
    assert changed[3]["previous_version"] == "6.1.2"


def test_a_correct_event_is_not_touched() -> None:
    stored = [event(1, "4.0.0", "3.0.0", "major")]

    assert migration.replay(stored, versions("3.0.0", "4.0.0"), SCHEME.bump) == {}


def test_prerelease_then_release_keeps_measuring_from_the_stable_one() -> None:
    stored = [event(1, "3.2.0rc1", "3.1.1", "pre"), event(2, "3.2.0", "3.2.0rc1", "minor")]

    assert migration.replay(stored, versions("3.1.1", "3.2.0rc1", "3.2.0"), SCHEME.bump) == {}


def test_a_later_sync_sees_what_an_earlier_one_added() -> None:
    stored = [event(1, "2.0.1", "2.0.0", "patch"), event(2, "2.0.2", "2.0.1", "patch")]

    assert migration.replay(stored, versions("2.0.0", "2.0.1", "2.0.2"), SCHEME.bump) == {}


def test_backfill_is_marked_and_other_details_stay() -> None:
    stored = [event(1, "0.4.1.post2", "2.0.0", "other", modules=["m"]), event(2, "2.0.1", "2.0.0", "patch")]

    changed = migration.replay(stored, versions("2.0.0", "0.4.1.post2", "2.0.1"), SCHEME.bump, backfill={1})

    assert changed[1]["backfill"] is True
    assert changed[1]["modules"] == ["m"]
    assert 2 not in changed


def test_nothing_lower_known_keeps_the_stored_previous_version() -> None:
    stored = [event(1, "1.0.0", "2.0.0", "other")]

    assert migration.replay(stored, versions("2.0.0", "1.0.0"), SCHEME.bump) == {}


def test_version_no_longer_in_the_catalog_is_skipped() -> None:
    stored = [event(1, "9.9.9", "1.0.0", "major")]

    assert migration.replay(stored, versions("1.0.0"), SCHEME.bump) == {}
