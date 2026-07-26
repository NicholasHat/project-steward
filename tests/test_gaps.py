from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis import gaps as gaps_module
from truth_engine.analysis.gaps import run_project_gaps
from truth_engine.config import get_settings
from truth_engine.db.models import Artifact as ArtifactModel
from truth_engine.db.models import (
    ArtifactContent,
    AssignmentSource,
    DateSignalSource,
    DecisionAudit,
    DomainClassification,
    Gap,
    GapStatus,
    GapType,
    PhaseAssignment,
    PhaseTemplate,
    ProcessingState,
    Project,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    User,
)
from truth_engine.reasoning.providers import LLMProvider

SETTINGS = get_settings()
_BASE_DATE = datetime(2024, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fake provider: canned, ordered responses -- no network call, hermetic.     #
# Same shape as test_phases.py's / test_direction.py's.                      #
# --------------------------------------------------------------------------- #
class FakeLLMProvider(LLMProvider):
    def __init__(self, responses: list[str], model: str = "fake-llm") -> None:
        self.model = model
        self._responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append(prompt)
        return self._responses.pop(0)


# --------------------------------------------------------------------------- #
# Fixture helpers                                                            #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session,
    project: Project,
    *,
    content_hash: str,
    filename: str,
    raw_text: str | None = None,
    chosen_date: datetime | None = None,
) -> ArtifactModel:
    artifact = ArtifactModel(
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

    if raw_text is not None:
        db_session.add(
            ArtifactContent(
                artifact_id=artifact.id,
                raw_text=raw_text,
                structure=None,
                embedded_metadata=None,
                parser_name="test",
                parser_version="1",
            )
        )
    if chosen_date is not None:
        db_session.add(
            ResolvedDate(
                artifact_id=artifact.id,
                candidate_date=chosen_date,
                signal_source=DateSignalSource.content,
                confidence=0.8,
                evidence_text="test",
                extractor="test",
                is_chosen=True,
            )
        )
    db_session.flush()
    return artifact


def _make_domain_classification(
    db_session: Session, project: Project, *, domain: str = "research", confidence: float = 0.9
) -> DomainClassification:
    classification = DomainClassification(
        project_id=project.id,
        domain=domain,
        confidence=confidence,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-gaps-tests",
    )
    db_session.add(classification)
    db_session.flush()
    return classification


def _research_phases(db_session: Session) -> list[PhaseTemplate]:
    return list(
        db_session.scalars(
            select(PhaseTemplate)
            .where(PhaseTemplate.domain == "research")
            .order_by(PhaseTemplate.ordinal)
        ).all()
    )


def _assign_phase(
    db_session: Session,
    artifact: ArtifactModel,
    phase: PhaseTemplate,
    *,
    source: AssignmentSource = AssignmentSource.auto,
) -> PhaseAssignment:
    row = PhaseAssignment(
        artifact_id=artifact.id, phase_id=phase.id, confidence=0.8, rationale="test", source=source
    )
    db_session.add(row)
    db_session.flush()
    return row


def _gaps(db_session: Session, project: Project) -> list[Gap]:
    return list(db_session.scalars(select(Gap).where(Gap.project_id == project.id)).all())


def _structural_gap_for(db_session: Session, project: Project, phase: PhaseTemplate) -> Gap | None:
    return db_session.scalar(
        select(Gap).where(
            Gap.project_id == project.id, Gap.type == GapType.structural, Gap.phase_id == phase.id
        )
    )


def _promised_gaps(db_session: Session, project: Project) -> list[Gap]:
    return list(
        db_session.scalars(
            select(Gap).where(
                Gap.project_id == project.id, Gap.type == GapType.promised_unfulfilled
            )
        ).all()
    )


def _audits(db_session: Session, decision_type: str, target_id: uuid.UUID) -> list[DecisionAudit]:
    return list(
        db_session.scalars(
            select(DecisionAudit).where(
                DecisionAudit.decision_type == decision_type, DecisionAudit.target_id == target_id
            )
        ).all()
    )


def _judgment_response(
    fulfilled: bool, confidence: float = 0.5, rationale: str = "llm rationale"
) -> str:
    return json.dumps(
        {
            "judgments": [
                {
                    "candidate_index": 0,
                    "fulfilled": fulfilled,
                    "confidence": confidence,
                    "rationale": rationale,
                }
            ]
        }
    )


# --------------------------------------------------------------------------- #
# Structural gaps -- deterministic, higher confidence                       #
# --------------------------------------------------------------------------- #
def test_structural_gap_for_uncovered_phase(db_session: Session, project: Project) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    assert len(phases) == 4  # Conceptualization, Execution, Analysis, Reporting & Dissemination

    covered_phase, uncovered_phase = phases[0], phases[-1]
    a1 = _make_artifact(
        db_session, project, content_hash="h1", filename="a1.txt", chosen_date=_BASE_DATE
    )
    a2 = _make_artifact(
        db_session, project, content_hash="h2", filename="a2.txt", chosen_date=_BASE_DATE
    )
    _assign_phase(db_session, a1, covered_phase)
    _assign_phase(db_session, a2, covered_phase)
    db_session.commit()

    provider = FakeLLMProvider([])  # no promise markers anywhere -- any call would raise
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.structural >= 1
    gap = _structural_gap_for(db_session, project, uncovered_phase)
    assert gap is not None
    assert gap.status == GapStatus.open
    assert gap.confidence == pytest.approx(SETTINGS.gap_structural_confidence_zero)
    assert "0 of 2" in gap.evidence
    assert uncovered_phase.phase_name in gap.description

    # The covered phase gets no gap.
    assert _structural_gap_for(db_session, project, covered_phase) is None

    # Structural gaps are deterministic and self-documenting -- no audit row.
    assert _audits(db_session, "gap_structural", gap.id) == []
    assert provider.calls == []


def test_structural_gap_confidence_bands_zero_vs_few(db_session: Session, project: Project) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    covered_a, covered_b, few_phase, zero_phase = phases[0], phases[1], phases[2], phases[3]

    # Fully cover the first two phases so only `few_phase` (1 artifact, below
    # the default threshold of 2) and `zero_phase` (0 artifacts) are gaps.
    for i in range(2):
        artifact = _make_artifact(
            db_session,
            project,
            content_hash=f"cov{i}",
            filename=f"cov{i}.txt",
            chosen_date=_BASE_DATE,
        )
        _assign_phase(db_session, artifact, covered_a)
        _assign_phase(db_session, artifact, covered_b)
    a_few = _make_artifact(
        db_session, project, content_hash="h1", filename="a1.txt", chosen_date=_BASE_DATE
    )
    _assign_phase(db_session, a_few, few_phase)
    db_session.commit()

    result = run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()
    assert result.structural == 2  # zero_phase and few_phase only

    zero_gap = _structural_gap_for(db_session, project, zero_phase)
    few_gap = _structural_gap_for(db_session, project, few_phase)
    assert zero_gap is not None and few_gap is not None
    assert zero_gap.confidence == pytest.approx(SETTINGS.gap_structural_confidence_zero)
    assert few_gap.confidence == pytest.approx(SETTINGS.gap_structural_confidence_few)
    assert zero_gap.confidence > few_gap.confidence


# --------------------------------------------------------------------------- #
# Promised-but-unfulfilled gaps -- content-level, lower/fuzzier confidence   #
# --------------------------------------------------------------------------- #
def test_promise_with_no_later_artifact_creates_gap_without_llm_call(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="notes.txt",
        raw_text="Lab notebook. We still need to run the control experiment before writing "
        "this up.",
        chosen_date=_BASE_DATE,
    )
    db_session.commit()

    provider = FakeLLMProvider([])  # no later artifact to judge -- must not call the LLM at all
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.promised == 1
    gaps = _promised_gaps(db_session, project)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.status == GapStatus.open
    assert gap.phase_id is None
    assert "control experiment" in gap.evidence
    assert artifact.original_filename in gap.description
    assert gap.confidence == pytest.approx(SETTINGS.gap_promised_confidence_no_later_artifacts)
    assert provider.calls == []

    audits = _audits(db_session, "gap_promised_unfulfilled", gap.id)
    assert len(audits) == 1
    assert audits[0].model == gaps_module._PROMISE_RULESET


def test_promise_unresolved_by_deterministic_check_is_judged_unfulfilled_by_llm(
    db_session: Session, project: Project
) -> None:
    source = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="meeting.txt",
        raw_text="Meeting notes: next step: calibrate the spectrometer before the next run.",
        chosen_date=_BASE_DATE,
    )
    _make_artifact(
        db_session,
        project,
        content_hash="h2",
        filename="unrelated.txt",
        raw_text="Budget review: travel costs came in under estimate this quarter.",
        chosen_date=_BASE_DATE + timedelta(days=10),
    )
    db_session.commit()

    provider = FakeLLMProvider(
        [_judgment_response(fulfilled=False, confidence=0.5, rationale="not mentioned")]
    )
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.promised == 1
    assert len(provider.calls) == 1  # deterministic overlap failed -> escalated to the LLM
    gaps = _promised_gaps(db_session, project)
    assert len(gaps) == 1
    assert gaps[0].confidence == pytest.approx(0.5)
    assert source.original_filename in gaps[0].description

    audits = _audits(db_session, "gap_promised_unfulfilled", gaps[0].id)
    assert len(audits) == 1
    assert audits[0].model == "fake-llm"
    assert audits[0].rationale == "not mentioned"


