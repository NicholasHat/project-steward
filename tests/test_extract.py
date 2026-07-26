from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    DateSignalSource,
    DecisionAudit,
    Entity,
    EntityMention,
    EntityType,
    ProcessingState,
    Project,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.extract.dates import (
    DateCandidate,
    best_anchor,
    choose,
    content_candidates,
    doc_meta_candidates,
    filesystem_candidates,
)
from truth_engine.extract.entities import extract_entities, normalize_value
from truth_engine.extract.nlp import get_nlp
from truth_engine.extract.service import extract_artifact, extract_project


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session,
    project: Project,
    *,
    content_hash: str = "h",
    fs_created: datetime | None = None,
    fs_modified: datetime | None = None,
) -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path="/tmp/x.txt",
        original_filename="x.txt",
        file_type="txt",
        size_bytes=1,
        fs_created=fs_created,
        fs_modified=fs_modified,
        processing_state=ProcessingState.parsed,
    )
    db_session.add(artifact)
    db_session.flush()
    return artifact


def _make_content(
    db_session: Session,
    artifact: Artifact,
    *,
    raw_text: str | None = None,
    embedded_metadata: dict | None = None,
    structure: dict | None = None,
) -> ArtifactContent:
    content = ArtifactContent(
        artifact_id=artifact.id,
        raw_text=raw_text,
        structure=structure,
        embedded_metadata=embedded_metadata,
        parser_name="test",
        parser_version="1",
    )
    db_session.add(content)
    db_session.flush()
    return content


def _resolved_dates(db_session: Session, artifact: Artifact) -> list[ResolvedDate]:
    return list(
        db_session.scalars(
            select(ResolvedDate).where(ResolvedDate.artifact_id == artifact.id)
        ).all()
    )


def _mentions(db_session: Session, artifact: Artifact) -> list[EntityMention]:
    return list(
        db_session.scalars(
            select(EntityMention).where(EntityMention.artifact_id == artifact.id)
        ).all()
    )


def _audits(
    db_session: Session, decision_type: str, target_ids: Iterable[uuid.UUID]
) -> list[DecisionAudit]:
    """Audit rows of `decision_type` scoped to `target_ids` — the rows this test
    created. DecisionAudit is a project-agnostic ledger keyed by `target_id`, so
    scoping by the caller's own targets keeps assertions immune to pre-existing
    rows in the shared dev DB (mirrors the codebase's owner-scoping invariant)."""
    return list(
        db_session.scalars(
            select(DecisionAudit).where(
                DecisionAudit.decision_type == decision_type,
                DecisionAudit.target_id.in_(list(target_ids)),
            )
        ).all()
    )


UTC_2024_03_20 = datetime(2024, 3, 20, 12, 0, 0, tzinfo=UTC)
UTC_2024_01_01 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Signal 3 — filesystem                                                       #
# --------------------------------------------------------------------------- #
def test_filesystem_candidates_created_outranks_modified_confidence() -> None:
    candidates = filesystem_candidates(UTC_2024_01_01, UTC_2024_03_20)
    assert len(candidates) == 2
    created = next(c for c in candidates if c.evidence_text == "Artifact.fs_created")
    modified = next(c for c in candidates if c.evidence_text == "Artifact.fs_modified")
    assert all(c.signal_source == DateSignalSource.filesystem for c in candidates)
    assert created.confidence > modified.confidence


def test_filesystem_candidates_handle_missing_timestamps() -> None:
    assert filesystem_candidates(None, None) == []
    assert len(filesystem_candidates(UTC_2024_01_01, None)) == 1


# --------------------------------------------------------------------------- #
# Signal 2 — document metadata                                                #
# --------------------------------------------------------------------------- #
def test_doc_meta_candidates_parses_iso_from_office_core_properties() -> None:
    candidates = doc_meta_candidates({"created": "2023-03-12T10:15:00+00:00", "author": "Alice"})
    assert len(candidates) == 1
    assert candidates[0].signal_source == DateSignalSource.doc_meta
    assert candidates[0].candidate_date == datetime(2023, 3, 12, 10, 15, tzinfo=UTC)


def test_doc_meta_candidates_naive_iso_assumed_utc_not_host_local() -> None:
    # python-pptx core properties are sometimes naive (no tzinfo baked in).
    candidates = doc_meta_candidates({"created": "2013-01-27T09:14:16"})
    assert candidates[0].candidate_date == datetime(2013, 1, 27, 9, 14, 16, tzinfo=UTC)


