"""The nuget.org catalog detector (IMPL I4, SPEC §8.1) on canned catalog documents."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ecosystems.base import ChangeSet, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.nuget import changes
from app.ecosystems.nuget.changes import NuGetCatalogChangesDetector

CATALOG = "https://catalog.example/index.json"
REGISTRY = RegistryConfig(id="r1", slug="nuget-org", ecosystem="nuget", protocol="nuget-v3",
                          base_url="https://api.example/v3/index.json", priority=0, config={"catalog_url": CATALOG},
                          routing=None, auth_type="none", credentials_ref=None)
FRESH = {"reconciled_at": datetime.now(UTC).isoformat()}
T0 = datetime(2026, 10, 29, tzinfo=UTC)


def _stamp(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _tracked(name: str, revision: str | None = "etag-1") -> TrackedPackage:
    return TrackedPackage(ref=PackageRef("nuget", "", name.lower()), package_id=f"id-{name.lower()}", revision=revision,
                          etag=None, unavailable=False)


def _entry(package_id: str, minutes: int, kind: str = "nuget:PackageDetails") -> dict:
    return {"@type": kind, "nuget:id": package_id, "nuget:version": "1.0.0", "commitTimeStamp": _stamp(minutes)}


def _serve(monkeypatch, pages: dict[str, tuple[int, list[dict] | httpx.Response]]) -> list[str]:
    asked: list[str] = []
    index = {"commitTimeStamp": _stamp(max((s for s, _ in pages.values()), default=0)),
             "items": [{"@id": f"https://catalog.example/{name}.json", "commitTimeStamp": _stamp(stamp)}
                       for name, (stamp, _) in pages.items()]}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if str(request.url) == CATALOG:
            return httpx.Response(200, json=index)
        answer = pages[request.url.path.strip("/").removesuffix(".json")][1]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json={"items": answer})

    monkeypatch.setattr(changes, "make_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


def _detect(tracked, cursor, reconciler=None) -> ChangeSet:
    return asyncio.run(NuGetCatalogChangesDetector(REGISTRY, CATALOG, reconciler=reconciler).detect(tracked, cursor))


def test_pages_newer_than_the_cursor_filtered_by_the_catalog_case_insensitively(monkeypatch) -> None:
    asked = _serve(monkeypatch, {
        "page0": (-5, [_entry("Newtonsoft.Json", -5)]),                       # older than the cursor: not read
        "page1": (3, [_entry("Newtonsoft.Json", 1), _entry("Other.Package", 2), _entry("serilog", 3, "nuget:PackageDelete")]),
        "page2": (4, [_entry("NEWTONSOFT.JSON", 4), {"@type": "nuget:Unknown", "nuget:id": "Serilog", "commitTimeStamp": _stamp(5)}]),
    })
    tracked = [_tracked("Newtonsoft.Json"), _tracked("Serilog", None), _tracked("Dapper")]
    result = _detect(tracked, {"commit": T0.isoformat(), **FRESH})
    assert {(p.ref.name, rev) for p, rev in result.changed} == {("newtonsoft.json", "not:etag-1"), ("serilog", None)}
    assert result.cursor["commit"] == (T0 + timedelta(minutes=4)).isoformat()
    assert not any("page0" in url for url in asked) and (result.reconcile_all, result.missing) == (False, [])


def test_commits_after_the_run_started_wait_for_the_next_run(monkeypatch) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    _serve(monkeypatch, {"page1": (60 * 24 * 365, [_entry("Dapper", 1), {**_entry("Serilog", 0),
                                                                          "commitTimeStamp": future.isoformat()}])})
    result = _detect([_tracked("Dapper"), _tracked("Serilog")], {"commit": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["dapper"]
    assert result.cursor["commit"] == (T0 + timedelta(minutes=1)).isoformat()


def test_first_run_starts_now_and_checks_everything(monkeypatch) -> None:
    asked = _serve(monkeypatch, {})
    result = _detect([_tracked("Dapper")], {})
    assert result.reconcile_all is True and result.changed == [] and asked == []


def test_failure_after_a_page_keeps_what_was_read(monkeypatch) -> None:
    _serve(monkeypatch, {"page1": (1, [_entry("Dapper", 1)]), "page2": (2, httpx.Response(503))})
    result = _detect([_tracked("Dapper")], {"commit": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["dapper"]
    assert result.cursor["commit"] == (T0 + timedelta(minutes=1)).isoformat()


def test_failure_on_the_index_or_the_first_page_raises(monkeypatch) -> None:
    _serve(monkeypatch, {"page1": (1, httpx.Response(503))})
    with pytest.raises(httpx.HTTPStatusError):
        _detect([_tracked("Dapper")], {"commit": T0.isoformat(), **FRESH})


class _Reconciler:
    def __init__(self, changed, missing) -> None:
        self.result = ChangeSet(changed=changed, missing=missing, cursor={})
        self.asked = 0

    async def detect(self, tracked, cursor) -> ChangeSet:
        self.asked += 1
        return self.result


def test_the_weekly_check_goes_through_the_reconciler(monkeypatch) -> None:
    dapper, gone = _tracked("Dapper"), _tracked("Gone.Package")
    _serve(monkeypatch, {"page1": (1, [_entry("Dapper", 1)])})
    reconciler = _Reconciler([(dapper, "etag-2")], [gone])
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    result = _detect([dapper, gone], {"commit": T0.isoformat(), "reconciled_at": old}, reconciler=reconciler)
    assert reconciler.asked == 1 and result.changed == [(dapper, "etag-2")]
    assert (result.missing, result.reconcile_all) == ([gone], False)


def test_entries_past_the_index_commit_wait_for_the_next_run(monkeypatch) -> None:
    # The index says its last commit is at minute 2; an entry at minute 5 belongs to a commit still being written.
    _serve(monkeypatch, {"page1": (2, [_entry("Dapper", 2), _entry("Serilog", 5)])})
    result = _detect([_tracked("Dapper"), _tracked("Serilog")], {"commit": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["dapper"]
    assert result.cursor["commit"] == (T0 + timedelta(minutes=2)).isoformat()
