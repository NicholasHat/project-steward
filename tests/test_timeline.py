from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis import timeline as timeline_module
from truth_engine.analysis.timeline import (
    SOURCE_CONTENT_DESCRIBED,
    SOURCE_PLACEMENT_CONTENT,
    SOURCE_PLACEMENT_DOC_META,
    SOURCE_PLACEMENT_FILESYSTEM,
    assemble_artifact_timeline,
    assemble_project_timeline,
)
from truth_engine.db.models import (
    Artifact,
    DateSignalSource,
    ProcessingState,
    Project,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    TimelineEvent,
    User,
)

UTC_2024_03_20 = datetime(2024, 3, 20, 12, 0, 0, tzinfo=UTC)
UTC_2024_03_12 = datetime(2024, 3, 12, 9, 0, 0, tzinfo=UTC)
UTC_2024_01_01 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session,
    project: Project,
    *,
    content_hash: str = "h",
    original_filename: str = "notes.txt",
) -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path=f"/tmp/{original_filename}",
        original_filename=original_filename,
        file_type="txt",
        size_bytes=1,
        processing_state=ProcessingState.extracted,
    )
    db_session.add(artifact)
    db_session.flush()
    return artifact


def _make_resolved_date(
    db_session: Session,
    artifact: Artifact,
    *,
    candidate_date: datetime,
    signal_source: DateSignalSource,
    confidence: float,
    evidence_text: str | None = None,
    is_chosen: bool = False,
    extractor: str = "test",
) -> ResolvedDate:
    row = ResolvedDate(
        artifact_id=artifact.id,
        candidate_date=candidate_date,
        signal_source=signal_source,
        confidence=confidence,
        evidence_text=evidence_text,
        extractor=extractor,
        is_chosen=is_chosen,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _events(db_session: Session, artifact: Artifact) -> list[TimelineEvent]:
    return list(
        db_session.scalars(
            select(TimelineEvent)
            .where(TimelineEvent.artifact_id == artifact.id)
            .order_by(TimelineEvent.event_date)
        ).all()
    )


def _stage_state(db_session: Session, artifact: Artifact) -> StageState | None:
    return db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == artifact.id, StageState.stage == Stage.timeline
        )
    )


# --------------------------------------------------------------------------- #
# Placement event: carries the chosen date's confidence + a signal-derived   #
# source label                                                               #
# --------------------------------------------------------------------------- #
def test_placement_event_at_chosen_date_carries_confidence_and_source(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, original_filename="lab_notes.pdf")
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.35,
        evidence_text="Artifact.fs_created",
        is_chosen=True,
    )
    db_session.commit()

    changed = assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    assert changed is True
    events = _events(db_session, artifact)
    assert len(events) == 1
    event = events[0]
    assert event.project_id == artifact.project_id
    assert event.event_date == UTC_2024_01_01
    assert event.confidence == 0.35
    assert event.source == SOURCE_PLACEMENT_FILESYSTEM
    assert event.description == "lab_notes.pdf"


@pytest.mark.parametrize(
    ("signal_source", "expected_source"),
    [
        (DateSignalSource.content, SOURCE_PLACEMENT_CONTENT),
        (DateSignalSource.doc_meta, SOURCE_PLACEMENT_DOC_META),
        (DateSignalSource.filesystem, SOURCE_PLACEMENT_FILESYSTEM),
    ],
)
def test_placement_source_label_reflects_the_chosen_signal(
    db_session: Session,
    project: Project,
    signal_source: DateSignalSource,
    expected_source: str,
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=signal_source,
        confidence=0.5,
        is_chosen=True,
    )
    db_session.commit()

    assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    event = _events(db_session, artifact)[0]
    assert event.source == expected_source


# --------------------------------------------------------------------------- #
# Content-described events: dates the text itself refers to                  #
# --------------------------------------------------------------------------- #
def test_content_described_event_surfaced_from_non_chosen_content_date(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    # Artifact is placed via filesystem (low trust)...
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.35,
        is_chosen=True,
    )
    # ...but the text separately references a meeting date.
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="as discussed in our March 12, 2024 meeting",
        is_chosen=False,
    )
    db_session.commit()

    assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    events = _events(db_session, artifact)
    assert len(events) == 2
    placement = next(e for e in events if e.source == SOURCE_PLACEMENT_FILESYSTEM)
    described = next(e for e in events if e.source == SOURCE_CONTENT_DESCRIBED)
    assert placement.confidence == 0.35
    assert described.confidence == 0.85
    assert described.event_date == UTC_2024_03_12
    assert described.description == "as discussed in our March 12, 2024 meeting"


