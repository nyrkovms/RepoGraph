# One-command checks. `make check` runs what CI runs, except the docker-based jobs
# (integration tests, acceptance smoke) and the frontend production build.

UV_RUN = uv run -q --python 3.12

.PHONY: check backend-lint mypy backend-unit frontend

check: backend-lint mypy backend-unit frontend

backend-lint:
	cd backend && $(UV_RUN) --with ruff -- ruff check app tests

# The plugin contract (design artifact) and, once it exists, the ecosystems package must pass `mypy --strict`.
MYPY_TARGETS = ../design/ecosystem_contract.py $(if $(wildcard backend/app/ecosystems),app/ecosystems)

mypy:
	cd backend && $(UV_RUN) --with-editable . --with mypy -- mypy --strict $(MYPY_TARGETS)

backend-unit:
	cd backend && LOG_TO_FILE=false $(UV_RUN) --with-editable . --with pytest --with greenlet -- pytest tests/unit

frontend:
	@test -d frontend/node_modules || { echo "frontend/node_modules is missing: run 'cd frontend && npm ci'"; exit 1; }
	cd frontend && npm run lint && npm run test:run
