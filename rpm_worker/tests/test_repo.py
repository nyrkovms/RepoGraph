"""The repodata cache, pools and the tasks over a synthetic repository (K3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rpm_worker import http, repo, worker
from tests.repos import Server, write_repo

OPENSSL = {"name": "openssl", "evr": "1:3.0.7-27.el9", "license": "Apache-2.0", "url": "https://www.openssl.org/",
           "requires": ["libc.so.6()(64bit)", ("openssl-libs(x86-64)", "EQ", "1:3.0.7-27.el9"), "/bin/sh",
                        "rpmlib(CompressedFileNames)"],
           "recommends": ["crypto-policies-scripts"], "files": ["/usr/bin/openssl"]}
LIBS = {"name": "openssl-libs", "evr": "1:3.0.7-27.el9", "provides": ["libssl.so.3()(64bit)", "openssl-libs(x86-64)"]}
BASH = {"name": "bash", "evr": "5.1.8-9.el9", "files": ["/bin/sh", "/usr/bin/bash"]}
DATA = {"name": "tzdata", "evr": "2024a-1.el9", "arch": "noarch"}


def _source(url: str, **extra: object) -> dict:
    return {"id": "11111111-1111-1111-1111-111111111111", "slug": "test-baseos", "base_url": url + "$basearch/os/",
            "auth_type": extra.pop("auth_type", "none"), "credentials_ref": extra.pop("credentials_ref", None),
            "config": {"namespace": "rocky-9", "arch": extra.pop("arch", ["x86_64"]), "releasever": "9"}}


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "mirror"
    write_repo(root / "x86_64" / "os", [OPENSSL, LIBS, BASH, DATA])
    write_repo(root / "aarch64" / "os", [dict(OPENSSL, arch="aarch64"), DATA])
    with Server(root) as server:
        yield server, root


def test_a_sync_caches_the_snapshot_and_answers_304_after(served) -> None:
    server, _ = served
    src = repo.source_from(_source(server.url))
    first = repo.sync(src, "x86_64")
    assert first.solv.exists() and set(first.files) == {"primary"}
    again = repo.sync(src, "x86_64")
    assert again.directory == first.directory
    assert [s for m, p, s in server.requests if p.endswith("repomd.xml")] == [200, 304]


def test_a_changed_repository_gets_a_new_snapshot_and_the_old_one_goes(served) -> None:
    server, root = served
    src = repo.source_from(_source(server.url))
    first = repo.sync(src, "x86_64")
    write_repo(root / "x86_64" / "os", [dict(OPENSSL, evr="1:3.0.7-28.el9"), LIBS, BASH])
    second = repo.sync(src, "x86_64")
    assert second.revision != first.revision and not first.directory.exists()


def test_fetch_package_lists_builds_per_arch_and_noarch_once(served) -> None:
    server, _ = served
    result = worker.fetch_package(_source(server.url, arch=["x86_64", "aarch64"]), "openssl")
    assert result is not None
    assert [(b["arch"], b["evr"]) for b in result["builds"]] == [("aarch64", "1:3.0.7-27.el9"), ("x86_64", "1:3.0.7-27.el9")]
    build = result["builds"][1]
    assert (build["epoch"], build["version"], build["release"]) == (1, "3.0.7", "27.el9")
    assert build["license"] == "Apache-2.0" and build["url"] == "https://www.openssl.org/"
    # libsolv keeps its own order; rpmlib() needs are rpm's, not a package's.
    assert sorted(build["requires"]) == ["/bin/sh", "libc.so.6()(64bit)", "openssl-libs(x86-64) = 1:3.0.7-27.el9"]
    assert build["recommends"] == ["crypto-policies-scripts"] and build["repo_arches"] == ["x86_64"]
    tz = worker.fetch_package(_source(server.url, arch=["x86_64", "aarch64"]), "tzdata")
    assert tz is not None and [(b["arch"], b["repo_arches"]) for b in tz["builds"]] == [("noarch", ["x86_64", "aarch64"])]
    assert worker.fetch_package(_source(server.url), "nothing") is None


def test_detect_changes_reports_changed_and_missing_packages(served) -> None:
    server, root = served
    source = _source(server.url)
    known = worker.fetch_package(source, "openssl")["revision"]
    bash = worker.fetch_package(source, "bash")["revision"]
    write_repo(root / "x86_64" / "os", [dict(OPENSSL, evr="1:3.0.7-28.el9"), LIBS, BASH])
    result = worker.detect_changes(source, {"openssl": known, "bash": bash, "tzdata": "x"})
    assert set(result["changed"]) == {"openssl"} and result["missing"] == ["tzdata"]


def test_whatprovides_finds_libraries_and_files(served) -> None:
    server, _ = served
    source = _source(server.url)
    assert [p["name"] for p in worker.whatprovides([source], "x86_64", "libssl.so.3()(64bit)")] == ["openssl-libs"]
    assert [p["name"] for p in worker.whatprovides([source], "x86_64", "/bin/sh")] == ["bash"]


def test_a_missing_repository_is_no_package_not_an_error(tmp_path) -> None:
    with Server(tmp_path) as server:
        assert worker.fetch_package(_source(server.url), "openssl") is None
        assert worker.sync_repo(_source(server.url))["missing"] is True


def test_basic_auth_reads_the_secret_and_a_refusal_is_a_source_error(tmp_path, monkeypatch) -> None:
    root = tmp_path / "mirror"
    write_repo(root / "x86_64" / "os", [BASH])
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "satellite").write_text("user:s3cret\n")
    monkeypatch.setenv("SECRETS_DIR", str(secrets))
    with Server(root, credentials=("user", "s3cret")) as server:
        assert worker.fetch_package(_source(server.url, auth_type="basic", credentials_ref="satellite"), "bash")
        with pytest.raises(http.SourceAuthError):
            worker.fetch_package(_source(server.url), "bash")
        (secrets / "satellite").write_text("user:wrong\n")
        with pytest.raises(http.SourceAuthError):
            worker.test_connection(_source(server.url, auth_type="basic", credentials_ref="satellite"))


def test_answers_recorded_once_replay_without_the_network(served, tmp_path, monkeypatch, cache_dir) -> None:
    server, _ = served
    record = tmp_path / "record"
    monkeypatch.setenv("HTTP_RECORD_DIR", str(record))
    recorded = worker.fetch_package(_source(server.url), "openssl")
    monkeypatch.delenv("HTTP_RECORD_DIR")
    monkeypatch.setenv("HTTP_REPLAY_DIR", str(record))
    monkeypatch.setenv("RPM_CACHE_DIR", str(tmp_path / "fresh"))
    server.httpd.shutdown()
    assert worker.fetch_package(_source(server.url), "openssl") == recorded
    assert list(record.glob("127.0.0.1/*.json"))


def _certificates(directory: Path) -> None:
    """A CA, a server certificate for 127.0.0.1 and a client certificate, as an entitlement would be."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=str(directory), check=True, capture_output=True)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=test-ca", "-keyout", "ca.key",
        "-out", "ca.crt")
    (directory / "san.cnf").write_text("subjectAltName=IP:127.0.0.1\n")
    for name, extra in (("server", ["-extfile", "san.cnf"]), ("client", [])):
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=%s" % name, "-keyout", "%s.key" % name,
            "-out", "%s.csr" % name)
        run("x509", "-req", "-in", "%s.csr" % name, "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "2", "-out", "%s.crt" % name, *extra)


def test_a_client_certificate_opens_the_repository_and_its_absence_is_a_source_error(tmp_path, monkeypatch) -> None:
    import shutil
    import ssl

    import httpx

    certs = tmp_path / "certs"
    certs.mkdir()
    _certificates(certs)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certs / "server.crt"), str(certs / "server.key"))
    context.load_verify_locations(str(certs / "ca.crt"))
    context.verify_mode = ssl.CERT_REQUIRED
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for suffix, source in ((".crt", "client.crt"), (".key", "client.key"), (".ca", "ca.crt")):
        shutil.copy(str(certs / source), str(secrets / ("rhel-entitlement" + suffix)))
    monkeypatch.setenv("SECRETS_DIR", str(secrets))
    root = tmp_path / "mirror"
    write_repo(root / "x86_64" / "os", [BASH])
    with Server(root, tls=context) as server:
        source = _source(server.url, auth_type="tls_client_cert", credentials_ref="rhel-entitlement")
        assert worker.fetch_package(source, "bash") is not None
        # Without the client certificate the handshake fails: an error of the source, never "no such package".
        (secrets / "rhel-entitlement.crt").unlink()
        with pytest.raises(http.SourceAuthError):
            worker.fetch_package(dict(source, id="33333333-3333-3333-3333-333333333333"), "bash")
        anonymous = dict(_source(server.url), id="44444444-4444-4444-4444-444444444444")
        with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
            worker.fetch_package(anonymous, "bash")
