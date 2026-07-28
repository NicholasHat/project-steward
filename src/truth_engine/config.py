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

    # --- CORS (api/app.py) ---
    # Origins allowed to call the API with credentials (Bearer tokens, not
    # cookies, but the browser still enforces CORS on the Authorization
    # header + fetch). Vite's dev server default port is included so the
    # frontend/ dashboard works out of the box in local dev; override with a
    # comma-separated list in production.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="Allowed CORS origins for the dashboard frontend.",
    )

    # --- Uploaded artifact storage (api/routers/pipeline.py) ---
    # `./data` for bare local dev; docker-compose.yml mounts the `artifacts`
    # volume at `/data` and sets TRUTH_DATA_ROOT=/data for the containerized app.
    # Files land at `{data_root}/{project_id}/` -- these are the sacrosanct
    # originals (PROJECTSPECS.md §3.1); nothing downstream ever writes here.
    data_root: str = Field(
        default="./data", description="Root directory for per-project uploaded originals."
    )
    upload_max_files: int = Field(
        default=200, description="Max files accepted in a single upload call."
    )
    upload_max_file_bytes: int = Field(
        default=100_000_000, description="Max size of a single uploaded file, in bytes."
    )

    # --- Parse (step 2): spreadsheet/table -> text projection ---
    # xlsx/csv keep their lossless content in `StructuredTable`, but every
    # analysis stage (embed, NER, date resolution, phase, direction/drift)
    # keys off `raw_text` — a table with `raw_text=None` is invisible to all
    # of them, which for a spreadsheet-heavy corpus means most of the project
    # never reaches the dashboard. The spreadsheet handlers synthesize a
    # compact textual projection (filename + sheet names + column headers + a
    # bounded row sample) as `raw_text` so tables participate in analysis like
    # prose documents; the `StructuredTable` rows remain the lossless record.
    parse_table_text_max_rows_per_table: int = Field(
        default=50,
        description="Max data rows per sheet/table sampled into the synthesized text projection. "
        "A representative sample carries the file's topic (for clustering/phase assignment) "
        "without embedding thousands of numeric cells.",
    )
    parse_table_text_max_chars: int = Field(
        default=20_000,
        description="Hard cap on the synthesized table-text projection length, in characters.",
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
    # Plausibility bounds on *content-derived* dates (spaCy DATE + dateparser).
    # spaCy tags bare 4-digit tokens as DATE regardless of meaning, so a product
    # or model number ("Nvidia 5090"), an address, or a large quantity becomes a
    # year — and, as a max-trust content signal, wins the chosen date. A single
    # such artifact dated year 5090 then becomes the corpus's "latest" date and
    # poisons every recency-window comparison downstream (timeline span,
    # direction/drift). A content date outside this range is treated as a parsing
    # artifact and dropped; doc-meta/filesystem timestamps (real dates by
    # construction) are never clamped. 2100 is a fixed, deterministic ceiling —
    # a genuine research-timeline date past it is not a case worth admitting the
    # false positives for.
    extract_min_year: int = Field(
        default=1970, description="Content dates before this year are dropped as parsing artifacts."
    )
    extract_max_year: int = Field(
        default=2100, description="Content dates after this year are dropped as parsing artifacts."
    )

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

    # --- Analysis: citation/reference graph (step 8, Signal B), PROJECTSPECS.md §3.4 ---
    # Deterministic, precision-favoring per Open Risk #3 -- these knobs exist to keep
    # short/generic strings from becoming false-positive match keys.
    graph_min_match_key_chars: int = Field(
        default=6,
        description="Filenames/headings shorter than this are too generic to use as a "
        "reference match key (e.g. 'a.txt', 'Notes') and are excluded from the index.",
    )
    graph_max_shared_entity_artifacts: int = Field(
        default=4,
        description="An entity mentioned across more than this many distinct artifacts is "
        "treated as common/generic (e.g. a recurring collaborator or the project name) "
        "rather than a distinctive cross-reference, and contributes no graph edges.",
    )
    graph_confidence_floor: float = Field(
        default=0.5,
        description="Candidate relationship edges below this confidence are dropped rather "
        "than persisted as low-confidence noise.",
    )

    # --- Analysis: direction & drift (steps 8-9, Signal A + Signal B), PROJECTSPECS.md §3.4 ---
    # Signal A (embedding cluster-drift): below this many doc-level embeddings,
    # clustering a handful of docs is meaningless (HDBSCAN may call everything
    # noise) -- degrade to an honest recency-only fallback instead of fabricating
    # drift (Open Risk #4).
    direction_min_corpus_size: int = Field(
        default=6,
        description="Below this many doc-level embeddings, skip HDBSCAN clustering entirely "
        "and fall back to a conservative recency-only Signal A.",
    )
    direction_min_cluster_size: int = Field(
        default=2, description="HDBSCAN min_cluster_size for the Signal A clustering pass."
    )
    # Shared "what counts as recent" anchor for both signals: a cluster/artifact/
    # citation dated within this many days of the corpus's latest chosen date is
    # part of the current-direction window.
    direction_recency_window_days: int = Field(
        default=90,
        description="Days before the corpus's latest resolved date that still count as "
        "'recent' -- defines the current-direction centroid (Signal A), a cluster going "
        "'quiet', and citation-recency decay (Signal B).",
    )
    # Transparent combining-rule thresholds (documented, not learned) -- see
    # analysis/direction.py module docstring for the full rule.
    direction_current_threshold: float = Field(
        default=0.6, description="Combined score >= this -> label current."
    )
    direction_superseded_threshold: float = Field(
        default=0.4, description="Combined score <= this -> label superseded (drift)."
    )
    direction_min_confident_label: float = Field(
        default=0.3,
        description="Combined-signal confidence below this always labels unclear, regardless "
        "of the score -- covers missing signals and signal_a/signal_b disagreement alike.",
    )
    direction_small_corpus_confidence_cap: float = Field(
        default=0.35,
        description="Confidence ceiling when Signal A is in the small-corpus recency-only "
        "fallback. Combined with direction_min_confident_label, this also means 'superseded' "
        "is never asserted below direction_min_corpus_size -- claiming a dead end from too "
        "little data is a stronger, riskier claim than 'this still looks current'.",
    )
    direction_single_signal_availability: float = Field(
        default=0.6,
        description="Confidence multiplier applied when only one of signal_a/signal_b is "
        "available for an artifact (the other has no data), vs. both agreeing.",
    )
    direction_snapshot_recent_artifacts: int = Field(
        default=8,
        description="Cap on recent-window artifacts fed to the DirectionSnapshot summary prompt.",
    )
    direction_summary_snippet_chars: int = Field(
        default=200,
        description="Leading raw_text snippet length per artifact in the summary prompt.",
    )

    # --- Analysis: gap detection (step 10), PROJECTSPECS.md §3.5 ---
    # Structural gaps (deterministic): a selected-template phase with fewer than
    # this many distinctly-covered artifacts (auto or human PhaseAssignment
    # alike -- a human correction counts as coverage too) is flagged. Zero
    # coverage and "a few" are both gaps but distinguished by confidence below.
    gap_structural_few_threshold: int = Field(
        default=2,
        description="A phase with fewer than this many mapped artifacts is flagged as a "
        "structural gap ('few'); zero always qualifies regardless of this setting.",
    )
    gap_structural_confidence_zero: float = Field(
        default=0.9, description="Gap.confidence for a phase with zero mapped artifacts."
    )
    gap_structural_confidence_few: float = Field(
        default=0.7,
        description="Gap.confidence for a phase with 1..gap_structural_few_threshold-1 mapped "
        "artifacts -- still a structural gap, slightly less stark than zero coverage. Both "
        "structural bands stay above every promised-unfulfilled band (see below), per "
        "PROJECTSPECS.md §3.5's 'different confidence levels of gap'.",
    )

    # Promised-but-unfulfilled gaps (content-level). Candidate detection is
    # deterministic marker matching; fulfillment judgment is fuzzy (a
    # deterministic keyword-overlap fallback, optionally refined by a batched
    # LLM pass) -- see analysis/gaps.py module docstring for the full rule.
    gap_promised_evidence_radius: int = Field(
        default=200,
        description="Cap on chars of surrounding text captured on each side of a matched "
        "promise marker if no sentence boundary is found sooner -- the marker's own "
        "containing sentence(s) are preferred over this fixed radius when punctuation is "
        "present (see analysis/gaps.py's `_sentence_span`).",
    )
    gap_promised_merge_gap_chars: int = Field(
        default=20,
        description="Two marker matches within this many characters of each other in the same "
        "artifact are treated as one promise candidate, not two.",
    )
    gap_promised_fulfillment_token_overlap: float = Field(
        default=0.6,
        description="Deterministic fulfillment fallback: a promise is considered fulfilled by "
        "a later artifact whose text contains at least this fraction of the promise's "
        "significant (non-boilerplate) tokens.",
    )
    gap_promised_llm_enabled: bool = Field(
        default=True,
        description="Refine the deterministic fulfillment heuristic with a batched LLM "
        "judgment pass. Disable for a fully deterministic (lower-recall, zero-LLM-cost) pass.",
    )
    gap_promised_batch_size: int = Field(
        default=8, description="Promise candidates grouped per fulfillment-judgment LLM call."
    )
    gap_promised_later_artifacts_per_candidate: int = Field(
        default=5,
        description="Cap on candidate later-artifact excerpts shown to the LLM per promise, "
        "ranked by chosen date (soonest-after first).",
    )
    gap_promised_snippet_chars: int = Field(
        default=300,
        description="Leading raw_text snippet length per later-artifact excerpt in the "
        "fulfillment-judgment prompt.",
    )
    gap_promised_confidence_no_later_artifacts: float = Field(
        default=0.55,
        description="Gap.confidence when no later (or, absent a source date, no other) "
        "artifact exists in the project at all to check -- the simplest, least ambiguous "
        "'unfulfilled' case, but still below every structural band.",
    )
    gap_promised_confidence_deterministic: float = Field(
        default=0.35,
        description="Gap.confidence when only the deterministic token-overlap heuristic judged "
        "the promise unfulfilled (LLM pass disabled, or its response was unusable).",
    )
    gap_promised_confidence_llm_cap: float = Field(
        default=0.65,
        description="Ceiling on Gap.confidence for an LLM-judged unfulfilled promise -- keeps "
        "the entire promised-unfulfilled band below gap_structural_confidence_few, so the two "
        "gap kinds never overlap in confidence.",
    )

    # --- Analysis: view/projection (step 11), PROJECTSPECS.md §3.6 ---
    # Renaming suggestions are LLM-partial: a batched pass proposes a
    # descriptive slug; a deterministic fallback (date + cleaned original
    # filename + top entity) is always available and used when the pass is
    # disabled or its output doesn't parse/validate.
    view_llm_naming_enabled: bool = Field(
        default=True,
        description="Refine the deterministic name fallback with a batched LLM slug-suggestion "
        "pass. Disable for a fully deterministic (zero-LLM-cost) naming pass.",
    )
    view_name_batch_size: int = Field(
        default=5, description="Artifacts grouped per naming-suggestion LLM call."
    )
    view_name_snippet_chars: int = Field(
        default=300,
        description="Leading raw_text snippet length per artifact in the naming prompt.",
    )
    view_name_top_entities: int = Field(
        default=3,
        description="Top per-artifact entities (by mention count) surfaced in the naming prompt "
        "and available to the deterministic fallback (which uses only the single top entity).",
    )
    view_name_max_length: int = Field(
        default=80,
        description="Hard cap on suggested_name length (date prefix + slug + extension). The "
        "slug is trimmed to fit; the date prefix and extension are never truncated.",
    )
    view_name_min_slug_chars: int = Field(
        default=3,
        description="An LLM-proposed slug that normalizes to fewer than this many characters "
        "is treated as junk and the deterministic fallback is used instead.",
    )
    view_generic_category: str = Field(
        default="Uncategorized",
        description="suggested_category for an artifact with no PhaseAssignment (unphased, or "
        "phases hasn't run yet) — the domain-generic bucket §3.6 asks for.",
    )
    view_undated_folder: str = Field(
        default="undated",
        description="Date-folder segment (in virtual_path) and date prefix (in suggested_name) "
        "used when an artifact has no chosen ResolvedDate.",
    )

    # --- Analysis: self-updating report (step 12), PROJECTSPECS.md §3.7-3.8 ---
    # Recent activity is deterministic (placement TimelineEvents), capped for
    # readability; the current-direction narrative is the one LLM-partial
    # section, refining DirectionSnapshot.inferred_direction_summary + the
    # current-labeled artifact set into report-voice prose -- see
    # analysis/report.py module docstring for the full section/fingerprint
    # design.
    report_recent_activity_count: int = Field(
        default=10,
        description="Most recent placement TimelineEvents surfaced in the report's Recent "
        "Activity section.",
    )
    report_llm_direction_enabled: bool = Field(
        default=True,
        description="Synthesize the Current Direction section's narrative with an LLM call over "
        "DirectionSnapshot.inferred_direction_summary + current-labeled artifacts. Disable for a "
        "fully deterministic (zero-LLM-cost) report -- the section still renders the direction "
        "snapshot's own narrative directly.",
    )
    report_direction_max_current_artifacts: int = Field(
        default=8,
        description="Cap on current-labeled artifacts (most-recently-dated first) fed to the "
        "Current Direction synthesis prompt and listed in that section.",
    )
    report_direction_snippet_chars: int = Field(
        default=200,
        description="Leading raw_text snippet length per current artifact in the Current "
        "Direction synthesis prompt.",
    )

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (strips the +psycopg async marker is unnecessary;
        psycopg 3 is used for both sync and async)."""
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
