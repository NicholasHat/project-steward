"""Step 5 — Embed.

Produce chunk-level and doc-level (summary) embeddings via the local embedding
provider (reasoning.providers). Store model + model_version alongside each vector
so re-embeds are detectable when the model changes.

Failure modes: chunk boundaries vs. preserving structured tables, doc-summary
quality, embedding-model drift.

Pipeline logic is intentionally not implemented yet — this is scaffolding.
"""
