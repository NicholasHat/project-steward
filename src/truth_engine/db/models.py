"""SQLAlchemy 2.0 models — the full Truth Engine schema.

Design invariants baked into the schema:
  * Identity is the artifact's UUID + content_hash, never its filename/path.
  * Multi-tenant: everything is reachable to a `users` row via project_id/owner_id;
    queries must be owner-scoped.
  * Auditability: `decision_audit` records every inferred fact and who/what made it.
  * Reversibility: view_projection + versioned decisions; originals are never mutated.
  * Config-driven extensibility: phase templates are data, not code.

Vector dimension is fixed for migration stability; it MUST match
Settings.embedding_dim and the chosen embedding model.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from truth_engine.db.base import Base

# Must match Settings.embedding_dim and the embedding model (BAAI/bge-base = 768).
EMBEDDING_DIM = 768


# --------------------------------------------------------------------------- #
# Enums (stored as VARCHAR + CHECK via native_enum=False for easy migrations)  #
# --------------------------------------------------------------------------- #
class ProcessingState(enum.StrEnum):
    pending = "pending"
    parsed = "parsed"
    extracted = "extracted"
    embedded = "embedded"
    analyzed = "analyzed"
    error = "error"


class DateSignalSource(enum.StrEnum):
    content = "content"  # highest trust
    doc_meta = "doc_meta"  # medium trust
    filesystem = "filesystem"  # lowest trust, cross-check only


class EntityType(enum.StrEnum):
    person = "person"
    group = "group"
    tool = "tool"
    cost = "cost"
    experiment = "experiment"
    hypothesis = "hypothesis"
    citation = "citation"


class EmbeddingLevel(enum.StrEnum):
    chunk = "chunk"
    doc = "doc"


class AssignmentSource(enum.StrEnum):
    auto = "auto"
    human = "human"


class EdgeType(enum.StrEnum):
    references = "references"
    builds_on = "builds_on"
    mentioned_by = "mentioned_by"


class DirectionLabelValue(enum.StrEnum):
    current = "current"
    superseded = "superseded"  # vision drift
    unclear = "unclear"


class GapType(enum.StrEnum):
    structural = "structural"  # phase with no/few artifacts
    promised_unfulfilled = "promised_unfulfilled"  # referenced in text, never delivered


class GapStatus(enum.StrEnum):
    open = "open"
    confirmed = "confirmed"
    dismissed = "dismissed"
    resolved = "resolved"


class AuditActor(enum.StrEnum):
    system = "system"
    user = "user"


class Stage(enum.StrEnum):
    ingest = "ingest"
    parse = "parse"
    extract = "extract"
    embed = "embed"
    timeline = "timeline"
    phases = "phases"
    graph = "graph"  # step 8, Signal B: citation/reference graph (analysis.graph)
    direction = "direction"
    gaps = "gaps"
    view = "view"
    report = "report"


class StageStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- #
# Identity & tenancy                                                          #
# --------------------------------------------------------------------------- #
class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "users"
    created_at: Mapped[datetime] = _ts()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    root_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()

    artifacts: Mapped[list[Artifact]] = relationship(back_populates="project")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "content_hash", name="artifact_project_hash"),
    )

    id: Mapped[uuid.UUID] = _pk()  # stable internal ID
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    current_path: Mapped[str] = mapped_column(Text)  # mutable presentation, not identity
    original_filename: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(Integer)
    fs_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fs_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _ts()
    processing_state: Mapped[ProcessingState] = mapped_column(
        Enum(ProcessingState, native_enum=False, length=16),
        default=ProcessingState.pending,
    )

    project: Mapped[Project] = relationship(back_populates="artifacts")
    content: Mapped[ArtifactContent | None] = relationship(
        back_populates="artifact", uselist=False
    )


# --------------------------------------------------------------------------- #
# Parsed content                                                              #
# --------------------------------------------------------------------------- #
class ArtifactContent(Base):
    __tablename__ = "artifact_content"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), unique=True, index=True
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    structure: Mapped[dict | None] = mapped_column(JSONB)  # headings/slide titles/sheet names
    embedded_metadata: Mapped[dict | None] = mapped_column(JSONB)  # EXIF / Office / PDF props
    parser_name: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32))

    artifact: Mapped[Artifact] = relationship(back_populates="content")


class StructuredTable(Base):
    """Spreadsheets/tables kept queryable — not flattened into prose."""

    __tablename__ = "structured_tables"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(255))  # sheet / tab name
    table_schema: Mapped[dict | None] = mapped_column(JSONB)
    rows: Mapped[list | None] = mapped_column(JSONB)


# --------------------------------------------------------------------------- #
# Entities & dates (multi-signal, confidence-scored)                          #
# --------------------------------------------------------------------------- #
class Entity(Base):
    """Canonical entity, scoped to a project — "Alice" in one project is a
    distinct entity from "Alice" in another (owner isolation + correct
    cross-project separation for the relationship graph)."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "type", "normalized_value", name="entity_project_type_value"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[EntityType] = mapped_column(Enum(EntityType, native_enum=False, length=16))
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)


