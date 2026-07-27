"""The reconstructed chronology (PROJECTSPECS.md §3.2/§13) — every
`TimelineEvent` with its confidence and source surfaced directly, never
collapsed into a single flat, equally-confident list."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from truth_engine.api.assemble import clean_name, current_projections
from truth_engine.api.deps import PageParamsDep, ProjectDep, SyncSessionDep
from truth_engine.api.schemas import Page, TimelineEventDTO
from truth_engine.db.models import Artifact, TimelineEvent

router = APIRouter(prefix="/projects/{project_id}/timeline", tags=["timeline"])


@router.get("", response_model=Page[TimelineEventDTO])
def list_timeline(
    project: ProjectDep, session: SyncSessionDep, page: PageParamsDep
) -> Page[TimelineEventDTO]:
    query = select(TimelineEvent).where(TimelineEvent.project_id == project.id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(TimelineEvent.event_date, TimelineEvent.id)
            .limit(page.limit)
            .offset(page.offset)
        ).all()
    )

    artifact_ids = [e.artifact_id for e in rows if e.artifact_id is not None]
    artifacts = (
        {a.id: a for a in session.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids)))}
        if artifact_ids
        else {}
    )
    projections = current_projections(session, artifact_ids)

    items = [
        TimelineEventDTO(
            id=e.id,
            artifact_id=e.artifact_id,
            artifact_name=(
                clean_name(artifacts[e.artifact_id], projections.get(e.artifact_id))
                if e.artifact_id in artifacts
                else None
            ),
            event_date=e.event_date,
            description=e.description,
            confidence=e.confidence,
            source=e.source,
        )
        for e in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)
