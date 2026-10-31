"""Resolve as dnf 4 does (SPEC-MULTI-ECOSYSTEM.md §9.8): a libsolv install transaction over the pool of the request's
sources, after modular filtering.

Request: `sources` (the profile's sources of the distro, as the API sends them), `arch`, `roots` (`[{"spec": name or
capability, "constraint": ">= 1.2"}]`), `weak_deps`, `module_streams`, `disabled_modules`, `module_install`.
Result: `nodes` (NEVRA, repo), `edges` (capability → provider, from the requirements among the installed set),
`modules` (the active streams), or `problems` when libsolv finds none.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

import solv

from rpm_worker import modules, pools, repo

_KINDS = (("requires", "SOLVABLE_REQUIRES"), ("recommends", "SOLVABLE_RECOMMENDS"))


def nevra(s: Any) -> str:
    epoch, version, release = pools.evr_parts(s.evr)
    return "%s-%d:%s-%s.%s" % (s.name, epoch or 0, version, release, s.arch)


def _consider(pool: Any, keep_out: Set[int]) -> None:
    if not keep_out:
        return
    considered = [s.id for s in pool.solvables_iter() if s.id not in keep_out]
    pool.set_considered_list(considered)


def resolve(request: Mapping[str, Any]) -> Dict[str, Any]:
    arch = str(request.get("arch") or "x86_64")
    pairs = []
    snapshots = []
    for raw in request.get("sources") or []:
        src = repo.source_from(raw)
        if arch not in src.arches:
            continue
        try:
            snapshot = repo.sync(src, arch)
        except repo.Missing:
            continue
        pairs.append((src.slug, snapshot.solv))
        snapshots.append(snapshot)
    pool, _ = pools.make_pool(arch, pairs)

    module_paths = [p for s in snapshots if (p := s.path("modules")) is not None]
    selection: Optional[modules.Selection] = None
    if module_paths:
        catalog = modules.load(module_paths)
        try:
            selection = modules.select(catalog, request.get("module_streams") or {},
                                       request.get("disabled_modules") or [], request.get("module_install") or [])
        except modules.ModuleConflict as exc:
            return {"problems": [{"code": "module_conflict", "module": exc.module, "message": str(exc)}]}
        keep_out = modules.excluded(catalog, selection,
                                    ((s.id, nevra(s), s.name) for s in pool.solvables_iter()))
        _consider(pool, keep_out)
        pool.createwhatprovides()

    jobs = []
    unknown = []
    specs = [(str(r["spec"]), str(r.get("constraint") or "")) for r in request.get("roots") or []]
    specs += [(name, "") for name in (selection.install if selection else [])]
    flags = (solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_PROVIDES | solv.Selection.SELECTION_CANON
             | solv.Selection.SELECTION_DOTARCH | solv.Selection.SELECTION_REL | solv.Selection.SELECTION_FILELIST)
    for spec, constraint in specs:
        text = "%s %s" % (spec, constraint) if constraint else spec
        selected = pool.select(text, flags)
        if selected.isempty():
            unknown.append(text)
            continue
        # dnf's best=True: the newest candidate or an error, not a silent older one.
        jobs += selected.jobs(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_FORCEBEST)
    if unknown:
        return {"problems": [{"code": "not_found", "message": "no package provides %s" % u, "spec": u}
                             for u in unknown]}

    solver = pool.Solver()
    solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 0 if request.get("weak_deps", True) else 1)
    solver.set_flag(solv.Solver.SOLVER_FLAG_BEST_OBEY_POLICY, 1)
    problems = solver.solve(jobs)
    if problems:
        return {"problems": [{"code": "unsolvable", "message": str(p),
                              "solutions": [str(e) for sol in p.solutions() for e in sol.elements(True)]}
                             for p in problems]}
    installed = list(solver.transaction().newsolvables())
    ids = {s.id for s in installed}
    nodes = [{"nevra": nevra(s), "name": s.name, "evr": s.evr, "arch": s.arch, "repo": s.repo.name}
             for s in sorted(installed, key=lambda s: (s.name, s.arch))]
    edges: List[Dict[str, str]] = []
    for s in installed:
        for kind, attribute in _KINDS:
            for dep in s.lookup_deparray(getattr(solv, attribute)):
                text = str(dep)
                if text.startswith("rpmlib("):
                    continue
                for provider in pool.whatprovides(dep):
                    if provider.id in ids and provider.id != s.id:
                        edges.append({"from": nevra(s), "to": nevra(provider), "capability": text, "kind": kind})
                        break
    roots = []
    for spec, constraint in specs:
        text = "%s %s" % (spec, constraint) if constraint else spec
        for candidate in pool.select(text, flags).solvables():
            if candidate.id in ids:
                roots.append({"spec": text, "nevra": nevra(candidate)})
                break
    return {"nodes": nodes, "edges": edges, "roots": roots,
            "modules": dict(selection.active) if selection else {}}