def test_doc_meta_candidates_parses_pdf_date_format() -> None:
    candidates = doc_meta_candidates({"CreationDate": "D:20230312101500-05'00'"})
    assert candidates[0].candidate_date == datetime(2023, 3, 12, 10, 15, 0, tzinfo=UTC)


def test_doc_meta_candidates_parses_exif_datetime() -> None:
    candidates = doc_meta_candidates({"exif": {"EXIF DateTimeOriginal": "2022:06:01 08:30:00"}})
    assert len(candidates) == 1
    assert candidates[0].candidate_date == datetime(2022, 6, 1, 8, 30, tzinfo=UTC)


def test_doc_meta_created_outranks_modified_confidence() -> None:
    candidates = doc_meta_candidates(
        {"created": "2023-01-01T00:00:00+00:00", "modified": "2023-06-01T00:00:00+00:00"}
    )
    created = next(c for c in candidates if "created" in (c.evidence_text or ""))
    modified = next(c for c in candidates if "modified" in (c.evidence_text or ""))
    assert created.confidence > modified.confidence


def test_doc_meta_candidates_empty_for_no_metadata() -> None:
    assert doc_meta_candidates(None) == []
    assert doc_meta_candidates({"author": "Alice"}) == []  # no date-ish keys


# --------------------------------------------------------------------------- #
# Signal 1 — content (explicit + relative, spaCy + dateparser)                #
# --------------------------------------------------------------------------- #
def test_content_candidates_explicit_date_has_high_confidence() -> None:
    text = "The team met on March 12, 2024 to review the budget."
    doc = get_nlp()(text)
    candidates = content_candidates(text, doc, anchor=None)
    assert len(candidates) == 1
    assert candidates[0].candidate_date.date() == datetime(2024, 3, 12).date()
    assert candidates[0].confidence >= 0.8
    assert candidates[0].anchor_date is None  # explicit dates don't need anchoring


def test_content_candidates_relative_date_anchored_and_lower_confidence() -> None:
    text = "Results from last week's run were promising."
    doc = get_nlp()(text)
    anchor = UTC_2024_03_20
    candidates = content_candidates(text, doc, anchor=anchor)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.anchor_date == anchor
    # "last week" relative to March 20 2024 -> March 13 2024
    assert candidate.candidate_date.date() == datetime(2024, 3, 13).date()
    assert candidate.confidence < 0.8  # relative dates are less trusted than explicit ones


def test_content_candidates_empty_without_text() -> None:
    assert content_candidates(None, None, anchor=None) == []


# --------------------------------------------------------------------------- #
# Anchor selection                                                            #
# --------------------------------------------------------------------------- #
def test_best_anchor_prefers_doc_meta_over_filesystem() -> None:
    fs = filesystem_candidates(UTC_2024_01_01, UTC_2024_01_01)
    meta = doc_meta_candidates({"created": "2023-06-15T00:00:00+00:00"})
    anchor = best_anchor(fs + meta)
    assert anchor == datetime(2023, 6, 15, tzinfo=UTC)


def test_best_anchor_falls_back_to_filesystem() -> None:
    fs = filesystem_candidates(UTC_2024_01_01, None)
    anchor = best_anchor(fs)
    assert anchor == UTC_2024_01_01


def test_best_anchor_none_when_no_candidates() -> None:
    assert best_anchor([]) is None


# --------------------------------------------------------------------------- #
# The chosen-date rule: signal rank first, confidence breaks ties within it   #
# --------------------------------------------------------------------------- #
def test_choose_prefers_content_signal_even_over_lower_confidence() -> None:
    low_confidence_content = DateCandidate(
        candidate_date=UTC_2024_03_20,
        signal_source=DateSignalSource.content,
        confidence=0.3,
        evidence_text="ev",
        extractor="test",
    )
    high_confidence_fs = DateCandidate(
        candidate_date=UTC_2024_01_01,
        signal_source=DateSignalSource.filesystem,
        confidence=0.99,
        evidence_text="ev",
        extractor="test",
    )
    chosen = choose([low_confidence_content, high_confidence_fs])
    assert chosen is low_confidence_content  # signal rank beats raw confidence


def test_choose_breaks_ties_within_a_signal_by_confidence() -> None:
    weaker = DateCandidate(UTC_2024_01_01, DateSignalSource.doc_meta, 0.55, "ev", "test")
    stronger = DateCandidate(UTC_2024_03_20, DateSignalSource.doc_meta, 0.70, "ev", "test")
    assert choose([weaker, stronger]) is stronger


def test_choose_none_for_no_candidates() -> None:
    assert choose([]) is None


