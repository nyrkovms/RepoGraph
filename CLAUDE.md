# PackageHub — guide for agents

Internal catalog of package metadata: PyPI today, being extended to npm, Maven, NuGet, Go, crates.io and RPM.
FastAPI + SQLAlchemy async + Alembic + Celery + Postgres 16 + Redis; React/Vite frontend.

## Documents (read the part your task points to, not everything)

| File | What |
|---|---|
| `SPEC-MULTI-ECOSYSTEM.md` | Design of the multi-ecosystem catalog. Source of truth for behaviour |
| `IMPL-MULTI-ECOSYSTEM.md` | Implementation plan: stages, work packages (WP), process rules, migration table |
| `design/schema-target.sql`, `design/ecosystem_contract.py` | Target DDL and plugin contract |
| `SPEC-DAILY-UPDATE.md`, `SPEC-MVP-API.md` | Daily update/digest and current API contracts |

## Layout

```
backend/app/            api/v1 (routes), models, schemas, fetcher (pipeline, events), tasks (celery),
                        connectors (PyPI today; moves to ecosystems/ in E0), resolver, services
backend/alembic/versions/
backend/tests/unit, backend/tests/integration
frontend/src/pages, frontend/src/lib/api.js
scripts/stand.sh        long-lived test stand
design/                 design artifacts (DDL, contract, schema checks)
```

## Commands

```bash
make check                      # everything CI runs without docker: ruff, mypy, backend unit, frontend lint+tests
make backend-unit               # backend unit tests only
make frontend                   # frontend lint + tests
scripts/stand.sh status         # test stand: containers, migration, catalog size
scripts/stand.sh migrate        # backup, stop worker/beat, alembic upgrade head, start them again
scripts/check_migrations.sh     # migrations on synthetic data: base -> head -> downgrade -> head (needs postgres from compose)
scripts/record_fixtures.sh       # re-record upstream responses for offline CI (live network)
```

- Backend tools run through `uv` (the local `.venv` is not complete): see the Makefile targets.
- Integration tests need the stack: `docker compose up -d --build postgres redis api`, then
  `docker compose run --rm api pytest tests/integration`. They run offline against recorded upstream responses
  (`HTTP_REPLAY_DIR`); never add tests that need the live internet to the default suite.
- The stand (`packagehub-stand`) holds long-lived data. Do not run `docker compose down -v` against it and do not
  drop its database. Take `scripts/stand.sh backup` before anything that changes its schema.

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

## Process (IMPL-MULTI-ECOSYSTEM.md §0)

- One branch and one draft PR per stage (`claude/me-<stage>`); WPs are commits inside it.
- Implementer subagents (Sonnet) take self-contained WP tasks (`.claude/templates/wp-task.md`) and work in their own
  worktree; Opus checks the result before it lands on the stage branch (cherry-pick), then removes the worktree.
- Worktree for a WP: the harness `isolation: worktree` forks from `main` and deletes an unchanged worktree when the
  agent stops, so implementers create their own instead:
  `git worktree add -b wp/<id> .claude/worktrees/wp-<id> <stage-branch-head-sha>` and keep it until Opus removes it.
- A reviewer subagent checks every stage PR against the spec before it is marked ready.
