"""Database layer — SQLAlchemy 2.0 models, engine, and session management.

One store (PostgreSQL + pgvector) holds structured metadata, entities, timeline,
phase assignments, the relationship graph, audit/versioning, AND embeddings.
"""

from truth_engine.db.base import Base
from truth_engine.db.session import get_async_session, get_engine, get_sessionmaker

__all__ = ["Base", "get_async_session", "get_engine", "get_sessionmaker"]
