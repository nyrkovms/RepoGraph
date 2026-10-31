"""Modularity as dnf does it (SPEC-MULTI-ECOSYSTEM.md §9.8.3), from the `modules.yaml` of EL8/EL9 repositories.

libsolv knows nothing of modules, so the pool is filtered before solving:
1. active streams: the ones asked for (`module_streams`, `module_install`) and what they require, hard; the default
   stream of every other module that is not disabled, weak (dropped when its needs clash); `platform` is the
   distribution itself and always holds; two asked streams of one module, or an asked stream needing a disabled
   module, are a `module_conflict`;
2. of an active stream, the contexts whose requirements agree with the active streams (all their versions stay in
   the pool and libsolv picks the build; profiles of `module_install` come from the newest version);
3. excluded: artifacts of every stream that is not active, and non-modular packages that share a name with an artifact
   of an active stream ("modular filtering": the module wins).
"""

from __future__ import annotations

import gzip
import lzma
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import gi

gi.require_version("Modulemd", "2.0")
from gi.repository import Modulemd  # noqa: E402

PLATFORM = "platform"


class ModuleConflict(Exception):
    def __init__(self, module: str, streams: Iterable[str], why: str) -> None:
        super().__init__("module %s: %s (%s)" % (module, ", ".join(sorted(streams)), why))
        self.module = module


@dataclass
class Stream:
    module: str
    stream: str
    version: int
    context: str
    artifacts: List[str]                                      # NEVRA `name-epoch:version-release.arch`
    requires: List[Dict[str, List[str]]]                      # one dict per dependencies block
    profiles: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class Catalog:
    streams: List[Stream] = field(default_factory=list)
    defaults: Dict[str, str] = field(default_factory=dict)            # module -> default stream
    default_profiles: Dict[Tuple[str, str], List[str]] = field(default_factory=dict)

    def of(self, module: str, stream: Optional[str] = None) -> List[Stream]:
        return [s for s in self.streams if s.module == module and (stream is None or s.stream == stream)]


def _read(path: Path) -> str:
    data = path.read_bytes()
    if path.name.endswith(".xz"):
        data = lzma.decompress(data)
    elif path.name.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8")


_CATALOGS: Dict[Tuple[str, ...], Catalog] = {}


def load(paths: Iterable[Path]) -> Catalog:
    """The modules of the given snapshot files, parsed once per set of files (they never change in place)."""
    key = tuple(sorted(str(p) for p in paths))
    if key not in _CATALOGS:
        if len(_CATALOGS) >= 8:
            _CATALOGS.pop(next(iter(_CATALOGS)))
        _CATALOGS[key] = _load([Path(p) for p in key])
    return _CATALOGS[key]


def _load(paths: Iterable[Path]) -> Catalog:
    index = Modulemd.ModuleIndex.new()
    for path in paths:
        index.update_from_string(_read(path), True)
    catalog = Catalog()
    for name in index.get_module_names():
        module = index.get_module(name)
        defaults = module.get_defaults()
        if defaults is not None and defaults.get_default_stream():
            catalog.defaults[name] = defaults.get_default_stream()
            for stream_name in module.get_stream_names():
                profiles = defaults.get_default_profiles_for_stream(stream_name)
                if profiles:
                    catalog.default_profiles[(name, stream_name)] = list(profiles)
        for stream in module.get_all_streams():
            requires = []
            for deps in stream.get_dependencies():
                block = {}
                for required in deps.get_runtime_modules():
                    block[required] = list(deps.get_runtime_streams(required))
                requires.append(block)
            profiles = {p: list(stream.get_profile(p).get_rpms()) for p in stream.get_profile_names()}
            catalog.streams.append(Stream(name, stream.get_stream_name(), int(stream.get_version()),
                                          stream.get_context() or "", list(stream.get_rpm_artifacts()), requires,
                                          profiles))
    return catalog


def _agrees(block: Mapping[str, List[str]], active: Mapping[str, str]) -> bool:
    for module, streams in block.items():
        if module == PLATFORM or not streams:
            continue
        wanted = [s for s in streams if not s.startswith("-")]
        refused = [s[1:] for s in streams if s.startswith("-")]
        current = active.get(module)
        if current is None:
            if wanted:
                return False                                   # needed and not active yet: to be enabled
            continue
        if (wanted and current not in wanted) or current in refused:
            return False
    return True


@dataclass
class Selection:
    active: Dict[str, str]                                     # module -> stream
    contexts: List[Stream]                                     # the contexts of active streams that apply
    install: List[str] = field(default_factory=list)           # package names from module_install profiles

    def artifacts(self) -> Set[str]:
        return {a for s in self.contexts for a in s.artifacts}


