from __future__ import annotations

import hashlib
import math
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.db.models import (
    EMBEDDING_DIM,
    Artifact,
    ArtifactContent,
    Chunk,
    Embedding,
    EmbeddingLevel,
    ProcessingState,
    Project,
    Stage,
    StageState,
    StageStatus,
    StructuredTable,
)
from truth_engine.embed.chunker import chunk_words
from truth_engine.embed.service import embed_artifact, embed_project
from truth_engine.reasoning.providers import EmbeddingProvider


# --------------------------------------------------------------------------- #
# Fake provider: deterministic, offline, unit-normalized (matches the real   #
# provider's contract) — no network call, so pytest runs hermetic/offline.   #
# --------------------------------------------------------------------------- #
class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str = "fake-embed", dim: int = EMBEDDING_DIM) -> None:
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int(hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest(), 16)
        values = []
        for i in range(self.dim):
            seed = (seed * 1_103_515_245 + 12_345 + i) & 0xFFFFFFFF
            values.append((seed / 0xFFFFFFFF) * 2 - 1)
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


class RaisingEmbeddingProvider(FakeEmbeddingProvider):
    """Fails after `fail_after` successful calls — for error-isolation tests."""

    def __init__(self, *, fail_after: int = 0, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._calls = 0
        self._fail_after = fail_after

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._calls += 1
        if self._calls > self._fail_after:
            raise RuntimeError("embedding provider unreachable")
        return super().embed(texts)


def _expected_mean_pool(vectors: list[list[float]]) -> list[float]:
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in mean))
    return [x / norm for x in mean]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _make_artifact(db_session: Session, project: Project, *, content_hash: str = "h") -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path="/tmp/x.txt",
        original_filename="x.txt",
        file_type="txt",
        size_bytes=1,
        processing_state=ProcessingState.parsed,
    )
    db_session.add(artifact)
    db_session.flush()
    return artifact


def _make_content(
    db_session: Session, artifact: Artifact, *, raw_text: str | None
) -> ArtifactContent:
    content = ArtifactContent(
        artifact_id=artifact.id,
        raw_text=raw_text,
        structure=None,
        embedded_metadata=None,
        parser_name="test",
        parser_version="1",
    )
    db_session.add(content)
    db_session.flush()
    return content


def _chunks(db_session: Session, artifact: Artifact) -> list[Chunk]:
    return list(
        db_session.scalars(
            select(Chunk).where(Chunk.artifact_id == artifact.id).order_by(Chunk.ordinal)
        ).all()
    )


def _embeddings(
    db_session: Session, artifact: Artifact, *, level: EmbeddingLevel | None = None
) -> list[Embedding]:
    stmt = select(Embedding).where(Embedding.artifact_id == artifact.id)
    if level is not None:
        stmt = stmt.where(Embedding.level == level)
    return list(db_session.scalars(stmt).all())


def _as_floats(vector: object) -> list[float]:
    return [float(x) for x in vector]  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Chunker: boundaries, overlap, empty/short text                             #
# --------------------------------------------------------------------------- #
def test_chunk_words_windows_with_overlap() -> None:
    text = " ".join(f"w{i}" for i in range(12))
    chunks = chunk_words(text, chunk_size=5, overlap=2)
    assert [c.split() for c in chunks] == [
        ["w0", "w1", "w2", "w3", "w4"],
        ["w3", "w4", "w5", "w6", "w7"],
        ["w6", "w7", "w8", "w9", "w10"],
        ["w9", "w10", "w11"],
    ]


def test_chunk_words_empty_text_returns_no_chunks() -> None:
    assert chunk_words("", chunk_size=100, overlap=10) == []
    assert chunk_words("   \n\t  ", chunk_size=100, overlap=10) == []


def test_chunk_words_short_text_is_a_single_chunk() -> None:
    text = "one two three"
    assert chunk_words(text, chunk_size=100, overlap=10) == [text]


def test_chunk_words_misconfigured_overlap_still_terminates() -> None:
    # overlap >= chunk_size would make (chunk_size - overlap) <= 0; the step
    # is clamped to 1 so this must still terminate rather than loop forever.
    text = " ".join(f"w{i}" for i in range(10))
    chunks = chunk_words(text, chunk_size=3, overlap=5)
    assert len(chunks) == 8


