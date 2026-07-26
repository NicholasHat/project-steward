from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis.graph import (
    ENTITY_CONFIDENCE,
    FILENAME_CONFIDENCE,
    TITLE_CONFIDENCE,
    build_project_graph,
)
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    DateSignalSource,
    EdgeType,
    Entity,
    EntityMention,
    EntityType,
    ProcessingState,
    Project,
    RelationshipEdge,
    ResolvedDate,
    Stage,
    StageState,
    User,
)

EARLIER = datetime(2024, 1, 1, tzinfo=UTC)
LATER = datetime(2024, 6, 1, tzinfo=UTC)
LATEST = datetime(2024, 9, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session,
    project: Project,
    *,
    content_hash: str,
    filename: str,
    raw_text: str | None = None,
    structure: dict | None = None,
    chosen_date: datetime | None = None,
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

    if raw_text is not None or structure is not None:
        db_session.add(
            ArtifactContent(
                artifact_id=artifact.id,
                raw_text=raw_text,
                structure=structure,
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


def _make_entity_mention(
    db_session: Session,
    project: Project,
    artifact: Artifact,
    *,
    entity_type: EntityType,
    value: str,
    context: str = "",
) -> None:
    entity = db_session.scalar(
        select(Entity).where(
            Entity.project_id == project.id,
            Entity.type == entity_type,
            Entity.normalized_value == value.lower(),
        )
    )
    if entity is None:
        entity = Entity(
            project_id=project.id, type=entity_type, value=value, normalized_value=value.lower()
        )
        db_session.add(entity)
        db_session.flush()
    db_session.add(
        EntityMention(
            entity_id=entity.id,
            artifact_id=artifact.id,
            span="0:1",
            context=context,
            confidence=0.7,
            extractor="test",
        )
    )
    db_session.flush()


def _edges_from(db_session: Session, artifact: Artifact) -> list[RelationshipEdge]:
    return list(
        db_session.scalars(
            select(RelationshipEdge).where(RelationshipEdge.src_artifact_id == artifact.id)
        ).all()
    )


def _stage_state(db_session: Session, artifact: Artifact) -> StageState | None:
    return db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == artifact.id, StageState.stage == Stage.graph
        )
    )


# --------------------------------------------------------------------------- #
# Filename match -> edge with evidence                                       #
# --------------------------------------------------------------------------- #
def test_filename_reference_produces_edge_with_evidence(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session, project, content_hash="b", filename="experiment_log.txt", chosen_date=EARLIER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="analysis.txt",
        raw_text="Building on the results in experiment_log.txt, we conclude...",
        chosen_date=LATER,
    )
    db_session.commit()

    result = build_project_graph(db_session, project.id)
    db_session.commit()

    assert result.rebuilt == 2
    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.dst_artifact_id == target.id
    assert edge.confidence == FILENAME_CONFIDENCE
    assert "experiment_log.txt" in edge.evidence
    # source is dated after target -> builds_on
    assert edge.type == EdgeType.builds_on


# --------------------------------------------------------------------------- #
# Title/heading match -> edge, lower confidence than a filename match        #
# --------------------------------------------------------------------------- #
def test_title_reference_produces_edge_with_lower_confidence_than_filename(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session,
        project,
        content_hash="b",
        filename="b.txt",
        structure={"headings": ["Catalyst Screening Results"]},
        chosen_date=EARLIER,
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="Following up on Catalyst Screening Results from last quarter.",
        chosen_date=LATER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].dst_artifact_id == target.id
    assert edges[0].confidence == TITLE_CONFIDENCE
    assert edges[0].confidence < FILENAME_CONFIDENCE
    assert "Catalyst Screening Results" in edges[0].evidence


# --------------------------------------------------------------------------- #
# Temporal orientation                                                       #
# --------------------------------------------------------------------------- #
def test_earlier_artifact_referencing_later_one_is_typed_references_not_builds_on(
    db_session: Session, project: Project
) -> None:
    later_target = _make_artifact(
        db_session, project, content_hash="b", filename="future_plan.txt", chosen_date=LATER
    )
    earlier_source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="See future_plan.txt for the roadmap.",
        chosen_date=EARLIER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, earlier_source)
    assert len(edges) == 1
    assert edges[0].dst_artifact_id == later_target.id
    assert edges[0].type == EdgeType.references


def test_reference_without_either_chosen_date_is_typed_references(
    db_session: Session, project: Project
) -> None:
    _make_artifact(db_session, project, content_hash="b", filename="notes_v2.txt")
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="Continuing from notes_v2.txt.",
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].type == EdgeType.references