def test_promise_fulfilled_by_later_artifact_produces_no_gap(
    db_session: Session, project: Project
) -> None:
    _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="plan.txt",
        raw_text="Next step: compute the reaction yield from the latest batch.",
        chosen_date=_BASE_DATE,
    )
    _make_artifact(
        db_session,
        project,
        content_hash="h2",
        filename="results.txt",
        raw_text="We computed the reaction yield from the latest batch: 82 percent.",
        chosen_date=_BASE_DATE + timedelta(days=5),
    )
    db_session.commit()

    # No canned response queued -- the deterministic keyword-overlap check
    # must resolve this as fulfilled without ever reaching the LLM.
    provider = FakeLLMProvider([])
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.promised == 0
    assert _promised_gaps(db_session, project) == []
    assert provider.calls == []


def test_promise_gap_pruned_when_later_fulfillment_appears_writes_audit(
    db_session: Session, project: Project
) -> None:
    """A promised gap that stops being reproduced (the promise is now
    fulfilled) is pruned -- and per the module's auditability decision, that
    disappearance is itself audited, not silent."""
    _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="plan.txt",
        raw_text="Next step: compute the reaction yield from the latest batch.",
        chosen_date=_BASE_DATE,
    )
    db_session.commit()

    run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()
    gaps = _promised_gaps(db_session, project)
    assert len(gaps) == 1
    gap_id = gaps[0].id

    # Now a later, fulfilling artifact appears -- a genuine corpus change.
    _make_artifact(
        db_session,
        project,
        content_hash="h2",
        filename="results.txt",
        raw_text="We computed the reaction yield from the latest batch: 82 percent.",
        chosen_date=_BASE_DATE + timedelta(days=5),
    )
    db_session.commit()

    result = run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()

    assert result.pruned == 1
    assert _promised_gaps(db_session, project) == []

    audits = _audits(db_session, "gap_promised_unfulfilled", gap_id)
    assert len(audits) == 2  # one for creation, one for the prune
    prune_audit = audits[-1]
    assert prune_audit.new_value is None
    assert prune_audit.old_value is not None
    assert prune_audit.model == gaps_module._PROMISE_RULESET
    assert prune_audit.rationale


