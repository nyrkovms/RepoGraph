"""Resolve as dnf 4 does (SPEC-MULTI-ECOSYSTEM.md §9.8): a libsolv install transaction over the pool of the request's
sources, after modular filtering, with libdnf's choices: best=True (SOLVER_FORCEBEST), yum-style obsoletes, vendor
change allowed, weak dependencies on request, and a package named on the command line standing for the packages that
obsolete it too (hawkey's `q.union(obsoletes=q)`).

Request: `sources` (the profile's sources of the distro, as the API sends them), `arch`, `roots` (`[{"spec": name or
capability, "constraint": ">= 1.2"}]`), `weak_deps`, `module_streams`, `disabled_modules`, `module_install`.
Result: `nodes` (NEVRA, repo), `edges` (capability → provider among the installed set; recommends only with weak
dependencies), `roots`, `modules` (the active streams), or `problems` when there is no solution — with the stream to
enable when a root exists only in an inactive one (§9.8.3.6).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

import solv

from rpm_worker import modules, pools, repo

_SELECT = (solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_PROVIDES | solv.Selection.SELECTION_CANON
           | solv.Selection.SELECTION_DOTARCH | solv.Selection.SELECTION_REL | solv.Selection.SELECTION_FILELIST)


def nevra(s: Any) -> str:
    """With the epoch always written, as modulemd lists artifacts."""
    epoch, version, release = pools.evr_parts(s.evr)
    return "%s-%d:%s-%s.%s" % (s.name, epoch or 0, version, release, s.arch)


def display_nevra(s: Any) -> str:
    """As rpm writes it: no epoch when it is 0."""
    return "%s-%s.%s" % (s.name, s.evr, s.arch)


def _consider(pool: Any, keep_out: Set[int]) -> None:
    if keep_out:
        pool.set_considered_list([s.id for s in pool.solvables_iter() if s.id not in keep_out])


def _exact(pool: Any, s: Any) -> Any:
    """`name = evr` of a build: Obsoletes match versions (perl-interpreter obsoletes only `perl < 4:5.22`)."""
    return pool.Dep(s.name).Rel(solv.REL_EQ, pool.Dep(s.evr))


def _with_obsoleters(pool: Any, selection: Any, by_name: bool) -> Any:
    """dnf installs a named package or anything that obsoletes it (the newest wins under best=True)."""
    if not by_name:
        return selection
    ids = {s.id for s in selection.solvables()}
    extra = set()
    for s in selection.solvables():
        for obsoleter in pool.whatmatchesdep(solv.SOLVABLE_OBSOLETES, _exact(pool, s)):
            if obsoleter.id not in ids:
                extra.add(obsoleter.id)
    if not extra:
        return selection
    return _one_of(pool, sorted(ids | extra))


def _one_of(pool: Any, ids: List[int]) -> Any:
    """One job over all candidates (installing each would install both a package and its obsoleter)."""
    selection = pool.Selection()
    selection.add_raw(solv.Job.SOLVER_SOLVABLE_ONE_OF, pool.towhatprovides(ids))
    return selection


def _inactive_hint(catalog: Optional[modules.Catalog], selection: Optional[modules.Selection], spec: str) -> Optional[str]:
    if catalog is None or selection is None:
        return None
    name = spec.split(" ")[0]
    streams = sorted({"%s:%s" % (s.module, s.stream) for s in catalog.streams
                      if any(modules.nevra_name(a) == name for a in s.artifacts)
                      and selection.active.get(s.module) != s.stream})
    return ("%s is only in module streams that are not active: enable one of %s" % (name, ", ".join(streams))
            if streams else None)


def resolve(request: Mapping[str, Any]) -> Dict[str, Any]:
    arch = str(request.get("arch") or "x86_64")
    weak = bool(request.get("weak_deps", True))
    pairs = []
    snapshots = []
    for raw in request.get("sources") or []:
        src = repo.source_from(raw)
        if arch in src.arches:
            snapshot = repo.snapshot(src, arch)
            pairs.append((src.slug, snapshot.solv))
            snapshots.append(snapshot)
    pool, _ = pools.make_pool(arch, pairs)

    module_paths = [p for s in snapshots if (p := s.path("modules")) is not None]
    catalog: Optional[modules.Catalog] = None
    selection: Optional[modules.Selection] = None
    if module_paths:
        catalog = modules.load(module_paths)
        try:
            selection = modules.select(catalog, request.get("module_streams") or {},
                                       request.get("disabled_modules") or [], request.get("module_install") or [])
        except modules.ModuleConflict as exc:
            return {"problems": [{"code": "module_conflict", "module": exc.module, "message": str(exc)}]}
        keep_out = modules.excluded(catalog, selection, ((s.id, nevra(s), s.name) for s in pool.solvables_iter()))
        _consider(pool, keep_out)
        pool.createwhatprovides()

    jobs = []
    problems: List[Dict[str, Any]] = []
    specs = [(str(r["spec"]), str(r.get("constraint") or "")) for r in request.get("roots") or []]
    specs += [(name, "") for name in (selection.install if selection else [])]
    for spec, constraint in specs:
        text = "%s %s" % (spec, constraint) if constraint else spec
        selected = pool.select(text, _SELECT)
        if selected.isempty():
            problem = {"code": "not_found", "message": "no package provides %s" % text, "spec": text}
            hint = _inactive_hint(catalog, selection, text)
            if hint:
                problem["hint"] = hint
            problems.append(problem)
            continue
        by_name = any(s.name == spec for s in selected.solvables())
        selected = _with_obsoleters(pool, selected, by_name)
        jobs += selected.jobs(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_FORCEBEST)
    if problems:
        return {"problems": problems}

    solver = pool.Solver()
    solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 0 if weak else 1)
    solver.set_flag(solv.Solver.SOLVER_FLAG_BEST_OBEY_POLICY, 1)
    solver.set_flag(solv.Solver.SOLVER_FLAG_YUM_OBSOLETES, 1)
    solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_VENDORCHANGE, 1)
    found = solver.solve(jobs)
    if found:
        return {"problems": [{"code": "unsolvable", "message": str(p),
                              "solutions": [e.str() for sol in p.solutions() for e in sol.elements(True)]}
                             for p in found]}
    installed = list(solver.transaction().newsolvables())
    ids = {s.id for s in installed}
    nodes = [{"nevra": display_nevra(s), "name": s.name, "evr": s.evr, "arch": s.arch, "repo": s.repo.name}
             for s in sorted(installed, key=lambda s: (s.name, s.arch))]
    kinds = [("requires", solv.SOLVABLE_REQUIRES)] + ([("recommends", solv.SOLVABLE_RECOMMENDS)] if weak else [])
    edges: List[Dict[str, str]] = []
    for s in installed:
        for kind, attribute in kinds:
            for dep in s.lookup_deparray(attribute):
                text = str(dep)
                if text.startswith("rpmlib("):
                    continue
                for provider in pool.whatprovides(dep):
                    if provider.id in ids and provider.id != s.id:
                        edges.append({"from": display_nevra(s), "to": display_nevra(provider), "capability": text,
                                      "kind": kind})
                        break
    roots = []
    for spec, constraint in specs:
        text = "%s %s" % (spec, constraint) if constraint else spec
        candidates = list(pool.select(text, _SELECT).solvables())
        candidates += [o for c in list(candidates)
                       for o in pool.whatmatchesdep(solv.SOLVABLE_OBSOLETES, _exact(pool, c))]
        chosen = next((c for c in candidates if c.id in ids), None)
        if chosen is not None:
            roots.append({"spec": text, "nevra": display_nevra(chosen)})
    return {"nodes": nodes, "edges": edges, "roots": roots,
            "modules": dict(selection.active) if selection else {}}