# --------------------------------------------------------------------------- #
# No self-edges                                                              #
# --------------------------------------------------------------------------- #
def test_artifact_mentioning_its_own_filename_gets_no_self_edge(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="self_report.txt",
        raw_text="This document, self_report.txt, summarizes findings.",
        chosen_date=EARLIER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    assert _edges_from(db_session, artifact) == []


# --------------------------------------------------------------------------- #
# Dedupe: multiple signals to the same destination collapse to one edge      #
# --------------------------------------------------------------------------- #
def test_multiple_signals_to_same_destination_dedupe_to_the_higher_confidence_edge(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session,
        project,
        content_hash="b",
        filename="experiment_log.txt",
        structure={"headings": ["Catalyst Screening Results"]},
        chosen_date=EARLIER,
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text=(
            "Building on experiment_log.txt and the Catalyst Screening Results "
            "discussed there."
        ),
        chosen_date=LATER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, source)
    assert len(edges) == 1  # not two edges for the same (src, dst)
    assert edges[0].dst_artifact_id == target.id
    assert edges[0].confidence == FILENAME_CONFIDENCE  # the stronger of the two signals wins


# --------------------------------------------------------------------------- #
# Precision: a generic/common shared entity does NOT create an edge         #
# --------------------------------------------------------------------------- #
def test_entity_shared_across_too_many_artifacts_creates_no_edge(
    db_session: Session, project: Project
) -> None:
    # The same "Experiment 7" mentioned in 5 artifacts is a project-wide
    # recurring identifier, not a distinctive pairwise cross-reference --
    # above Settings.graph_max_shared_entity_artifacts (default 4).
    artifacts = [
        _make_artifact(
            db_session,
            project,
            content_hash=f"h{i}",
            filename=f"doc{i}.txt",
            chosen_date=datetime(2024, i + 1, 1, tzinfo=UTC),
        )
        for i in range(5)
    ]
    for artifact in artifacts:
        _make_entity_mention(
            db_session, project, artifact, entity_type=EntityType.experiment, value="Experiment 7"
        )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    for artifact in artifacts:
        assert _edges_from(db_session, artifact) == []


def test_short_generic_filename_below_min_length_is_not_a_match_key(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session, project, content_hash="b", filename="a.txt", chosen_date=EARLIER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="c.txt",
        raw_text="Please see a.txt for details.",
        chosen_date=LATER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    assert _edges_from(db_session, source) == []
    assert target.id  # sanity: target exists, just never referenced as an edge


# --------------------------------------------------------------------------- #
# Shared distinctive entity -> mentioned_by, oriented later -> earlier       #
# --------------------------------------------------------------------------- #
def test_shared_distinctive_entity_produces_mentioned_by_edge_oriented_by_date(
    db_session: Session, project: Project
) -> None:
    earlier = _make_artifact(
        db_session, project, content_hash="a", filename="intro.txt", chosen_date=EARLIER
    )
    later = _make_artifact(
        db_session, project, content_hash="b", filename="followup.txt", chosen_date=LATER
    )
    _make_entity_mention(
        db_session,
        project,
        earlier,
        entity_type=EntityType.hypothesis,
        value="Hypothesis 3",
        context="we propose Hypothesis 3",
    )
    _make_entity_mention(
        db_session,
        project,
        later,
        entity_type=EntityType.hypothesis,
        value="Hypothesis 3",
        context="revisiting Hypothesis 3 from before",
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, later)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.dst_artifact_id == earlier.id
    assert edge.type == EdgeType.mentioned_by
    assert edge.confidence == ENTITY_CONFIDENCE
    assert "Hypothesis 3" in edge.evidence
    # No edge the other direction.
    assert _edges_from(db_session, earlier) == []


# --------------------------------------------------------------------------- #
# StageState idempotency                                                     #
# --------------------------------------------------------------------------- #
def test_second_run_over_unchanged_corpus_is_a_noop(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session, project, content_hash="b", filename="log.txt2024", chosen_date=EARLIER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="See log.txt2024 for raw data.",
        chosen_date=LATER,
    )
    db_session.commit()

    first = build_project_graph(db_session, project.id)
    db_session.commit()
    assert first.rebuilt == 2
    ids_before = {e.id for e in _edges_from(db_session, source)}

    second = build_project_graph(db_session, project.id)
    db_session.commit()
    assert second.rebuilt == 0
    assert second.skipped == 2
    ids_after = {e.id for e in _edges_from(db_session, source)}
    assert ids_before == ids_after  # rows untouched, not deleted + reinserted
    assert target.id


def test_rebuilds_when_source_artifact_text_changes(
    db_session: Session, project: Project
) -> None:
    target = _make_artifact(
        db_session, project, content_hash="b", filename="dataset.csv", chosen_date=EARLIER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="Nothing relevant here.",
        chosen_date=LATER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()
    assert _edges_from(db_session, source) == []

    # Simulate a re-parse that changed the source's content and content_hash.
    content = db_session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == source.id)
    )
    assert content is not None
    content.raw_text = "Now referencing dataset.csv directly."
    source.content_hash = "a-changed"
    db_session.commit()

    result = build_project_graph(db_session, project.id)
    db_session.commit()

    assert result.rebuilt >= 1
    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].dst_artifact_id == target.id