def test_promise_llm_confirms_fulfillment_produces_no_gap(
    db_session: Session, project: Project
) -> None:
    _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="plan.txt",
        raw_text="We are still waiting on results from Group X before proceeding.",
        chosen_date=_BASE_DATE,
    )
    _make_artifact(
        db_session,
        project,
        content_hash="h2",
        filename="update.txt",
        raw_text="Group X sent over their findings yesterday afternoon, as it turns out.",
        chosen_date=_BASE_DATE + timedelta(days=3),
    )
    db_session.commit()

    provider = FakeLLMProvider(
        [_judgment_response(fulfilled=True, confidence=0.8, rationale="Group X's findings arrived")]
    )
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.promised == 0
    assert _promised_gaps(db_session, project) == []
    assert len(provider.calls) == 1


def test_malformed_llm_promise_response_degrades_to_deterministic_gap(
    db_session: Session, project: Project
) -> None:
    source = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="meeting.txt",
        raw_text="Cost estimate TBD pending final vendor quotes.",
        chosen_date=_BASE_DATE,
    )
    _make_artifact(
        db_session,
        project,
        content_hash="h2",
        filename="unrelated.txt",
        raw_text="Unrelated later document with no relevant content at all.",
        chosen_date=_BASE_DATE + timedelta(days=1),
    )
    db_session.commit()

    provider = FakeLLMProvider(["not json at all, sorry"])
    result = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.promised == 1
    gaps = _promised_gaps(db_session, project)
    assert len(gaps) == 1
    assert gaps[0].confidence == pytest.approx(SETTINGS.gap_promised_confidence_deterministic)
    assert source.original_filename in gaps[0].description
    audits = _audits(db_session, "gap_promised_unfulfilled", gaps[0].id)
    assert audits[0].model == gaps_module._PROMISE_RULESET


