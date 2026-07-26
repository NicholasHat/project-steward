from __future__ import annotations

import random
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis.direction import (
    build_project_direction_snapshot,
    label_project_direction,
    run_project_direction,
)
from truth_engine.config import get_settings
from truth_engine.db.models import (
    EMBEDDING_DIM,
    Artifact,
    DateSignalSource,
    DecisionAudit,
    DirectionLabel,
    DirectionLabelValue,
    DirectionSnapshot,
    EdgeType,
    Embedding,
    EmbeddingLevel,
    ProcessingState,
    Project,
    RelationshipEdge,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    User,
)
from truth_engine.reasoning.providers import LLMProvider

SETTINGS = get_settings()

# --------------------------------------------------------------------------- #
# Fake provider: canned, ordered responses -- no network call, hermetic.     #
# (Same shape as test_phases.py's -- popping past the end raises loudly.)    #
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
# Deterministic 768-dim vectors forming two well-separated directions        #
# --------------------------------------------------------------------------- #
def _vector(sign: float, seed: int) -> list[float]:
    """A vector dominated by `sign` in dim 0 plus small per-seed noise in the
    rest -- two opposite signs (+1 / -1) put their vectors at cosine
    similarity ~-1 (clearly separated clusters), while the noise keeps
    same-sign vectors from being literally identical."""
    rng = random.Random(seed)
    vector = [sign] + [rng.uniform(-0.01, 0.01) for _ in range(EMBEDDING_DIM - 1)]
    return vector


def _early_vector(seed: int) -> list[float]:
    return _vector(1.0, seed)


def _recent_vector(seed: int) -> list[float]:
    return _vector(-1.0, seed)


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
) -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path=f"/tmp/{filename}",
        original_filename=filename,
        file_type="txt",
        size_bytes=1,
        processing_state=ProcessingState.embedded,
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


def _make_doc_embedding(db_session: Session, artifact: Artifact, vector: list[float]) -> Embedding:
    embedding = Embedding(
        artifact_id=artifact.id,
        chunk_id=None,
        level=EmbeddingLevel.doc,
        vector=vector,
        model="fake-embed",
        model_version="1",
    )
    db_session.add(embedding)
    db_session.flush()
    return embedding


def _make_edge(
    db_session: Session,
    src: Artifact,
    dst: Artifact,
    *,
    type: EdgeType = EdgeType.references,
    confidence: float = 0.8,
) -> RelationshipEdge:
    edge = RelationshipEdge(
        src_artifact_id=src.id, dst_artifact_id=dst.id, type=type, confidence=confidence,
        evidence="test",
    )
    db_session.add(edge)
    db_session.flush()
    return edge


def _label(db_session: Session, artifact: Artifact) -> DirectionLabel | None:
    return db_session.scalar(
        select(DirectionLabel).where(DirectionLabel.artifact_id == artifact.id)
    )


def _stage_state(db_session: Session, artifact: Artifact) -> StageState | None:
    return db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == artifact.id, StageState.stage == Stage.direction
        )
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


# --------------------------------------------------------------------------- #
# Shared scenario: an early ("X") cluster and a recent ("Y") cluster, plus   #
# one early-but-still-cited artifact to force a genuine signal conflict.     #
# --------------------------------------------------------------------------- #
CORPUS_LATEST = datetime(2024, 6, 1, tzinfo=UTC)


