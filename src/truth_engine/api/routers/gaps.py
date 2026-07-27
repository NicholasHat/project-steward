"""Gap review (PROJECTSPECS.md §3.5/§13) and its human-review action.
`analysis/gaps.py`'s `_reconcile_gaps` only ever regenerates a gap whose
`status == open`; `patch_gap` moving it to `confirmed`/`dismissed`/`resolved`
is exactly what makes a later `run_project_gaps` call leave it untouched (see
that module's "Human review -- never clobbered")."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from truth_engine.api.deps import CurrentUser, GapDep, PageParamsDep, ProjectDep, SyncSessionDep
from truth_engine.api.schemas import GapDTO, GapPatchRequest, Page
from truth_engine.db.models import AuditActor, DecisionAudit, Gap, GapStatus, GapType, PhaseTemplate

router = APIRouter(prefix="/projects/{project_id}/gaps", tags=["gaps"])


def _gap_dto(gap: Gap, phase_name: str | None) -> GapDTO:
    return GapDTO(
        id=gap.id,
        type=gap.type,
        phase_id=gap.phase_id,
        phase_name=phase_name,
        description=gap.description,
        evidence=gap.evidence,
        confidence=gap.confidence,
        status=gap.status,
    )


@router.get("", response_model=Page[GapDTO])
def list_gaps(
    project: ProjectDep,
    session: SyncSessionDep,
    page: PageParamsDep,
    status_filter: GapStatus | None = Query(None, alias="status"),
    type_filter: GapType | None = Query(None, alias="type"),
) -> Page[GapDTO]:
    query = select(Gap).where(Gap.project_id == project.id)
    if status_filter is not None:
        query = query.where(Gap.status == status_filter)
    if type_filter is not None:
        query = query.where(Gap.type == type_filter)

    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(Gap.confidence.desc(), Gap.id).limit(page.limit).offset(page.offset)
        ).all()
    )
    phase_ids = [g.phase_id for g in rows if g.phase_id is not None]
    phase_names: dict[uuid.UUID, str] = (
        dict(
            session.execute(
                select(PhaseTemplate.id, PhaseTemplate.phase_name).where(
                    PhaseTemplate.id.in_(phase_ids)
                )
            ).all()
        )
        if phase_ids
        else {}
    )
    items = [_gap_dto(g, phase_names.get(g.phase_id)) for g in rows]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.patch("/{gap_id}", response_model=GapDTO)
def patch_gap(
    gap: GapDep, body: GapPatchRequest, user: CurrentUser, session: SyncSessionDep
) -> GapDTO:
    old = {"status": gap.status.value}
    gap.status = body.status
    session.add(
        DecisionAudit(
            decision_type="gap_status",
            target_id=gap.id,
            old_value=old,
            new_value={"status": gap.status.value},
            actor=AuditActor.user,
            model=None,
            model_version=None,
            rationale=f"Status set to {body.status.value} by {user.email}.",
        )
    )
    session.commit()
    session.refresh(gap)

    phase_name = None
    if gap.phase_id is not None:
        phase_name = session.scalar(
            select(PhaseTemplate.phase_name).where(PhaseTemplate.id == gap.phase_id)
        )
    return _gap_dto(gap, phase_name)