# --------------------------------------------------------------------------- #
# Distinguishing the two kinds: confidence bands never overlap              #
# --------------------------------------------------------------------------- #
def test_structural_and_promised_confidence_bands_do_not_overlap(
    db_session: Session, project: Project
) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    covered_phase = phases[0]

    a1 = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="a1.txt",
        raw_text="We still need to run the control experiment.",
        chosen_date=_BASE_DATE,
    )
    _assign_phase(db_session, a1, covered_phase)
    db_session.commit()

    result = run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()
    assert result.structural >= 1
    assert result.promised == 1

    structural_confidences = [
        g.confidence for g in _gaps(db_session, project) if g.type == GapType.structural
    ]
    promised_confidences = [g.confidence for g in _promised_gaps(db_session, project)]
    assert structural_confidences and promised_confidences
    assert min(structural_confidences) > max(promised_confidences)
    # And the settings themselves encode that guarantee, not just this sample.
    assert SETTINGS.gap_structural_confidence_few > SETTINGS.gap_promised_confidence_llm_cap


# --------------------------------------------------------------------------- #
# Human review: never clobber a gap a human has acted on                    #
# --------------------------------------------------------------------------- #
def test_dismissed_gap_is_preserved_on_rerun_while_open_gaps_refresh(
    db_session: Session, project: Project
) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    covered_phase, other_uncovered, dismissed_phase = phases[0], phases[1], phases[2]

    a1 = _make_artifact(
        db_session, project, content_hash="h1", filename="a1.txt", chosen_date=_BASE_DATE
    )
    a2 = _make_artifact(
        db_session, project, content_hash="h2", filename="a2.txt", chosen_date=_BASE_DATE
    )
    a3 = _make_artifact(
        db_session, project, content_hash="h3", filename="a3.txt", chosen_date=_BASE_DATE
    )
    for a in (a1, a2, a3):
        _assign_phase(db_session, a, covered_phase)
    db_session.commit()

    run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()

    dismissed_gap = _structural_gap_for(db_session, project, dismissed_phase)
    other_gap = _structural_gap_for(db_session, project, other_uncovered)
    assert dismissed_gap is not None and other_gap is not None
    assert "0 of 3" in dismissed_gap.evidence
    dismissed_gap.status = GapStatus.dismissed
    db_session.commit()

    # Genuinely change the corpus (bumps the project-wide fingerprint, so the
    # stage recomputes instead of being skipped as a no-op) without covering
    # either uncovered phase.
    a4 = _make_artifact(
        db_session, project, content_hash="h4", filename="a4.txt", chosen_date=_BASE_DATE
    )
    _assign_phase(db_session, a4, covered_phase)
    db_session.commit()

    result = run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()

    # The dismissed gap: same row, same (stale) evidence, status untouched.
    reloaded_dismissed = db_session.get(Gap, dismissed_gap.id)
    assert reloaded_dismissed is not None
    assert reloaded_dismissed.status == GapStatus.dismissed
    assert "0 of 3" in reloaded_dismissed.evidence  # NOT refreshed to "0 of 4"
    assert result.preserved == 1

    # The still-open gap for the other uncovered phase: refreshed in place.
    reloaded_other = db_session.get(Gap, other_gap.id)
    assert reloaded_other is not None
    assert reloaded_other.status == GapStatus.open
    assert "0 of 4" in reloaded_other.evidence

    # Exactly one gap still exists per phase -- the dismissed one was not
    # duplicated by a fresh "open" row at the same identity.
    assert len([g for g in _gaps(db_session, project) if g.phase_id == dismissed_phase.id]) == 1


