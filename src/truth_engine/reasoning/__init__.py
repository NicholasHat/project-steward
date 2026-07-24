"""Reasoning — provider adapters for the few LLM/embedding calls.

All model access goes through the adapter interfaces in `providers` so the
default deployment stays fully private (local Ollama LLM + local
sentence-transformers embeddings, no third-party egress), while a hosted model
can be opted into per-deployment via configuration.

LLM reasoning is reserved for judgment: direction/drift labeling, gap detection,
renaming suggestions, and report synthesis — never for reading raw files.
"""

from truth_engine.reasoning.providers import (
    EmbeddingProvider,
    LLMProvider,
    get_embedding_provider,
    get_llm_provider,
)

__all__ = [
    "EmbeddingProvider",
    "LLMProvider",
    "get_embedding_provider",
    "get_llm_provider",
]