def _build_drift_scenario(db_session: Session, project: Project) -> dict[str, Artifact]:
    x1 = _make_artifact(
        db_session, project, content_hash="x1", filename="x1.txt",
        chosen_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    x2 = _make_artifact(
        db_session, project, content_hash="x2", filename="x2.txt",
        chosen_date=datetime(2023, 1, 3, tzinfo=UTC),
    )
    x3 = _make_artifact(
        db_session, project, content_hash="x3", filename="x3.txt",
        chosen_date=datetime(2023, 1, 5, tzinfo=UTC),
    )
    e_conflict = _make_artifact(
        db_session, project, content_hash="ec", filename="e_conflict.txt",
        chosen_date=datetime(2023, 2, 1, tzinfo=UTC),
    )
    y1 = _make_artifact(
        db_session, project, content_hash="y1", filename="y1.txt",
        chosen_date=datetime(2024, 5, 20, tzinfo=UTC),
    )
    y2 = _make_artifact(
        db_session, project, content_hash="y2", filename="y2.txt",
        chosen_date=datetime(2024, 5, 25, tzinfo=UTC),
    )
    y3 = _make_artifact(
        db_session, project, content_hash="y3", filename="y3.txt", chosen_date=CORPUS_LATEST
    )

    for i, artifact in enumerate([x1, x2, x3, e_conflict]):
        _make_doc_embedding(db_session, artifact, _early_vector(seed=i))
    for i, artifact in enumerate([y1, y2, y3]):
        _make_doc_embedding(db_session, artifact, _recent_vector(seed=100 + i))

    # y3 (latest) cites both y1 (still-active current work) and the old
    # e_conflict (an idea from the early cluster that's still being built on)
    # -- the second edge is what manufactures the Signal A/B conflict.
    _make_edge(db_session, src=y3, dst=y1, type=EdgeType.builds_on)
    _make_edge(db_session, src=y3, dst=e_conflict, type=EdgeType.references)

    db_session.commit()
    return {
        "x1": x1, "x2": x2, "x3": x3, "e_conflict": e_conflict, "y1": y1, "y2": y2, "y3": y3,
    }


SUMMARY_RESPONSE = '{"summary": "The project pivoted from approach X to approach Y."}'


# --------------------------------------------------------------------------- #
# Combining rule: current / superseded / unclear                            #
# --------------------------------------------------------------------------- #
def test_recent_cluster_and_still_referenced_artifact_is_labeled_current(
    db_session: Session, project: Project
) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()

    label = _label(db_session, artifacts["y1"])
    assert label is not None
    assert label.label == DirectionLabelValue.current
    assert label.signal_a_score is not None and label.signal_a_score >= 0.6
    assert label.signal_b_score is not None and label.signal_b_score >= 0.6
    assert label.confidence >= SETTINGS.direction_min_confident_label
    assert label.confirmed_by_user is False


def test_early_abandoned_and_never_again_referenced_is_labeled_superseded(
    db_session: Session, project: Project
) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()

    label = _label(db_session, artifacts["x1"])
    assert label is not None
    assert label.label == DirectionLabelValue.superseded
    assert label.signal_a_score is not None and label.signal_a_score <= 0.4
    assert label.signal_b_score == 0.0
    assert "quiet" in label.rationale
    assert "candidate dead end" in label.rationale


def test_conflicting_signals_are_labeled_unclear_not_superseded(
    db_session: Session, project: Project
) -> None:
    """The genuine-drift-vs-topic-diversity case (Open Risk #4): an old,
    quiet-cluster artifact that a later artifact still explicitly cites.
    Signal A says drifted-away-from; Signal B says still-adopted. The
    combining rule must not silently average this into a confident verdict
    either way."""
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()

    label = _label(db_session, artifacts["e_conflict"])
    assert label is not None
    assert label.label == DirectionLabelValue.unclear
    assert label.signal_a_score is not None and label.signal_a_score <= 0.4
    assert label.signal_b_score is not None and label.signal_b_score >= 0.6
    assert label.confidence < SETTINGS.direction_min_confident_label


# --------------------------------------------------------------------------- #
# Rationale cites concrete signals; scores persisted for inspectability      #
# --------------------------------------------------------------------------- #
def test_rationale_cites_concrete_signals_and_dates(db_session: Session, project: Project) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()

    label = _label(db_session, artifacts["x1"])
    assert label is not None
    assert "Signal A (embedding cluster)" in label.rationale
    assert "Signal B (citation graph)" in label.rationale
    assert "2023" in label.rationale  # a concrete date, not a bare tag
    assert "combined score" in label.rationale.lower() or "Combined score" in label.rationale


# --------------------------------------------------------------------------- #
# Small-corpus fallback (Open Risk #4): honest, never fabricates drift       #
# --------------------------------------------------------------------------- #
def test_small_corpus_never_asserts_superseded(db_session: Session, project: Project) -> None:
    assert SETTINGS.direction_min_corpus_size > 3  # sanity: this test is actually below threshold

    old = _make_artifact(
        db_session, project, content_hash="old", filename="old.txt",
        chosen_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    mid = _make_artifact(
        db_session, project, content_hash="mid", filename="mid.txt",
        chosen_date=datetime(2023, 6, 1, tzinfo=UTC),
    )
    new = _make_artifact(
        db_session, project, content_hash="new", filename="new.txt",
        chosen_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    for artifact in (old, mid, new):
        _make_doc_embedding(db_session, artifact, _early_vector(seed=hash(artifact.content_hash)))
    db_session.commit()

    result = label_project_direction(db_session, project.id)
    db_session.commit()

    assert result.errors == []  # no crash
    labels = [_label(db_session, a) for a in (old, mid, new)]
    assert all(label is not None for label in labels)
    assert all(
        label.label != DirectionLabelValue.superseded for label in labels
    )  # never a dead end
    old_label = _label(db_session, old)
    assert "too small" in old_label.rationale or "below the" in old_label.rationale
    assert old_label.confidence <= SETTINGS.direction_small_corpus_confidence_cap


def test_small_corpus_still_allows_a_current_label(db_session: Session, project: Project) -> None:
    old = _make_artifact(
        db_session, project, content_hash="old2", filename="old2.txt",
        chosen_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    new = _make_artifact(
        db_session, project, content_hash="new2", filename="new2.txt",
        chosen_date=datetime(2023, 1, 2, tzinfo=UTC),
    )
    for artifact in (old, new):
        _make_doc_embedding(db_session, artifact, _early_vector(seed=hash(artifact.content_hash)))
    db_session.commit()

    label_project_direction(db_session, project.id)
    db_session.commit()

    # `new` is the corpus's latest artifact -- position score 1.0, and not
    # yet had a chance to be cited -- the asymmetric guard only blocks
    # `superseded`, not `current`, even in small-corpus mode.
    new_label = _label(db_session, new)
    assert new_label is not None
    assert new_label.label == DirectionLabelValue.current
    assert new_label.confidence <= SETTINGS.direction_small_corpus_confidence_cap


# --------------------------------------------------------------------------- #
# Auditability                                                               #
# --------------------------------------------------------------------------- #
def test_decision_audit_written_per_label_and_snapshot(
    db_session: Session, project: Project
) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    provider = FakeLLMProvider([SUMMARY_RESPONSE])
    result = run_project_direction(db_session, project.id, provider=provider)
    db_session.commit()

    all_labels = [_label(db_session, a) for a in artifacts.values()]
    label_audits = _audits(db_session, "direction_label", [lbl.id for lbl in all_labels if lbl])
    assert len(label_audits) == len(artifacts)
    assert all(a.actor.value == "system" for a in label_audits)
    assert all(a.rationale for a in label_audits)

    assert result.snapshot is not None
    snapshot_audits = _audits(db_session, "direction_snapshot", [result.snapshot.id])
    assert len(snapshot_audits) == 1
    assert snapshot_audits[0].model == "fake-llm"
    assert snapshot_audits[0].rationale


# --------------------------------------------------------------------------- #
# Human-confirmation checkpoint -- the load-bearing invariant                #
# --------------------------------------------------------------------------- #
def test_confirmed_label_is_not_overwritten_on_rerun(db_session: Session, project: Project) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()

    x1_label = _label(db_session, artifacts["x1"])
    assert x1_label is not None
    # sanity: matches the unconfirmed test above
    assert x1_label.label == DirectionLabelValue.superseded

    # Human confirms and overrides the verdict.
    x1_label.confirmed_by_user = True
    x1_label.label = DirectionLabelValue.current
    x1_label.rationale = "human override: this is still actively used"
    db_session.commit()
    confirmed_id = x1_label.id

    # Change the corpus (new artifact + embedding) to force a fingerprint
    # change that would otherwise trigger a full relabel.
    new_artifact = _make_artifact(
        db_session, project, content_hash="new-after-confirm", filename="new_after_confirm.txt",
        chosen_date=CORPUS_LATEST,
    )
    _make_doc_embedding(db_session, new_artifact, _recent_vector(seed=999))
    db_session.commit()

    result = label_project_direction(db_session, project.id)
    db_session.commit()

    assert result.confirmed_skipped >= 1
    after = _label(db_session, artifacts["x1"])
    assert after is not None
    assert after.id == confirmed_id  # same row, not delete+reinsert
    assert after.confirmed_by_user is True
    assert after.label == DirectionLabelValue.current
    assert after.rationale == "human override: this is still actively used"

    # Bookkeeping only: StageState still reflects the new corpus fingerprint
    # for this artifact, without the label itself having been touched.
    state = _stage_state(db_session, artifacts["x1"])
    assert state is not None
    assert state.status == StageStatus.done


# --------------------------------------------------------------------------- #
# StageState idempotency                                                     #
# --------------------------------------------------------------------------- #
def test_label_project_direction_second_run_is_a_noop(
    db_session: Session, project: Project
) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    first = label_project_direction(db_session, project.id)
    db_session.commit()
    assert first.labeled == len(artifacts)
    ids_before = {a.id: _label(db_session, a).id for a in artifacts.values()}  # type: ignore[union-attr]

    second = label_project_direction(db_session, project.id)
    db_session.commit()

    assert second.labeled == 0
    assert second.skipped == len(artifacts)
    ids_after = {a.id: _label(db_session, a).id for a in artifacts.values()}  # type: ignore[union-attr]
    assert ids_before == ids_after  # rows updated in place, not deleted + reinserted


def test_label_project_direction_rebuilds_when_corpus_changes(
    db_session: Session, project: Project
) -> None:
    artifacts = _build_drift_scenario(db_session, project)

    label_project_direction(db_session, project.id)
    db_session.commit()
    x1_label_before = _label(db_session, artifacts["x1"])
    assert x1_label_before is not None

    # A new later artifact that cites x1 -- x1's Signal B (and thus its
    # label) should flip away from "dead end".
    new_artifact = _make_artifact(
        db_session,
        project,
        content_hash="cites-x1",
        filename="cites_x1.txt",
        chosen_date=CORPUS_LATEST,
    )
    _make_doc_embedding(db_session, new_artifact, _recent_vector(seed=321))
    _make_edge(db_session, src=new_artifact, dst=artifacts["x1"], type=EdgeType.references)
    db_session.commit()

    result = label_project_direction(db_session, project.id)
    db_session.commit()

    assert result.labeled > 0  # the whole corpus was recomputed, not just the new artifact
    x1_label_after = _label(db_session, artifacts["x1"])
    assert x1_label_after is not None
    assert x1_label_after.id == x1_label_before.id  # same row, updated in place
    assert x1_label_after.signal_b_score is not None and x1_label_after.signal_b_score > 0.0
    assert x1_label_after.label != DirectionLabelValue.superseded


# --------------------------------------------------------------------------- #
# Snapshot narrative: same current-direction member set, graceful fallback   #
# --------------------------------------------------------------------------- #
def test_snapshot_uses_current_direction_members_and_llm_summary(
    db_session: Session, project: Project
) -> None:
    _build_drift_scenario(db_session, project)
    provider = FakeLLMProvider([SUMMARY_RESPONSE])

    snapshot, skipped = build_project_direction_snapshot(db_session, project.id, provider=provider)
    db_session.commit()

    assert skipped is False
    expected_summary = "The project pivoted from approach X to approach Y."
    assert snapshot.inferred_direction_summary == expected_summary
    assert len(provider.calls) == 1
    prompt = provider.calls[0]
    # The recent-Y cluster, not the quiet-X cluster, seeds the narrative.
    assert "y1.txt" in prompt
    assert "y2.txt" in prompt
    assert "y3.txt" in prompt
    assert "x1.txt" not in prompt


def test_snapshot_second_run_with_unchanged_corpus_makes_no_llm_call(
    db_session: Session, project: Project
) -> None:
    _build_drift_scenario(db_session, project)
    provider = FakeLLMProvider([SUMMARY_RESPONSE])
    first, first_skipped = build_project_direction_snapshot(
        db_session, project.id, provider=provider
    )
    db_session.commit()
    assert first_skipped is False

    empty_provider = FakeLLMProvider([])  # any call pops from an empty list and raises
    second, second_skipped = build_project_direction_snapshot(
        db_session, project.id, provider=empty_provider
    )
    db_session.commit()

    assert second_skipped is True
    assert second.id == first.id
    assert empty_provider.calls == []

    snapshots = db_session.scalars(
        select(DirectionSnapshot).where(DirectionSnapshot.project_id == project.id)
    ).all()
    assert len(snapshots) == 1  # no duplicate history row for an unchanged corpus


def test_snapshot_degrades_gracefully_on_malformed_llm_response(
    db_session: Session, project: Project
) -> None:
    _build_drift_scenario(db_session, project)
    provider = FakeLLMProvider(["Sorry, I can't help with that right now."])

    snapshot, skipped = build_project_direction_snapshot(db_session, project.id, provider=provider)
    db_session.commit()

    assert skipped is False
    assert snapshot.inferred_direction_summary  # non-empty deterministic fallback, not a crash
    assert "y1.txt" in snapshot.inferred_direction_summary or "Current direction" in (
        snapshot.inferred_direction_summary
    )


# --------------------------------------------------------------------------- #
# Project scoping                                                            #
# --------------------------------------------------------------------------- #
def test_run_project_direction_is_scoped_to_the_given_project(
    db_session: Session, project: Project, user: User
) -> None:
    other_project = Project(owner_id=user.id, name="Other Project", root_path="/tmp/other")
    db_session.add(other_project)
    db_session.flush()

    _build_drift_scenario(db_session, project)
    other_artifact = _make_artifact(
        db_session, other_project, content_hash="other", filename="other.txt",
        chosen_date=CORPUS_LATEST,
    )
    _make_doc_embedding(db_session, other_artifact, _recent_vector(seed=777))
    db_session.commit()

    provider = FakeLLMProvider([SUMMARY_RESPONSE])
    run_project_direction(db_session, project.id, provider=provider)
    db_session.commit()

    assert _label(db_session, other_artifact) is None
    other_snapshot = db_session.scalar(
        select(DirectionSnapshot).where(DirectionSnapshot.project_id == other_project.id)
    )
    assert other_snapshot is None
