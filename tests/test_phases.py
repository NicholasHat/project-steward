from __future__ import annotations

import uuid
from collections.abc import Iterable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis import phases as phases_module
from truth_engine.analysis.phases import (
    _candidate_domains,
    _corpus_fingerprint,
    _fingerprint_hash,
    assign_project_phases,
    classify_project_domain,
    run_project_phases,
)
from truth_engine.config import get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    AssignmentSource,
    DecisionAudit,
    DomainClassification,
    PhaseAssignment,
    PhaseTemplate,
    ProcessingState,
    Project,
    Stage,
    StageState,
    StageStatus,
    User,
)
from truth_engine.reasoning.providers import LLMProvider


# --------------------------------------------------------------------------- #
# Fake provider: canned, ordered responses -- no network call, hermetic.     #
# --------------------------------------------------------------------------- #
class FakeLLMProvider(LLMProvider):
    """Pops one canned response per `complete()` call, in the order they're
    scheduled. Popping past the end of `responses` raises -- deliberately,
    so a test asserting "no LLM call happens" (confirmed_by_user, or a
    second no-op run) fails loudly if that invariant regresses."""

    def __init__(self, responses: list[str], model: str = "fake-llm") -> None:
        self.model = model
        self._responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append(prompt)
        return self._responses.pop(0)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session, project: Project, *, content_hash: str = "h", filename: str = "notes.txt"
) -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path=f"/tmp/{filename}",
        original_filename=filename,
        file_type="txt",
        size_bytes=1,
        processing_state=ProcessingState.extracted,
    )
    db_session.add(artifact)
    db_session.flush()
    return artifact


def _make_content(
    db_session: Session, artifact: Artifact, *, raw_text: str, structure: dict | None = None
) -> ArtifactContent:
    content = ArtifactContent(
        artifact_id=artifact.id,
        raw_text=raw_text,
        structure=structure,
        embedded_metadata=None,
        parser_name="test",
        parser_version="1",
    )
    db_session.add(content)
    db_session.flush()
    return content


def _phase_assignments(db_session: Session, artifact: Artifact) -> list[PhaseAssignment]:
    return list(
        db_session.scalars(
            select(PhaseAssignment).where(PhaseAssignment.artifact_id == artifact.id)
        ).all()
    )


def _phase_template(db_session: Session, phase_id: uuid.UUID) -> PhaseTemplate:
    row = db_session.get(PhaseTemplate, phase_id)
    assert row is not None
    return row


def _domain_classification(db_session: Session, project: Project) -> DomainClassification | None:
    return db_session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project.id)
    )


def _audits(
    db_session: Session, decision_type: str, target_ids: Iterable[uuid.UUID]
) -> list[DecisionAudit]:
    ids = list(target_ids)
    if not ids:
        return []
    return list(
        db_session.scalars(
            select(DecisionAudit).where(
                DecisionAudit.decision_type == decision_type, DecisionAudit.target_id.in_(ids)
            )
        ).all()
    )


RESEARCH_TEXT = (
    "Lab notebook: hypothesis testing for the new catalyst. We ran the "
    "experiment three times and collected spectra for analysis."
)
DOMAIN_RESPONSE_RESEARCH = (
    '{"domain": "research", "confidence": 0.9, "rationale": "reads like lab notes"}'
)


# --------------------------------------------------------------------------- #
# Candidate domains: derived from phase_templates, not hardcoded             #
# --------------------------------------------------------------------------- #
def test_candidate_domains_are_derived_from_phase_templates_data(db_session: Session) -> None:
    db_session.add(
        PhaseTemplate(domain="space_exploration", phase_name="Launch", ordinal=0, description="")
    )
    db_session.flush()

    domains = _candidate_domains(db_session)

    assert "space_exploration" in domains  # a new vertical needs only a seeded row, no code change
    assert "generic" not in domains  # generic is the fallback, never a classification target


