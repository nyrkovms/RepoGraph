# One-command checks. `make check` runs what CI runs, except the docker-based jobs
# (integration tests, acceptance smoke) and the frontend production build.

UV_RUN = uv run -q --python 3.12
# Same versions as the CI image (backend/pyproject.toml extras), so local and CI verdicts match.
RUFF = ruff==0.17.3
MYPY = mypy==2.4.0
PYTEST = pytest==9.2.0

.PHONY: check backend-lint mypy backend-unit openapi frontend docs-check

check: backend-lint mypy backend-unit frontend docs-check

backend-lint:
	cd backend && $(UV_RUN) --with $(RUFF) -- ruff check app tests

# The application and the plugin contract (design artifact); backend/pyproject.toml makes the plugin layer and the
# contract strict.
mypy:
	cd backend && $(UV_RUN) --with-editable . --with $(MYPY) -- mypy app ../design/ecosystem_contract.py

backend-unit:
	cd backend && LOG_TO_FILE=false $(UV_RUN) --with-editable . --with $(PYTEST) --with greenlet -- pytest tests/unit

# Refresh the OpenAPI snapshot after an intended API change; update SPEC-MVP-API.md in the same PR.
openapi:
	cd backend && LOG_TO_FILE=false UPDATE_OPENAPI_SNAPSHOT=1 $(UV_RUN) --with-editable . --with $(PYTEST) --with greenlet -- pytest tests/unit/test_openapi_snapshot.py

frontend:
	@test -d frontend/node_modules || { echo "frontend/node_modules is missing: run 'cd frontend && npm ci'"; exit 1; }
	cd frontend && npm run lint && npm run test:run

# Relative links in *.md must resolve to existing files.
docs-check:
	python3 -I scripts/check_doc_links.py
