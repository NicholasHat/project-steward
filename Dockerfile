# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install deps first (cached) using only the manifest.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

# App source.
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv pip install --system --no-cache -e .

EXPOSE 8000

# Apply migrations, seed phase templates, then serve.
CMD ["sh", "-c", "alembic upgrade head && python -m truth_engine.db.seed && uvicorn truth_engine.api.app:app --host 0.0.0.0 --port 8000"]
