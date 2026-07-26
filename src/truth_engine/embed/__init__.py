"""Step 5 — Embed (deterministic chunking + local chunk/doc embeddings).

Chunk each artifact's `ArtifactContent.raw_text` into overlapping word-window
chunks (`embed.chunker`) and embed them via the local embedding provider
(`reasoning.providers.get_embedding_provider` — never call Ollama directly).
Persists `Chunk` + chunk-level `Embedding` rows, plus exactly one doc-level
`Embedding` per non-empty artifact (full text when it fits the provider's
context window, else mean-pooled + renormalized chunk vectors — see
`embed.service` for the full rationale).

The chunker only ever reads `raw_text`; it never touches `StructuredTable`.
Spreadsheets/tables already live there as queryable data (Parse leaves
`raw_text=None` for those formats), so there is nothing for this stage to
flatten or shred (PROJECTSPECS.md open risk #6) without any special-casing
here — see `embed.chunker` for the per-format detail.

Deterministic, local, no egress — no LLM calls, no `DecisionAudit` rows
(derived vectors, not inferred judgments; mirrors parse's precedent).
`StageState(stage=embed)` gates idempotent incremental reprocessing; every
`Embedding` stores `model` + `model_version` so a model swap is detectable
and triggers a re-embed.

Failure modes designed against: chunk boundaries vs. preserving structured
tables (chunker never sees tables at all), doc-level quality on long text
(mean-pool fallback rather than silent truncation), embedding-model drift
(model/model_version folded into `StageState.input_hash`).
"""

from __future__ import annotations

from truth_engine.embed.service import EmbedResult, embed_artifact, embed_project

__all__ = ["EmbedResult", "embed_artifact", "embed_project"]
