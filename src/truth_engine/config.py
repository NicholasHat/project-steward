"""Application settings, loaded from environment / .env (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_name: str = "Project Truth Engine"
    debug: bool = False
    secret_key: str = Field(
        default="change-me-in-production",
        description="Signing key for auth tokens. MUST be overridden in production.",
    )

    # --- Database (async SQLAlchemy via psycopg 3) ---
    database_url: str = Field(
        default="postgresql+psycopg://steward:steward@localhost:5432/steward",
        description="Async SQLAlchemy URL. Alembic derives the sync URL from this.",
    )

    # --- Embedding provider (default: local, Ollama-served nomic-embed-text) ---
    # nomic-embed-text: 768-dim, natively 8192-token context (vs. bge-base's 512),
    # which the doc-level summary embeddings feeding drift detection actually need.
    embedding_provider: str = Field(
        default="ollama",
        description="ollama (local, no egress) | sentence_transformers (in-process HF)",
    )
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = Field(
        default=768,
        description="Vector dimension. Must match embedding_model and the pgvector column.",
    )
    # Ollama caps nomic-embed-text at 2048 tokens by default even though the model
    # supports 8192 — set this explicitly or long summaries get silently truncated.
    embedding_num_ctx: int = 8192

    # --- Embed (step 5): deterministic chunking + chunk/doc embeddings ---
    # Word-count based (not a real tokenizer) — a sensible target size without
    # adding a tokenizer dependency to a deterministic stage; token_count on
    # `Chunk` is likewise an estimate.
    embed_chunk_size_words: int = Field(
        default=400, description="Target chunk size in whitespace-delimited words."
    )
    embed_chunk_overlap_words: int = Field(
        default=60, description="Word overlap between consecutive chunks, for context continuity."
    )
    # Deliberately conservative vs. embedding_num_ctx (8192 tokens): Ollama
    # truncates silently past num_ctx rather than erroring, so a permissive
    # threshold risks a valid-looking, silently-truncated doc vector. Budgets
    # ~2 tokens/word (dense technical/chemistry text tokenizes sub-word more
    # than everyday English) against ~70% of num_ctx, leaving headroom.
    embed_doc_max_words: int = Field(
        default=3_000,
        description="Doc-level embedding uses the full raw_text only at/below this word count; "
        "otherwise it mean-pools + renormalizes the chunk vectors instead.",
    )

    # --- Extract (step 3): spaCy NER + dateparser + rules, deterministic ---
    # en_core_web_sm ships with `pipeline` and needs no extra download beyond
    # `spacy download`; en_core_web_trf (transformer, higher quality) is a drop-in
    # config swap but pulls torch and is much slower — opt in per-environment.
    spacy_model: str = Field(
        default="en_core_web_sm",
        description="spaCy pipeline for NER. en_core_web_trf available for higher "
        "quality at the cost of a torch dependency and slower inference.",
    )
    # Bounds spaCy's per-artifact cost on very large documents; raw_text beyond
    # this is not scanned for entities/dates (still fully covered by doc-meta and
    # filesystem signals).
    extract_max_text_chars: int = 200_000

    # --- LLM provider (default: local Ollama; no third-party egress) ---
    llm_provider: str = Field(default="ollama", description="ollama | anthropic")
    llm_model: str = "llama3.1:8b"
    ollama_base_url: str = "http://localhost:11434"
    # Only used when llm_provider == "anthropic" (opt-in, trades some privacy).
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # --- Analysis: Domain -> Phase (step 7), PROJECTSPECS.md §3.3 ---
    # Below this, DomainClassification still records the LLM's honest best
    # guess + confidence, but phase assignment maps artifacts against the
    # "generic" template instead of forcing a possibly-wrong domain-specific
    # one (§3.3.4 — never force a bad fit, but don't fake certainty either).
    domain_confidence_threshold: float = Field(
        default=0.5,
        description="DomainClassification.confidence below this falls back to the generic "
        "phase template for phase assignment.",
    )
    # Corpus-level fingerprint fed to the domain-classification prompt: cheap
    # signals only (filenames, structure headings/titles, short excerpts,
    # recurring entities) — never the raw corpus text.
    domain_fingerprint_max_artifacts: int = Field(
        default=40,
        description="Cap on artifacts sampled into the domain-classification fingerprint.",
    )
    domain_fingerprint_snippet_chars: int = Field(
        default=200, description="Leading raw_text snippet length per artifact in that fingerprint."
    )
    domain_fingerprint_max_entities: int = Field(
        default=20,
        description="Top recurring entities (by mention count) included in the fingerprint.",
    )
    # Per-artifact phase-assignment prompts: same "compact fingerprint, no
    # raw-corpus dump" discipline, batched to bound total LLM call count.
    phase_assignment_snippet_chars: int = Field(
        default=500,
        description="Leading raw_text snippet length per artifact in phase-assignment prompts.",
    )
    phase_assignment_batch_size: int = Field(
        default=5, description="Artifacts grouped per phase-assignment LLM call."
    )

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (strips the +psycopg async marker is unnecessary;
        psycopg 3 is used for both sync and async)."""
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
