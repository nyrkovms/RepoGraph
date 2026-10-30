"""The npm replication feed detector (IMPL F4, SPEC §8.2) on canned feed pages."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ecosystems.base import ChangeSet, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.npm import changes
from app.ecosystems.npm.changes import NpmChangesDetector

FEED = "https://replicate.example/registry/"
REGISTRY = RegistryConfig(id="r1", slug="npmjs", ecosystem="npm", protocol="npm-registry",
                          base_url="https://registry.example/", priority=0, config={"changes_url": FEED}, routing=None,
                          auth_type="none", credentials_ref=None)
FRESH = {"reconciled_at": datetime.now(UTC).isoformat()}


def _tracked(name: str, revision: str | None = '"etag-1"') -> TrackedPackage:
    namespace, _, local = name[1:].partition("/") if name.startswith("@") else ("", "", name)
    return TrackedPackage(ref=PackageRef("npm", namespace, local), package_id=f"id-{name}", revision=revision,
                          etag=None, unavailable=False)


def _serve(monkeypatch, update_seq: int, pages: dict[int, object]) -> list[str]:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path.endswith("/_changes"):
            answer = pages[int(request.url.params["since"])]
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        return httpx.Response(200, json={"db_name": "registry", "update_seq": update_seq})

    monkeypatch.setattr(changes, "make_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


def _page(last_seq: int, *ids: str, deleted: str | None = None) -> dict:
    results = [{"seq": last_seq, "id": i, "changes": [{"rev": "1-a"}]} for i in ids]
    if deleted:
        results.append({"seq": last_seq, "id": deleted, "deleted": True, "changes": [{"rev": "2-b"}]})
    return {"results": results, "last_seq": last_seq}


def _detect(tracked, cursor, page_size: int = 2):
    return asyncio.run(NpmChangesDetector(REGISTRY, FEED, page_size=page_size).detect(tracked, cursor))


def test_pages_up_to_the_update_seq_filtered_by_the_catalog(monkeypatch) -> None:
    asked = _serve(monkeypatch, 300, {100: _page(200, "react", "unrelated"), 200: _page(300, "@angular/core",
                                                                                         deleted="left-pad")})
    tracked = [_tracked("react"), _tracked("@angular/core", None), _tracked("left-pad"), _tracked("vue")]
    result = _detect(tracked, {"last_seq": 100, **FRESH})
    assert {(p.ref.name, rev) for p, rev in result.changed} == {
        ("react", 'not:"etag-1"'), ("core", None), ("left-pad", 'not:"etag-1"')}   # deleted: re-read gives 404
    assert (result.cursor["last_seq"], result.reconcile_all, result.missing) == (300, False, [])
    assert len([u for u in asked if "_changes" in u]) == 2


def test_first_run_starts_the_feed_and_checks_everything(monkeypatch) -> None:
    _serve(monkeypatch, 500, {})
    result = _detect([_tracked("react")], {})
    assert (result.changed, result.cursor["last_seq"], result.reconcile_all) == ([], 500, True)


@pytest.mark.parametrize("pages", [{900: httpx.Response(400, json={"error": "bad since"})}, {}])
def test_lost_or_reset_feed_asks_for_a_full_check(monkeypatch, pages) -> None:
    _serve(monkeypatch, 500 if not pages else 1000, pages)
    cursor = {"last_seq": 900, **FRESH}
    result = _detect([_tracked("react")], cursor)
    assert result.reconcile_all is True and result.changed == []


def test_failure_after_a_page_keeps_what_was_read(monkeypatch) -> None:
    _serve(monkeypatch, 300, {100: _page(200, "react"), 200: httpx.Response(503)})
    result = _detect([_tracked("react")], {"last_seq": 100, **FRESH})
    assert ([p.ref.name for p, _ in result.changed], result.cursor["last_seq"]) == (["react"], 200)


def test_failure_on_the_first_page_raises(monkeypatch) -> None:
    _serve(monkeypatch, 300, {100: httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        _detect([_tracked("react")], {"last_seq": 100, **FRESH})


def test_a_full_check_once_a_week(monkeypatch) -> None:
    _serve(monkeypatch, 100, {})
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    result = _detect([_tracked("react")], {"last_seq": 100, "reconciled_at": old})
    assert result.reconcile_all is True and result.cursor["reconciled_at"] > old
    result = _detect([_tracked("react")], {"last_seq": 100, **FRESH})
    assert result.reconcile_all is False and result.cursor["reconciled_at"] == FRESH["reconciled_at"]


class _Reconciler:
    def __init__(self, changed: list[tuple[TrackedPackage, str | None]], missing: list[TrackedPackage]) -> None:
        self.result = ChangeSet(changed=changed, missing=missing, cursor={})
        self.asked: list[list[str]] = []

    async def detect(self, tracked, cursor) -> ChangeSet:
        self.asked.append([p.ref.name for p in tracked])
        return self.result


def test_a_full_check_goes_through_the_reconciler(monkeypatch) -> None:
    react, vue, gone = _tracked("react"), _tracked("vue"), _tracked("gone")
    _serve(monkeypatch, 300, {100: _page(300, "react", "vue")})
    reconciler = _Reconciler([(vue, '"etag-2"')], [gone])
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    result = asyncio.run(NpmChangesDetector(REGISTRY, FEED, reconciler=reconciler)
                         .detect([react, vue, gone], {"last_seq": 100, "reconciled_at": old}))
    assert reconciler.asked == [["react", "vue", "gone"]]
    # The ETag answer wins; a feed entry the packument does not show yet keeps waiting for a new revision.
    assert sorted((p.ref.name, rev) for p, rev in result.changed) == [("react", 'not:"etag-1"'), ("vue", '"etag-2"')]
    assert (result.missing, result.reconcile_all, result.cursor["last_seq"]) == ([gone], False, 300)

    reconciler.asked.clear()
    asyncio.run(NpmChangesDetector(REGISTRY, FEED, reconciler=reconciler).detect([react], {"last_seq": 300, **FRESH}))
    assert reconciler.asked == []


def test_a_string_seq_fails_the_source_instead_of_freezing_the_cursor(monkeypatch) -> None:
    _serve(monkeypatch, 300, {100: {"results": [{"seq": "200-g1AAA", "id": "react"}], "last_seq": "200-g1AAA"}})
    with pytest.raises(ValueError, match="not a number"):
        _detect([_tracked("react")], {"last_seq": 100, **FRESH})