def test_rebuilds_when_a_referenced_artifacts_chosen_date_changes_without_text_changing(
    db_session: Session, project: Project
) -> None:
    # Edge type (references vs. builds_on) is decided from chosen dates, not
    # from text -- so a date-only change (e.g. a re-extract that re-chooses a
    # different candidate) must still invalidate a cached edge even though
    # neither artifact's content_hash changed.
    target = _make_artifact(
        db_session, project, content_hash="b", filename="notes_final.txt", chosen_date=LATER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="See notes_final.txt for the writeup.",
        chosen_date=EARLIER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()
    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].type == EdgeType.references  # source is earlier than target

    target_date = db_session.scalar(
        select(ResolvedDate).where(
            ResolvedDate.artifact_id == target.id, ResolvedDate.is_chosen.is_(True)
        )
    )
    assert target_date is not None
    target_date.candidate_date = datetime(2023, 1, 1, tzinfo=UTC)  # now earlier than source
    db_session.commit()

    result = build_project_graph(db_session, project.id)
    db_session.commit()

    assert result.rebuilt >= 1  # source's cached edge is invalidated by the date-only change
    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].type == EdgeType.builds_on  # source is now later than target


def test_rebuilds_when_a_new_target_artifact_appears(db_session: Session, project: Project) -> None:
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="Building on results_v3.txt from last month.",
        chosen_date=LATER,
    )
    db_session.commit()

    first = build_project_graph(db_session, project.id)
    db_session.commit()
    assert first.rebuilt == 1
    assert _edges_from(db_session, source) == []  # results_v3.txt doesn't exist yet

    new_target = _make_artifact(
        db_session, project, content_hash="c", filename="results_v3.txt", chosen_date=EARLIER
    )
    db_session.commit()

    second = build_project_graph(db_session, project.id)
    db_session.commit()

    assert second.rebuilt == 2  # both artifacts rebuilt: the new one + the source whose
    # candidate-target set changed
    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].dst_artifact_id == new_target.id


# --------------------------------------------------------------------------- #
# Project scoping                                                            #
# --------------------------------------------------------------------------- #
def test_no_edges_across_projects(db_session: Session, project: Project, user: User) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    target_other_project = _make_artifact(
        db_session, other_project, content_hash="b", filename="shared_name.txt", chosen_date=EARLIER
    )
    source_this_project = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="See shared_name.txt for background.",
        chosen_date=LATER,
    )
    db_session.commit()

    result = build_project_graph(db_session, project.id)
    db_session.commit()

    assert result.rebuilt == 1  # only the in-scope artifact was processed
    assert _edges_from(db_session, source_this_project) == []  # no cross-project edge
    assert target_other_project.id  # exists, untouched

    other_result = build_project_graph(db_session, other_project.id)
    db_session.commit()
    assert other_result.rebuilt == 1
    assert _edges_from(db_session, target_other_project) == []


# --------------------------------------------------------------------------- #
# Confidence carried through onto the persisted edge                        #
# --------------------------------------------------------------------------- #
def test_confidence_is_carried_from_signal_to_persisted_edge(
    db_session: Session, project: Project
) -> None:
    _make_artifact(
        db_session, project, content_hash="b", filename="raw_data.xlsx", chosen_date=EARLIER
    )
    source = _make_artifact(
        db_session,
        project,
        content_hash="a",
        filename="a.txt",
        raw_text="Analysis of raw_data.xlsx follows.",
        chosen_date=LATER,
    )
    db_session.commit()

    build_project_graph(db_session, project.id)
    db_session.commit()

    edges = _edges_from(db_session, source)
    assert len(edges) == 1
    assert edges[0].confidence == FILENAME_CONFIDENCE