def select(catalog: Catalog, asked: Mapping[str, str], disabled: Iterable[str] = (),
           install: Iterable[str] = ()) -> Selection:
    """The active streams and their applicable contexts (steps 1-2). Asked streams are hard: what they need is
    enabled, and a contradiction is a module_conflict. Default streams are weak, as libdnf solves them: one whose needs
    clash with the asked, disabled or other active streams drops out quietly, and it never moves another stream."""
    disabled_set = set(disabled)
    active: Dict[str, str] = {}
    pinned: Set[str] = set()
    profiles_asked: List[Tuple[str, str, Optional[str]]] = []
    asked = dict(asked)
    for spec in install:                                       # `nodejs:18/common`, `nodejs:18`, `nodejs/common`
        name_stream, _, profile = spec.partition("/")
        name, _, stream = name_stream.partition(":")
        stream = stream or asked.get(name) or catalog.defaults.get(name, "")
        if not stream:
            raise ModuleConflict(name, [], "no stream asked and no default stream")
        if not profile and not catalog.default_profiles.get((name, stream)):
            raise ModuleConflict(name, [stream], "no profile asked and the stream has no default profile")
        profiles_asked.append((name, stream, profile or None))
        asked[name] = stream
    for module, stream in asked.items():
        if not catalog.of(module, stream):
            raise ModuleConflict(module, [stream], "no such stream")
        active[module] = stream
        pinned.add(module)
    for module, stream in catalog.defaults.items():
        if module not in active and module not in disabled_set and catalog.of(module, stream):
            active[module] = stream
    for _ in range(64):
        changed = False
        for module, stream in list(active.items()):
            if module not in active:
                continue
            candidates = catalog.of(module, stream)
            if any(all(_agrees(b, active) for b in s.requires) for s in candidates):
                continue
            needs = _needs(max(candidates, key=lambda s: s.version), active)
            if module not in pinned:
                # A default stream: enable what it needs only where nothing decided otherwise, else let it go.
                if any(r in pinned or r in disabled_set or r in active for r, _ in needs):
                    del active[module]
                    changed = True
                    continue
            for required, wanted in needs:
                if required in pinned or required in disabled_set:
                    raise ModuleConflict(required, [active.get(required, "disabled")] + wanted,
                                         "%s:%s needs it" % (module, stream))
                # An asked stream overrides a default one of the module it needs.
                choice = catalog.defaults.get(required) if catalog.defaults.get(required) in wanted else wanted[0]
                active[required] = choice
                changed = True
        if not changed:
            break
    contexts = [s for module, stream in active.items() for s in catalog.of(module, stream)
                if all(_agrees(b, active) for b in s.requires)]
    for module in list(active):
        if not any(s.module == module for s in contexts):
            if module in pinned:
                raise ModuleConflict(module, [active[module]], "no context agrees with the active streams")
            del active[module]
    contexts = [s for s in contexts if s.module in active]
    names: List[str] = []
    for module, stream, profile in profiles_asked:
        profile_names = [profile] if profile else catalog.default_profiles.get((module, stream), [])
        newest = max(catalog.of(module, stream), key=lambda s: s.version)
        for name in profile_names:
            for rpm in newest.profiles.get(name, []):
                if rpm not in names:
                    names.append(rpm)
    return Selection(active, contexts, names)


def _needs(stream: Stream, active: Mapping[str, str]) -> List[Tuple[str, List[str]]]:
    """The modules a stream needs that the active streams do not give it, with the streams it accepts."""
    out = []
    for block in stream.requires:
        for required, streams in block.items():
            wanted = [s for s in streams if not s.startswith("-")]
            if required != PLATFORM and wanted and active.get(required) not in wanted:
                out.append((required, wanted))
    return out


def nevra_name(nevra: str) -> str:
    """`nodejs-1:18.20.4-1.module+el8.10.0+1.x86_64` → `nodejs`."""
    rest = nevra.rsplit(".", 1)[0]
    name_version, _, _release = rest.rpartition("-")
    return name_version.rpartition("-")[0]


def excluded(catalog: Catalog, selection: Selection, solvables: Iterable[Tuple[int, str, str]]) -> Set[int]:
    """Solvable ids to keep out of the pool (step 3), from `(id, nevra, name)`; NEVRAs as `name-epoch:ver-rel.arch`."""
    active = selection.artifacts()
    modular = {a for s in catalog.streams for a in s.artifacts}
    present = list(solvables)
    # Names of active artifacts the pool really has (libdnf): one listed in a repository outside the profile hides
    # nothing.
    active_names = {name for _, nevra, name in present if nevra in active}
    out: Set[int] = set()
    for sid, nevra, name in present:
        if nevra in modular:
            if nevra not in active:
                out.add(sid)
        elif name in active_names:
            out.add(sid)
    return out


def artifact_streams(snapshots: Iterable[Any], builds: List[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """`{nevra: ["nodejs:18", ...]}` for builds that are artifacts of a module stream (for the card and events)."""
    paths = [p for s in snapshots if (p := s.path("modules")) is not None]
    if not paths:
        return {}
    catalog = load(paths)
    wanted = {"%s-%s.%s" % (b["name"], b["evr"] if ":" in b["evr"] else "0:" + b["evr"], b["arch"]): b for b in builds}
    out: Dict[str, List[str]] = {}
    for stream in catalog.streams:
        for artifact in stream.artifacts:
            build = wanted.get(artifact)
            if build is not None:
                key = "%s-%s.%s" % (build["name"], build["evr"], build["arch"])
                label = "%s:%s" % (stream.module, stream.stream)
                if label not in out.setdefault(key, []):
                    out[key].append(label)
    return out
