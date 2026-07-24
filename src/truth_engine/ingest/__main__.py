"""CLI: ingest a folder into a project, then parse it (steps 1 + 2).

    uv run python -m truth_engine.ingest /path/to/folder \\
        --owner-id <existing-user-uuid> --project "My Project"

Owners are created through the auth API (`POST /auth/register`); this CLI
only manages projects and artifacts, mirroring the ergonomics of
`python -m truth_engine.db.seed`. Re-run freely — ingest dedupes by content
hash and parse skips artifacts whose content hasn't changed.
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.db.models import Project, User
from truth_engine.ingest.service import ingest_folder
from truth_engine.parse.service import parse_project


def _get_or_create_project(
    session: Session, owner_id: uuid.UUID, name: str, root_path: str
) -> Project:
    owner = session.get(User, owner_id)
    if owner is None:
        raise SystemExit(f"no user with id {owner_id} — create one via /auth/register first")
    project = session.scalar(
        select(Project).where(Project.owner_id == owner_id, Project.name == name)
    )
    if project is None:
        project = Project(owner_id=owner_id, name=name, root_path=root_path)
        session.add(project)
        session.flush()
    else:
        project.root_path = root_path
    return project


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("folder", type=Path, help="Folder to ingest.")
    parser.add_argument("--owner-id", required=True, type=uuid.UUID, help="Existing user UUID.")
    parser.add_argument("--project", required=True, help="Project name (created if new).")
    parser.add_argument("--no-parse", action="store_true", help="Ingest only; skip parsing.")
    args = parser.parse_args()

    folder = args.folder.resolve()
    if not folder.is_dir():
        raise SystemExit(f"not a directory: {folder}")

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        project = _get_or_create_project(session, args.owner_id, args.project, str(folder))
        session.commit()

        ingest_result = ingest_folder(session, project, folder)
        print(
            f"ingest: {ingest_result.created} created, {ingest_result.updated} updated, "
            f"{ingest_result.unchanged} unchanged, {len(ingest_result.errors)} errors "
            f"(project={project.id})"
        )
        for path, err in ingest_result.errors:
            print(f"  ERROR {path}: {err}")

        if not args.no_parse:
            parse_result = parse_project(session, project.id)
            print(
                f"parse: {parse_result.parsed} parsed, {parse_result.skipped} skipped, "
                f"{len(parse_result.errors)} errors"
            )
            for artifact, err in parse_result.errors:
                print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
