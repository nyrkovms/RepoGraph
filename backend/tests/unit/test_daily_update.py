import pytest

from app.tasks.daily_update import is_stale


@pytest.mark.parametrize(("expected", "stored", "stale"), [
    ("150", "140", True),       # the JSON API still serves the version before the release the index announced
    ("150", "150", False),
    ("150", "151", False),
    (None, "140", False),       # the source no longer lists the package: nothing to wait for
    ("150", None, False),
    ('"etag-2"', '"etag-1"', False),   # ETags are not ordered
])
def test_is_stale(expected: str | None, stored: str | None, stale: bool) -> None:
    assert is_stale(expected, stored) is stale


def test_changed_since_revision_is_stale_while_the_answer_is_unchanged() -> None:
    # npm: the feed only knows the package changed; an answer with the stored ETag is a lagging CDN.
    assert is_stale('not:"e1"', '"e1"') is True
    assert is_stale('not:"e1"', '"e2"') is False
