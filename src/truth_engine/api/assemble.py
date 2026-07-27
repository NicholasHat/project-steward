"""Bulk-fetch + DTO-assembly helpers shared across `api/routers/*.py`.

Unlike `analysis/*.py`'s deliberate per-module duplication of small helpers
(each analysis stage is independently invokable from its own CLI, so
duplication keeps them decoupled), the routers are all part of one API
surface and commonly need the same joins (an artifact's clean name, its
current `ViewProjection`, its `DirectionLabel`) — sharing one implementation
here means a rendering fix (e.g. how a clean name falls back) only has to
happen once.

Every function here takes a *batch* of artifact IDs and returns a dict keyed
by artifact ID — one query per related table across a page of artifacts, not
one query per artifact per table (N+1).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.api.schemas import DirectionLabelDTO, PhaseAssignmentDTO
from truth_engine.db.models import (
    Artifact,
    DirectionLabel,
    PhaseAssignment,
    PhaseTemplate,
    ResolvedDate,
    ViewProjection,
)


def clean_name(artifact: Artifact, projection: ViewProjection | None) -> str:
    """The artifact's human-facing name: its current suggested name when one
    exists, the raw original filename otherwise (view hasn't run yet, or
    genuinely produced nothing) — mirrors `analysis/report.py`'s `_clean_name`."""
    if projection is not None and projection.suggested_name:
        return projection.suggested_name
    return artifact.original_filename


def chosen_dates(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[datetime, float]]:
    if not artifact_ids:
        return {}
    rows = session.execute(
        select(
            ResolvedDate.artifact_id, ResolvedDate.candidate_date, ResolvedDate.confidence
        ).where(ResolvedDate.artifact_id.in_(artifact_ids), ResolvedDate.is_chosen.is_(True))
    ).all()
    return {aid: (date, confidence) for aid, date, confidence in rows}


def current_projections(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ViewProjection]:
    if not artifact_ids:
        return {}
    rows = session.scalars(
        select(ViewProjection).where(
            ViewProjection.artifact_id.in_(artifact_ids), ViewProjection.superseded_by.is_(None)
        )
    ).all()
    return {row.artifact_id: row for row in rows}


def direction_labels(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, DirectionLabel]:
    if not artifact_ids:
        return {}
    rows = session.scalars(
        select(DirectionLabel).where(DirectionLabel.artifact_id.in_(artifact_ids))
    ).all()
    return {row.artifact_id: row for row in rows}


def phase_assignments_by_artifact(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PhaseAssignmentDTO]]:
    if not artifact_ids:
        return {}
    rows = session.execute(
        select(PhaseAssignment, PhaseTemplate.phase_name)
        .join(PhaseTemplate, PhaseTemplate.id == PhaseAssignment.phase_id)
        .where(PhaseAssignment.artifact_id.in_(artifact_ids))
    ).all()
    result: dict[uuid.UUID, list[PhaseAssignmentDTO]] = {}
    for assignment, phase_name in rows:
        result.setdefault(assignment.artifact_id, []).append(
            phase_assignment_dto(assignment, phase_name)
        )
    return result


def phase_assignment_dto(assignment: PhaseAssignment, phase_name: str) -> PhaseAssignmentDTO:
    return PhaseAssignmentDTO(
        id=assignment.id,
        artifact_id=assignment.artifact_id,
        phase_id=assignment.phase_id,
        phase_name=phase_name,
        confidence=assignment.confidence,
        rationale=assignment.rationale,
        source=assignment.source,
    )


def direction_label_dto(label: DirectionLabel, artifact_name: str) -> DirectionLabelDTO:
    return DirectionLabelDTO(
        id=label.id,
        artifact_id=label.artifact_id,
        artifact_name=artifact_name,
        label=label.label,
        rationale=label.rationale,
        signal_a_score=label.signal_a_score,
        signal_b_score=label.signal_b_score,
        confidence=label.confidence,
        confirmed_by_user=label.confirmed_by_user,
        created_at=label.created_at,
    )