class EntityMention(Base):
    __tablename__ = "entity_mentions"

    id: Mapped[uuid.UUID] = _pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    span: Mapped[str | None] = mapped_column(String(64))  # char offsets, e.g. "120:135"
    context: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    extractor: Mapped[str] = mapped_column(String(64))


class ResolvedDate(Base):
    """Multiple candidate dates per artifact; exactly one is_chosen."""

    __tablename__ = "resolved_dates"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    candidate_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signal_source: Mapped[DateSignalSource] = mapped_column(
        Enum(DateSignalSource, native_enum=False, length=16)
    )
    confidence: Mapped[float] = mapped_column(Float)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    extractor: Mapped[str] = mapped_column(String(64))
    is_chosen: Mapped[bool] = mapped_column(default=False)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE")
    )
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))


# --------------------------------------------------------------------------- #
# Embeddings                                                                  #
# --------------------------------------------------------------------------- #
class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer)


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE")
    )
    level: Mapped[EmbeddingLevel] = mapped_column(
        Enum(EmbeddingLevel, native_enum=False, length=8)
    )
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model: Mapped[str] = mapped_column(String(128))
    model_version: Mapped[str] = mapped_column(String(64))


# --------------------------------------------------------------------------- #
# Domain / phases (config-driven)                                             #
# --------------------------------------------------------------------------- #
class PhaseTemplate(Base):
    """Seed data, not code — new domains are added as rows, not classes."""

    __tablename__ = "phase_templates"
    __table_args__ = (
        UniqueConstraint("domain", "ordinal", name="phase_template_domain_ordinal"),
    )

    id: Mapped[uuid.UUID] = _pk()
    domain: Mapped[str] = mapped_column(String(64), index=True)
    phase_name: Mapped[str] = mapped_column(String(128))
    ordinal: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)


class DomainClassification(Base):
    __tablename__ = "domain_classification"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(String(128))
    confirmed_by_user: Mapped[bool] = mapped_column(default=False)
    # Hash of the corpus-level fingerprint (filenames/headings/snippets/top
    # entities) the classification was computed from — this table has one
    # row per project (no per-artifact StageState to key off of), so the
    # idempotency check ("recompute if absent or the corpus changed, but
    # never clobber confirmed_by_user") lives directly on the row instead.
    corpus_fingerprint_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class PhaseAssignment(Base):
    """An artifact may map to multiple phases."""

    __tablename__ = "phase_assignments"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    phase_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("phase_templates.id", ondelete="CASCADE")
    )
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    source: Mapped[AssignmentSource] = mapped_column(
        Enum(AssignmentSource, native_enum=False, length=8), default=AssignmentSource.auto
    )


