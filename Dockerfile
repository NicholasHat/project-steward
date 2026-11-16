# syntax=docker/dockerfile:1

# --- Build stage: resolve the locked deps into /app/.venv. -------------------
FROM python:3.12-slim AS build

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

# A C toolchain for deps without a prebuilt wheel on every platform
# (hdbscan ships no linux/arm64 wheel, e.g. Docker on Apple Silicon).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Locked deps first (cached) using only the manifest + lockfile. The API runs
# the pipeline in-process, so it needs the `pipeline` extra too.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra pipeline --no-install-project
# NER model for the extract stage (spaCy installs it via uv; the venv has no pip).
RUN python -m spacy download en_core_web_sm

# App source.
COPY README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev --extra pipeline --inexact --no-editable

# --- Runtime stage: no compiler, just the venv + migrations. -----------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

# Apply migrations, seed phase templates, then serve.
CMD ["sh", "-c", "alembic upgrade head && python -m truth_engine.db.seed && uvicorn truth_engine.api.app:app --host 0.0.0.0 --port 8000"]
