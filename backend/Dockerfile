FROM python:3.12-slim

ARG PYPI_REPOSITORY_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN set -eux; \
    export PIP_INDEX_URL="${PYPI_REPOSITORY_URL}"; \
    trusted_host="$(python -c 'import os; from urllib.parse import urlparse; parsed = urlparse(os.environ["PYPI_REPOSITORY_URL"]); print(parsed.hostname or "" if parsed.scheme.lower() == "http" else "")')"; \
    if [ -n "${trusted_host}" ]; then \
      export PIP_TRUSTED_HOST="${trusted_host}"; \
    fi; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir ".[test,lint]"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
