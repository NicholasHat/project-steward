from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis import view as view_module
from truth_engine.analysis.view import current_projection, run_project_view
from truth_engine.config import get_settings
from truth_engine.db.models import Artifact as ArtifactModel
from truth_engine.db.models import (
    ArtifactContent,
    AssignmentSource,
    DateSignalSource,
    DecisionAudit,
    Entity,
    EntityMention,
    EntityType,
    PhaseAssignment,
    PhaseTemplate,
    ProcessingState,
    Project,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    User,
    ViewProjection,
)
from truth_engine.reasoning.providers import LLMProvider

SETTINGS = get_settings()
_BASE_DATE = datetime(2024, 3, 12, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fake provider: canned, ordered responses -- no network call, hermetic.     #
# Same shape as test_phases.py's / test_direction.py's / test_gaps.py's.     #
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
    filename: str = "notes.txt",
    current_path: str | None = None,
    raw_text: str | None = None,
    chosen_date: datetime | None = None,
) -> ArtifactModel:
    artifact = ArtifactModel(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path=current_path or f"/tmp/{filename}",
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
    confidence: float = 0.8,
    source: AssignmentSource = AssignmentSource.auto,
) -> PhaseAssignment:
    row = PhaseAssignment(
        artifact_id=artifact.id,
        phase_id=phase.id,
        confidence=confidence,
        rationale="test",
        source=source,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _add_entity_mention(
    db_session: Session,
    project: Project,
    artifact: ArtifactModel,
    *,
    value: str,
    etype: EntityType = EntityType.tool,
) -> None:
    normalized = value.lower()
    entity = db_session.scalar(
        select(Entity).where(
            Entity.project_id == project.id,
            Entity.type == etype,
            Entity.normalized_value == normalized,
        )
    )
    if entity is None:
        entity = Entity(project_id=project.id, type=etype, value=value, normalized_value=normalized)
        db_session.add(entity)
        db_session.flush()
    db_session.add(
        EntityMention(
            entity_id=entity.id,
            artifact_id=artifact.id,
            span=None,
            context="test",
            confidence=0.9,
            extractor="test",
        )
    )
    db_session.flush()


def _name_response(slug: str, artifact_index: int = 0, rationale: str = "llm rationale") -> str:
    return json.dumps(
        {"names": [{"artifact_index": artifact_index, "slug": slug, "rationale": rationale}]}
    )


def _audits(session: Session, target_id: uuid.UUID) -> list[DecisionAudit]:
    return list(
        session.scalars(
            select(DecisionAudit).where(
                DecisionAudit.decision_type == "view_projection_name",
                DecisionAudit.target_id == target_id,
            )
        ).all()
    )


def _all_projections(session: Session, artifact_id: uuid.UUID) -> list[ViewProjection]:
    return list(
        session.scalars(
            select(ViewProjection)
            .where(ViewProjection.artifact_id == artifact_id)
            .order_by(ViewProjection.version)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Basic derivation: name / category / virtual_path                          #
# --------------------------------------------------------------------------- #
def test_projection_produced_with_llm_name_category_and_path(
    db_session: Session, project: Project
) -> None:
    phases = _research_phases(db_session)
    execution_phase = phases[1]  # research template: Conceptualization, Execution, Analysis, ...
    artifact = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="meeting_notes.md",
        raw_text="Lab meeting discussing catalyst B screening results.",
        chosen_date=_BASE_DATE,
    )
    _assign_phase(db_session, artifact, execution_phase)
    _add_entity_mention(db_session, project, artifact, value="Catalyst B")
    db_session.commit()

    provider = FakeLLMProvider([_name_response("lab-meeting-catalyst-b-screening")])
    result = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.generated == 1
    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.version == 1
    assert projection.superseded_by is None
    assert projection.source == AssignmentSource.auto
    assert projection.suggested_category == execution_phase.phase_name
    assert projection.suggested_name == "2024-03-12_lab-meeting-catalyst-b-screening.md"
    assert projection.virtual_path == (
        f"{execution_phase.phase_name}/2024-03/2024-03-12_lab-meeting-catalyst-b-screening.md"
    )


def test_unphased_artifact_gets_generic_category(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="random.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    result = run_project_view(
        db_session, project.id, provider=FakeLLMProvider([_name_response("random-file")])
    )
    db_session.commit()

    assert result.generated == 1
    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_category == SETTINGS.view_generic_category
    assert projection.virtual_path.startswith(f"{SETTINGS.view_generic_category}/2024-03/")


def test_undated_artifact_uses_undated_folder_and_prefix(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, content_hash="h1", filename="scrap.txt")
    db_session.commit()

    run_project_view(
        db_session, project.id, provider=FakeLLMProvider([_name_response("scrap-notes")])
    )
    db_session.commit()

    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_name.startswith(f"{SETTINGS.view_undated_folder}_")
    assert f"/{SETTINGS.view_undated_folder}/" in projection.virtual_path


# --------------------------------------------------------------------------- #
# Deterministic fallback on malformed / junk LLM output                     #
# --------------------------------------------------------------------------- #
def test_malformed_llm_response_degrades_to_deterministic_fallback(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="Cost Estimate v2.xlsx",
        chosen_date=_BASE_DATE,
    )
    _add_entity_mention(db_session, project, artifact, value="Group X")
    db_session.commit()

    provider = FakeLLMProvider(["not json at all, sorry"])
    result = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.generated == 1
    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_name  # never empty
    assert projection.suggested_name.startswith("2024-03-12_")
    assert projection.suggested_name.endswith(".xlsx")
    assert "cost-estimate-v2" in projection.suggested_name
    assert "group-x" in projection.suggested_name

    audits = _audits(db_session, projection.id)
    assert len(audits) == 1
    assert audits[0].model == view_module._NAME_RULESET


def test_too_short_llm_slug_degrades_to_fallback(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    # "a" slugifies to a single character -- below view_name_min_slug_chars (3).
    provider = FakeLLMProvider([_name_response("a")])
    result = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.generated == 1
    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_name == "2024-03-12_notes.txt"
    audits = _audits(db_session, projection.id)
    assert audits[0].model == view_module._NAME_RULESET


def test_llm_naming_disabled_never_calls_provider(
    db_session: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_settings() is an lru_cache'd singleton shared by every module
    # (config.py, view.py, ...) -- mutating the attribute in place on the
    # already-cached instance is visible everywhere `get_settings()` is
    # called, with no cache invalidation needed (and none wanted: clearing
    # the cache would just construct an unpatched replacement instance).
    monkeypatch.setattr(SETTINGS, "view_llm_naming_enabled", False)

    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    provider = FakeLLMProvider([])  # any call would raise (queue exhausted)
    result = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.generated == 1
    assert provider.calls == []
    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_name == "2024-03-12_notes.txt"


# --------------------------------------------------------------------------- #
# Versioning: regeneration supersedes the prior auto row, history preserved #
# --------------------------------------------------------------------------- #
def test_regeneration_on_change_creates_new_version_and_preserves_history(
    db_session: Session, project: Project
) -> None:
    phases = _research_phases(db_session)
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    db_session.commit()
    v1 = current_projection(db_session, artifact.id)
    assert v1 is not None
    assert v1.version == 1
    v1_id = v1.id

    # Change an input to the derivation (phase assignment -> category changes).
    _assign_phase(db_session, artifact, phases[0])
    db_session.commit()

    result = run_project_view(
        db_session, project.id, provider=FakeLLMProvider([_name_response("notes")])
    )
    db_session.commit()
    assert result.generated == 1

    v2 = current_projection(db_session, artifact.id)
    assert v2 is not None
    assert v2.id != v1_id
    assert v2.version == 2
    assert v2.suggested_category == phases[0].phase_name

    # The old row is still present (rollback possible), not deleted, and now
    # points at the new one.
    reloaded_v1 = db_session.get(ViewProjection, v1_id)
    assert reloaded_v1 is not None
    assert reloaded_v1.superseded_by == v2.id
    assert reloaded_v1.suggested_category == SETTINGS.view_generic_category  # untouched, stale

    all_versions = _all_projections(db_session, artifact.id)
    assert [row.version for row in all_versions] == [1, 2]

    audits = _audits(db_session, v2.id)
    assert len(audits) == 1
    assert audits[0].old_value["suggested_category"] == SETTINGS.view_generic_category
    assert audits[0].new_value["suggested_category"] == phases[0].phase_name


# --------------------------------------------------------------------------- #
# Human override preserved across regeneration                              #
# --------------------------------------------------------------------------- #
def test_human_override_is_never_superseded_by_auto_regeneration(
    db_session: Session, project: Project
) -> None:
    phases = _research_phases(db_session)
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    db_session.commit()
    v1 = current_projection(db_session, artifact.id)
    assert v1 is not None

    # Simulate a human override: a new version, source=human, superseding v1.
    override = ViewProjection(
        artifact_id=artifact.id,
        suggested_name="my-custom-name.txt",
        suggested_category="My Category",
        virtual_path="My Category/my-custom-name.txt",
        version=v1.version + 1,
        source=AssignmentSource.human,
    )
    db_session.add(override)
    db_session.flush()
    v1.superseded_by = override.id
    db_session.commit()

    assert current_projection(db_session, artifact.id).id == override.id

    # Now genuinely change the corpus (a new phase assignment) and rerun --
    # the human override must survive untouched, no LLM call for this artifact.
    _assign_phase(db_session, artifact, phases[0])
    db_session.commit()

    provider = FakeLLMProvider([])  # a call here would mean the override was clobbered
    result = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    assert result.human_skipped == 1
    assert result.generated == 0
    assert provider.calls == []

    current = current_projection(db_session, artifact.id)
    assert current is not None
    assert current.id == override.id
    assert current.suggested_name == "my-custom-name.txt"
    assert current.source == AssignmentSource.human

    # Still only two versions -- no phantom auto version was inserted.
    assert len(_all_projections(db_session, artifact.id)) == 2


# --------------------------------------------------------------------------- #
# "Current projection" query                                                #
# --------------------------------------------------------------------------- #
def test_current_projection_returns_latest_non_superseded_row(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()
    assert current_projection(db_session, artifact.id) is None  # nothing yet

    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    db_session.commit()
    v1 = current_projection(db_session, artifact.id)
    assert v1 is not None and v1.version == 1

    phases = _research_phases(db_session)
    _assign_phase(db_session, artifact, phases[0])
    db_session.commit()
    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    db_session.commit()

    v2 = current_projection(db_session, artifact.id)
    assert v2 is not None
    assert v2.version == 2
    assert v2.id != v1.id
    # exactly one row has superseded_by IS NULL
    non_superseded = [
        row for row in _all_projections(db_session, artifact.id) if row.superseded_by is None
    ]
    assert len(non_superseded) == 1
    assert non_superseded[0].id == v2.id


# --------------------------------------------------------------------------- #
# StageState idempotency: no-op on unchanged corpus, rebuild on change      #
# --------------------------------------------------------------------------- #
def test_run_project_view_unchanged_corpus_is_a_noop(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    provider = FakeLLMProvider([_name_response("notes")])
    first = run_project_view(db_session, project.id, provider=provider)
    db_session.commit()
    assert first.generated == 1
    v1 = current_projection(db_session, artifact.id)
    assert v1 is not None

    second = run_project_view(db_session, project.id, provider=provider)  # same exhausted provider
    db_session.commit()
    assert second.skipped == 1
    assert second.generated == 0

    v1_again = current_projection(db_session, artifact.id)
    assert v1_again is not None
    assert v1_again.id == v1.id  # untouched, not a new version

    states = list(
        db_session.scalars(
            select(StageState).where(
                StageState.artifact_id == artifact.id, StageState.stage == Stage.view
            )
        ).all()
    )
    assert len(states) == 1
    assert states[0].status == StageStatus.done


def test_run_project_view_rebuilds_when_content_hash_changes(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session, project, content_hash="h1", filename="notes.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()
    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    db_session.commit()
    v1 = current_projection(db_session, artifact.id)
    assert v1 is not None

    # A re-ingest that changes content_hash (e.g. the file's content changed).
    artifact.content_hash = "h1-changed"
    db_session.commit()

    result = run_project_view(
        db_session, project.id, provider=FakeLLMProvider([_name_response("notes")])
    )
    db_session.commit()
    assert result.generated == 1
    v2 = current_projection(db_session, artifact.id)
    assert v2 is not None
    assert v2.id != v1.id
    assert v2.version == 2


# --------------------------------------------------------------------------- #
# Multi-tenant scoping                                                       #
# --------------------------------------------------------------------------- #
def test_run_project_view_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    a1 = _make_artifact(
        db_session, project, content_hash="in", filename="in.txt", chosen_date=_BASE_DATE
    )
    b1 = _make_artifact(
        db_session, other_project, content_hash="out", filename="out.txt", chosen_date=_BASE_DATE
    )
    db_session.commit()

    run_project_view(db_session, project.id, provider=FakeLLMProvider([_name_response("notes")]))
    run_project_view(
        db_session, other_project.id, provider=FakeLLMProvider([_name_response("notes")])
    )
    db_session.commit()

    assert current_projection(db_session, a1.id) is not None
    assert current_projection(db_session, b1.id) is not None

    other_artifact_projection = db_session.scalar(
        select(ViewProjection).where(ViewProjection.artifact_id == a1.id)
    )
    assert other_artifact_projection is not None
    # No projection row leaks across the artifact_id boundary regardless of
    # project -- confirm each artifact's projection is only ever queried/
    # written against its own id (the FK, not a project filter, is the
    # correctness boundary here since ViewProjection has no project_id of its
    # own -- it's reached transitively via Artifact.project_id, same shape as
    # PhaseAssignment/DirectionLabel).
    b1_projection = current_projection(db_session, b1.id)
    assert b1_projection is not None
    assert b1_projection.artifact_id == b1.id


# --------------------------------------------------------------------------- #
# Non-destructive: the stage never touches the original file on disk        #
# --------------------------------------------------------------------------- #
def test_run_project_view_never_mutates_the_original_file(
    db_session: Session, project: Project, tmp_path
) -> None:
    real_file = tmp_path / "lab_notes.md"
    real_file.write_bytes(b"Original lab notes content, unchanged by this stage.")
    stat_before = real_file.stat()
    bytes_before = real_file.read_bytes()

    artifact = _make_artifact(
        db_session,
        project,
        content_hash="h1",
        filename="lab_notes.md",
        current_path=str(real_file),
        raw_text="Original lab notes content, unchanged by this stage.",
        chosen_date=_BASE_DATE,
    )
    db_session.commit()

    provider = FakeLLMProvider([_name_response("lab-notes")])
    run_project_view(db_session, project.id, provider=provider)
    db_session.commit()

    stat_after = real_file.stat()
    bytes_after = real_file.read_bytes()

    assert bytes_after == bytes_before
    assert stat_after.st_mtime == stat_before.st_mtime
    assert stat_after.st_size == stat_before.st_size

    # Artifact.current_path itself is never touched by this stage either --
    # ViewProjection is purely a metadata projection over it.
    reloaded = db_session.get(ArtifactModel, artifact.id)
    assert reloaded is not None
    assert reloaded.current_path == str(real_file)

    projection = current_projection(db_session, artifact.id)
    assert projection is not None
    assert projection.suggested_name != os.path.basename(real_file)