# --------------------------------------------------------------------------- #
# Structured tables are never chunked/shredded (PROJECTSPECS.md risk #6)     #
# --------------------------------------------------------------------------- #
def test_embed_artifact_does_not_shred_structured_tables(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=None)  # spreadsheets: Parse leaves raw_text=None
    db_session.add(
        StructuredTable(
            artifact_id=artifact.id,
            source="Costs",
            table_schema=["item", "amount"],
            rows=[{"item": "reagent A", "amount": 120}],
        )
    )
    db_session.commit()

    embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()

    assert _chunks(db_session, artifact) == []
    assert _embeddings(db_session, artifact) == []
    table = db_session.scalar(
        select(StructuredTable).where(StructuredTable.artifact_id == artifact.id)
    )
    assert table is not None
    assert table.rows == [{"item": "reagent A", "amount": 120}]  # untouched, still queryable


# --------------------------------------------------------------------------- #
# Chunk-level embeddings: count parity, vector shape, model/model_version    #
# --------------------------------------------------------------------------- #
def test_embed_artifact_chunk_count_matches_chunk_embedding_count(
    db_session: Session, project: Project
) -> None:
    text = " ".join(f"word{i}" for i in range(1000))  # spans multiple default-sized chunks
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=text)
    db_session.commit()

    provider = FakeEmbeddingProvider()
    changed = embed_artifact(db_session, artifact, provider=provider)
    db_session.commit()

    assert changed is True
    chunks = _chunks(db_session, artifact)
    chunk_embeddings = _embeddings(db_session, artifact, level=EmbeddingLevel.chunk)
    assert len(chunks) > 1
    assert len(chunk_embeddings) == len(chunks)
    assert {e.chunk_id for e in chunk_embeddings} == {c.id for c in chunks}
    for e in chunk_embeddings:
        assert len(_as_floats(e.vector)) == EMBEDDING_DIM
        assert e.model == provider.model
        assert e.model_version == provider.model_version


def test_reembed_replaces_chunks_wholesale_on_content_change(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, content_hash="h1")
    _make_content(db_session, artifact, raw_text="one two three")
    db_session.commit()
    embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()
    assert len(_chunks(db_session, artifact)) == 1

    content = db_session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    assert content is not None
    content.raw_text = " ".join(f"w{i}" for i in range(1000))
    artifact.content_hash = "h2"
    db_session.commit()

    embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()

    chunks = _chunks(db_session, artifact)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len(chunks) > 1  # no leftover single chunk from the previous, shorter text


# --------------------------------------------------------------------------- #
# Doc-level: exactly one per non-empty artifact; full-text vs. mean-pool     #
# --------------------------------------------------------------------------- #
def test_embed_artifact_exactly_one_doc_level_embedding(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text="A short note about the experiment.")
    db_session.commit()

    embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()

    doc_embeddings = _embeddings(db_session, artifact, level=EmbeddingLevel.doc)
    assert len(doc_embeddings) == 1
    assert doc_embeddings[0].chunk_id is None
    assert len(_as_floats(doc_embeddings[0].vector)) == EMBEDDING_DIM


def test_embed_artifact_doc_level_uses_full_text_when_within_word_budget(
    db_session: Session, project: Project
) -> None:
    text = "Short document, well within the doc-level word budget."
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=text)
    db_session.commit()

    provider = FakeEmbeddingProvider()
    embed_artifact(db_session, artifact, provider=provider)
    db_session.commit()

    doc_vector = _as_floats(_embeddings(db_session, artifact, level=EmbeddingLevel.doc)[0].vector)
    expected = provider.embed([text])[0]
    assert doc_vector == pytest.approx(expected, rel=1e-4)


def test_embed_artifact_doc_level_mean_pools_when_text_exceeds_word_budget(
    db_session: Session, project: Project
) -> None:
    text = " ".join(f"word{i}" for i in range(3_500))  # exceeds default embed_doc_max_words=3000
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=text)
    db_session.commit()

    provider = FakeEmbeddingProvider()
    embed_artifact(db_session, artifact, provider=provider)
    db_session.commit()

    chunk_vectors = [
        _as_floats(e.vector) for e in _embeddings(db_session, artifact, level=EmbeddingLevel.chunk)
    ]
    doc_vector = _as_floats(_embeddings(db_session, artifact, level=EmbeddingLevel.doc)[0].vector)

    assert doc_vector == pytest.approx(_expected_mean_pool(chunk_vectors), rel=1e-3)
    # sanity: the full-text path was NOT taken
    full_text_vector = provider.embed([text])[0]
    assert doc_vector != pytest.approx(full_text_vector, rel=1e-3)


