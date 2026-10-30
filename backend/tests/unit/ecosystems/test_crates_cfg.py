"""Platform conditions and the crates environment (IMPL G4), on expressions taken from the recorded index files of
tokio, reqwest and serde."""

import pytest

from app.ecosystems.base import NormalizedDependency, PackageRef
from app.ecosystems.crates.cfg import InvalidCfg, cfg_of, known_targets, parse_platform, platform_matches
from app.ecosystems.crates.environment import CratesEnvironment, dependency_applies, parse_environment

LINUX, WINDOWS, MAC, WASM, WASI = (
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc", "aarch64-apple-darwin", "wasm32-unknown-unknown", "wasm32-wasip1")


@pytest.mark.parametrize(("spec", "on"), [
    ("cfg(unix)", {LINUX, MAC}),
    ("cfg(windows)", {WINDOWS}),
    ("cfg(not(windows))", {LINUX, MAC, WASM, WASI}),
    ('cfg(target_os = "macos")', {MAC}),
    ('cfg(all(unix, not(target_os = "emscripten")))', {LINUX, MAC}),
    ('cfg(all(target_arch = "wasm32", any(target_os = "unknown", target_os = "none")))', {WASM}),
    ('cfg(not(all(target_arch = "wasm32", any(target_os = "unknown", target_os = "none"))))', {LINUX, WINDOWS, MAC, WASI}),
    ('cfg(any(not(target_family = "wasm"), all(target_os = "wasi", not(target_env = "p1"))))', {LINUX, WINDOWS, MAC}),
    ('cfg(all(tokio_unstable, target_os = "linux"))', set()),          # --cfg flags Cargo does not pass
    ("cfg(docsrs)", set()),
    ('cfg(feature = "tracing")', set()),                               # features are not platform conditions
    ("cfg(any())", set()),
    ("cfg(all())", {LINUX, WINDOWS, MAC, WASM, WASI}),
    ('cfg(target_pointer_width = "32")', {WASM, WASI}),
    ('cfg(all(target_has_atomic = "64", unix,))', {LINUX, MAC}),       # a trailing comma is allowed
    ("x86_64-pc-windows-msvc", {WINDOWS}),                            # a plain triple
    ("i686-pc-windows-gnu", set()),
])
def test_platform_matches(spec: str, on: set[str]) -> None:
    assert {t for t in (LINUX, WINDOWS, MAC, WASM, WASI) if platform_matches(spec, t)} == on


@pytest.mark.parametrize("spec", ["cfg(", "cfg(all(unix)", "cfg(not(unix, windows))", 'cfg(target_os = linux)',
                                  "cfg(unix windows)", "cfg(1abc)", "not a target", 'cfg(a = "b" = "c")'])
def test_unreadable_expressions_apply_nowhere(spec: str) -> None:
    with pytest.raises(InvalidCfg):
        parse_platform(spec)
    assert not platform_matches(spec, LINUX)


def test_no_target_applies_everywhere_and_the_table_is_rustcs() -> None:
    assert platform_matches(None, WASM) and platform_matches("", WINDOWS)
    linux = cfg_of(LINUX)
    assert {"unix", "debug_assertions"} <= linux.names
    assert {("target_os", "linux"), ("target_env", "gnu"), ("target_abi", "")} <= linux.pairs
    assert len(known_targets()) == 21 and LINUX in known_targets()
    with pytest.raises(ValueError, match="unknown Rust target"):
        cfg_of("x86_64-unknown-plan9")


def test_parse_environment() -> None:
    assert parse_environment({}) == CratesEnvironment(specified=False)
    assert parse_environment({"target": None, "features": ""}).specified is False
    env = parse_environment({"target": WINDOWS, "features": "rt, macros,rt", "default_features": "false"})
    assert env == CratesEnvironment(WINDOWS, ("macros", "rt"), False)
    assert env.active_features() == {"macros", "rt"}
    env = parse_environment({"features": ["full"]})
    assert (env.target, env.active_features()) == (LINUX, {"full", "default"})
    with pytest.raises(ValueError, match="unknown Rust target"):
        parse_environment({"target": "linux"})


def _dep(kind: str, target: str | None = None, enabled_by: list[str] | None = None) -> NormalizedDependency:
    return NormalizedDependency(
        target=PackageRef("crates", "", "x"), constraint="^1", dependency_type=kind,  # type: ignore[arg-type]
        scope=kind, raw_spec="x = ^1",
        environment_markers={"rust_cfg": target} if target else None,
        details={"enabled_by": enabled_by} if enabled_by is not None else None,
    )


def test_dependency_applies() -> None:
    linux, windows = CratesEnvironment(), CratesEnvironment(target=WINDOWS, default_features=False)
    assert dependency_applies(_dep("runtime"), linux, False) and dependency_applies(_dep("build"), linux, False)
    assert not dependency_applies(_dep("dev"), linux, True)
    assert dependency_applies(_dep("runtime", "cfg(windows)"), windows, False)
    assert not dependency_applies(_dep("runtime", "cfg(windows)"), linux, False)
    # Optional: on when an active feature turns it on (default counts unless switched off), or on request.
    assert dependency_applies(_dep("optional", enabled_by=["default", "std"]), linux, False)
    assert not dependency_applies(_dep("optional", enabled_by=["default", "std"]), windows, False)
    assert dependency_applies(_dep("optional", enabled_by=["std"]), CratesEnvironment(features=("std",)), False)
    assert dependency_applies(_dep("optional", enabled_by=[]), linux, True)
    assert not dependency_applies(_dep("optional", "cfg(windows)", ["default"]), linux, True)