# --------------------------------------------------------------------------- #
# Corpus fingerprint must be stable when artifacts tie on ingested_at        #
# --------------------------------------------------------------------------- #
def test_corpus_fingerprint_is_stable_when_artifacts_share_ingested_at(
    db_session: Session, project: Project
) -> None:
    """A whole-folder ingest commits every artifact in one transaction, and
    Postgres's `now()` is constant within a transaction -- so artifacts tying
    on `ingested_at` is the common case for a fresh project, not an edge
    case. Without a deterministic tiebreak, repeated reads of the same,
    unchanged corpus could return rows in different orders and produce a
    different fingerprint hash each time, which would make
    `classify_project_domain`'s "recompute only if the corpus changed" check
    fire spuriously. Commit many artifacts together (so they're genuinely
    tied, not just close in time) and assert the hash is identical across
    repeated reads.
    """
    for i in range(20):
        artifact = _make_artifact(db_session, project, content_hash=f"h{i}", filename=f"f{i}.txt")
        _make_content(db_session, artifact, raw_text=f"note number {i} about research")
    db_session.commit()

    timestamps = {
        a.ingested_at
        for a in db_session.scalars(select(Artifact).where(Artifact.project_id == project.id)).all()
    }
    assert len(timestamps) == 1  # sanity check: the tie this test targets actually occurred

    settings = get_settings()
    hashes = {
        _fingerprint_hash(_corpus_fingerprint(db_session, project.id, settings)) for _ in range(10)
    }
    assert len(hashes) == 1


# --------------------------------------------------------------------------- #
# Domain classification: persisted with model/confidence + audit            #
# --------------------------------------------------------------------------- #
def test_classify_project_domain_persists_domain_confidence_and_model(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    provider = FakeLLMProvider([DOMAIN_RESPONSE_RESEARCH])
    classification = classify_project_domain(db_session, project.id, provider=provider)
    db_session.commit()

    assert classification.domain == "research"
    assert classification.confidence == pytest.approx(0.9)
    assert classification.model == "fake-llm"
    assert classification.confirmed_by_user is False
    assert classification.corpus_fingerprint_hash  # non-empty
    assert len(provider.calls) == 1

    persisted = _domain_classification(db_session, project)
    assert persisted is not None
    assert persisted.id == classification.id

    audits = _audits(db_session, "domain_classification", [classification.id])
    assert len(audits) == 1
    assert audits[0].actor.value == "system"
    assert audits[0].model == "fake-llm"
    assert audits[0].rationale


# --------------------------------------------------------------------------- #
# Low confidence -> generic template for phase assignment, honest record     #
# --------------------------------------------------------------------------- #
def test_low_confidence_domain_falls_back_to_generic_template(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    low_confidence_response = (
        '{"domain": "research", "confidence": 0.2, "rationale": "ambiguous signals"}'
    )
    domain_provider = FakeLLMProvider([low_confidence_response])
    classification = classify_project_domain(db_session, project.id, provider=domain_provider)
    db_session.commit()

    # Honest record: the classifier's actual guess and confidence are kept,
    # not silently coerced to "generic" -- that's the *template* decision.
    assert classification.domain == "research"
    assert classification.confidence == pytest.approx(0.2)

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Middle", "confidence": 0.5, "rationale": "generic fallback"}]}]}'
    )
    phase_provider = FakeLLMProvider([phase_response])
    result = assign_project_phases(db_session, project.id, classification, provider=phase_provider)
    db_session.commit()

    assert result.assigned == 1
    assignments = _phase_assignments(db_session, artifact)
    assert len(assignments) == 1
    phase = _phase_template(db_session, assignments[0].phase_id)
    assert phase.domain == "generic"  # never forced onto the low-confidence "research" template


# --------------------------------------------------------------------------- #
# An artifact may map to more than one phase                                 #
# --------------------------------------------------------------------------- #
def test_artifact_can_be_assigned_to_multiple_phases(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-this-test",
    )
    db_session.add(classification)
    db_session.flush()

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": ['
        '{"phase": "Execution", "confidence": 0.8, "rationale": "ran experiments"}, '
        '{"phase": "Analysis", "confidence": 0.6, "rationale": "discusses spectra"}'
        "]}]}"
    )
    provider = FakeLLMProvider([phase_response])
    result = assign_project_phases(db_session, project.id, classification, provider=provider)
    db_session.commit()

    assert result.assigned == 1
    assignments = _phase_assignments(db_session, artifact)
    assert len(assignments) == 2
    phase_names = {_phase_template(db_session, a.phase_id).phase_name for a in assignments}
    assert phase_names == {"Execution", "Analysis"}
    assert all(a.source == AssignmentSource.auto for a in assignments)

    audits = _audits(db_session, "phase_assignment", [a.id for a in assignments])
    assert len(audits) == 2
    assert {a.model for a in audits} == {"fake-llm"}


