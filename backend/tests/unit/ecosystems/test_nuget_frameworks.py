"""NuGet target frameworks and the nearest dependency group (IMPL I5, SPEC §9.6, Q7)."""

import pytest

from app.ecosystems.base import NormalizedDependency, PackageRef
from app.ecosystems.nuget.environment import NuGetEnvironment, dependency_applies, parse_environment
from app.ecosystems.nuget.frameworks import nearest, parse, project, short_name

# Groups of Microsoft.Extensions.Logging 11.0.0-rc.3 and Newtonsoft.Json 14.0.1-beta2, as their registrations write them.
LOGGING = ["net10.0", "net11.0", ".NETFramework4.6.2", ".NETStandard2.0"]
NEWTONSOFT = ["net10.0", ".NETFramework2.0", ".NETFramework3.5", ".NETFramework4.0", ".NETFramework4.5",
              ".NETStandard1.0", ".NETStandard1.3", ".NETStandard2.0", "net6.0"]


@pytest.mark.parametrize(("raw", "framework"), [
    (".NETStandard2.0", ("netstandard", (2, 0))), ("netstandard2.1", ("netstandard", (2, 1))),
    (".NETCoreApp3.1", ("netcoreapp", (3, 1))), (".NETCoreApp5.0", ("net", (5, 0))), ("net10.0", ("net", (10, 0))),
    ("net8.0", ("net", (8, 0))), (".NETFramework4.6.2", ("netframework", (4, 6, 2))), ("net472", ("netframework", (4, 7, 2))),
    ("", ("any", ())), (None, ("any", ())), ("net8.0-windows", None), ("portable-net45+win8", None), ("tizen40", None),
])
def test_parse(raw: str | None, framework: object) -> None:
    assert parse(raw) == framework


@pytest.mark.parametrize(("target", "groups", "chosen"), [
    ("net8.0", LOGGING, ".NETStandard2.0"),        # net10.0/net11.0 are newer than the project
    ("net10.0", LOGGING, "net10.0"),
    ("net11.0", LOGGING, "net11.0"),
    ("net8.0", NEWTONSOFT, "net6.0"),
    ("net5.0", NEWTONSOFT, ".NETStandard2.0"),
    ("netstandard1.3", NEWTONSOFT, ".NETStandard1.3"),
    ("net8.0", [".NETCoreApp3.1", ".NETStandard2.1"], ".NETCoreApp3.1"),
    ("net8.0", ["", ".NETStandard2.0"], ".NETStandard2.0"),
    ("net8.0", [""], ""),                           # a group without framework applies everywhere
    ("net8.0", [".NETFramework4.6.2", "net8.0-windows"], None),
    ("netstandard2.0", ["net6.0", ".NETStandard2.1"], None),
])
def test_nearest_group(target: str, groups: list[str], chosen: str | None) -> None:
    assert nearest(project(target), groups) == chosen


def test_project_frameworks_and_the_environment() -> None:
    assert short_name(project(".NETStandard2.0")) == "netstandard2.0" and short_name(project("NET8.0")) == "net8.0"
    for bad in ("net472", "netcoreapp3.1", "net8.0-windows", "junk"):
        with pytest.raises(ValueError, match="unsupported target framework"):
            project(bad)
    assert parse_environment({}) == NuGetEnvironment(specified=False)
    assert parse_environment({"target_framework": "net10.0"}).target_framework == "net10.0"


def _dep(group: str | None, groups: list[str]) -> NormalizedDependency:
    return NormalizedDependency(target=PackageRef("nuget", "", "x"), constraint="[1.0.0, )", dependency_type="runtime",
                                raw_spec="x [1.0.0, )", environment_markers={"dotnet_target_framework": group},
                                details={"groups": groups})


def test_dependency_applies_by_its_group() -> None:
    env = NuGetEnvironment("net8.0")
    assert dependency_applies(_dep(".NETStandard2.0", LOGGING), env, False)
    assert not dependency_applies(_dep("net10.0", LOGGING), env, False)
    assert dependency_applies(_dep("net10.0", LOGGING), NuGetEnvironment("net10.0"), False)
