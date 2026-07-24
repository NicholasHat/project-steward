"""Stage 1 orchestration: discover files under a project root, hash them, and
persist `Artifact` + `StageState` rows.

Dedupe and moved-file handling both fall out of one rule: an artifact's
identity is `(project_id, content_hash)`, never its path. A file seen at a new
path with a hash that already has an `Artifact` row updates that row's
presentation fields (path/filename/fs timestamps) in place rather than
creating a second artifact — this covers both "the file moved" and "this is a
duplicate of a file we already have" with the same code path, which is the
correct behavior for both: identity is content, not location.

Editing a file's *content* is a different case: that produces a new
content_hash, hence a new `Artifact` row with a new UUID. This is intentional
and not a gap — steps 1-2 capture raw snapshots, not file lineage; tracking
"this is an edited version of that artifact" is an inference left to later
pipeline stages (relationship_edges), not identity.

No `DecisionAudit` rows are written here: ingest captures filesystem facts
(hash, size, timestamps) directly, it doesn't infer anything. Audit is for
*inferred* facts (dates/entities/phases/labels/gaps/renames) starting at the
extract stage.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.db.models import (
    Artifact,
    ProcessingState,
    Project,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.ingest.discover import discover_files
from truth_engine.ingest.hashing import hash_file


@dataclass
class IngestResult:
    created: int = 0
    updated: int = 0  # existing artifact whose path/metadata changed (move or duplicate)
    unchanged: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return self.created + self.updated + self.unchanged + len(self.errors)


def ingest_folder(session: Session, project: Project, root: Path) -> IngestResult:
    """Walk `root`, hash every file, and upsert `Artifact` rows for `project`.

    Idempotent: re-running over an unchanged folder writes nothing. Safe to
    call repeatedly as new files land (incremental ingestion).
    """
    result = IngestResult()
    for path in discover_files(root):
        try:
            content_hash = hash_file(path)
            stat = path.stat()
            fs_created, fs_modified = _fs_timestamps(stat)
        except OSError as exc:
            result.errors.append((path, str(exc)))
            continue

        existing = session.scalar(
            select(Artifact).where(
                Artifact.project_id == project.id,
                Artifact.content_hash == content_hash,
            )
        )
        if existing is None:
            artifact = Artifact(
                id=uuid.uuid4(),
                project_id=project.id,
                content_hash=content_hash,
                current_path=str(path),
                original_filename=path.name,
                file_type=_file_type(path),
                size_bytes=stat.st_size,
                fs_created=fs_created,
                fs_modified=fs_modified,
                processing_state=ProcessingState.pending,
            )
            session.add(artifact)
            session.add(
                StageState(
                    artifact_id=artifact.id,
                    stage=Stage.ingest,
                    status=StageStatus.done,
                    input_hash=content_hash,
                )
            )
            result.created += 1
        elif existing.current_path != str(path):
            existing.current_path = str(path)
            existing.original_filename = path.name
            existing.fs_created = fs_created
            existing.fs_modified = fs_modified
            result.updated += 1
        else:
            result.unchanged += 1

    session.commit()
    return result


def _file_type(path: Path) -> str:
    return path.suffix.lstrip(".").lower() or "unknown"


def _fs_timestamps(stat: os.stat_result) -> tuple[datetime, datetime]:
    # st_birthtime (true creation time) is macOS/BSD-only; POSIX has no
    # portable creation time, so fall back to ctime (closest cross-platform
    # proxy) elsewhere. Either way this is Signal 3 (lowest trust) per the
    # date subsystem — a fallback/cross-check, never ground truth.
    created_ts = getattr(stat, "st_birthtime", None) or stat.st_ctime
    return (
        datetime.fromtimestamp(created_ts, tz=UTC),
        datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )
