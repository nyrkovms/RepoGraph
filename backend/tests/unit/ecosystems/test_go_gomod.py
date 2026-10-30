"""The go.mod reader on hand-written texts for every directive form and on go.mod files recorded from
proxy.golang.org on 2026-10-30 (tests/fixtures/go/goproxy/): gin v1.12.0, go-redis v9.23.0 (`retract` block with
rationales), mattn/go-sqlite3 v1.14.52 (`retract` interval), golang/protobuf v1.5.4 (`// Deprecated:` above `module`),
docker/docker v28.5.2+incompatible (a bare `module` line)."""

from pathlib import Path

import pytest

from app.ecosystems.go.gomod import GoMod, GoModError, Replace, Require, Retract, parse_go_mod

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "go" / "goproxy"


def _recorded(path: str) -> GoMod:
    return parse_go_mod((FIXTURES / path).read_text())


# --- module, go, toolchain ----------------------------------------------------------------------------------


def test_module_go_and_toolchain() -> None:
    parsed = parse_go_mod("module example.com/m\n\ngo 1.21.3\n\ntoolchain go1.22.1\n")
    assert (parsed.module, parsed.go, parsed.toolchain) == ("example.com/m", "1.21.3", "go1.22.1")
    assert parsed.requires == parsed.excludes == parsed.replaces == parsed.retracts == []
    assert parsed.deprecated is None


def test_a_quoted_module_path_and_crlf_lines() -> None:
    parsed = parse_go_mod('module "example.com/m"\r\ngo 1.20\r\n')
    assert (parsed.module, parsed.go) == ("example.com/m", "1.20")


def test_a_go_version_may_be_a_release_candidate() -> None:
    assert parse_go_mod("module example.com/m\ngo 1.21rc2\n").go == "1.21rc2"


@pytest.mark.parametrize("pruned_go, expected", [
    ("1.16", False), ("1.17", True), ("1.17.5", True), ("1.20", True), ("1.9", False), ("1.100", True),
    ("1.21rc1", True), (None, False),
])
def test_pruned_from_go_1_17_and_a_missing_go_line_is_1_16(pruned_go: str | None, expected: bool) -> None:
    assert GoMod(module="example.com/m", go=pruned_go).pruned is expected


def test_a_go_mod_without_go_line_is_not_pruned() -> None:
    parsed = parse_go_mod("module example.com/m\nrequire example.com/a v1.0.0\n")
    assert parsed.go is None and parsed.pruned is False


# --- require ------------------------------------------------------------------------------------------------


def test_require_single_line_block_and_indirect() -> None:
    parsed = parse_go_mod(
        'module example.com/m\n'
        'require example.com/one v1.0.0\n'
        'require example.com/two v1.2.3 // indirect\n'
        'require (\n'
        '\texample.com/three v0.1.0\n'
        '\t"example.com/four" v2.0.0+incompatible // indirect\n'
        '\n'
        '\t// a comment line\n'
        '\texample.com/five v0.0.0-20200101000000-abcdef123456 // indirect; for the test\n'
        '\texample.com/six v1.0.0 // not indirect\n'
        ')\n'
    )
    assert parsed.requires == [
        Require("example.com/one", "v1.0.0"),
        Require("example.com/two", "v1.2.3", indirect=True),
        Require("example.com/three", "v0.1.0"),
        Require("example.com/four", "v2.0.0+incompatible", indirect=True),
        Require("example.com/five", "v0.0.0-20200101000000-abcdef123456", indirect=True),
        Require("example.com/six", "v1.0.0"),
    ]


def test_a_comment_marker_inside_a_quoted_path_is_not_a_comment() -> None:
    parsed = parse_go_mod('module example.com/m\nrequire "example.com//x" v1.0.0 // indirect\n')
    assert parsed.requires == [Require("example.com//x", "v1.0.0", indirect=True)]


def test_a_raw_string_path() -> None:
    assert parse_go_mod("module `example.com/m`\nrequire `example.com/a` v1.0.0\n").requires == [
        Require("example.com/a", "v1.0.0")]


# --- exclude, replace ---------------------------------------------------------------------------------------


def test_exclude_single_line_and_block() -> None:
    parsed = parse_go_mod(
        "module example.com/m\nexclude example.com/a v1.0.0\nexclude (\n\texample.com/b v2.0.0\n\texample.com/c v3.0.0\n)\n")
    assert parsed.excludes == [("example.com/a", "v1.0.0"), ("example.com/b", "v2.0.0"), ("example.com/c", "v3.0.0")]


