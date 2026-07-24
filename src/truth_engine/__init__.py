"""Project Truth Engine.

Ingests a messy folder of project artifacts and reconstructs a trustworthy,
self-updating picture of the project: what happened, in what order, what's
missing, and what the *current* direction actually is.

Package layout mirrors the pipeline (see PROJECTSPECS.md / CLAUDE.md):

    ingest    -> step 1   discover files, assign stable IDs, hash, dedupe
    parse     -> step 2   deterministic per-format extraction
    extract   -> step 3   NER, confidence-scored date resolution, tables
    store     -> step 4   persistence helpers over db.models
    embed     -> step 5   chunk + doc-level embeddings (local model)
    analysis  -> steps 6-10  timeline, domain/phase, direction/drift, gaps
    reasoning -> LLM + embedding provider adapters (judgment calls only)
    auth      -> users + per-user (owner_id) isolation
    api       -> FastAPI surface for the dashboard

Deterministic stages (parse/extract/embed) never dump raw files into an LLM;
LLM reasoning is reserved for judgment (direction, drift, gaps, renaming,
report). Every inferred fact is auditable and reversible.
"""

__version__ = "0.0.1"
