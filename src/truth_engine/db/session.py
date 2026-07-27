"""Async engine + session factory (psycopg 3 driver), plus a sync bridge for
the (sync) pipeline/analysis services.

**Why two engines.** fastapi-users ships an async `SQLAlchemyUserDatabase`, so
auth (`auth/users.py`) is async end-to-end. Every pipeline service
(`analysis/*.py`, `ingest`, `parse`, ...) is sync `sqlalchemy.orm.Session` —
rewriting them onto `AsyncSession` just to satisfy the API layer would mean
either forking every analysis module into async twins or awkwardly running
sync ORM calls inside `run_in_executor` call sites scattered through routers.
Instead, the data/action routers depend on a **second, sync** engine
(`get_sync_engine`/`get_sync_session`) pointed at the same
`Settings.database_url` (psycopg 3 serves both) — the same split
`alembic/env.py` already uses for migrations (see CLAUDE.md's Gotchas). A sync
route handler is dispatched by FastAPI to its threadpool, so a blocking
`Session` call here never blocks the event loop; the async `current_active_user`
dependency resolves independently (its own connection) before the sync
handler runs. The two sessions are never mixed within one request."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from truth_engine.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession — auth routes only."""
    async with get_sessionmaker()() as session:
        yield session


@lru_cache
def get_sync_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True)


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


def get_sync_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a sync `Session` — every data/action route
    (artifacts, timeline, direction, gaps, phases, report) uses this, the same
    `Session` type every `analysis`/pipeline service is already written
    against."""
    with get_sync_sessionmaker()() as session:
        yield session
