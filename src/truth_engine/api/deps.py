"""Shared FastAPI dependencies for the data/action routers.

Two layers, composed:

  * `SyncSessionDep` — the sync `Session` bridge (`db.session.get_sync_session`),
    used by every route in this package instead of the async session
    fastapi-users owns (see `db/session.py`'s module docstring for the
    rationale).
  * Owner-scoping — `ProjectDep` loads a `Project` and 404s unless it belongs
    to `current_active_user`; `ArtifactDep`/`GapDep` additionally 404 unless
    the sub-resource belongs to *that* project. Every project-scoped and
    sub-resource-scoped route in `api/routers/*.py` depends on one of these
    three, never queries `Project`/`Artifact`/`Gap` directly — this is the
    single place the multi-tenant invariant (CLAUDE.md) is enforced, so a new
    router can't accidentally skip it.
  * `SyncSessionFactoryDep` — a *factory* for a brand-new sync `Session`,
    used only by `api/routers/pipeline.py`'s background job, which must run
    against its own session rather than the request-scoped `SyncSessionDep`
    (see `db/session.py`'s module docstring for why the two must never mix).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.auth.users import current_active_user
from truth_engine.db.models import Artifact, Gap, Project, User
from truth_engine.db.session import get_sync_session, get_sync_sessionmaker
from truth_engine.reasoning.providers import EmbeddingProvider, LLMProvider

SyncSessionDep = Annotated[Session, Depends(get_sync_session)]
CurrentUser = Annotated[User, Depends(current_active_user)]


def get_sync_session_factory() -> Callable[[], AbstractContextManager[Session]]:
    """Factory for a **new**, independently owned sync `Session` per call.

    Calling the returned factory yields a context manager that closes the
    session on `__exit__`; production returns `get_sync_sessionmaker()`
    itself — a SQLAlchemy `Session` already supports the context-manager
    protocol, so `sessionmaker()` needs no wrapping. Tests override this
    dependency to bind the background job to the test's own transactional
    session instead of opening a new connection to the real database (a
    genuinely new connection can't see another connection's uncommitted
    transaction, which is how `tests/conftest.py`'s `db_session` fixture
    isolates every test).
    """
    return get_sync_sessionmaker()


SyncSessionFactoryDep = Annotated[
    Callable[[], AbstractContextManager[Session]], Depends(get_sync_session_factory)
]


def get_pipeline_llm_provider() -> LLMProvider | None:
    """`None` in production — each pipeline stage resolves its own default
    (the real Ollama/Anthropic-backed provider, per `Settings`). Tests
    override this dependency to inject one hermetic fake for the *whole*
    `POST /projects/{id}/run` background job, mirroring how
    `run_project_phases`/`run_project_view`/etc. already accept an optional
    `provider` for the same reason at the stage level."""
    return None


def get_pipeline_embedding_provider() -> EmbeddingProvider | None:
    """`None` in production — see `get_pipeline_llm_provider`."""
    return None


PipelineLLMProviderDep = Annotated[LLMProvider | None, Depends(get_pipeline_llm_provider)]
PipelineEmbeddingProviderDep = Annotated[
    EmbeddingProvider | None, Depends(get_pipeline_embedding_provider)
]


def get_owned_project(
    project_id: uuid.UUID, user: CurrentUser, session: SyncSessionDep
) -> Project:
    """404 (never 403) for both "doesn't exist" and "exists but isn't yours" —
    a project-existence oracle for other users' projects is itself an
    isolation leak worth avoiding."""
    project = session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


ProjectDep = Annotated[Project, Depends(get_owned_project)]


def get_project_artifact(
    artifact_id: uuid.UUID, project: ProjectDep, session: SyncSessionDep
) -> Artifact:
    artifact = session.scalar(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.project_id == project.id)
    )
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return artifact


ArtifactDep = Annotated[Artifact, Depends(get_project_artifact)]


def get_project_gap(gap_id: uuid.UUID, project: ProjectDep, session: SyncSessionDep) -> Gap:
    gap = session.scalar(select(Gap).where(Gap.id == gap_id, Gap.project_id == project.id))
    if gap is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "gap not found")
    return gap


GapDep = Annotated[Gap, Depends(get_project_gap)]


class PageParams:
    """Basic offset pagination, shared by every list-shaped surface whose size
    scales with corpus size (artifacts, timeline, direction labels, gaps,
    phase assignments)."""

    def __init__(self, limit: int = 50, offset: int = 0) -> None:
        self.limit = max(1, min(limit, 200))
        self.offset = max(0, offset)


PageParamsDep = Annotated[PageParams, Depends(PageParams)]