def test_dedupe_rule_chosen_content_date_is_not_also_emitted_as_content_described(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="as discussed in our March 12, 2024 meeting",
        is_chosen=True,
    )
    db_session.commit()

    assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    events = _events(db_session, artifact)
    # Exactly one event (the placement) — not a second content-described
    # event repeating the same date/confidence under a different label.
    assert len(events) == 1
    assert events[0].source == SOURCE_PLACEMENT_CONTENT
    assert events[0].confidence == 0.85
    # The evidence text isn't silently dropped in favor of the filename —
    # it's folded into the placement description, since this is the one
    # case where "which artifact" and "why this date" are the same fact.
    assert "as discussed in our March 12, 2024 meeting" in events[0].description
    assert artifact.original_filename in events[0].description


def test_multiple_content_dates_only_the_chosen_one_is_deduped(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    chosen = _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="March 12 meeting",
        is_chosen=True,
    )
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_20,
        signal_source=DateSignalSource.content,
        confidence=0.60,
        evidence_text="follow-up planned for next week",
        is_chosen=False,
    )
    db_session.commit()

    assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    events = _events(db_session, artifact)
    assert len(events) == 2
    assert sum(e.source == SOURCE_PLACEMENT_CONTENT for e in events) == 1
    assert sum(e.source == SOURCE_CONTENT_DESCRIBED for e in events) == 1
    described = next(e for e in events if e.source == SOURCE_CONTENT_DESCRIBED)
    assert described.event_date == UTC_2024_03_20
    assert described.event_date != chosen.candidate_date


# --------------------------------------------------------------------------- #
# High vs. low confidence stay distinguishable side by side                  #
# --------------------------------------------------------------------------- #
def test_high_confidence_content_and_low_confidence_filesystem_are_distinguishable(
    db_session: Session, project: Project
) -> None:
    dated_artifact = _make_artifact(
        db_session, project, content_hash="h1", original_filename="a.txt"
    )
    undated_artifact = _make_artifact(
        db_session, project, content_hash="h2", original_filename="b.txt"
    )
    _make_resolved_date(
        db_session,
        dated_artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="March 12, 2024",
        is_chosen=True,
    )
    _make_resolved_date(
        db_session,
        undated_artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.35,
        is_chosen=True,
    )
    db_session.commit()

    result = assemble_project_timeline(db_session, project.id)
    assert result.assembled == 2

    high = _events(db_session, dated_artifact)[0]
    low = _events(db_session, undated_artifact)[0]
    assert high.source == SOURCE_PLACEMENT_CONTENT
    assert low.source == SOURCE_PLACEMENT_FILESYSTEM
    assert high.confidence > low.confidence
    assert high.source != low.source  # UI can tell them apart without comparing floats