# --------------------------------------------------------------------------- #
# Entity extraction: normalization/dedup, spans, rule-vs-spaCy precedence     #
# --------------------------------------------------------------------------- #
def test_extract_entities_person_and_group_via_spacy() -> None:
    text = "Alice Smith and the Synthesis Group discussed the results."
    doc = get_nlp()(text)
    mentions = extract_entities(text, doc)
    types = {(m.entity_type, m.value) for m in mentions}
    assert (EntityType.person, "Alice Smith") in types
    assert any(t == EntityType.group for t, _ in types)


def test_extract_entities_rule_beats_spacy_mislabel() -> None:
    # en_core_web_sm mislabels "Hypothesis 2" as ORG; the rule should win the span.
    text = "We still need to run the control experiment for Hypothesis 2."
    doc = get_nlp()(text)
    mentions = extract_entities(text, doc)
    hyp = [m for m in mentions if m.entity_type == EntityType.hypothesis]
    assert len(hyp) == 1
    assert hyp[0].value == "Hypothesis 2"
    assert hyp[0].extractor == "rule"
    assert not any(m.entity_type == EntityType.group and "Hypothesis" in m.value for m in mentions)


def test_extract_entities_cost_and_tool_and_citation() -> None:
    text = (
        "The budget was $1,200 for reagents. We used Python and MATLAB for analysis. "
        "This builds on (Smith et al., 2022)."
    )
    doc = get_nlp()(text)
    mentions = extract_entities(text, doc)
    by_type = {t: [m for m in mentions if m.entity_type == t] for t in EntityType}
    assert any(m.value == "$1,200" for m in by_type[EntityType.cost])
    assert {m.normalized_value for m in by_type[EntityType.tool]} >= {"python", "matlab"}
    assert any("Smith et al" in m.value for m in by_type[EntityType.citation])


def test_extract_entities_span_matches_source_text() -> None:
    text = "Alice Smith led the project."
    doc = get_nlp()(text)
    mentions = extract_entities(text, doc)
    person = next(m for m in mentions if m.entity_type == EntityType.person)
    start, end = (int(x) for x in person.span.split(":"))
    assert text[start:end] == person.value


def test_normalize_value_cost_canonicalizes_notation() -> None:
    assert normalize_value(EntityType.cost, "$1,200") == "1200.00"
    assert normalize_value(EntityType.cost, "$5k") == "5000.00"
    assert normalize_value(EntityType.cost, "500 USD") == "500.00"


def test_normalize_value_collapses_whitespace_and_case() -> None:
    assert normalize_value(EntityType.person, "  Alice   Smith ") == "alice smith"


def test_extract_entities_empty_without_text() -> None:
    assert extract_entities(None, None) == []


# --------------------------------------------------------------------------- #
# Service: end-to-end persistence, chosen-date selection, audit, idempotency  #
# --------------------------------------------------------------------------- #
def test_extract_artifact_persists_all_three_signals_and_chooses_one(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session,
        project,
        fs_created=UTC_2024_01_01,
        fs_modified=UTC_2024_01_01,
    )
    _make_content(
        db_session,
        artifact,
        raw_text="As discussed in our March 12, 2024 meeting, budget is $1,200.",
        embedded_metadata={"created": "2024-02-01T00:00:00+00:00"},
    )
    db_session.commit()

    changed = extract_artifact(db_session, artifact)
    db_session.commit()

    assert changed is True
    resolved = _resolved_dates(db_session, artifact)
    sources = {r.signal_source for r in resolved}
    assert sources == {
        DateSignalSource.content,
        DateSignalSource.doc_meta,
        DateSignalSource.filesystem,
    }

    chosen = [r for r in resolved if r.is_chosen]
    assert len(chosen) == 1
    assert chosen[0].signal_source == DateSignalSource.content  # highest-trust signal wins
    assert chosen[0].candidate_date.date() == datetime(2024, 3, 12).date()

    assert artifact.processing_state == ProcessingState.extracted


def test_extract_artifact_anchors_relative_date_against_doc_meta(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session, project, fs_created=UTC_2024_01_01, fs_modified=UTC_2024_01_01
    )
    _make_content(
        db_session,
        artifact,
        raw_text="Results from last week's run confirmed the hypothesis.",
        embedded_metadata={"created": "2024-03-20T12:00:00+00:00"},
    )
    db_session.commit()

    extract_artifact(db_session, artifact)
    db_session.commit()

    chosen = next(r for r in _resolved_dates(db_session, artifact) if r.is_chosen)
    assert chosen.signal_source == DateSignalSource.content
    # anchored to the doc-meta created date (2024-03-20) -> "last week" = 2024-03-13
    assert chosen.candidate_date.date() == datetime(2024, 3, 13).date()

    audit = _audits(
        db_session, "resolved_date", [r.id for r in _resolved_dates(db_session, artifact)]
    )
    assert len(audit) == 1
    assert "anchored" in audit[0].rationale


