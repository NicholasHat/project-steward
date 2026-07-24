"""CLI: (re)parse every artifact in a project.

    uv run python -m truth_engine.parse --project-id <uuid>

Only artifacts whose content changed since the last successful parse are
reprocessed — `StageState` gates idempotency. Usually invoked indirectly via
`python -m truth_engine.ingest <folder> ...`, which runs this after ingest;
use this directly to re-parse an already-ingested project without touching
the filesystem.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.parse.service import parse_project


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = parse_project(session, args.project_id)

    print(f"parse: {result.parsed} parsed, {result.skipped} skipped, {len(result.errors)} errors")
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