def test_replace_forms() -> None:
    parsed = parse_go_mod(
        "module example.com/m\n"
        "replace example.com/a => example.com/fork v1.1.0\n"
        "replace example.com/b v1.0.0 => ../b\n"
        "replace (\n"
        "\texample.com/c v1.2.0 => example.com/c2 v1.3.0\n"
        "\texample.com/d => ./vendor/d\n"
        "\t\"example.com/e\" => /abs/e\n"
        ")\n"
    )
    assert parsed.replaces == [
        Replace("example.com/a", None, "example.com/fork", "v1.1.0"),
        Replace("example.com/b", "v1.0.0", "../b", None),
        Replace("example.com/c", "v1.2.0", "example.com/c2", "v1.3.0"),
        Replace("example.com/d", None, "./vendor/d", None),
        Replace("example.com/e", None, "/abs/e", None),
    ]


# --- retract ------------------------------------------------------------------------------------------------


def test_retract_single_versions_and_intervals_with_rationale() -> None:
    parsed = parse_go_mod(
        "module example.com/m\n"
        "retract v1.0.0 // published by mistake\n"
        "retract [v1.1.0, v1.1.5]\n"
        "retract (\n"
        "\tv1.2.0 // broken build\n"
        "\t[v1.3.0, v1.3.9] // security\n"
        "\tv1.4.0\n"
        "\t// the comment above wins when the line has none\n"
        "\tv1.5.0\n"
        ")\n"
    )
    assert parsed.retracts == [
        Retract("v1.0.0", "v1.0.0", "published by mistake"),
        Retract("v1.1.0", "v1.1.5", None),
        Retract("v1.2.0", "v1.2.0", "broken build"),
        Retract("v1.3.0", "v1.3.9", "security"),
        Retract("v1.4.0", "v1.4.0", None),
        Retract("v1.5.0", "v1.5.0", "the comment above wins when the line has none"),
    ]


def test_retract_interval_without_spaces() -> None:
    assert parse_go_mod("module example.com/m\nretract [v1.0.0,v1.2.0]\n").retracts == [
        Retract("v1.0.0", "v1.2.0", None)]


# --- deprecated ---------------------------------------------------------------------------------------------


def test_deprecated_in_the_comment_above_the_module_line() -> None:
    parsed = parse_go_mod("// Deprecated: use example.com/new instead.\nmodule example.com/m\n")
    assert parsed.deprecated == "use example.com/new instead."


def test_deprecated_paragraph_may_span_lines_and_follow_other_text() -> None:
    parsed = parse_go_mod(
        "// Package m does things.\n//\n// Deprecated: use example.com/new,\n// it is maintained.\n"
        "module example.com/m\n")
    assert parsed.deprecated == "use example.com/new, it is maintained."


def test_deprecated_on_the_module_line() -> None:
    assert parse_go_mod("module example.com/m // Deprecated: gone\n").deprecated == "gone"


def test_a_comment_separated_by_a_blank_line_does_not_deprecate() -> None:
    assert parse_go_mod("// Deprecated: nope\n\nmodule example.com/m\n").deprecated is None


def test_deprecated_elsewhere_in_the_file_does_not_deprecate_the_module() -> None:
    parsed = parse_go_mod("module example.com/m\n// Deprecated: not about the module\nrequire example.com/a v1.0.0\n")
    assert parsed.deprecated is None


def test_a_comment_that_merely_mentions_deprecated_is_not_a_notice() -> None:
    assert parse_go_mod("// This is not Deprecated: really\nmodule example.com/m\n").deprecated is None


# --- what is skipped ----------------------------------------------------------------------------------------


def test_unknown_directives_and_blocks_are_skipped() -> None:
    parsed = parse_go_mod(
        "module example.com/m\n"
        "go 1.24\n"
        "godebug default=go1.21\n"
        "godebug (\n\tpanicnil=1\n\tasynctimerchan=0\n)\n"
        "tool example.com/cmd/stringer\n"
        "tool (\n\texample.com/cmd/a\n\texample.com/cmd/b\n)\n"
        "ignore ./node_modules\n"
        "something-new (\n\twith ( odd ) tokens\n)\n"
        "require example.com/a v1.0.0\n"
    )
    assert parsed.requires == [Require("example.com/a", "v1.0.0")]
    assert parsed.go == "1.24"


# --- real files ---------------------------------------------------------------------------------------------


