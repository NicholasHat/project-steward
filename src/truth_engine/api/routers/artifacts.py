"""The artifact browser + detail view (PROJECTSPECS.md §2 step 13's first
surface) and the human name-override action (§3.6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import func, select

from truth_engine.analysis.view import apply_human_name_override, current_projection
from truth_engine.api.assemble import (
    chosen_dates,
    clean_name,
    current_projections,
    direction_label_dto,
    direction_labels,
    phase_assignment_dto,
    phase_assignments_by_artifact,
)
from truth_engine.api.deps import (
    ArtifactDep,
    CurrentUser,
    PageParamsDep,
    ProjectDep,
    SyncSessionDep,
)
from truth_engine.api.schemas import (
    ArtifactDetailDTO,
    ArtifactNamePutRequest,
    ArtifactSummaryDTO,
    EntityMentionDTO,
    Page,
    PhaseAssignmentDTO,
    RelationshipEdgeDTO,
    ResolvedDateDTO,
    ViewProjectionDTO,
)
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    DirectionLabel,
    DirectionLabelValue,
    Entity,
    EntityMention,
    PhaseAssignment,
    PhaseTemplate,
    RelationshipEdge,
    ResolvedDate,
    ViewProjection,
)

router = APIRouter(prefix="/projects/{project_id}/artifacts", tags=["artifacts"])


def _summarize(
    artifact: Artifact,
    dates: dict[uuid.UUID, tuple[datetime, float]],
    projections: dict[uuid.UUID, ViewProjection],
    labels: dict[uuid.UUID, DirectionLabel],
    phases: dict[uuid.UUID, list[PhaseAssignmentDTO]],
) -> ArtifactSummaryDTO:
    projection = projections.get(artifact.id)
    label = labels.get(artifact.id)
    date = dates.get(artifact.id)
    return ArtifactSummaryDTO(
        id=artifact.id,
        original_filename=artifact.original_filename,
        file_type=artifact.file_type,
        processing_state=artifact.processing_state,
        chosen_date=date[0] if date else None,
        chosen_date_confidence=date[1] if date else None,
        view=ViewProjectionDTO.model_validate(projection) if projection else None,
        direction=direction_label_dto(label, clean_name(artifact, projection)) if label else None,
        phases=phases.get(artifact.id, []),
    )


# --------------------------------------------------------------------------- #
# Browser                                                                     #
# --------------------------------------------------------------------------- #
@router.get("", response_model=Page[ArtifactSummaryDTO])
def list_artifacts(
    project: ProjectDep,
    session: SyncSessionDep,
    page: PageParamsDep,
    phase_id: uuid.UUID | None = None,
    direction: DirectionLabelValue | None = None,
) -> Page[ArtifactSummaryDTO]:
    query = select(Artifact).where(Artifact.project_id == project.id)
    if phase_id is not None:
        query = query.where(
            Artifact.id.in_(
                select(PhaseAssignment.artifact_id).where(PhaseAssignment.phase_id == phase_id)
            )
        )
    if direction is not None:
        query = query.where(
            Artifact.id.in_(
                select(DirectionLabel.artifact_id).where(DirectionLabel.label == direction)
            )
        )

    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(Artifact.ingested_at, Artifact.id).limit(page.limit).offset(page.offset)
        ).all()
    )
    ids = [a.id for a in rows]
    dates = chosen_dates(session, ids)
    projections = current_projections(session, ids)
    labels = direction_labels(session, ids)
    phases = phase_assignments_by_artifact(session, ids)
    items = [_summarize(a, dates, projections, labels, phases) for a in rows]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


# --------------------------------------------------------------------------- #
# Detail                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/{artifact_id}", response_model=ArtifactDetailDTO)
def get_artifact(artifact: ArtifactDep, session: SyncSessionDep) -> ArtifactDetailDTO:
    content = session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    projection = current_projection(session, artifact.id)
    label = session.scalar(select(DirectionLabel).where(DirectionLabel.artifact_id == artifact.id))

    phase_rows = session.execute(
        select(PhaseAssignment, PhaseTemplate.phase_name)
        .join(PhaseTemplate, PhaseTemplate.id == PhaseAssignment.phase_id)
        .where(PhaseAssignment.artifact_id == artifact.id)
    ).all()
    phases = [phase_assignment_dto(pa, name) for pa, name in phase_rows]

    date_row = session.execute(
        select(ResolvedDate.candidate_date, ResolvedDate.confidence).where(
            ResolvedDate.artifact_id == artifact.id, ResolvedDate.is_chosen.is_(True)
        )
    ).first()

    entity_rows = session.execute(
        select(EntityMention, Entity.type, Entity.value)
        .join(Entity, Entity.id == EntityMention.entity_id)
        .where(EntityMention.artifact_id == artifact.id)
    ).all()
    entities = [
        EntityMentionDTO(
            entity_id=em.entity_id,
            type=etype,
            value=value,
            context=em.context,
            confidence=em.confidence,
            extractor=em.extractor,
        )
        for em, etype, value in entity_rows
    ]

    resolved_dates = [
        ResolvedDateDTO.model_validate(rd)
        for rd in session.scalars(
            select(ResolvedDate)
            .where(ResolvedDate.artifact_id == artifact.id)
            .order_by(ResolvedDate.confidence.desc())
        ).all()
    ]

    outgoing = session.execute(
        select(RelationshipEdge, Artifact)
        .join(Artifact, Artifact.id == RelationshipEdge.dst_artifact_id)
        .where(RelationshipEdge.src_artifact_id == artifact.id)
    ).all()
    incoming = session.execute(
        select(RelationshipEdge, Artifact)
        .join(Artifact, Artifact.id == RelationshipEdge.src_artifact_id)
        .where(RelationshipEdge.dst_artifact_id == artifact.id)
    ).all()
    other_ids = [a.id for _, a in outgoing] + [a.id for _, a in incoming]
    other_projections = current_projections(session, other_ids)
    edges = [
        RelationshipEdgeDTO(
            id=edge.id,
            direction="outgoing",
            other_artifact_id=other.id,
            other_artifact_name=clean_name(other, other_projections.get(other.id)),
            type=edge.type.value,
            confidence=edge.confidence,
            evidence=edge.evidence,
        )
        for edge, other in outgoing
    ] + [
        RelationshipEdgeDTO(
            id=edge.id,
            direction="incoming",
            other_artifact_id=other.id,
            other_artifact_name=clean_name(other, other_projections.get(other.id)),
            type=edge.type.value,
            confidence=edge.confidence,
            evidence=edge.evidence,
        )
        for edge, other in incoming
    ]

    return ArtifactDetailDTO(
        id=artifact.id,
        original_filename=artifact.original_filename,
        file_type=artifact.file_type,
        processing_state=artifact.processing_state,
        chosen_date=date_row[0] if date_row else None,
        chosen_date_confidence=date_row[1] if date_row else None,
        view=ViewProjectionDTO.model_validate(projection) if projection else None,
        direction=direction_label_dto(label, clean_name(artifact, projection)) if label else None,
        phases=phases,
        current_path=artifact.current_path,
        size_bytes=artifact.size_bytes,
        fs_created=artifact.fs_created,
        fs_modified=artifact.fs_modified,
        ingested_at=artifact.ingested_at,
        parser_name=content.parser_name if content else None,
        parser_version=content.parser_version if content else None,
        structure=content.structure if content else None,
        embedded_metadata=content.embedded_metadata if content else None,
        entities=entities,
        resolved_dates=resolved_dates,
        edges=edges,
    )


# --------------------------------------------------------------------------- #
# Human action: name override (PROJECTSPECS.md §3.6)                         #
# --------------------------------------------------------------------------- #
@router.put("/{artifact_id}/name", response_model=ViewProjectionDTO)
def override_artifact_name(
    artifact: ArtifactDep,
    body: ArtifactNamePutRequest,
    user: CurrentUser,
    session: SyncSessionDep,
) -> ViewProjection:
    """Creates a new `source=human` `ViewProjection` version superseding the
    prior current one (`analysis.view.apply_human_name_override`) — never
    mutates the original file, never clobbered by a later `run_project_view`
    call. Writes its own `DecisionAudit(actor=user)` internally."""
    projection = apply_human_name_override(
        session, artifact, body.suggested_name, rationale=f"Renamed by {user.email}."
    )
    session.commit()
    session.refresh(projection)
    return projection
