from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis import report as report_module
from truth_engine.analysis.report import (
    SECTION_ACTIVITY,
    SECTION_DIRECTION,
    SECTION_GAPS,
    SECTION_OVERVIEW,
    SECTION_STALE,
    current_report,
    run_project_report,
)
from truth_engine.config import get_settings
from truth_engine.db.models import Artifact as ArtifactModel
from truth_engine.db.models import (
    AssignmentSource,
    DateSignalSource,
    DecisionAudit,
    DirectionLabel,
    DirectionLabelValue,
    DirectionSnapshot,
    DomainClassification,
    Gap,
    GapStatus,
    GapType,
    ProcessingState,
    Project,
    Report,
    ResolvedDate,
    TimelineEvent,
    User,
    ViewProjection,
)
from truth_engine.reasoning.providers import LLMProvider

SETTINGS = get_settings()
_BASE_DATE = datetime(2024, 3, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fake provider: canned, ordered responses -- no network call, hermetic.     #
# Same shape as test_direction.py's / test_gaps.py's / test_view.py's.       #
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
        processing_state=ProcessingState.analyzed,
    )
    db_session.add(artifact)
    db_session.flush()

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


def _make_timeline_event(
    db_session: Session,
    project: Project,
    artifact: ArtifactModel,
    *,
    event_date: datetime,
    confidence: float = 0.8,
    source: str = "placement:content",
) -> TimelineEvent:
    event = TimelineEvent(
        project_id=project.id,
        artifact_id=artifact.id,
        event_date=event_date,
        description=artifact.original_filename,
        confidence=confidence,
        source=source,
    )
    db_session.add(event)
    db_session.flush()
    return event


def _make_domain_classification(
    db_session: Session, project: Project, *, domain: str = "research", confidence: float = 0.9
) -> DomainClassification:
    classification = DomainClassification(
        project_id=project.id,
        domain=domain,
        confidence=confidence,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant-for-report-tests",
    )
    db_session.add(classification)
    db_session.flush()
    return classification