# --------------------------------------------------------------------------- #
# StageState idempotency: no-op on unchanged corpus, rebuild on change      #
# --------------------------------------------------------------------------- #
def test_run_project_gaps_unchanged_corpus_is_a_noop(db_session: Session, project: Project) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    a1 = _make_artifact(
        db_session, project, content_hash="h1", filename="a1.txt", chosen_date=_BASE_DATE
    )
    _assign_phase(db_session, a1, phases[0])
    db_session.commit()

    provider = FakeLLMProvider([])
    first = run_project_gaps(db_session, project.id, provider=provider)
    db_session.commit()
    assert first.skipped == 0
    gap_ids_before = {g.id for g in _gaps(db_session, project)}
    assert gap_ids_before

    second = run_project_gaps(
        db_session, project.id, provider=provider
    )  # same (exhausted) provider
    db_session.commit()
    assert second.skipped == 1
    assert second.created == 0
    assert second.updated == 0
    gap_ids_after = {g.id for g in _gaps(db_session, project)}
    assert gap_ids_before == gap_ids_after  # untouched, not deleted + reinserted

    states = list(
        db_session.scalars(
            select(StageState).where(
                StageState.artifact_id == a1.id, StageState.stage == Stage.gaps
            )
        ).all()
    )
    assert len(states) == 1
    assert states[0].status == StageStatus.done


def test_run_project_gaps_rebuilds_when_phase_assignment_changes(
    db_session: Session, project: Project
) -> None:
    _make_domain_classification(db_session, project)
    phases = _research_phases(db_session)
    target_phase = phases[-1]
    a1 = _make_artifact(
        db_session, project, content_hash="h1", filename="a1.txt", chosen_date=_BASE_DATE
    )
    a2 = _make_artifact(
        db_session, project, content_hash="h2", filename="a2.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()
    assert _structural_gap_for(db_session, project, target_phase) is not None

    # Reach full coverage (>= gap_structural_few_threshold, default 2) so the
    # gap clears entirely rather than merely shifting confidence bands.
    _assign_phase(db_session, a1, target_phase)
    _assign_phase(db_session, a2, target_phase)
    db_session.commit()

    result = run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    db_session.commit()
    assert result.skipped == 0  # recomputed, not skipped -- the corpus changed
    assert _structural_gap_for(db_session, project, target_phase) is None  # now covered, gap pruned


# --------------------------------------------------------------------------- #
# Multi-tenant scoping                                                       #
# --------------------------------------------------------------------------- #
def test_run_project_gaps_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    _make_domain_classification(db_session, project)
    _make_domain_classification(db_session, other_project)
    phases = _research_phases(db_session)

    a1 = _make_artifact(
        db_session, project, content_hash="in", filename="in.txt", chosen_date=_BASE_DATE
    )
    b1 = _make_artifact(
        db_session, other_project, content_hash="out", filename="out.txt", chosen_date=_BASE_DATE
    )
    _assign_phase(db_session, a1, phases[0])
    _assign_phase(db_session, b1, phases[0])
    db_session.commit()

    run_project_gaps(db_session, project.id, provider=FakeLLMProvider([]))
    run_project_gaps(db_session, other_project.id, provider=FakeLLMProvider([]))
    db_session.commit()

    assert _gaps(db_session, project) != []
    assert _gaps(db_session, other_project) != []
    assert {g.project_id for g in _gaps(db_session, project)} == {project.id}
    assert {g.project_id for g in _gaps(db_session, other_project)} == {other_project.id}