# --------------------------------------------------------------------------- #
# confirmed_by_user is never overwritten or re-queried                      #
# --------------------------------------------------------------------------- #
def test_confirmed_domain_is_not_overwritten_on_rerun(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    confirmed = DomainClassification(
        project_id=project.id,
        domain="engineering",
        confidence=0.99,
        model="human",
        confirmed_by_user=True,
        corpus_fingerprint_hash="stale-hash-does-not-match-current-corpus",
    )
    db_session.add(confirmed)
    db_session.commit()

    provider = FakeLLMProvider([])  # any call would pop from an empty list and raise
    result = classify_project_domain(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.id == confirmed.id
    assert result.domain == "engineering"
    assert result.confidence == pytest.approx(0.99)
    assert result.confirmed_by_user is True
    assert provider.calls == []

    audits = _audits(db_session, "domain_classification", [confirmed.id])
    assert audits == []  # no new decision was made


# --------------------------------------------------------------------------- #
# Malformed LLM JSON degrades gracefully, never crashes                     #
# --------------------------------------------------------------------------- #
def test_malformed_domain_json_degrades_to_generic(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    provider = FakeLLMProvider(["Sorry, I can't classify this without more context."])
    classification = classify_project_domain(db_session, project.id, provider=provider)
    db_session.commit()

    assert classification.domain == "generic"
    assert classification.confidence == 0.0
    audits = _audits(db_session, "domain_classification", [classification.id])
    assert len(audits) == 1
    assert audits[0].rationale


def test_malformed_phase_batch_json_degrades_gracefully(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-this-test",
    )
    db_session.add(classification)
    db_session.flush()

    provider = FakeLLMProvider(["```\nthe model rambled instead of returning JSON\n```"])
    result = assign_project_phases(db_session, project.id, classification, provider=provider)
    db_session.commit()

    assert result.errors == []
    assert result.assigned == 1  # processed, just with nothing usable to persist
    assert _phase_assignments(db_session, artifact) == []

    state = db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == artifact.id, StageState.stage == Stage.phases
        )
    )
    assert state is not None
    assert state.status == StageStatus.done


# --------------------------------------------------------------------------- #
# Human-sourced assignments are never clobbered                             #
# --------------------------------------------------------------------------- #
def test_human_sourced_phase_assignment_is_not_deleted_on_rebuild(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    template = db_session.scalars(
        select(PhaseTemplate).where(PhaseTemplate.domain == "research")
    ).all()
    human_row = PhaseAssignment(
        artifact_id=artifact.id,
        phase_id=template[0].id,
        confidence=1.0,
        rationale="human override",
        source=AssignmentSource.human,
    )
    db_session.add(human_row)
    db_session.commit()

    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-this-test",
    )
    db_session.add(classification)
    db_session.flush()

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Analysis", "confidence": 0.7, "rationale": "auto"}]}]}'
    )
    provider = FakeLLMProvider([phase_response])
    assign_project_phases(db_session, project.id, classification, provider=provider)
    db_session.commit()

    assignments = _phase_assignments(db_session, artifact)
    assert any(a.id == human_row.id and a.source == AssignmentSource.human for a in assignments)
    assert any(a.source == AssignmentSource.auto for a in assignments)


# --------------------------------------------------------------------------- #
# StageState idempotency: no-op on unchanged input, rebuild on domain change #
# --------------------------------------------------------------------------- #
def test_assign_project_phases_unchanged_domain_is_a_noop(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-this-test",
    )
    db_session.add(classification)
    db_session.flush()

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Execution", "confidence": 0.8, "rationale": "r"}]}]}'
    )
    provider = FakeLLMProvider([phase_response])
    first = assign_project_phases(db_session, project.id, classification, provider=provider)
    db_session.commit()
    assert first.assigned == 1
    ids_before = {a.id for a in _phase_assignments(db_session, artifact)}

    second = assign_project_phases(db_session, project.id, classification, provider=provider)
    db_session.commit()
    assert second.assigned == 0
    assert second.skipped == 1
    assert len(provider.calls) == 1  # second run made no additional LLM call
    ids_after = {a.id for a in _phase_assignments(db_session, artifact)}
    assert ids_before == ids_after  # rows untouched, not deleted + reinserted


