"""Project management: create/list/read/delete. Owner-scoping is enforced by
construction here — `create_project` always sets `owner_id = user.id`,
`list_projects` always filters `owner_id == user.id`, and read/delete go
through `ProjectDep` (404s on anyone else's project)."""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from truth_engine.api.deps import CurrentUser, ProjectDep, SyncSessionDep
from truth_engine.api.schemas import ProjectCreate, ProjectDTO
from truth_engine.db.models import Project

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectDTO, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, user: CurrentUser, session: SyncSessionDep) -> Project:
    project = Project(owner_id=user.id, name=body.name, root_path=body.root_path)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("", response_model=list[ProjectDTO])
def list_projects(user: CurrentUser, session: SyncSessionDep) -> list[Project]:
    return list(
        session.scalars(
            select(Project).where(Project.owner_id == user.id).order_by(Project.created_at)
        ).all()
    )


@router.get("/{project_id}", response_model=ProjectDTO)
def get_project(project: ProjectDep) -> Project:
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project: ProjectDep, session: SyncSessionDep) -> None:
    session.delete(project)
    session.commit()
