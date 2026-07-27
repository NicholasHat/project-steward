"""Direction/drift view (PROJECTSPECS.md §3.4/§13) and its human-confirmation
action. `label_project_direction`/`build_project_direction_snapshot` treat a
`confirmed_by_user=True` `DirectionLabel` as inviolate — `patch_direction_label`
sets exactly that flag, so a confirmation or override here survives any later
rerun of the direction stage (see `analysis/direction.py`'s module docstring,
"Human-confirmation checkpoint")."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from truth_engine.analysis.view import current_projection
from truth_engine.api.assemble import clean_name, current_projections, direction_label_dto
from truth_engine.api.deps import (
    ArtifactDep,
    CurrentUser,
    PageParamsDep,
    ProjectDep,
    SyncSessionDep,
)
from truth_engine.api.schemas import (
    DirectionLabelDTO,
    DirectionOverviewDTO,
    DirectionPatchRequest,
    DirectionSnapshotDTO,
    Page,
)
from truth_engine.db.models import (
    Artifact,
    AuditActor,
    DecisionAudit,
    DirectionLabel,
    DirectionSnapshot,
)

router = APIRouter(prefix="/projects/{project_id}/direction", tags=["direction"])


@router.get("", response_model=DirectionOverviewDTO)
def get_direction(
    project: ProjectDep, session: SyncSessionDep, page: PageParamsDep
) -> DirectionOverviewDTO:
    snapshot = session.scalar(
        select(DirectionSnapshot)
        .where(DirectionSnapshot.project_id == project.id)
        .order_by(DirectionSnapshot.computed_at.desc())
        .limit(1)
    )

    query = (
        select(DirectionLabel, Artifact)
        .join(Artifact, Artifact.id == DirectionLabel.artifact_id)
        .where(Artifact.project_id == project.id)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(
        query.order_by(DirectionLabel.created_at.desc()).limit(page.limit).offset(page.offset)
    ).all()
    artifact_ids = [a.id for _, a in rows]
    projections = current_projections(session, artifact_ids)
    items = [direction_label_dto(label, clean_name(a, projections.get(a.id))) for label, a in rows]

    return DirectionOverviewDTO(
        snapshot=DirectionSnapshotDTO.model_validate(snapshot) if snapshot else None,
        labels=Page(items=items, total=total, limit=page.limit, offset=page.offset),
    )


@router.patch("/{artifact_id}", response_model=DirectionLabelDTO)
def patch_direction_label(
    artifact: ArtifactDep,
    body: DirectionPatchRequest,
    user: CurrentUser,
    session: SyncSessionDep,
) -> DirectionLabelDTO:
    """Confirm or override an artifact's `DirectionLabel` (PROJECTSPECS.md
    §3.4's human confirmation checkpoint). Always sets `confirmed_by_user`;
    optionally overrides `label`. Once set, `analysis.direction.
    label_project_direction` never recomputes or overwrites this row again."""
    label = session.scalar(select(DirectionLabel).where(DirectionLabel.artifact_id == artifact.id))
    if label is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no direction label for this artifact yet")

    old = {"label": label.label.value, "confirmed_by_user": label.confirmed_by_user}
    note = ""
    if body.label is not None:
        label.label = body.label
        note = f" Overridden to {body.label.value}."
    label.confirmed_by_user = True

    session.add(
        DecisionAudit(
            decision_type="direction_label",
            target_id=label.id,
            old_value=old,
            new_value={"label": label.label.value, "confirmed_by_user": True},
            actor=AuditActor.user,
            model=None,
            model_version=None,
            rationale=f"Confirmed by {user.email}.{note}",
        )
    )
    session.commit()
    session.refresh(label)

    projection = current_projection(session, artifact.id)
    return direction_label_dto(label, clean_name(artifact, projection))
