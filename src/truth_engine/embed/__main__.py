"""CLI: (re)chunk and embed every artifact in a project.

    uv run python -m truth_engine.embed --project-id <uuid>

Only artifacts whose content, the embedding model, or the chunker version
changed since the last successful embed are reprocessed — `StageState` gates
idempotency. Usually run after `python -m truth_engine.parse` (embedding does
not require extract to have run first). Requires the embedding provider
configured via `Settings.embedding_provider` to be reachable — the default is
a local Ollama serving `nomic-embed-text`:

    uv run ollama pull nomic-embed-text
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.embed.service import embed_project


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = embed_project(session, args.project_id)

    print(
        f"embed: {result.embedded} embedded, {result.skipped} skipped, "
        f"{len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
