"""libsolv resolve and dnf-like modularity on synthetic repositories (K5, K6)."""

from __future__ import annotations

import pytest

from rpm_worker import solver
from tests.repos import Server, write_repo

BASH = {"name": "bash", "evr": "5.1.8-9.el8", "files": ["/bin/sh"]}
LIBS = {"name": "openssl-libs", "evr": "1:1.1.1k-12.el8", "provides": ["libssl.so.1.1()(64bit)"]}
OPENSSL = {"name": "openssl", "evr": "1:1.1.1k-12.el8", "requires": ["libssl.so.1.1()(64bit)", "/bin/sh"],
           "recommends": ["crypto-policies"]}
POLICIES = {"name": "crypto-policies", "evr": "20230731-1.el8", "arch": "noarch"}
OLDNODE = {"name": "nodejs", "evr": "1:10.24.0-1.el8"}                          # not modular: filtered by the module
NODE16 = {"name": "nodejs", "evr": "1:16.20.2-4.module+el8.9.0+1"}
NODE18 = {"name": "nodejs", "evr": "1:18.20.4-1.module+el8.10.0+1", "requires": ["/bin/sh"]}
NPM18 = {"name": "npm", "evr": "1:10.7.0-1.18.20.4.1.module+el8.10.0+1", "requires": [("nodejs", "EQ", "1:18.20.4-1.module+el8.10.0+1")]}
PERL532 = {"name": "perl", "evr": "4:5.32.1-471.module+el8.6.0+1"}
PERL526 = {"name": "perl", "evr": "4:5.26.3-422.el8"}
TOOL = {"name": "perl-tool", "evr": "1.0-1.module+el8.6.0+2", "requires": ["perl"]}


def _stream(name: str, stream: str, artifacts: list, requires: str = "platform: [el8]",
            profiles: str = "") -> str:
    rpms = "".join("\n    - %s" % a for a in artifacts)
    return ("---\ndocument: modulemd\nversion: 2\ndata:\n  name: %s\n  stream: \"%s\"\n  version: 8100020240101\n"
            "  context: c0ffee42\n  arch: x86_64\n  summary: %s\n  description: %s\n  license:\n    module: [MIT]\n"
            "  dependencies:\n  - buildrequires: {platform: [el8]}\n    requires: {%s}\n%s"
            "  artifacts:\n    rpms:%s\n...\n" % (name, stream, name, name, requires, profiles, rpms))


def _defaults(name: str, stream: str) -> str:
    return ("---\ndocument: modulemd-defaults\nversion: 1\ndata:\n  module: %s\n  stream: \"%s\"\n"
            "  profiles:\n    \"%s\": [common]\n...\n" % (name, stream, stream))


MODULES = (
    _stream("nodejs", "16", ["nodejs-1:16.20.2-4.module+el8.9.0+1.x86_64"],
            profiles="  profiles:\n    common:\n      rpms: [nodejs]\n")
    + _stream("nodejs", "18", ["nodejs-1:18.20.4-1.module+el8.10.0+1.x86_64",
                               "npm-1:10.7.0-1.18.20.4.1.module+el8.10.0+1.x86_64"],
              profiles="  profiles:\n    common:\n      rpms: [nodejs, npm]\n")
    + _defaults("nodejs", "16")
    + _stream("perl", "5.32", ["perl-4:5.32.1-471.module+el8.6.0+1.x86_64"])
    + _stream("perl-tool", "1", ["perl-tool-0:1.0-1.module+el8.6.0+2.x86_64"], requires="platform: [el8], perl: [5.32]")
).encode()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "mirror"
    write_repo(root / "x86_64" / "os", [BASH, LIBS, OPENSSL, POLICIES, OLDNODE, NODE16, NODE18, NPM18,
                                        PERL532, PERL526, TOOL], modules=MODULES)
    with Server(root) as server:
        yield {"id": "22222222-2222-2222-2222-222222222222", "slug": "test-appstream",
               "base_url": server.url + "$basearch/os/",
               "config": {"namespace": "rocky-8", "arch": ["x86_64"], "releasever": "8", "modular": True}}


def _resolve(source, *roots, **extra):
    return solver.resolve(dict({"sources": [source], "arch": "x86_64",
                                "roots": [{"spec": r} if isinstance(r, str) else r for r in roots]}, **extra))


def _set(result) -> set:
    assert "problems" not in result, result
    return {(n["name"], n["evr"]) for n in result["nodes"]}


def test_an_install_transaction_with_weak_dependencies_and_capability_edges(source) -> None:
    result = _resolve(source, "openssl")
    assert _set(result) == {("openssl", "1:1.1.1k-12.el8"), ("openssl-libs", "1:1.1.1k-12.el8"),
                            ("bash", "5.1.8-9.el8"), ("crypto-policies", "20230731-1.el8")}
    edges = {(e["from"].split("-")[0], e["to"].split("-")[0], e["capability"], e["kind"]) for e in result["edges"]}
    assert ("openssl", "openssl", "libssl.so.1.1()(64bit)", "requires") in edges   # openssl-libs (name prefix)
    assert ("openssl", "bash", "/bin/sh", "requires") in edges
    assert result["roots"] == [{"spec": "openssl", "nevra": "openssl-1:1.1.1k-12.el8.x86_64"}]
    assert ("crypto-policies", "20230731-1.el8") not in _set(_resolve(source, "openssl", weak_deps=False))