def test_extract_artifact_writes_decision_audit_for_chosen_date_and_each_mention(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session, project, fs_created=UTC_2024_01_01, fs_modified=UTC_2024_01_01
    )
    _make_content(db_session, artifact, raw_text="Alice Smith led Experiment 7.")
    db_session.commit()

    extract_artifact(db_session, artifact)
    db_session.commit()

    mentions = _mentions(db_session, artifact)
    date_audits = _audits(
        db_session, "resolved_date", [r.id for r in _resolved_dates(db_session, artifact)]
    )
    mention_audits = _audits(db_session, "entity_mention", [m.id for m in mentions])
    assert len(date_audits) == 1
    assert date_audits[0].actor.value == "system"
    assert len(mention_audits) == len(mentions) > 0
    assert {a.target_id for a in mention_audits} == {m.id for m in mentions}


def test_extract_artifact_normalizes_and_dedupes_entities_across_artifacts(
    db_session: Session, project: Project
) -> None:
    a1 = _make_artifact(db_session, project, content_hash="h1", fs_created=UTC_2024_01_01)
    a2 = _make_artifact(db_session, project, content_hash="h2", fs_created=UTC_2024_01_01)
    _make_content(db_session, a1, raw_text="Alice Smith reviewed the data.")
    _make_content(db_session, a2, raw_text="Alice   Smith signed off on the report.")
    db_session.commit()

    extract_artifact(db_session, a1)
    extract_artifact(db_session, a2)
    db_session.commit()

    people = db_session.scalars(
        select(Entity).where(
            Entity.type == EntityType.person, Entity.project_id == project.id
        )
    ).all()
    assert len(people) == 1  # "Alice Smith" normalizes the same regardless of whitespace
    mentions = db_session.scalars(
        select(EntityMention).where(EntityMention.entity_id == people[0].id)
    ).all()
    assert {m.artifact_id for m in mentions} == {a1.id, a2.id}


def test_extract_artifact_no_content_falls_back_to_filesystem_only(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session, project, fs_created=UTC_2024_01_01, fs_modified=UTC_2024_01_01
    )
    db_session.commit()  # no ArtifactContent row at all (e.g. parse failed)

    extract_artifact(db_session, artifact)
    db_session.commit()

    resolved = _resolved_dates(db_session, artifact)
    assert resolved
    assert all(r.signal_source == DateSignalSource.filesystem for r in resolved)
    assert sum(r.is_chosen for r in resolved) == 1
    assert _mentions(db_session, artifact) == []


# --------------------------------------------------------------------------- #
# StageState idempotency                                                      #
# --------------------------------------------------------------------------- #
def test_reextract_unchanged_artifact_is_a_noop(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(db_session, project, fs_created=UTC_2024_01_01)
    _make_content(db_session, artifact, raw_text="Plain note, no dates or entities of note.")
    db_session.commit()

    assert extract_artifact(db_session, artifact) is True
    db_session.commit()
    resolved_before = {r.id for r in _resolved_dates(db_session, artifact)}

    assert extract_artifact(db_session, artifact) is False  # StageState.input_hash unchanged
    db_session.commit()
    resolved_after = {r.id for r in _resolved_dates(db_session, artifact)}
    assert resolved_before == resolved_after  # rows untouched, not deleted+reinserted


def test_extract_project_skips_already_extracted_artifacts(
    db_session: Session, project: Project
) -> None:
    a1 = _make_artifact(db_session, project, content_hash="h0", fs_created=UTC_2024_01_01)
    a2 = _make_artifact(db_session, project, content_hash="h1", fs_created=UTC_2024_01_01)
    _make_content(db_session, a1, raw_text="Note one.")
    _make_content(db_session, a2, raw_text="Note two.")
    db_session.commit()

    first = extract_project(db_session, project.id)
    assert first.extracted == 2
    assert first.skipped == 0

    second = extract_project(db_session, project.id)
    assert second.extracted == 0
    assert second.skipped == 2

    for artifact in (a1, a2):
        state = db_session.scalar(
            select(StageState).where(
                StageState.artifact_id == artifact.id, StageState.stage == Stage.extract
            )
        )
        assert state is not None
        assert state.status == StageStatus.done
