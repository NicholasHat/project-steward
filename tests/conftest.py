"""Shared fixtures for pipeline tests.

Tests run against the already-migrated dev Postgres (`TRUTH_DATABASE_URL`) —
this repo treats Alembic migrations as the source of schema truth (see
CLAUDE.md), so building a parallel schema via `Base.metadata.create_all()`
would drift from it (and can't handle the `vector` extension the `embeddings`
table needs). Each test gets a real transaction that's rolled back afterward:
a SAVEPOINT-per-commit session so code under test can call `session.commit()`
freely (as the ingest/parse services do) without ever touching the outer
transaction, which is what actually gets rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.db.models import Project, User
from truth_engine.db.seed import seed


@pytest.fixture(scope="session", autouse=True)
def _seed_reference_data() -> None:
    """Phase templates are reference data the analysis stages look up by domain
    (phases, gaps, view all read them). Seed once per session so the suite is
    self-contained on any migrated database, rather than silently depending on
    a hand-seeded dev DB. Idempotent."""
    seed()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(get_settings().database_url)
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def user(db_session: Session) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def project(db_session: Session, user: User, tmp_path) -> Project:
    p = Project(owner_id=user.id, name="Test Project", root_path=str(tmp_path))
    db_session.add(p)
    db_session.flush()
    return p
