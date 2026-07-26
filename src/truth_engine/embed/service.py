"""Stage 5 orchestration: deterministic chunking (`embed.chunker`) + chunk-
and doc-level embeddings via the local embedding provider
(`reasoning.providers.get_embedding_provider`), persisted as `Chunk` /
`Embedding`, gated by `StageState`.

**Doc-level strategy:** embed the full `raw_text` in one call when it fits
comfortably inside the provider's configured context window
(`Settings.embed_doc_max_words` — deliberately conservative relative to
`Settings.embedding_num_ctx`; see CLAUDE.md's Ollama num_ctx Gotcha: text
that overflows num_ctx is truncated *silently*, not rejected, so an over-
generous threshold risks a valid-looking, silently-wrong doc vector).
Otherwise, mean-pool and renormalize the already-computed chunk vectors — no
second embedding call, stays in the same fixed-size vector space, and is the
standard way to represent a long document from its parts. Deliberately not
an LLM summary: the deterministic/LLM boundary (CLAUDE.md) reserves LLM
reasoning for the analysis stages, and this stage must stay fully local and
auditable.

No `DecisionAudit` rows: embeddings are derived vectors, not inferred facts
(mirrors parse's precedent, not extract's — see `parse.service` docstring).

Idempotency: `StageState(artifact_id, stage=embed).input_hash` folds in
`content_hash`, the embedding provider's `model` + `model_version`, the
chunk-size/overlap settings, and `CHUNKER_VERSION` — changing any of them
(model swap, chunk-size retune, or a chunker algorithm bump) invalidates
cached state and re-embeds, mirroring extract's `spacy_model` +
`RULESET_VERSION` key (see `extract.service` docstring).

Zero-text artifacts (scanned PDFs, images with no EXIF text, empty files)
produce no chunks and no embeddings but are still marked `done` — nothing to
embed is not the same as pending or failed.

Failure isolation and the "replace this artifact's rows wholesale on re-run"
pattern mirror `parse.service` / `extract.service`.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    Chunk,
    Embedding,
    EmbeddingLevel,
    ProcessingState,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.embed.chunker import chunk_words
from truth_engine.reasoning.providers import EmbeddingProvider, get_embedding_provider

# Bump when `chunk_words`'s windowing algorithm changes materially enough
# that already-embedded artifacts should be reprocessed.
CHUNKER_VERSION = "1"


@dataclass
class EmbedResult:
    embedded: int = 0
    skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _input_hash(artifact: Artifact, settings: Settings, provider: EmbeddingProvider) -> str:
    raw = (
        f"{artifact.content_hash}:{provider.model}:{provider.model_version}:"
        f"{settings.embed_chunk_size_words}:{settings.embed_chunk_overlap_words}:"
        f"{CHUNKER_VERSION}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _mean_pool_normalize(vectors: list[list[float]]) -> list[float]:
    """Elementwise mean of `vectors`, renormalized to unit length — the
    doc-level fallback for text too long to embed whole."""
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i, x in enumerate(vector):
            sums[i] += x
    mean = [s / len(vectors) for s in sums]
    norm = math.sqrt(sum(x * x for x in mean))
    return [x / norm for x in mean] if norm else mean


def embed_artifact(
    session: Session, artifact: Artifact, *, provider: EmbeddingProvider | None = None
) -> bool:
    """Chunk + embed one artifact if its content, the embedding model, or the
    chunker version changed since the last successful run. Returns True if
    (re)embedded, False if skipped as already current.

    `provider` defaults to `get_embedding_provider()`; callers pass one
    explicitly to inject a stub in tests (kept hermetic/offline) or to reuse
    one provider instance across a batch.
    """
    settings = get_settings()
    provider = provider or get_embedding_provider()
    input_hash = _input_hash(artifact, settings, provider)
    state = _stage_state(session, artifact.id, Stage.embed)
    if state and state.status == StageStatus.done and state.input_hash == input_hash:
        return False

    content = session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    raw_text = content.raw_text if content else None

    _replace_chunks_and_embeddings(session, artifact, raw_text, settings, provider)

    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.embed, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None

    artifact.processing_state = ProcessingState.embedded
    return True


def embed_project(
    session: Session, project_id: uuid.UUID, *, provider: EmbeddingProvider | None = None
) -> EmbedResult:
    """Embed every artifact in a project, skipping ones already up to date."""
    result = EmbedResult()
    provider = provider or get_embedding_provider()
    artifacts = session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()

    for artifact in artifacts:
        try:
            changed = embed_artifact(session, artifact, provider=provider)
        except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
            _record_error(session, artifact, str(exc), provider)
            result.errors.append((artifact, str(exc)))
            continue

        session.commit()
        result.embedded += 1 if changed else 0
        result.skipped += 0 if changed else 1

    return result


def _replace_chunks_and_embeddings(
    session: Session,
    artifact: Artifact,
    raw_text: str | None,
    settings: Settings,
    provider: EmbeddingProvider,
) -> None:
    # Wholesale replace, mirroring parse/extract: the source of truth is
    # always the current raw_text, never a diff against the previous run.
    session.execute(
        delete(Embedding).where(
            Embedding.artifact_id == artifact.id, Embedding.level == EmbeddingLevel.doc
        )
    )
    # Chunk-level embeddings cascade-delete via Embedding.chunk_id's
    # ON DELETE CASCADE — no separate delete needed for those.
    session.execute(delete(Chunk).where(Chunk.artifact_id == artifact.id))

    chunk_texts = chunk_words(
        raw_text or "",
        chunk_size=settings.embed_chunk_size_words,
        overlap=settings.embed_chunk_overlap_words,
    )
    if not chunk_texts:
        return  # zero-text artifact: no chunks, no embeddings; caller still marks the stage done

    chunk_rows = [
        Chunk(artifact_id=artifact.id, ordinal=i, text=text, token_count=len(text.split()))
        for i, text in enumerate(chunk_texts)
    ]
    session.add_all(chunk_rows)
    session.flush()  # populate chunk_rows[*].id for the embeddings' FK

    full_doc_text = (
        raw_text if raw_text is not None and len(raw_text.split()) <= settings.embed_doc_max_words
        else None
    )
    texts_to_embed = chunk_texts + ([full_doc_text] if full_doc_text is not None else [])
    vectors = provider.embed(texts_to_embed)
    chunk_vectors = vectors[: len(chunk_texts)]
    doc_vector = vectors[-1] if full_doc_text is not None else _mean_pool_normalize(chunk_vectors)

    for row, vector in zip(chunk_rows, chunk_vectors, strict=True):
        session.add(
            Embedding(
                artifact_id=artifact.id,
                chunk_id=row.id,
                level=EmbeddingLevel.chunk,
                vector=vector,
                model=provider.model,
                model_version=provider.model_version,
            )
        )

    session.add(
        Embedding(
            artifact_id=artifact.id,
            chunk_id=None,
            level=EmbeddingLevel.doc,
            vector=doc_vector,
            model=provider.model,
            model_version=provider.model_version,
        )
    )


def _record_error(
    session: Session, artifact: Artifact, message: str, provider: EmbeddingProvider
) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.embed)
    if state is None:
        state = StageState(
            artifact_id=artifact.id,
            stage=Stage.embed,
            input_hash=_input_hash(artifact, get_settings(), provider),
        )
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    artifact.processing_state = ProcessingState.error
    session.commit()
