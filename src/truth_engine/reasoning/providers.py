"""Provider adapters for LLM completion and text embeddings.

Two small interfaces keep model access swappable and keep the *default*
deployment fully private:

  * LLMProvider       -> default OllamaProvider (local, no egress);
                         AnthropicProvider is an opt-in for when a small local
                         model isn't good enough (trades some privacy).
  * EmbeddingProvider -> default OllamaEmbeddingProvider (local nomic-embed-text,
                         no egress); SentenceTransformersEmbeddingProvider is an
                         in-process alternate for a CPU box that doesn't run Ollama.

Heavy dependencies (sentence-transformers) are imported lazily so the API and
migrations boot without the ML stack installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

import httpx

from truth_engine.config import Settings, get_settings


# --------------------------------------------------------------------------- #
# LLM                                                                          #
# --------------------------------------------------------------------------- #
class LLMProvider(ABC):
    """Judgment calls only: direction/drift labels, gaps, renaming, report."""

    model: str

    @property
    def model_version(self) -> str:
        return self.model

    @abstractmethod
    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        ...


class OllamaProvider(LLMProvider):
    """Local model via Ollama — the private default."""

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        payload: dict[str, object] = {"model": self.model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self._base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()["response"]


class AnthropicProvider(LLMProvider):
    """Opt-in hosted model (Claude). Only used when TRUTH_LLM_PROVIDER=anthropic.

    Sends content to a third party — a deliberate privacy trade for quality.
    """

    _API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("anthropic provider selected but TRUTH_ANTHROPIC_API_KEY is unset")
        self._api_key = api_key
        self.model = model

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        body: dict[str, object] = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self._API_URL, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]


def _build_llm(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "ollama":
            return OllamaProvider(settings.ollama_base_url, settings.llm_model)
        case "anthropic":
            return AnthropicProvider(settings.anthropic_api_key or "", settings.anthropic_model)
        case other:  # pragma: no cover - guarded by config
            raise ValueError(f"unknown llm_provider: {other!r}")


@lru_cache
def get_llm_provider() -> LLMProvider:
    return _build_llm(get_settings())


# --------------------------------------------------------------------------- #
# Embeddings                                                                   #
# --------------------------------------------------------------------------- #
class EmbeddingProvider(ABC):
    model: str
    dim: int

    @property
    def model_version(self) -> str:
        """Mirrors `LLMProvider.model_version`: the model name doubles as its
        own version tag. Doesn't catch a same-named model upgraded in place
        (e.g. a `nomic-embed-text` re-pull) — same documented limitation as
        `extract.service`'s spaCy-model versioning; pin the model in the
        deployment environment if this matters."""
        return self.model

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Local embeddings via Ollama — the private default (nomic-embed-text).

    IMPORTANT: nomic-embed-text natively supports an 8192-token context, but Ollama
    defaults to num_ctx=2048. We pass num_ctx explicitly so long doc-level summaries
    (which feed drift detection) aren't silently truncated before embedding. Do not
    drop this option without a matching model-context decision.
    """

    def __init__(self, base_url: str, model: str, dim: int, num_ctx: int) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self._num_ctx = num_ctx

    def embed(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{self._base_url}/api/embed",
                json={
                    "model": self.model,
                    "input": texts,
                    "options": {"num_ctx": self._num_ctx},
                },
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]


class SentenceTransformersEmbeddingProvider(EmbeddingProvider):
    """In-process HF embeddings (alternate: a CPU box that doesn't run Ollama).

    Set TRUTH_EMBEDDING_MODEL to the HF id (e.g. nomic-ai/nomic-embed-text-v1.5).
    max_seq_length is the sentence-transformers equivalent of Ollama's num_ctx.
    """

    def __init__(self, model: str, dim: int, num_ctx: int) -> None:
        self.model = model
        self.dim = dim
        self._num_ctx = num_ctx
        self._st = None

    def _ensure_loaded(self) -> None:
        if self._st is None:
            from sentence_transformers import SentenceTransformer  # lazy: pipeline extra

            self._st = SentenceTransformer(self.model, trust_remote_code=True)
            self._st.max_seq_length = self._num_ctx

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        assert self._st is not None
        return self._st.encode(texts, normalize_embeddings=True).tolist()


def _build_embedding(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case "ollama":
            return OllamaEmbeddingProvider(
                settings.ollama_base_url,
                settings.embedding_model,
                settings.embedding_dim,
                settings.embedding_num_ctx,
            )
        case "sentence_transformers":
            return SentenceTransformersEmbeddingProvider(
                settings.embedding_model, settings.embedding_dim, settings.embedding_num_ctx
            )
        case other:  # pragma: no cover - guarded by config
            raise ValueError(f"unknown embedding_provider: {other!r}")


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    return _build_embedding(get_settings())
