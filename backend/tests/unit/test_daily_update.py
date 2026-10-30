import pytest

from app.ecosystems.base import PackageRef
from app.tasks.daily_update import PENDING, _carry_over, _Work, is_stale


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


def test_still_stale_packages_get_one_more_run() -> None:
    fresh = _Work("p1", PackageRef("npm", "", "react"), expected={"r1": 'not:"e1"'})
    again = _Work("p2", PackageRef("npm", "", "vue"), expected={"r1": 'not:"e1"'}, pending=True)
    other = _Work("p3", PackageRef("npm", "", "ms"), expected={"r2": 'not:"e1"'})
    cursors: dict = {"r1": {"last_seq": 10}}
    _carry_over(cursors, [fresh, again, other])
    # r2 failed detection this run: no cursor to write to; vue was already carried over once.
    assert cursors == {"r1": {"last_seq": 10, PENDING: ["p1"]}}
