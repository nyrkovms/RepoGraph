"""The index.golang.org detector (IMPL H4, SPEC §8.1) on canned index pages."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ecosystems.base import ChangeSet, PackageRef, RegistryConfig, TrackedPackage
from app.ecosystems.go import changes
from app.ecosystems.go.changes import GoIndexChangesDetector

INDEX = "https://index.example/index"
REGISTRY = RegistryConfig(id="r1", slug="proxy-golang-org", ecosystem="go", protocol="goproxy",
                          base_url="https://proxy.example/", priority=0, config={"index_url": INDEX}, routing=None,
                          auth_type="none", credentials_ref=None)
FRESH = {"reconciled_at": datetime.now(UTC).isoformat()}
T0 = datetime(2026, 10, 29, tzinfo=UTC)


def _tracked(path: str, revision: str | None = "rev-1") -> TrackedPackage:
    return TrackedPackage(ref=PackageRef("go", "", path), package_id=f"id-{path}", revision=revision, etag=None,
                          unavailable=False)


def _line(path: str, version: str, minutes: int) -> str:
    return json.dumps({"Path": path, "Version": version,
                       "Timestamp": (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")})


def _serve(monkeypatch, pages: dict[str, object]) -> list[httpx.URL]:
    asked: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        answer = pages[request.url.params["since"]]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, text="\n".join(answer) + "\n")   # type: ignore[arg-type]

    monkeypatch.setattr(changes, "make_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asked


def _detect(tracked, cursor, page_size: int = 2, reconciler=None) -> ChangeSet:
    return asyncio.run(GoIndexChangesDetector(REGISTRY, INDEX, page_size=page_size, reconciler=reconciler)
                       .detect(tracked, cursor))


def test_pages_until_the_index_has_nothing_newer_filtered_by_the_catalog(monkeypatch) -> None:
    asked = _serve(monkeypatch, {
        "2026-10-29T00:00:00Z": [_line("github.com/gin-gonic/gin", "v1.10.1", 1), _line("example.com/other", "v1.0.0", 2)],
        "2026-10-29T00:02:00Z": [_line("golang.org/x/net", "v0.31.0", 3)],
    })
    tracked = [_tracked("github.com/gin-gonic/gin"), _tracked("golang.org/x/net", None), _tracked("rsc.io/quote")]
    result = _detect(tracked, {"since": T0.isoformat(), **FRESH})
    assert {(p.ref.name, rev) for p, rev in result.changed} == {
        ("github.com/gin-gonic/gin", "not:rev-1"), ("golang.org/x/net", None)}
    assert result.cursor["since"] == (T0 + timedelta(minutes=3)).isoformat()
    assert (result.reconcile_all, result.missing) == (False, [])
    assert [u.params["limit"] for u in asked] == ["2", "2"]


def test_pseudo_versions_change_nothing(monkeypatch) -> None:
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": [_line("golang.org/x/net", "v0.31.1-0.20261029000100-abcdefabcdef", 1)]})
    result = _detect([_tracked("golang.org/x/net")], {"since": T0.isoformat(), **FRESH})
    assert result.changed == [] and result.cursor["since"] == (T0 + timedelta(minutes=1)).isoformat()


def test_first_run_starts_now_and_checks_everything(monkeypatch) -> None:
    asked = _serve(monkeypatch, {})
    result = _detect([_tracked("rsc.io/quote")], {})
    assert result.reconcile_all is True and result.changed == [] and asked == []
    assert datetime.fromisoformat(str(result.cursor["since"])) <= datetime.now(UTC)


def test_a_refused_cursor_asks_for_a_full_check(monkeypatch) -> None:
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": httpx.Response(400, text="bad since")})
    result = _detect([_tracked("rsc.io/quote")], {"since": T0.isoformat(), **FRESH})
    assert result.reconcile_all is True and result.changed == []


def test_failure_after_a_page_keeps_what_was_read(monkeypatch) -> None:
    _serve(monkeypatch, {
        "2026-10-29T00:00:00Z": [_line("rsc.io/quote", "v1.5.3", 1), _line("example.com/x", "v1.0.0", 2)],
        "2026-10-29T00:02:00Z": httpx.Response(503),
    })
    result = _detect([_tracked("rsc.io/quote")], {"since": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["rsc.io/quote"]
    assert result.cursor["since"] == (T0 + timedelta(minutes=2)).isoformat()


def test_failure_on_the_first_page_raises(monkeypatch) -> None:
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        _detect([_tracked("rsc.io/quote")], {"since": T0.isoformat(), **FRESH})


def test_a_full_page_of_one_timestamp_does_not_loop(monkeypatch) -> None:
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": [_line("a.example/x", "v1.0.0", 0), _line("b.example/y", "v1.0.0", 0)]})
    result = _detect([_tracked("a.example/x")], {"since": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["a.example/x"] and result.cursor["since"] == T0.isoformat()


class _Reconciler:
    def __init__(self, changed, missing) -> None:
        self.result = ChangeSet(changed=changed, missing=missing, cursor={})
        self.asked = 0

    async def detect(self, tracked, cursor) -> ChangeSet:
        self.asked += 1
        return self.result


def test_the_weekly_check_goes_through_the_reconciler(monkeypatch) -> None:
    quote, gone = _tracked("rsc.io/quote"), _tracked("gone.example/x")
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": [_line("rsc.io/quote", "v1.5.3", 1)]})
    reconciler = _Reconciler([(quote, "rev-2")], [gone])
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    result = _detect([quote, gone], {"since": T0.isoformat(), "reconciled_at": old}, reconciler=reconciler)
    assert reconciler.asked == 1 and result.changed == [(quote, "rev-2")]   # the list's answer wins
    assert (result.missing, result.reconcile_all) == ([gone], False)
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": []})
    _detect([quote], {"since": T0.isoformat(), **FRESH}, reconciler=reconciler)
    assert reconciler.asked == 1


def test_pseudo_versions_count_for_a_module_without_tags(monkeypatch) -> None:
    _serve(monkeypatch, {"2026-10-29T00:00:00Z": [_line("golang.org/x/exp", "v0.0.0-20261029000100-abcdefabcdef", 1)]})
    tagless = _tracked("golang.org/x/exp", "latest:v0.0.0-20261001000000-aaaaaaaaaaaa")
    result = _detect([tagless], {"since": T0.isoformat(), **FRESH})
    assert [p.ref.name for p, _ in result.changed] == ["golang.org/x/exp"]
