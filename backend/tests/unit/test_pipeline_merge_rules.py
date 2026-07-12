from dataclasses import dataclass

from app.fetcher.pipeline import find_checksum_conflicts, protocol_richness
from app.tasks.enumeration import select_enumeration_batch


def test_protocol_richness_ordering():
    assert protocol_richness("pypi-json") > protocol_richness("pypi-simple")
    assert protocol_richness("npm") > protocol_richness("pypi-simple")
    assert protocol_richness("pypi-simple") > protocol_richness(None)
    assert protocol_richness("unknown-protocol") == 0


@dataclass
class FakeArtifactRow:
    version_id: str
    registry_id: str
    filename: str
    checksum_sha256: str | None


def test_checksum_conflict_detected_across_registries():
    rows = [
        FakeArtifactRow("v1", "reg-public", "pkg-1.0-py3-none-any.whl", "aaa"),
        FakeArtifactRow("v1", "reg-internal", "pkg-1.0-py3-none-any.whl", "bbb"),
    ]
    assert find_checksum_conflicts(rows) == {"reg-public", "reg-internal"}


def test_same_checksum_is_not_a_conflict():
    rows = [
        FakeArtifactRow("v1", "reg-public", "pkg-1.0-py3-none-any.whl", "aaa"),
        FakeArtifactRow("v1", "reg-internal", "pkg-1.0-py3-none-any.whl", "aaa"),
    ]
    assert find_checksum_conflicts(rows) == set()


def test_null_checksums_ignored():
    rows = [
        FakeArtifactRow("v1", "reg-public", "pkg-1.0-py3-none-any.whl", None),
        FakeArtifactRow("v1", "reg-internal", "pkg-1.0-py3-none-any.whl", "bbb"),
    ]
    assert find_checksum_conflicts(rows) == set()


def test_different_filenames_never_conflict():
    rows = [
        FakeArtifactRow("v1", "reg-public", "pkg-1.0-py3-none-any.whl", "aaa"),
        FakeArtifactRow("v1", "reg-internal", "pkg-1.0.tar.gz", "bbb"),
    ]
    assert find_checksum_conflicts(rows) == set()


def test_enumeration_batch_walks_cursor():
    names = ["alpha", "beta", "gamma", "delta", "epsilon"]

    batch, cursor, completed = select_enumeration_batch(names, "", 2)
    assert batch == ["alpha", "beta"]
    assert cursor == "beta"
    assert completed is False

    batch, cursor, completed = select_enumeration_batch(names, cursor, 2)
    assert batch == ["delta", "epsilon"]
    assert completed is False

    batch, cursor, completed = select_enumeration_batch(names, cursor, 2)
    assert batch == ["gamma"]
    assert cursor == ""
    assert completed is True


def test_enumeration_batch_deduplicates_and_completes_small_listing():
    batch, cursor, completed = select_enumeration_batch(["b", "a", "b"], "", 10)
    assert batch == ["a", "b"]
    assert cursor == ""
    assert completed is True
