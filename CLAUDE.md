# PackageHub — guide for agents

Internal catalog of package metadata for 7 ecosystems: pypi, npm, crates, go, nuget, maven, rpm.
Done: multi-ecosystem stages E0–E6, catalog pages UX0–UX6, Updates digest U0–U2 (U3–U5 open).
FastAPI + SQLAlchemy async + Alembic + Celery + Postgres 16 + Redis; React/Vite frontend.

## Documents (read the part your task points to, not everything)

| File | What |
|---|---|
| `SPEC-MULTI-ECOSYSTEM.md` | Design of the multi-ecosystem catalog. Source of truth for behaviour; also SLA (§1.1), dependency semantics (§7.5), pipeline write rules (§8.4) |
| `IMPL-MULTI-ECOSYSTEM.md` | Implementation plan: stages, work packages (WP), process rules, migration table |
| `design/schema-target.sql`, `design/ecosystem_contract.py` | Target DDL and plugin contract |
| `SPEC-MVP-API.md` | HTTP API contract (all endpoints; feature specs link here for fields) |
| `SPEC-DAILY-UPDATE.md` | Daily update: runs, per-ecosystem change detectors, event kinds, metrics |
| `SPEC-UPDATES-DIGEST.md` | Updates page redesign: U0–U2 done, U3–U5 open |
| `SPEC-CATALOG-PAGES.md` | Home page, search and package card: behaviour; stages UX0–UX6 implemented |
| `README.md`, `docs/ecosystems.md` | Setup, commands, ops; per-ecosystem addressing and resolve examples |
| `RESOLVER.md` | Reference: per-ecosystem dependency resolution algorithms (RPM: SPEC-MULTI §9.8) |
| `docs/archive/` | MVP-era specs (PyPI-only), history only: do not use as a source of behaviour |

## Layout

```
backend/app/            api/v1 (routes), models, schemas, fetcher (pipeline, events), tasks (celery), services, auth,
                        normalizers, resolver/ (service, catalog, cache), cli.py, celery_app.py
                        connectors/base.py: legacy exceptions/types, still imported by tasks/, fetcher/, resolver/
backend/app/ecosystems/ shared: base.py (plugin contract), registry, routing, merge, purl, schemas (jsonb),
                        versions_common. One package per ecosystem (pypi npm crates go nuget maven rpm):
                        plugin, coordinates, versions, environment, resolver, adapters/ (upstream clients), and
                        changes (change detector; pypi, npm, go, nuget) plus ecosystem-specific modules
rpm_worker/             separate container (libsolv), Celery queues `rpm` and `rpm-batch`
backend/registries*.yaml  sources/profiles for `app.cli registries import`
backend/alembic/versions/
backend/tests/          unit, integration, reference, migrations, fixtures (fixtures/http = recorded upstream)
frontend/src/           pages, lib (api.js, helpers, shared components), test
scripts/stand.sh        long-lived test stand
design/                 design artifacts (DDL, contract, schema checks)
```

## Commands

```bash
make check                      # everything CI runs without docker: ruff, mypy, backend unit, frontend lint+tests, docs-check
make mypy                       # mypy only
make backend-unit               # backend unit tests only
make frontend                   # frontend lint + tests
make docs-check                 # relative links in *.md resolve to existing files
make openapi                    # refresh backend/tests/fixtures/openapi.json after an API change (update SPEC-MVP-API.md too)
scripts/stand.sh status         # test stand: containers, migration, catalog size
scripts/stand.sh migrate        # backup, stop worker/beat, alembic upgrade head, start them again
scripts/check_migrations.sh     # migrations on synthetic data: base -> head -> downgrade -> head (own compose project)
scripts/record_fixtures.sh       # re-record upstream responses for offline CI (live network)
```

- `scripts/stand.sh`: up, seed, daily-update, logs, psql, backup, restore, down, migrate, status.
  `python -m app.cli`: seed, fetch, retry-failed, daily-update, presence-probe, stats, registries (import, test, list).
- Backend tools run through `uv` (the local `.venv` is not complete): see the Makefile targets.
- Integration tests need the stack: `docker compose up -d --build postgres redis api rpm-worker`, then
  `docker compose run --rm api pytest tests/integration`. They run offline against recorded upstream responses
  (`HTTP_REPLAY_DIR`); never add tests that need the live internet to the default suite.
- The stand (`packagehub-stand`) holds long-lived data. Do not run `docker compose down -v` against it and do not
  drop its database. Take `scripts/stand.sh backup` before anything that changes its schema.
  The stand runs `main` from its own worktree `.claude/worktrees/stand` (detached; its `backups/` live there). After a
  stage merges: `git -C .claude/worktrees/stand checkout -q --detach origin/main` after a fetch, then `stand.sh up` from it.

## Rules

