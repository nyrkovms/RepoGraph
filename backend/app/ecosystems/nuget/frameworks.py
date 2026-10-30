"""Target frameworks (TFM) of NuGet dependency groups (SPEC-MULTI-ECOSYSTEM.md §9.6, Q7).

Registration documents name a group's framework in NuGet's long form (`.NETStandard2.0`, `.NETCoreApp3.1`,
`.NETFramework4.6.2`) or the short one (`net10.0`, `netstandard2.0`); a group without a framework applies to every
project. Only the modern frameworks are modelled (Q7): `net5.0` and newer, `netcoreapp`, `netstandard`. A project picks
the nearest group the way NuGet's FrameworkReducer does for these families: its own `netX.0`, then lower `netX.0` down
to `net5.0`, then `netcoreapp3.1` and lower, then `netstandard2.1` and lower; a `netstandardX` project only takes
`netstandard` groups up to its version. .NET Framework and platform-specific frameworks (`net8.0-windows`) never match.
"""

import re

ANY = "any"
_LONG = re.compile(r"\.net(standard|coreapp|framework)(\d+(?:\.\d+)*)", re.IGNORECASE)
_SHORT = re.compile(r"net(standard|coreapp)?(\d+(?:\.\d+)*)", re.IGNORECASE)

Framework = tuple[str, tuple[int, ...]]   # (family: "net" | "netcoreapp" | "netstandard" | "netframework", version)


def _version(text: str) -> tuple[int, ...]:
    parts = tuple(int(p) for p in text.split("."))
    return parts + (0,) * (2 - len(parts)) if len(parts) < 2 else parts


def parse(raw: str | None) -> Framework | None:
    """A group's framework; ("any", ()) for none; None for one outside the modelled families."""
    text = (raw or "").strip()
    if text in ("", ANY) or text.lower() == "any":
        return (ANY, ())
    if "-" in text or "," in text:
        return None                                  # platform-specific (net8.0-windows) or a profile
    long = _LONG.fullmatch(text)
    if long is not None:
        family, version = long.group(1).lower(), _version(long.group(2))
        if family == "coreapp":
            # .NETCoreApp5.0 and later is net5.0 (NuGet's own long form for netX.0).
            return ("net", version) if version >= (5, 0) else ("netcoreapp", version)
        return ("netstandard" if family == "standard" else "netframework", version)
    short = _SHORT.fullmatch(text)
    if short is not None:
        family, digits = short.group(1), short.group(2)
        if family is not None:
            return (f"net{family.lower()}", _version(digits))
        if "." in digits:                            # net5.0 .. net11.0
            version = _version(digits)
            return ("net", version) if version >= (5, 0) else None
        return ("netframework", tuple(int(d) for d in digits))   # net472
    return None


def short_name(framework: Framework) -> str:
    family, version = framework
    if family == ANY:
        return ANY
    if family == "netframework":
        return "net" + "".join(str(v) for v in version)
    return f"{family}{'.'.join(str(v) for v in version)}"


def project(raw: str) -> Framework:
    """A project's framework from the environment; ValueError for one outside Q7."""
    framework = parse(raw)
    if framework is None or framework[0] not in ("net", "netstandard"):
        raise ValueError(f"unsupported target framework {raw!r}: use net5.0 or newer, or netstandard1.0-2.1 (Q7)")
    return framework


def _rank(target: Framework, group: Framework) -> tuple[int, tuple[int, ...]] | None:
    """How near `group` is for `target` (higher is nearer); None when it does not apply."""
    t_family, t_version = target
    family, version = group
    if family == ANY:
        return (0, ())
    if t_family == "net":
        if family == "net" and version <= t_version:
            return (3, version)
        if family == "netcoreapp":
            return (2, version)
        if family == "netstandard" and version <= (2, 1):
            return (1, version)
        return None
    if t_family == "netstandard" and family == "netstandard" and version <= t_version:
        return (1, version)
    return None


def nearest(target: Framework, groups: list[str | None]) -> str | None:
    """The group (as written) a project on `target` takes, or None when no group applies."""
    best: tuple[tuple[int, tuple[int, ...]], str | None] | None = None
    for raw in groups:
        framework = parse(raw)
        rank = _rank(target, framework) if framework is not None else None
        if rank is not None and (best is None or rank > best[0]):
            best = (rank, raw)
    return best[1] if best is not None else None
