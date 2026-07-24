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

    # --- Embedding provider (default: local sentence-transformers) ---
    embedding_provider: str = Field(default="local", description="local | <future hosted>")
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = Field(
        default=768,
        description="Vector dimension. Must match embedding_model and the pgvector column.",
    )

    # --- LLM provider (default: local Ollama; no third-party egress) ---
    llm_provider: str = Field(default="ollama", description="ollama | anthropic")
    llm_model: str = "llama3.1:8b"
    ollama_base_url: str = "http://localhost:11434"
    # Only used when llm_provider == "anthropic" (opt-in, trades some privacy).
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (strips the +psycopg async marker is unnecessary;
        psycopg 3 is used for both sync and async)."""
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