1. **Spec first.** If the code, data or an upstream source disagrees with the spec, stop that part and report the
   discrepancy with options; do not invent behaviour.
2. **Protected paths** — change only when the task explicitly says so:
   `backend/app/ecosystems/base.py` (plugin contract), `backend/alembic/versions/`, `design/`, `.github/workflows/`.
3. **Moves are separate commits.** When a task moves code, the move commit changes no logic; logic changes follow.
4. **Tests come with the change.** New behaviour gets unit tests; upstream I/O is tested with recorded fixtures
   (`backend/tests/fixtures/http/`), not live calls.
5. **Style.** Match the surrounding code: naming, comment density (few, explaining why), no new dependencies without
   need. Python 3.12, ruff clean, type hints; `backend/app/ecosystems/` must pass `mypy --strict`.
6. **Commits.** Imperative subject line in English, body says what and why. Do not push, open PRs or merge unless
   the task says so.
7. **Report.** Finish with the report template from `.claude/templates/wp-report.md`.
8. **Bugs.** A bug not fixed in the change that found it gets a GitHub issue labelled `bug`: what happens, why,
   where. The fixing commit says `Fixes #N`, and every fix comes with a regression test that fails on the old code.
   A bug already in `main` that breaks the running app or the stand is fixed right away in a small PR from `main`
   (`claude/hotfix-<topic>`) that goes to auto-merge when checks are green, without waiting for the stage.
9. **Models.** Every task, not only stage WPs, goes to the cheapest model that can be trusted with it (see
   *Models and subagents*); Opus keeps the decisions and checks the result.
10. **Docs land with the change.** Every PR — stage, hotfix or other — updates in the same PR the documents its change
    makes stale, before it is marked ready; "later" does not exist. Each fact has one home, others link to it:
    - stage or WP finished: the spec's header (version, date, status) and stage table together with the "Сделано в …"
      note; a decision or a deviation goes into the spec (rule 1);
    - endpoint, field or parameter: `SPEC-MVP-API.md` (feature specs only link to it);
    - behaviour of daily update, sources, pipeline: `SPEC-DAILY-UPDATE.md` or `SPEC-MULTI-ECOSYSTEM.md`;
    - command, script, setting, layout, CI job: `README.md` and, if agents need it, this file;
    - migration: the list in `IMPL-MULTI-ECOSYSTEM.md` §2.
    A PR that changes none of these says "docs: none affected" in its description.

## Models and subagents

Applies to all work in the repo: stages, hotfixes, investigations, stand checks, docs.

| Model | Takes |
|---|---|
| Haiku (`Explore` or any agent with `model: haiku`) | Read-only lookups with a clear question: where something is done, callers and usages, reading logs, test output and CI results, stand status, summarizing a section of a spec |
| Sonnet (`implementer`, or `model: sonnet`) | Well-specified changes: WPs, bug fixes with a known cause and a failing case, fixtures, tests from a case list, frontend or routes after a sample, scripts, docs updates; research that needs reading code and judging, not deciding |
| Opus (the main session, `reviewer`) | Specs and owner questions, contracts, algorithms, data migrations, bugs with an unclear cause, reviews, and checking every subagent result |

- Keep a task in the main session when it is a few tool calls or leans on what the conversation already knows: a
  subagent starts cold, re-reads the context and costs more than it saves.
- Give the brief everything it needs: files or area, the question or the done criterion, the check command.
  Writers get `.claude/templates/wp-task.md` and their own worktree (see *Process*).
- A subagent's report is not proof: check the diff, the test run or the file it names before relying on it or
  reporting it to the owner.

## Process (IMPL-MULTI-ECOSYSTEM.md §0)

- One branch and one draft PR per stage (`claude/me-<stage>`); WPs are commits inside it.
- Implementer subagents (Sonnet) take self-contained WP tasks (`.claude/templates/wp-task.md`) and work in their own
  worktree; Opus checks the result before it lands on the stage branch (cherry-pick), then removes the worktree.
- Worktree for a WP: the harness `isolation: worktree` forks from `main` and deletes an unchanged worktree when the
  agent stops, so implementers create their own instead:
  `git worktree add -b wp/<id> .claude/worktrees/wp-<id> <stage-branch-head-sha>` and keep it until Opus removes it.
- A reviewer subagent checks every stage PR against the spec before it is marked ready.
- When the stage review is addressed and checks are green, Opus updates the PR description, marks it ready and
  enables auto-merge (merge commit) without asking, then starts the next stage from the updated `main`.
- Opus runs WPs and stages back to back in one turn: WP, checks, stage review, auto-merge, stand, next stage. The
  turn ends only for a question that is the owner's to answer (rule 1), a failure Opus cannot fix, or the end of the
  plan; progress notes go out along the way without ending the turn. CI failures, conflicts and review comments on
  the stage PR wake the session through the app's PR monitor (auto-fix), so nobody restarts it by hand.
