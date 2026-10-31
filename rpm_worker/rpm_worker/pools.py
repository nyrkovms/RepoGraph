"""libsolv pools from cached repositories (SPEC-MULTI-ECOSYSTEM.md §9.8.1-2), and what a package's builds say.

`primary` and `filelists` are loaded into one libsolv repo and written as `repo.solv`, so a pool over several sources
loads in milliseconds. A pool covers the sources of one `distro` and one arch policy (`x86_64` takes `noarch` and the
multilib arches too, as dnf's pool does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import solv

# rpmlib() capabilities are satisfied by rpm itself, not by a package.
_IGNORED_PREFIXES = ("rpmlib(",)
_WEAK = (("recommends", "SOLVABLE_RECOMMENDS"), ("suggests", "SOLVABLE_SUGGESTS"),
         ("supplements", "SOLVABLE_SUPPLEMENTS"), ("enhances", "SOLVABLE_ENHANCES"))


def write_solv(directory: Path, files: Mapping[str, str]) -> None:
    pool = solv.Pool()
    repo = pool.add_repo("snapshot")
    handle = solv.xfopen(str(directory / files["primary"]))
    repo.add_rpmmd(handle, None, 0)
    handle.close()
    if "filelists" in files:
        handle = solv.xfopen(str(directory / files["filelists"]))
        repo.add_rpmmd(handle, None, solv.Repo.REPO_EXTEND_SOLVABLES)
        handle.close()
    repo.internalize()
    out = solv.xfopen(str(directory / "repo.solv"), "w")
    repo.write(out)
    out.close()


def make_pool(arch: str, snapshots: Iterable[Tuple[str, Path]]) -> Tuple[Any, Dict[str, Any]]:
    """A pool over `(name, solv file)` pairs, ready for whatprovides; the repos by name."""
    pool = solv.Pool()
    pool.setarch(arch)
    repos: Dict[str, Any] = {}
    for name, path in snapshots:
        repo = pool.add_repo(name)
        handle = solv.xfopen(str(path))
        repo.add_solv(handle)
        handle.close()
        repos[name] = repo
    pool.addfileprovides()
    pool.createwhatprovides()
    return pool, repos


def evr_parts(evr: str) -> Tuple[Optional[int], str, str]:
    epoch, _, rest = evr.rpartition(":") if ":" in evr else ("", "", evr)
    version, _, release = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return (int(epoch) if epoch else None), version, release


def _deps(solvable: Any, key: int) -> List[str]:
    return [str(dep) for dep in solvable.lookup_deparray(key) if not str(dep).startswith(_IGNORED_PREFIXES)]


def describe(solvable: Any) -> Dict[str, Any]:
    """One build: NEVRA, package fields and its capabilities (SPEC §7.1, §7.2 rpm rows)."""
    epoch, version, release = evr_parts(solvable.evr)
    build: Dict[str, Any] = {
        "name": solvable.name, "epoch": epoch, "version": version, "release": release, "arch": solvable.arch,
        "evr": solvable.evr, "repo": solvable.repo.name,
        "summary": solvable.lookup_str(solv.SOLVABLE_SUMMARY),
        "description": solvable.lookup_str(solv.SOLVABLE_DESCRIPTION),
        "license": solvable.lookup_str(solv.SOLVABLE_LICENSE),
        "url": solvable.lookup_str(solv.SOLVABLE_URL),
        "buildtime": solvable.lookup_num(solv.SOLVABLE_BUILDTIME) or None,
        "sourcerpm": solvable.lookup_sourcepkg(),
        "location": solvable.lookup_location()[0],
        "checksum": _checksum(solvable),
        "size": solvable.lookup_num(solv.SOLVABLE_DOWNLOADSIZE) or None,
        "requires": _deps(solvable, solv.SOLVABLE_REQUIRES),
        "provides": _deps(solvable, solv.SOLVABLE_PROVIDES),
    }
    for name, attribute in _WEAK:
        build[name] = _deps(solvable, getattr(solv, attribute))
    return build


def _checksum(solvable: Any) -> Optional[Dict[str, str]]:
    checksum = solvable.lookup_checksum(solv.SOLVABLE_CHECKSUM)
    return {"type": checksum.typestr(), "value": checksum.hex()} if checksum else None


def builds(pool: Any, name: str) -> List[Dict[str, Any]]:
    """Every build named `name` in the pool (all arches the pool admits and noarch), source packages left out."""
    out = []
    for solvable in pool.solvables_iter():
        if solvable.name == name and solvable.arch not in ("src", "nosrc"):
            out.append(describe(solvable))
    return out


def whatprovides(pool: Any, capability: str) -> List[Dict[str, Any]]:
    dep = pool.Dep(capability) if not capability.startswith("/") else pool.Dep(capability)
    return [{"name": s.name, "evr": s.evr, "arch": s.arch, "repo": s.repo.name} for s in pool.whatprovides(dep)]
