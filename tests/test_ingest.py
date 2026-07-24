from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.db.models import Artifact, Project, Stage, StageState, StageStatus
from truth_engine.ingest.service import ingest_folder


def _artifacts(session: Session, project: Project) -> list[Artifact]:
    return list(session.scalars(select(Artifact).where(Artifact.project_id == project.id)).all())


def test_ingest_creates_artifacts_with_stable_ids_and_hashes(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    (tmp_path / "a.txt").write_text("hello world")
    (tmp_path / "b.txt").write_text("goodbye world")

    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 2
    assert result.updated == 0
    assert result.unchanged == 0
    assert not result.errors

    artifacts = _artifacts(db_session, project)
    assert len(artifacts) == 2
    for artifact in artifacts:
        assert artifact.content_hash  # sha256 hex digest present
        assert artifact.project_id == project.id
        state = db_session.scalar(
            select(StageState).where(
                StageState.artifact_id == artifact.id, StageState.stage == Stage.ingest
            )
        )
        assert state is not None
        assert state.status == StageStatus.done
        assert state.input_hash == artifact.content_hash


def test_ingest_dedupes_identical_content_across_paths(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    (tmp_path / "original.txt").write_text("same bytes")
    (tmp_path / "copy.txt").write_text("same bytes")

    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 1
    assert result.updated == 1  # second path observed for the same content_hash
    artifacts = _artifacts(db_session, project)
    assert len(artifacts) == 1


def test_reingest_unchanged_folder_is_a_noop(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    (tmp_path / "a.txt").write_text("hello world")
    ingest_folder(db_session, project, tmp_path)
    before = {a.id: (a.content_hash, a.current_path) for a in _artifacts(db_session, project)}

    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1
    after = {a.id: (a.content_hash, a.current_path) for a in _artifacts(db_session, project)}
    assert before == after


def test_moved_file_reingest_matches_on_hash_not_path(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    original = tmp_path / "a.txt"
    original.write_text("stable content")
    ingest_folder(db_session, project, tmp_path)
    [artifact_before] = _artifacts(db_session, project)
    original_id, original_hash = artifact_before.id, artifact_before.content_hash

    moved = tmp_path / "subdir"
    moved.mkdir()
    new_path = moved / "renamed.txt"
    original.rename(new_path)

    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 0
    assert result.updated == 1
    [artifact_after] = _artifacts(db_session, project)
    assert artifact_after.id == original_id  # identity preserved across the move
    assert artifact_after.content_hash == original_hash
    assert artifact_after.current_path == str(new_path)
    assert artifact_after.original_filename == "renamed.txt"


def test_edited_content_becomes_a_new_artifact(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "a.txt"
    path.write_text("version one")
    ingest_folder(db_session, project, tmp_path)

    path.write_text("version two")  # different bytes -> different content_hash
    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 1
    artifacts = _artifacts(db_session, project)
    assert len(artifacts) == 2  # both snapshots persist; identity follows content


def test_ingest_never_mutates_or_moves_originals(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "a.txt"
    original_bytes = b"do not touch"
    path.write_bytes(original_bytes)

    ingest_folder(db_session, project, tmp_path)

    assert path.read_bytes() == original_bytes
    assert path.exists()


def test_ingest_skips_hidden_files(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    (tmp_path / ".hidden").write_text("ignore me")
    real_file = tmp_path / "a.txt"
    real_file.write_text("hello world")

    result = ingest_folder(db_session, project, tmp_path)

    assert result.created == 1
    artifacts = _artifacts(db_session, project)
    assert [a.original_filename for a in artifacts] == ["a.txt"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_ingest_records_unreadable_file_as_error_without_aborting_batch(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    unreadable = tmp_path / "locked.txt"
    unreadable.write_text("secret")
    unreadable.chmod(0o000)
    readable = tmp_path / "a.txt"
    readable.write_text("hello world")

    try:
        result = ingest_folder(db_session, project, tmp_path)
    finally:
        unreadable.chmod(0o644)  # restore so tmp_path cleanup can remove it

    assert result.created == 1  # the readable file still gets ingested
    assert len(result.errors) == 1
    assert result.errors[0][0] == unreadable
    artifacts = _artifacts(db_session, project)
    assert [a.original_filename for a in artifacts] == ["a.txt"]