def _make_direction_snapshot(
    db_session: Session, project: Project, *, summary: str, fingerprint_hash: str
) -> DirectionSnapshot:
    snapshot = DirectionSnapshot(
        project_id=project.id,
        inferred_direction_summary=summary,
        corpus_fingerprint_hash=fingerprint_hash,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def _make_direction_label(
    db_session: Session,
    artifact: ArtifactModel,
    *,
    label: DirectionLabelValue,
    confidence: float = 0.8,
    rationale: str = "test rationale",
) -> DirectionLabel:
    row = DirectionLabel(
        artifact_id=artifact.id, label=label, rationale=rationale, confidence=confidence
    )
    db_session.add(row)
    db_session.flush()
    return row


def _make_gap(
    db_session: Session,
    project: Project,
    *,
    type: GapType = GapType.structural,
    description: str,
    confidence: float = 0.8,
    status: GapStatus = GapStatus.open,
) -> Gap:
    gap = Gap(
        project_id=project.id,
        type=type,
        phase_id=None,
        description=description,
        evidence="test evidence",
        confidence=confidence,
        status=status,
    )
    db_session.add(gap)
    db_session.flush()
    return gap


def _make_view_projection(
    db_session: Session, artifact: ArtifactModel, *, suggested_name: str, version: int = 1
) -> ViewProjection:
    projection = ViewProjection(
        artifact_id=artifact.id,
        suggested_name=suggested_name,
        suggested_category="Execution",
        virtual_path=f"Execution/2024-03/{suggested_name}",
        version=version,
        source=AssignmentSource.auto,
    )
    db_session.add(projection)
    db_session.flush()
    return projection


def _direction_response(narrative: str) -> str:
    return json.dumps({"narrative": narrative})


def _audits(session: Session, target_id: uuid.UUID) -> list[DecisionAudit]:
    return list(
        session.scalars(
            select(DecisionAudit).where(
                DecisionAudit.decision_type == "report", DecisionAudit.target_id == target_id
            )
        ).all()
    )


def _all_reports(session: Session, project_id: uuid.UUID) -> list[Report]:
    return list(
        session.scalars(
            select(Report).where(Report.project_id == project_id).order_by(Report.version)
        ).all()
    )


def _seed_full_project(db_session: Session, project: Project) -> ArtifactModel:
    """A project with data feeding all five sections: a classified domain, a
    dated + timelined + current-labeled artifact, a direction snapshot, one
    open structural gap, and one stale (superseded) artifact."""
    _make_domain_classification(db_session, project)
    current_artifact = _make_artifact(
        db_session, project, content_hash="cur1", filename="lab_notes.md", chosen_date=_BASE_DATE
    )
    _make_timeline_event(db_session, project, current_artifact, event_date=_BASE_DATE)
    _make_direction_snapshot(
        db_session,
        project,
        summary="The project is focused on catalyst screening.",
        fingerprint_hash="fp1",
    )
    _make_direction_label(db_session, current_artifact, label=DirectionLabelValue.current)

    stale_artifact = _make_artifact(
        db_session,
        project,
        content_hash="stale1",
        filename="old_plan.md",
        chosen_date=_BASE_DATE - timedelta(days=200),
    )
    _make_direction_label(
        db_session,
        stale_artifact,
        label=DirectionLabelValue.superseded,
        rationale="superseded rationale",
    )

    _make_gap(db_session, project, description="No artifacts detected for the 'Analysis' phase.")
    db_session.commit()
    return current_artifact


# --------------------------------------------------------------------------- #
# Basic assembly: all five sections populated, is_current=True              #
# --------------------------------------------------------------------------- #
def test_report_assembled_with_all_sections_and_is_current(
    db_session: Session, project: Project
) -> None:
    _seed_full_project(db_session, project)

    provider = FakeLLMProvider([_direction_response("The team is currently screening catalysts.")])
    result = run_project_report(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.is_new_version
    assert result.report is not None
    assert result.report.version == 1
    assert result.report.is_current is True
    assert set(result.changed_sections) == set(report_module.SECTION_ORDER)
    assert result.reused_sections == []

    report = current_report(db_session, project.id)
    assert report is not None
    assert report.id == result.report.id

    for name in report_module.SECTION_ORDER:
        assert name in report.sections
        assert report.sections[name]["content"]  # never empty

    assert "screening catalysts" in report.sections[SECTION_DIRECTION]["content"]
    assert "research" in report.sections[SECTION_OVERVIEW]["content"]
    assert "lab_notes.md" in report.sections[SECTION_ACTIVITY]["content"]
    assert "Analysis" in report.sections[SECTION_GAPS]["content"]
    assert "old_plan.md" in report.sections[SECTION_STALE]["content"]

    assert "# Project Report" in report.content
    assert "## Current Direction" in report.content
    assert "## Open Gaps" in report.content

    audits = _audits(db_session, report.id)
    assert len(audits) == 1
    assert audits[0].model == provider.model  # direction section was LLM-synthesized this run


# --------------------------------------------------------------------------- #
# LLM narrative vs. deterministic factual sections                          #
# --------------------------------------------------------------------------- #
def test_direction_section_uses_llm_others_are_deterministic(
    db_session: Session, project: Project
) -> None:
    _seed_full_project(db_session, project)

    provider = FakeLLMProvider([_direction_response("A concise report-voice narrative.")])
    run_project_report(db_session, project.id, provider=provider)
    db_session.commit()

    report = current_report(db_session, project.id)
    assert report is not None
    assert report.sections[SECTION_DIRECTION]["model"] == provider.model
    for name in (SECTION_OVERVIEW, SECTION_ACTIVITY, SECTION_GAPS, SECTION_STALE):
        assert report.sections[name]["model"] != provider.model
    assert len(provider.calls) == 1  # exactly one LLM call, for the direction section only


# --------------------------------------------------------------------------- #
# Graceful degradation: malformed LLM output -> deterministic section        #
# --------------------------------------------------------------------------- #
def test_malformed_llm_response_degrades_direction_section_to_deterministic(
    db_session: Session, project: Project
) -> None:
    _seed_full_project(db_session, project)

    provider = FakeLLMProvider(["not json at all, sorry"])
    result = run_project_report(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.is_new_version
    report = result.report
    assert report is not None
    assert report.sections[SECTION_DIRECTION]["content"]  # never empty
    assert "catalyst screening" in report.sections[SECTION_DIRECTION]["content"]
    assert report.sections[SECTION_DIRECTION]["model"] == report_module._DIRECTION_RULESET
    assert report.content  # the whole report is still produced


def test_no_direction_snapshot_yet_degrades_without_calling_llm(
    db_session: Session, project: Project
) -> None:
    # No DirectionSnapshot at all (steps 8-9 haven't run) -- an honest
    # "not yet inferred" section, and no LLM call attempted for it.
    _make_domain_classification(db_session, project)
    db_session.commit()

    provider = FakeLLMProvider([])  # any call would raise (queue exhausted)
    result = run_project_report(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.is_new_version
    assert provider.calls == []
    report = result.report
    assert report is not None
    assert "not yet been inferred" in report.sections[SECTION_DIRECTION]["content"]
    assert report.sections[SECTION_DIRECTION]["model"] == report_module._DIRECTION_RULESET


def test_llm_direction_disabled_never_calls_provider(
    db_session: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SETTINGS, "report_llm_direction_enabled", False)
    _seed_full_project(db_session, project)

    provider = FakeLLMProvider([])
    result = run_project_report(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.is_new_version
    assert provider.calls == []
    assert result.report is not None
    assert "catalyst screening" in result.report.sections[SECTION_DIRECTION]["content"]
    assert result.report.sections[SECTION_DIRECTION]["model"] == report_module._DIRECTION_RULESET


# --------------------------------------------------------------------------- #
# Versioning: regeneration creates a new version, prior flips is_current    #
# --------------------------------------------------------------------------- #
def test_regeneration_creates_new_version_and_preserves_history(
    db_session: Session, project: Project
) -> None:
    _seed_full_project(db_session, project)

    run_project_report(
        db_session, project.id, provider=FakeLLMProvider([_direction_response("v1 narrative.")])
    )
    db_session.commit()
    v1 = current_report(db_session, project.id)
    assert v1 is not None
    v1_id = v1.id

    # Change something that affects the corpus (a new open gap).
    _make_gap(db_session, project, description="Cost estimate TBD for reagents.", confidence=0.5)
    db_session.commit()

    result = run_project_report(
        db_session, project.id, provider=FakeLLMProvider([_direction_response("v1 narrative.")])
    )
    db_session.commit()
    assert result.is_new_version

    v2 = current_report(db_session, project.id)
    assert v2 is not None
    assert v2.id != v1_id
    assert v2.version == 2
    assert v2.is_current is True

    reloaded_v1 = db_session.get(Report, v1_id)
    assert reloaded_v1 is not None
    assert reloaded_v1.is_current is False
    assert reloaded_v1.version == 1  # history retained, untouched

    all_versions = _all_reports(db_session, project.id)
    assert [r.version for r in all_versions] == [1, 2]

    audits = _audits(db_session, v2.id)
    assert len(audits) == 1
    assert audits[0].old_value == {"version": 1}


# --------------------------------------------------------------------------- #
# THE incrementality promise: gaps-only change reuses the LLM section        #
# verbatim and never re-invokes the provider for it.                        #
# --------------------------------------------------------------------------- #
def test_gaps_only_change_reuses_direction_section_verbatim_without_llm_call(
    db_session: Session, project: Project
) -> None:
    _seed_full_project(db_session, project)

    provider_v1 = FakeLLMProvider([_direction_response("The team is screening catalysts.")])
    run_project_report(db_session, project.id, provider=provider_v1)
    db_session.commit()
    assert len(provider_v1.calls) == 1

    v1 = current_report(db_session, project.id)
    assert v1 is not None
    v1_direction_content = v1.sections[SECTION_DIRECTION]["content"]

    # Change ONLY gaps -- dismiss the existing one and add a new one. Nothing
    # about DomainClassification, DirectionSnapshot, DirectionLabel, or
    # ViewProjection changes.
    existing_gap = db_session.scalar(select(Gap).where(Gap.project_id == project.id))
    assert existing_gap is not None
    existing_gap.status = GapStatus.dismissed
    _make_gap(db_session, project, description="Waiting on results from Group X.", confidence=0.5)
    db_session.commit()

    provider_v2 = FakeLLMProvider([])  # a call here would mean the section wasn't reused
    result = run_project_report(db_session, project.id, provider=provider_v2)
    db_session.commit()

    assert result.is_new_version
    assert result.changed_sections == [SECTION_GAPS]
    assert set(result.reused_sections) == {
        SECTION_OVERVIEW, SECTION_DIRECTION, SECTION_ACTIVITY, SECTION_STALE
    }
    assert provider_v2.calls == []  # the fake LLM was NOT called for direction

    v2 = current_report(db_session, project.id)
    assert v2 is not None
    assert v2.version == 2
    # Byte-identical to v1 -- reused verbatim, not recomputed.
    assert v2.sections[SECTION_DIRECTION]["content"] == v1_direction_content
    v1_direction_fp = v1.sections[SECTION_DIRECTION]["fingerprint"]
    assert v2.sections[SECTION_DIRECTION]["fingerprint"] == v1_direction_fp
    assert v2.sections[SECTION_GAPS]["content"] != v1.sections[SECTION_GAPS]["content"]
    assert "Group X" in v2.sections[SECTION_GAPS]["content"]

    audits = _audits(db_session, v2.id)
    assert audits[0].model == report_module._REPORT_RULESET  # no LLM call happened this run
    assert audits[0].new_value["changed_sections"] == [SECTION_GAPS]


# --------------------------------------------------------------------------- #
# Idempotency: a fully unchanged corpus is a true no-op                     #
# --------------------------------------------------------------------------- #
def test_unchanged_corpus_is_a_true_noop(db_session: Session, project: Project) -> None:
    _seed_full_project(db_session, project)

    provider = FakeLLMProvider([_direction_response("stable narrative")])
    first = run_project_report(db_session, project.id, provider=provider)
    db_session.commit()
    assert first.is_new_version
    v1 = current_report(db_session, project.id)
    assert v1 is not None

    provider_second = FakeLLMProvider([])  # any call would fail loudly
    second = run_project_report(db_session, project.id, provider=provider_second)
    db_session.commit()

    assert not second.is_new_version
    assert second.report is not None
    assert second.report.id == v1.id  # same row, untouched
    assert provider_second.calls == []

    all_versions = _all_reports(db_session, project.id)
    assert len(all_versions) == 1  # no new version was written

    audits = _audits(db_session, v1.id)
    assert len(audits) == 1  # no second audit row either


# --------------------------------------------------------------------------- #
# Clean names + fingerprint invalidation on a ViewProjection rename          #
# --------------------------------------------------------------------------- #
def test_view_projection_rename_shows_clean_name_and_invalidates_affected_sections(
    db_session: Session, project: Project
) -> None:
    current_artifact = _seed_full_project(db_session, project)

    run_project_report(
        db_session, project.id, provider=FakeLLMProvider([_direction_response("v1 narrative.")])
    )
    db_session.commit()
    v1 = current_report(db_session, project.id)
    assert v1 is not None
    # Before any ViewProjection exists, sections fall back to the raw filename.
    assert "lab_notes.md" in v1.sections[SECTION_ACTIVITY]["content"]
    assert "lab_notes.md" in v1.sections[SECTION_DIRECTION]["content"]

    # Give the current-labeled artifact a human/auto-suggested clean name --
    # this is the artifact that feeds both Recent Activity (it has a
    # placement TimelineEvent) and Current Direction (it's DirectionLabel
    # current), so both sections' fingerprints fold in its projection
    # version (see report.py's _activity_fingerprint/_direction_fingerprint).
    _make_view_projection(
        db_session, current_artifact, suggested_name="2024-03-01_catalyst-b-screening.md"
    )
    db_session.commit()

    result = run_project_report(
        db_session, project.id, provider=FakeLLMProvider([_direction_response("v2 narrative.")])
    )
    db_session.commit()

    assert result.is_new_version
    assert set(result.changed_sections) == {SECTION_ACTIVITY, SECTION_DIRECTION}
    assert set(result.reused_sections) == {SECTION_OVERVIEW, SECTION_GAPS, SECTION_STALE}

    v2 = current_report(db_session, project.id)
    assert v2 is not None
    assert "2024-03-01_catalyst-b-screening.md" in v2.sections[SECTION_ACTIVITY]["content"]
    assert "2024-03-01_catalyst-b-screening.md" in v2.sections[SECTION_DIRECTION]["content"]
    assert "lab_notes.md" not in v2.sections[SECTION_ACTIVITY]["content"]
    assert "lab_notes.md" not in v2.sections[SECTION_DIRECTION]["content"]
    # Untouched sections carried forward byte-identical, same as the
    # gaps-only incrementality test above.
    assert v2.sections[SECTION_GAPS]["content"] == v1.sections[SECTION_GAPS]["content"]
    assert v2.sections[SECTION_STALE]["content"] == v1.sections[SECTION_STALE]["content"]


# --------------------------------------------------------------------------- #
# Multi-tenant scoping                                                       #
# --------------------------------------------------------------------------- #
def test_run_project_report_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    _seed_full_project(db_session, project)
    _make_domain_classification(db_session, other_project, domain="engineering")
    db_session.commit()

    run_project_report(
        db_session, project.id, provider=FakeLLMProvider([_direction_response("project narrative")])
    )
    run_project_report(db_session, other_project.id, provider=FakeLLMProvider([]))
    db_session.commit()

    report_a = current_report(db_session, project.id)
    report_b = current_report(db_session, other_project.id)
    assert report_a is not None
    assert report_b is not None
    assert report_a.id != report_b.id
    assert report_a.project_id == project.id
    assert report_b.project_id == other_project.id
    assert "engineering" in report_b.sections[SECTION_OVERVIEW]["content"]
    assert "research" in report_a.sections[SECTION_OVERVIEW]["content"]


def test_run_project_report_unknown_project_returns_empty_result(db_session: Session) -> None:
    result = run_project_report(db_session, uuid.uuid4(), provider=FakeLLMProvider([]))
    assert result.report is None
    assert not result.is_new_version
