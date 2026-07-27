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
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.auth.users import current_active_user
from truth_engine.db.models import Artifact, Gap, Project, User
from truth_engine.db.session import get_sync_session

SyncSessionDep = Annotated[Session, Depends(get_sync_session)]
CurrentUser = Annotated[User, Depends(current_active_user)]


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
