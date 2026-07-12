FROM python:3.12-slim

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

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[test,lint]"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
