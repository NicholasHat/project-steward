"""Stage 2 orchestration: dispatch each artifact to its format handler and
persist `ArtifactContent` + `StructuredTable`, gated by `StageState`.

Idempotency: `StageState(artifact_id, stage=parse).input_hash` is the
artifact's `content_hash`. Since content_hash only changes when the file's
bytes change (a new Artifact row entirely, per the ingest identity rule),
"unchanged input" here really means "already parsed this exact artifact" —
re-running parse over an untouched project is a pure no-op.

A failure on one artifact (encrypted PDF, malformed Office file, unsupported
format) is recorded on that artifact's StageState and does not abort the
batch — this mirrors ingest's per-file error isolation.

No `DecisionAudit` rows: parsing deterministically re-expresses a file's own
bytes as structured data: it infers nothing. Audit begins at extract (dates/
entities/phases are inferred facts; raw text/tables are not).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    ProcessingState,
    Stage,
    StageState,
    StageStatus,
    StructuredTable,
)
from truth_engine.parse import handlers  # noqa: F401  (import registers all handlers)
from truth_engine.parse.registry import UnsupportedFormatError, get_handler


@dataclass
class ParseResult:
    parsed: int = 0
    skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def parse_artifact(session: Session, artifact: Artifact) -> bool:
    """Parse one artifact if its content changed since the last successful
    parse. Returns True if (re)parsed, False if skipped as already current.
    Raises on handler failure — callers isolate per-artifact errors.
    """
    state = _stage_state(session, artifact.id, Stage.parse)
    if state and state.status == StageStatus.done and state.input_hash == artifact.content_hash:
        return False

    doc = get_handler(artifact.file_type)(Path(artifact.current_path))

    content = session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    if content is None:
        content = ArtifactContent(artifact_id=artifact.id)
        session.add(content)
    content.raw_text = doc.raw_text
    content.structure = doc.structure
    content.embedded_metadata = doc.embedded_metadata
    content.parser_name = doc.parser_name
    content.parser_version = doc.parser_version

    # Parse is re-runnable: replace this artifact's tables wholesale rather
    # than diffing, since the source of truth is always the file itself.
    session.execute(delete(StructuredTable).where(StructuredTable.artifact_id == artifact.id))
    for table in doc.tables:
        session.add(
            StructuredTable(
                artifact_id=artifact.id,
                source=table.source,
                table_schema=table.table_schema,
                rows=table.rows,
            )
        )

    if state is None:
        state = StageState(
            artifact_id=artifact.id, stage=Stage.parse, input_hash=artifact.content_hash
        )
        session.add(state)
    else:
        state.input_hash = artifact.content_hash
    state.status = StageStatus.done
    state.error = None

    artifact.processing_state = ProcessingState.parsed
    return True


def parse_project(session: Session, project_id: uuid.UUID) -> ParseResult:
    """Parse every artifact in a project, skipping ones already up to date."""
    result = ParseResult()
    artifacts = session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()

    for artifact in artifacts:
        try:
            changed = parse_artifact(session, artifact)
        except UnsupportedFormatError:
            _record_error(session, artifact, f"unsupported format: {artifact.file_type!r}")
            result.errors.append((artifact, f"unsupported format: {artifact.file_type!r}"))
            continue
        except Exception as exc:  # noqa: BLE001 - isolate one bad file from the batch
            _record_error(session, artifact, str(exc))
            result.errors.append((artifact, str(exc)))
            continue

        session.commit()
        result.parsed += 1 if changed else 0
        result.skipped += 0 if changed else 1

    return result


def _record_error(session: Session, artifact: Artifact, message: str) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.parse)
    if state is None:
        state = StageState(
            artifact_id=artifact.id, stage=Stage.parse, input_hash=artifact.content_hash
        )
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    artifact.processing_state = ProcessingState.error
    session.commit()
