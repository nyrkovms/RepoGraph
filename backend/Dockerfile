FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY app ./app
COPY tests ./tests
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY registries.example.yaml registries.pytorch.yaml registries.rpm.yaml ./

# Dependencies come from uv.lock, so CI, the stand and local `uv run` use the same versions.
RUN pip install --no-cache-dir --upgrade pip uv==0.11.7 \
    && uv export --frozen --no-hashes --no-emit-project --extra test --extra lint -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && pip uninstall -y uv && rm /tmp/requirements.txt

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