# --------------------------------------------------------------------------- #
# Zero-text artifacts: skipped but the stage still completes                 #
# --------------------------------------------------------------------------- #
def test_embed_artifact_zero_text_produces_no_chunks_but_marks_stage_done(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text=None)  # e.g. scanned PDF / image, no EXIF text
    db_session.commit()

    changed = embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()

    assert changed is True
    assert _chunks(db_session, artifact) == []
    assert _embeddings(db_session, artifact) == []
    assert artifact.processing_state == ProcessingState.embedded

    state = db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == artifact.id, StageState.stage == Stage.embed
        )
    )
    assert state is not None
    assert state.status == StageStatus.done


def test_embed_artifact_missing_content_row_is_also_skipped_but_marked_done(
    db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.commit()  # no ArtifactContent at all (e.g. parse hasn't run yet)

    changed = embed_artifact(db_session, artifact, provider=FakeEmbeddingProvider())
    db_session.commit()

    assert changed is True
    assert _chunks(db_session, artifact) == []
    assert artifact.processing_state == ProcessingState.embedded


# --------------------------------------------------------------------------- #
# StageState idempotency + re-embed on model change                         #
# --------------------------------------------------------------------------- #
def test_reembed_unchanged_artifact_is_a_noop(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text="A short note.")
    db_session.commit()

    provider = FakeEmbeddingProvider()
    assert embed_artifact(db_session, artifact, provider=provider) is True
    db_session.commit()
    chunk_ids_before = {c.id for c in _chunks(db_session, artifact)}

    assert embed_artifact(db_session, artifact, provider=provider) is False
    db_session.commit()
    chunk_ids_after = {c.id for c in _chunks(db_session, artifact)}
    assert chunk_ids_before == chunk_ids_after  # rows untouched, not deleted + reinserted


def test_reembed_when_embedding_model_changes(db_session: Session, project: Project) -> None:
    artifact = _make_artifact(db_session, project)
    _make_content(db_session, artifact, raw_text="A short note.")
    db_session.commit()

    provider_a = FakeEmbeddingProvider(model="model-a")
    assert embed_artifact(db_session, artifact, provider=provider_a) is True
    db_session.commit()
    assert embed_artifact(db_session, artifact, provider=provider_a) is False  # unchanged, no-op
    db_session.commit()

    provider_b = FakeEmbeddingProvider(model="model-b")
    assert embed_artifact(db_session, artifact, provider=provider_b) is True  # model changed
    db_session.commit()

    for e in _embeddings(db_session, artifact):
        assert e.model == "model-b"
        assert e.model_version == "model-b"


def test_embed_project_skips_already_embedded_artifacts(
    db_session: Session, project: Project
) -> None:
    a1 = _make_artifact(db_session, project, content_hash="h0")
    a2 = _make_artifact(db_session, project, content_hash="h1")
    _make_content(db_session, a1, raw_text="Note one about the experiment.")
    _make_content(db_session, a2, raw_text="Note two about the results.")
    db_session.commit()

    provider = FakeEmbeddingProvider()
    first = embed_project(db_session, project.id, provider=provider)
    assert first.embedded == 2
    assert first.skipped == 0

    second = embed_project(db_session, project.id, provider=provider)
    assert second.embedded == 0
    assert second.skipped == 2

    for artifact in (a1, a2):
        state = db_session.scalar(
            select(StageState).where(
                StageState.artifact_id == artifact.id, StageState.stage == Stage.embed
            )
        )
        assert state is not None
        assert state.status == StageStatus.done


def test_embed_project_records_error_without_aborting_batch(
    db_session: Session, project: Project
) -> None:
    good = _make_artifact(db_session, project, content_hash="good")
    bad = _make_artifact(db_session, project, content_hash="bad")
    _make_content(db_session, good, raw_text="fine note")
    _make_content(db_session, bad, raw_text="this one will fail")
    db_session.commit()

    provider = RaisingEmbeddingProvider(fail_after=1)
    result = embed_project(db_session, project.id, provider=provider)

    assert result.embedded == 1
    assert len(result.errors) == 1

    failed_artifact = result.errors[0][0]
    state = db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == failed_artifact.id, StageState.stage == Stage.embed
        )
    )
    assert state is not None
    assert state.status == StageStatus.error
    assert state.error
    assert failed_artifact.processing_state == ProcessingState.error