def test_recorded_gin_go_mod() -> None:
    parsed = _recorded("github.com/gin-gonic/gin/@v/v1.12.0.mod")
    assert (parsed.module, parsed.go, parsed.pruned) == ("github.com/gin-gonic/gin", "1.25.0", True)
    direct = [r for r in parsed.requires if not r.indirect]
    assert len(direct) == 15 and len(parsed.requires) == 35
    assert Require("gopkg.in/yaml.v3", "v3.0.1", indirect=True) in parsed.requires   # single-line `require … // indirect`
    assert Require("github.com/bytedance/sonic", "v1.15.0") in parsed.requires


def test_recorded_go_mod_with_a_retract_block() -> None:
    parsed = _recorded("github.com/redis/go-redis/v9/@v/v9.23.0.mod")
    assert [(r.low, r.rationale) for r in parsed.retracts][:2] == [
        ("v9.15.1", "This version is used to retract v9.15.0"),
        ("v9.15.0", "This version was accidentally released. It is identical to 9.15.0-beta.2"),
    ]
    assert len(parsed.retracts) == 5 and all(r.low == r.high for r in parsed.retracts)
    assert Require("github.com/klauspost/cpuid/v2", "v2.4.0", indirect=True) in parsed.requires


def test_recorded_go_mod_with_a_retract_interval() -> None:
    parsed = _recorded("github.com/mattn/go-sqlite3/@v/v1.14.52.mod")
    assert parsed.retracts == [Retract(
        "v2.0.0+incompatible", "v2.0.7+incompatible", "Accidental; no major changes or features.")]


def test_recorded_deprecated_go_mod() -> None:
    parsed = _recorded("github.com/golang/protobuf/@v/v1.5.4.mod")
    assert parsed.deprecated == 'Use the "google.golang.org/protobuf" module instead.'
    assert parsed.go == "1.17" and parsed.pruned


def test_recorded_go_mod_that_is_only_a_module_line() -> None:
    parsed = _recorded("github.com/docker/docker/@v/v28.5.2+incompatible.mod")
    assert parsed == GoMod(module="github.com/docker/docker") and not parsed.pruned


# --- broken files -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, line", [
    ("module example.com/m\nrequire example.com/a\n", 2),
    ("module example.com/m\nrequire (\n\texample.com/a\n)\n", 3),
    ("module example.com/m\ngo\n", 2),
    ("module example.com/m\ngo one.two\n", 2),
    ("module example.com/m\nrequire \"example.com/a v1.0.0\n", 2),
    ("module example.com/m\nrequire `example.com/a v1.0.0\n", 2),
    ('module example.com/m\nrequire "a\\qb" v1.0.0\n', 2),
    ("module example.com/m\nretract [v1.0.0, v1.1.0\n", 2),
    ("module example.com/m\nretract\n", 2),
    ("module a b\n", 1),
    ("module example.com/m\n)\n", 2),
    ("module example.com/m\n\n\nrequire (\n\texample.com/a v1.0.0\n", 4),
    ("module example.com/m\nrequire foo ( \n)\n", 2),
])
def test_a_line_that_cannot_be_read_raises_with_its_number(text: str, line: int) -> None:
    with pytest.raises(GoModError) as error:
        parse_go_mod(text)
    assert error.value.line == line and f"line {line}" in str(error.value)


def test_a_file_without_a_module_directive_is_not_a_go_mod() -> None:
    with pytest.raises(GoModError):
        parse_go_mod("go 1.20\nrequire example.com/a v1.0.0\n")
    with pytest.raises(GoModError):
        parse_go_mod("")


def test_a_dependency_go_mod_is_read_leniently_like_go_parse_lax() -> None:
    # Go ignores replace/exclude/toolchain of a dependency and reads its go line loosely: one bad line there must
    # not cost the requirements and retractions of the whole file.
    text = """module example.com/m

go 1.13.x
toolchain
require ()
require (
	example.com/a v1.2
	example.com/b v1
)
replace example.com/a v1.0.0
replace example.com/a => example.com/b
replace a => b => c
exclude example.com/a
exclude example.com/c v2
"""
    mod = parse_go_mod(text)
    assert (mod.go, mod.toolchain, mod.pruned) == ("1.13", None, False)
    assert [(r.path, r.version) for r in mod.requires] == [("example.com/a", "v1.2.0"), ("example.com/b", "v1.0.0")]
    assert mod.replaces == [] and mod.excludes == [("example.com/c", "v2.0.0")]
