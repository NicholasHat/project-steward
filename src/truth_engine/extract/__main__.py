"""CLI: (re)extract dates and entities for every artifact in a project.

    uv run python -m truth_engine.extract --project-id <uuid>

Only artifacts whose content or extractor version changed since the last
successful extract are reprocessed — `StageState` gates idempotency. Usually
run after `python -m truth_engine.parse`; requires the spaCy model configured
via `Settings.spacy_model` to be installed:

    uv run python -m spacy download en_core_web_sm
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.extract.service import extract_project


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = extract_project(session, args.project_id)

    print(
        f"extract: {result.extracted} extracted, {result.skipped} skipped, "
        f"{len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