def test_assign_project_phases_rebuilds_when_domain_changes(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    research = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="hash-a",
    )
    db_session.add(research)
    db_session.flush()

    response_a = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Execution", "confidence": 0.8, "rationale": "r"}]}]}'
    )
    provider_a = FakeLLMProvider([response_a])
    assign_project_phases(db_session, project.id, research, provider=provider_a)
    db_session.commit()
    before = _phase_assignments(db_session, artifact)
    assert len(before) == 1
    assert _phase_template(db_session, before[0].phase_id).domain == "research"

    engineering = DomainClassification(
        project_id=project.id,
        domain="engineering",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="hash-b",
    )
    response_b = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Execution", "confidence": 0.7, "rationale": "r2"}]}]}'
    )
    provider_b = FakeLLMProvider([response_b])
    result = assign_project_phases(db_session, project.id, engineering, provider=provider_b)
    db_session.commit()

    assert result.assigned == 1  # rebuilt, not skipped -- the selected domain changed
    after = _phase_assignments(db_session, artifact)
    assert len(after) == 1
    assert _phase_template(db_session, after[0].phase_id).domain == "engineering"
    assert {a.id for a in after}.isdisjoint({a.id for a in before})


def test_run_project_phases_second_full_run_is_a_noop(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=RESEARCH_TEXT)
    db_session.commit()

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Execution", "confidence": 0.8, "rationale": "r"}]}]}'
    )
    provider = FakeLLMProvider([DOMAIN_RESPONSE_RESEARCH, phase_response])
    first = run_project_phases(db_session, project.id, provider=provider)
    db_session.commit()
    assert first.domain == "research"
    assert first.assigned == 1

    # No further canned responses queued -- if either stage made an LLM call,
    # `FakeLLMProvider.complete` would raise popping from an empty list.
    second = run_project_phases(db_session, project.id, provider=provider)
    db_session.commit()
    assert second.assigned == 0
    assert second.skipped == 1


# --------------------------------------------------------------------------- #
# Error isolation: one bad artifact doesn't abort the batch                  #
# --------------------------------------------------------------------------- #
def test_assign_project_phases_records_error_without_aborting_batch(
    db_session: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _make_artifact(db_session, project, content_hash="good", filename="good.txt")
    bad = _make_artifact(db_session, project, content_hash="bad", filename="bad.txt")
    _make_content(db_session, good, raw_text=RESEARCH_TEXT)
    _make_content(db_session, bad, raw_text=RESEARCH_TEXT)
    db_session.commit()

    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.9,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-this-test",
    )
    db_session.add(classification)
    db_session.flush()

    one_phase = '{"phase": "Execution", "confidence": 0.8, "rationale": "r"}'
    phase_response = (
        '{"assignments": ['
        f'{{"artifact_index": 0, "phases": [{one_phase}]}}, '
        f'{{"artifact_index": 1, "phases": [{one_phase}]}}'
        "]}"
    )
    provider = FakeLLMProvider([phase_response])

    original = phases_module._replace_phase_assignments

    def flaky(session, artifact, candidates, phases_by_name, provider):  # noqa: ANN001
        if artifact.id == bad.id:
            raise RuntimeError("boom")
        return original(session, artifact, candidates, phases_by_name, provider)

    monkeypatch.setattr(phases_module, "_replace_phase_assignments", flaky)

    result = assign_project_phases(db_session, project.id, classification, provider=provider)

    assert result.assigned == 1
    assert len(result.errors) == 1
    failed_artifact = result.errors[0][0]
    assert failed_artifact.id == bad.id

    state = db_session.scalar(
        select(StageState).where(StageState.artifact_id == bad.id, StageState.stage == Stage.phases)
    )
    assert state is not None
    assert state.status == StageStatus.error
    assert state.error


# --------------------------------------------------------------------------- #
# Multi-tenant scoping                                                       #
# --------------------------------------------------------------------------- #
def test_run_project_phases_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    in_scope = _make_artifact(db_session, project, content_hash="in")
    out_of_scope = _make_artifact(db_session, other_project, content_hash="out")
    _make_content(db_session, in_scope, raw_text=RESEARCH_TEXT)
    _make_content(db_session, out_of_scope, raw_text=RESEARCH_TEXT)
    db_session.commit()

    phase_response = (
        '{"assignments": [{"artifact_index": 0, "phases": '
        '[{"phase": "Execution", "confidence": 0.8, "rationale": "r"}]}]}'
    )
    provider = FakeLLMProvider([DOMAIN_RESPONSE_RESEARCH, phase_response])
    run_project_phases(db_session, project.id, provider=provider)
    db_session.commit()

    assert _domain_classification(db_session, project) is not None
    assert _domain_classification(db_session, other_project) is None
    assert _phase_assignments(db_session, in_scope) != []
    assert _phase_assignments(db_session, out_of_scope) == []
