"""Pydantic response/request DTOs for the data + human-action API.

Never dump ORM objects directly (see `api/routers/*.py`) — every route maps
explicit ORM rows onto one of these. Confidence/source/rationale fields are
carried through deliberately, not trimmed for brevity: PROJECTSPECS.md §3.2/
§3.4 both ask the dashboard to *show* uncertainty rather than hide it behind a
single flattened value.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from truth_engine.db.models import (
    AssignmentSource,
    DateSignalSource,
    DirectionLabelValue,
    EntityType,
    GapStatus,
    GapType,
    PipelineRunStatus,
    ProcessingState,
    Stage,
)


class Page[T](BaseModel):
    """Shared offset-pagination envelope for every list whose size scales with
    corpus size (artifacts, timeline, direction labels, gaps, phase
    assignments) — see `api/deps.PageParams`."""

    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Projects                                                                    #
# --------------------------------------------------------------------------- #
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    root_path: str = Field(min_length=1)


class ProjectDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    root_path: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Shared sub-DTOs (view projection / phase assignment / direction label /    #
# resolved date) — reused by both the artifact browser and the detail view   #
# --------------------------------------------------------------------------- #
class ViewProjectionDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggested_name: str | None
    suggested_category: str | None
    virtual_path: str | None
    version: int
    source: AssignmentSource
    created_at: datetime


class PhaseAssignmentDTO(BaseModel):
    id: uuid.UUID
    artifact_id: uuid.UUID
    phase_id: uuid.UUID
    phase_name: str
    confidence: float
    rationale: str | None
    source: AssignmentSource


class DirectionLabelDTO(BaseModel):
    id: uuid.UUID
    artifact_id: uuid.UUID
    artifact_name: str
    label: DirectionLabelValue
    rationale: str | None
    signal_a_score: float | None
    signal_b_score: float | None
    confidence: float
    confirmed_by_user: bool
    created_at: datetime


class ResolvedDateDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_date: datetime
    signal_source: DateSignalSource
    confidence: float
    evidence_text: str | None
    extractor: str
    is_chosen: bool


# --------------------------------------------------------------------------- #
# Artifacts                                                                   #
# --------------------------------------------------------------------------- #
class ArtifactSummaryDTO(BaseModel):
    id: uuid.UUID
    original_filename: str
    file_type: str
    processing_state: ProcessingState
    chosen_date: datetime | None
    chosen_date_confidence: float | None
    view: ViewProjectionDTO | None
    direction: DirectionLabelDTO | None
    phases: list[PhaseAssignmentDTO]


class EntityMentionDTO(BaseModel):
    entity_id: uuid.UUID
    type: EntityType
    value: str
    context: str | None
    confidence: float
    extractor: str


class RelationshipEdgeDTO(BaseModel):
    id: uuid.UUID
    direction: str  # "incoming" | "outgoing", relative to this artifact
    other_artifact_id: uuid.UUID
    other_artifact_name: str
    type: str
    confidence: float
    evidence: str | None


class ArtifactDetailDTO(ArtifactSummaryDTO):
    current_path: str
    # Why an artifact wasn't fully processed, if applicable: the parse-stage
    # note for an unsupported format, or the error message for a failed stage.
    processing_note: str | None = None
    size_bytes: int
    fs_created: datetime | None
    fs_modified: datetime | None
    ingested_at: datetime
    parser_name: str | None
    parser_version: str | None
    structure: dict | None
    embedded_metadata: dict | None
    entities: list[EntityMentionDTO]
    resolved_dates: list[ResolvedDateDTO]
    edges: list[RelationshipEdgeDTO]


class ArtifactNamePutRequest(BaseModel):
    suggested_name: str = Field(min_length=1, max_length=255)


# --------------------------------------------------------------------------- #
# Timeline                                                                    #
# --------------------------------------------------------------------------- #
class TimelineEventDTO(BaseModel):
    id: uuid.UUID
    artifact_id: uuid.UUID | None
    artifact_name: str | None
    event_date: datetime
    description: str
    confidence: float
    source: str


# --------------------------------------------------------------------------- #
# Direction                                                                   #
# --------------------------------------------------------------------------- #
class DirectionSnapshotDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inferred_direction_summary: str
    computed_at: datetime


class DirectionOverviewDTO(BaseModel):
    snapshot: DirectionSnapshotDTO | None
    labels: Page[DirectionLabelDTO]


class DirectionPatchRequest(BaseModel):
    label: DirectionLabelValue | None = None


# --------------------------------------------------------------------------- #
# Gaps                                                                        #
# --------------------------------------------------------------------------- #
class GapDTO(BaseModel):
    id: uuid.UUID
    type: GapType
    phase_id: uuid.UUID | None
    phase_name: str | None
    description: str
    evidence: str | None
    confidence: float
    status: GapStatus


class GapPatchRequest(BaseModel):
    status: GapStatus


# --------------------------------------------------------------------------- #
# Phases / domain                                                             #
# --------------------------------------------------------------------------- #
class DomainClassificationDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    domain: str
    confidence: float
    model: str | None
    confirmed_by_user: bool
    created_at: datetime


class PhaseTemplateCoverageDTO(BaseModel):
    id: uuid.UUID
    phase_name: str
    ordinal: int
    description: str | None
    artifact_count: int


class PhasesOverviewDTO(BaseModel):
    domain_classification: DomainClassificationDTO | None
    template_domain: str | None
    phases: list[PhaseTemplateCoverageDTO]
    assignments: Page[PhaseAssignmentDTO]


class DomainPatchRequest(BaseModel):
    domain: str | None = None


# --------------------------------------------------------------------------- #
# Pipeline: upload / run / status                                            #
# --------------------------------------------------------------------------- #
class UploadedFileDTO(BaseModel):
    filename: str
    size_bytes: int


class UploadResponse(BaseModel):
    root_path: str
    files: list[UploadedFileDTO]


class RunResponse(BaseModel):
    run_id: uuid.UUID
    status: PipelineRunStatus


class StageProgressDTO(BaseModel):
    stage: Stage
    total: int
    done: int
    error: int
    pending: int
    # benign, settled (e.g. unsupported format) — counts toward complete, not failed
    skipped: int = 0


class ProjectStatusDTO(BaseModel):
    state: PipelineRunStatus
    run_id: uuid.UUID | None
    current_stage: Stage | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    stages: list[StageProgressDTO]


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
class ReportDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    content: str
    sections: dict
    generated_at: datetime


class ReportResponse(BaseModel):
    report: ReportDTO | None