def test_roots_by_capability_and_constraint_and_unknown_or_unsolvable_ones(source) -> None:
    assert ("openssl-libs", "1:1.1.1k-12.el8") in _set(_resolve(source, "libssl.so.1.1()(64bit)"))
    problems = _resolve(source, {"spec": "openssl", "constraint": ">= 1:3.0"})["problems"]
    assert problems[0]["code"] == "not_found"
    assert _resolve(source, "nothing-here")["problems"][0]["spec"] == "nothing-here"


def test_the_default_stream_wins_and_filters_the_non_modular_package(source) -> None:
    assert ("nodejs", "1:16.20.2-4.module+el8.9.0+1") in _set(_resolve(source, "nodejs"))
    result = _resolve(source, "nodejs", module_streams={"nodejs": "18"})
    assert ("nodejs", "1:18.20.4-1.module+el8.10.0+1") in _set(result) and result["modules"]["nodejs"] == "18"
    # The module disabled: its artifacts leave, and the plain package is not filtered any more.
    assert ("nodejs", "1:10.24.0-1.el8") in _set(_resolve(source, "nodejs", disabled_modules=["nodejs"]))


def test_module_install_takes_the_profile_and_stream_requirements_enable_streams(source) -> None:
    assert _set(_resolve(source, module_install=["nodejs:18/common"])) >= {
        ("nodejs", "1:18.20.4-1.module+el8.10.0+1"), ("npm", "1:10.7.0-1.18.20.4.1.module+el8.10.0+1")}
    # perl-tool:1 needs perl:5.32 (no default): it is enabled, and the non-modular perl 5.26 leaves.
    result = _resolve(source, "perl-tool", module_streams={"perl-tool": "1"})
    assert ("perl", "4:5.32.1-471.module+el8.6.0+1") in _set(result) and result["modules"]["perl"] == "5.32"


def test_two_streams_of_one_module_conflict(source) -> None:
    problems = _resolve(source, "perl-tool", module_streams={"perl-tool": "1", "perl": "5.26"})["problems"]
    assert problems[0]["code"] == "module_conflict" and problems[0]["module"] == "perl"


def test_whatprovides_parses_versioned_and_rich_capabilities(source) -> None:
    from rpm_worker import worker
    found = worker.whatprovides([source], "x86_64", "nodejs = 1:18.20.4-1.module+el8.10.0+1")
    assert [(p["name"], p["evr"]) for p in found] == [("nodejs", "1:18.20.4-1.module+el8.10.0+1")]
    assert [p["evr"] for p in worker.whatprovides([source], "x86_64", "nodejs >= 1:16")] == [
        "1:18.20.4-1.module+el8.10.0+1", "1:16.20.2-4.module+el8.9.0+1"]               # newest first
    assert {p["name"] for p in worker.whatprovides([source], "x86_64", "(bash or perl)")} == {"bash", "perl"}


def test_a_named_root_stands_for_what_obsoletes_it_and_no_recommends_edges_without_weak_deps(tmp_path) -> None:
    root = tmp_path / "mirror"
    write_repo(root / "x86_64" / "os", [
        BASH, {"name": "oldtool", "evr": "1.0-1.el8"},
        {"name": "newtool", "evr": "2.0-1.el8", "provides": [("oldtool", "EQ", "2.0-1.el8")],
         "obsoletes": [("oldtool", "LT", "2.0")], "recommends": ["bash"]}])
    with Server(root) as server:
        src = {"id": "66666666-6666-6666-6666-666666666666", "slug": "obs", "base_url": server.url + "$basearch/os/",
               "config": {"namespace": "rocky-8", "arch": ["x86_64"]}}
        result = solver.resolve({"sources": [src], "arch": "x86_64", "roots": [{"spec": "oldtool"}]})
        assert ("newtool", "2.0-1.el8") in _set(result) and ("oldtool", "1.0-1.el8") not in _set(result)
        assert result["roots"] == [{"spec": "oldtool", "nevra": "newtool-2.0-1.el8.x86_64"}]
        quiet = solver.resolve({"sources": [src], "arch": "x86_64", "roots": [{"spec": "newtool"}], "weak_deps": False})
        assert not [e for e in quiet["edges"] if e["kind"] == "recommends"] and ("bash", "5.1.8-9.el8") not in _set(quiet)


def test_a_default_stream_that_clashes_drops_out_and_an_asked_one_errors(source) -> None:
    # perl-tool has no default; give it one through a stream it needs disabled: the default goes, no error.
    result = _resolve(source, "perl", disabled_modules=["perl"])
    assert ("perl", "4:5.26.3-422.el8") in _set(result)                                 # the plain package
    problems = _resolve(source, "perl-tool", module_streams={"perl-tool": "1"}, disabled_modules=["perl"])["problems"]
    assert problems[0]["code"] == "module_conflict"


def test_a_root_only_in_an_inactive_stream_gets_a_hint(source) -> None:
    problems = _resolve(source, "npm")["problems"]
    assert problems[0]["code"] == "not_found" and "nodejs:18" in problems[0]["hint"]


def test_module_install_needs_a_profile_when_the_stream_has_no_default_one(source) -> None:
    problems = _resolve(source, module_install=["perl:5.32"])["problems"]
    assert problems[0]["code"] == "module_conflict" and "profile" in problems[0]["message"]