# --------------------------------------------------------------------------- #
# Gaps: no chosen date -> skipped but marked done, never crashes             #
# --------------------------------------------------------------------------- #
def test_artifact_with_no_resolved_dates_is_skipped_but_marked_done(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.commit()  # extract hasn't run: zero ResolvedDate rows

    changed = assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    assert changed is True
    assert _events(db_session, artifact) == []
    state = _stage_state(db_session, artifact)
    assert state is not None
    assert state.status == StageStatus.done


def test_artifact_with_candidates_but_none_chosen_skips_placement_defensively(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    # Defensive edge case: candidates exist but none is_chosen (shouldn't
    # happen in practice — filesystem always yields a candidate that
    # `dates.choose` would pick — but must not crash).
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="March 12, 2024",
        is_chosen=False,
    )
    db_session.commit()

    changed = assemble_artifact_timeline(db_session, artifact)
    db_session.commit()

    assert changed is True
    events = _events(db_session, artifact)
    assert all(e.source != SOURCE_PLACEMENT_CONTENT for e in events)
    # The content date is still surfaced as a described event even without a placement.
    assert len(events) == 1
    assert events[0].source == SOURCE_CONTENT_DESCRIBED


# --------------------------------------------------------------------------- #
# StageState idempotency: no-op on unchanged input, rebuild on change        #
# --------------------------------------------------------------------------- #
def test_reassemble_unchanged_artifact_is_a_noop(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(db_session, project)
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.35,
        is_chosen=True,
    )
    db_session.commit()

    assert assemble_artifact_timeline(db_session, artifact) is True
    db_session.commit()
    ids_before = {e.id for e in _events(db_session, artifact)}

    assert assemble_artifact_timeline(db_session, artifact) is False
    db_session.commit()
    ids_after = {e.id for e in _events(db_session, artifact)}
    assert ids_before == ids_after  # rows untouched, not deleted + reinserted


def test_reassemble_rebuilds_when_resolved_dates_change(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.35,
        is_chosen=True,
    )
    db_session.commit()
    assert assemble_artifact_timeline(db_session, artifact) is True
    db_session.commit()
    assert _events(db_session, artifact)[0].source == SOURCE_PLACEMENT_FILESYSTEM

    # Simulate a re-extract that found a content date and promoted it to chosen.
    db_session.execute(
        ResolvedDate.__table__.delete().where(ResolvedDate.artifact_id == artifact.id)
    )
    _make_resolved_date(
        db_session,
        artifact,
        candidate_date=UTC_2024_03_12,
        signal_source=DateSignalSource.content,
        confidence=0.85,
        evidence_text="March 12, 2024",
        is_chosen=True,
    )
    db_session.commit()

    assert assemble_artifact_timeline(db_session, artifact) is True
    db_session.commit()

    events = _events(db_session, artifact)
    assert len(events) == 1
    assert events[0].source == SOURCE_PLACEMENT_CONTENT
    assert events[0].confidence == 0.85


# --------------------------------------------------------------------------- #
# Project-level assembly: scoping, skip-already-done, error isolation        #
# --------------------------------------------------------------------------- #
def test_assemble_project_timeline_skips_already_assembled_artifacts(
    db_session: Session, project: Project
) -> None:
    a1 = _make_artifact(db_session, project, content_hash="h0")
    a2 = _make_artifact(db_session, project, content_hash="h1")
    for artifact in (a1, a2):
        _make_resolved_date(
            db_session,
            artifact,
            candidate_date=UTC_2024_01_01,
            signal_source=DateSignalSource.filesystem,
            confidence=0.35,
            is_chosen=True,
        )
    db_session.commit()

    first = assemble_project_timeline(db_session, project.id)
    assert first.assembled == 2
    assert first.skipped == 0

    second = assemble_project_timeline(db_session, project.id)
    assert second.assembled == 0
    assert second.skipped == 2

    for artifact in (a1, a2):
        state = _stage_state(db_session, artifact)
        assert state is not None
        assert state.status == StageStatus.done


def test_assemble_project_timeline_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    in_scope = _make_artifact(db_session, project, content_hash="in")
    out_of_scope = _make_artifact(db_session, other_project, content_hash="out")
    for artifact in (in_scope, out_of_scope):
        _make_resolved_date(
            db_session,
            artifact,
            candidate_date=UTC_2024_01_01,
            signal_source=DateSignalSource.filesystem,
            confidence=0.35,
            is_chosen=True,
        )
    db_session.commit()

    result = assemble_project_timeline(db_session, project.id)

    assert result.assembled == 1
    assert _events(db_session, in_scope) != []
    assert _events(db_session, out_of_scope) == []  # untouched: different project


def test_assemble_project_timeline_records_error_without_aborting_batch(
    db_session: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _make_artifact(db_session, project, content_hash="good")
    bad = _make_artifact(db_session, project, content_hash="bad")
    for artifact in (good, bad):
        _make_resolved_date(
            db_session,
            artifact,
            candidate_date=UTC_2024_01_01,
            signal_source=DateSignalSource.filesystem,
            confidence=0.35,
            is_chosen=True,
        )
    db_session.commit()

    original = timeline_module._replace_timeline_events

    def flaky(session: Session, artifact: Artifact, resolved: list[ResolvedDate]) -> None:
        if artifact.id == bad.id:
            raise RuntimeError("boom")
        return original(session, artifact, resolved)

    monkeypatch.setattr(timeline_module, "_replace_timeline_events", flaky)

    result = assemble_project_timeline(db_session, project.id)

    assert result.assembled == 1
    assert len(result.errors) == 1
    failed_artifact = result.errors[0][0]
    assert failed_artifact.id == bad.id

    state = _stage_state(db_session, bad)
    assert state is not None
    assert state.status == StageStatus.error
    assert state.error
    # ProcessingState is untouched by timeline in both directions (see
    # timeline.py's module docstring) — StageState(stage=timeline) alone is
    # the source of truth for this stage's status.
    assert bad.processing_state == ProcessingState.extracted