# --------------------------------------------------------------------------- #
# Direction, graph, gaps                                                       #
# --------------------------------------------------------------------------- #
class RelationshipEdge(Base):
    __tablename__ = "relationship_edges"

    id: Mapped[uuid.UUID] = _pk()
    src_artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    dst_artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[EdgeType] = mapped_column(Enum(EdgeType, native_enum=False, length=16))
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)


class DirectionSnapshot(Base):
    """Append-only history, one row per full recompute — like `DecisionAudit`,
    ordered by `computed_at` rather than updated in place; the latest row for
    a project is simply the one with the max `computed_at`. Unlike
    `DomainClassification` (single row per project, update-in-place), a
    project can accumulate several snapshots over its life, which is useful
    signal in its own right ("what did we believe the direction was, and
    when did that change")."""

    __tablename__ = "direction_snapshots"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    inferred_direction_summary: Mapped[str] = mapped_column(Text)
    # Hash of the corpus fingerprint (doc embeddings + graph edges + chosen
    # dates) this snapshot was computed from — mirrors
    # `DomainClassification.corpus_fingerprint_hash`'s role: recompute (a new
    # row) only if absent or the corpus changed since the latest snapshot,
    # otherwise reuse it untouched with no LLM call.
    corpus_fingerprint_hash: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _ts()


class DirectionLabel(Base):
    """One row per artifact (`artifact_id` unique) — like `DomainClassification`,
    not like `PhaseAssignment`'s many-per-artifact: an artifact has exactly one
    current direction verdict at a time, confirmable and never silently
    replaced once confirmed."""

    __tablename__ = "direction_labels"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), unique=True, index=True
    )
    label: Mapped[DirectionLabelValue] = mapped_column(
        Enum(DirectionLabelValue, native_enum=False, length=16)
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    signal_a_score: Mapped[float | None] = mapped_column(Float)  # cluster drift
    signal_b_score: Mapped[float | None] = mapped_column(Float)  # citation graph
    confidence: Mapped[float] = mapped_column(Float)
    confirmed_by_user: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = _ts()


class Gap(Base):
    __tablename__ = "gaps"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[GapType] = mapped_column(Enum(GapType, native_enum=False, length=24))
    phase_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("phase_templates.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[GapStatus] = mapped_column(
        Enum(GapStatus, native_enum=False, length=16), default=GapStatus.open
    )


# --------------------------------------------------------------------------- #
# Presentation, reporting, audit (the reversibility backbone)                 #
# --------------------------------------------------------------------------- #
class ViewProjection(Base):
    """Suggested names/structure as a projection over raw files. Never mutates
    the originals; every suggestion is versioned and reversible."""

    __tablename__ = "view_projection"

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    suggested_name: Mapped[str | None] = mapped_column(Text)
    suggested_category: Mapped[str | None] = mapped_column(String(128))
    virtual_path: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("view_projection.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts()


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = _ts()
    is_current: Mapped[bool] = mapped_column(default=True)


class DecisionAudit(Base):
    """Every inferred fact (date/entity/phase/label/gap/rename) and who/what made
    it — the backbone for inspect / override / rollback."""

    __tablename__ = "decision_audit"

    id: Mapped[uuid.UUID] = _pk()
    decision_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    actor: Mapped[AuditActor] = mapped_column(Enum(AuditActor, native_enum=False, length=8))
    model: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class StageState(Base):
    """Idempotent incremental processing: skip a stage when input_hash is unchanged."""

    __tablename__ = "stage_state"
    __table_args__ = (
        UniqueConstraint("artifact_id", "stage", name="stage_state_artifact_stage"),
        CheckConstraint("input_hash <> ''", name="input_hash_nonempty"),
    )

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[Stage] = mapped_column(Enum(Stage, native_enum=False, length=16))
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, native_enum=False, length=16), default=StageStatus.pending
    )
    input_hash: Mapped[str] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
