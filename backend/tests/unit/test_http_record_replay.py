import asyncio
import gzip

import httpx
import pytest

from app.config import get_settings
from app.services.http import RecordingTransport, ReplayMiss, ReplayTransport, make_client
from app.services.retry_policy import is_retryable_upstream_error


def _upstream(request: httpx.Request) -> httpx.Response:
    if request.headers.get("if-none-match") == '"v1"':
        return httpx.Response(304, headers={"etag": '"v1"'})
    if request.headers.get("accept") == "application/vnd.pypi.simple.v1+json":
        return httpx.Response(200, json={"format": "json"}, headers={"etag": '"v1"'})
    return httpx.Response(200, text="<html>simple</html>", headers={"etag": '"v1"', "content-type": "text/html"})


async def _record(root) -> None:
    transport = RecordingTransport(root, inner=httpx.MockTransport(_upstream))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://pypi.org/simple/flask/")
        await client.get("https://pypi.org/simple/flask/", headers={"Accept": "application/vnd.pypi.simple.v1+json"})
        # A conditional request answered with 304 must not overwrite the recorded body.
        await client.get("https://pypi.org/simple/flask/", headers={"If-None-Match": '"v1"'})


def test_replay_serves_recorded_responses_by_url_and_accept(tmp_path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        await _record(tmp_path)
        async with httpx.AsyncClient(transport=ReplayTransport(tmp_path)) as client:
            html = await client.get("https://pypi.org/simple/flask/")
            as_json = await client.get(
                "https://pypi.org/simple/flask/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
            )
            conditional = await client.get("https://pypi.org/simple/flask/", headers={"If-None-Match": '"v1"'})
        return html, as_json, conditional

    html, as_json, conditional = asyncio.run(scenario())

    assert html.status_code == 200 and html.text == "<html>simple</html>"
    assert html.headers["etag"] == '"v1"'
    assert as_json.json() == {"format": "json"}
    assert conditional.status_code == 304


def test_replay_miss_fails_fast_and_is_not_retried(tmp_path) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=ReplayTransport(tmp_path)) as client:
            await client.get("https://pypi.org/pypi/unknown/json")

    with pytest.raises(ReplayMiss) as exc_info:
        asyncio.run(scenario())

    assert "https://pypi.org/pypi/unknown/json" in str(exc_info.value)
    assert not is_retryable_upstream_error(exc_info.value)


def test_make_client_uses_replay_dir_from_settings(tmp_path, monkeypatch) -> None:
    asyncio.run(_record(tmp_path))
    monkeypatch.setenv("HTTP_REPLAY_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        async def scenario() -> httpx.Response:
            async with make_client(timeout=5) as client:
                return await client.get("https://pypi.org/simple/flask/")

        assert asyncio.run(scenario()).text == "<html>simple</html>"
    finally:
        get_settings.cache_clear()


def test_recording_passes_through_a_gzip_encoded_upstream_response(tmp_path) -> None:
    # pypi.org answers with Content-Encoding: gzip; the recorder must not make httpx decode the body twice.
    def gzipped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(b'{"info": {"name": "flask"}}'),
            headers={"content-encoding": "gzip", "content-type": "application/json", "etag": '"g1"'},
        )

    async def scenario() -> tuple[dict, dict]:
        recorder = RecordingTransport(tmp_path, inner=httpx.MockTransport(gzipped))
        async with httpx.AsyncClient(transport=recorder) as client:
            live = (await client.get("https://pypi.org/pypi/flask/json")).json()
        async with httpx.AsyncClient(transport=ReplayTransport(tmp_path)) as client:
            replayed = (await client.get("https://pypi.org/pypi/flask/json")).json()
        return live, replayed

    live, replayed = asyncio.run(scenario())

    assert live == replayed == {"info": {"name": "flask"}}


def _routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/missing":
        return httpx.Response(404, text="not found")
    if path == "/old":
        return httpx.Response(301, headers={"location": "https://repo.example/new"})
    if path == "/metadata.xml":
        if request.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(200, text="<metadata/>", headers={"last-modified": "Wed, 21 Oct 2026 07:28:00 GMT"})
    if request.method == "POST":
        return httpx.Response(200, json={"echo": request.content.decode()})
    return httpx.Response(200, text="get-body")


def _record_and_replay(tmp_path, calls):
    async def run(transport):
        async with httpx.AsyncClient(transport=transport) as client:
            return [await call(client) for call in calls]

    live = asyncio.run(run(RecordingTransport(tmp_path, inner=httpx.MockTransport(_routes))))
    replayed = asyncio.run(run(ReplayTransport(tmp_path)))
    return live, replayed


def test_replay_keeps_404_redirect_and_method(tmp_path) -> None:
    calls = [
        lambda c: c.get("https://repo.example/missing"),
        lambda c: c.get("https://repo.example/old"),
        lambda c: c.head("https://repo.example/file"),
        lambda c: c.get("https://repo.example/file"),
    ]
    _, replayed = _record_and_replay(tmp_path, calls)

    missing, redirect, head, get = replayed
    assert missing.status_code == 404
    assert redirect.status_code == 301 and redirect.headers["location"] == "https://repo.example/new"
    assert head.status_code == 200 and get.text == "get-body"


def test_replay_answers_if_modified_since_from_last_modified(tmp_path) -> None:
    calls = [lambda c: c.get("https://repo.example/metadata.xml")]
    _record_and_replay(tmp_path, calls)

    async def conditional(since: str) -> int:
        async with httpx.AsyncClient(transport=ReplayTransport(tmp_path)) as client:
            response = await client.get("https://repo.example/metadata.xml", headers={"If-Modified-Since": since})
            return response.status_code

    assert asyncio.run(conditional("Wed, 21 Oct 2026 07:28:00 GMT")) == 304
    assert asyncio.run(conditional("Tue, 20 Oct 2026 07:28:00 GMT")) == 200


def test_post_bodies_are_part_of_the_key(tmp_path) -> None:
    calls = [
        lambda c: c.post("https://api.example/query", content=b"a"),
        lambda c: c.post("https://api.example/query", content=b"b"),
    ]
    _, replayed = _record_and_replay(tmp_path, calls)

    assert [r.json()["echo"] for r in replayed] == ["a", "b"]


def test_replay_miss_is_logged_for_ci(tmp_path, caplog) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=ReplayTransport(tmp_path)) as client:
            await client.get("https://pypi.org/pypi/unknown/json")

    with pytest.raises(ReplayMiss):
        asyncio.run(scenario())

    assert any("REPLAY_MISS GET https://pypi.org/pypi/unknown/json" in r.getMessage() for r in caplog.records)
