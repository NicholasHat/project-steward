"""Upload + run + status: the HTTP surface for the parts of PROJECTSPECS.md
§13 that, until now, only existed as CLIs -- `POST .../files` accepts a
folder's worth of files over multipart upload, `POST .../run` kicks off the
full pipeline (`truth_engine.pipeline.run_project_pipeline`) as a background
job, and `GET .../status` is what a dashboard polls while it runs.

**Uploads are the originals** (PROJECTSPECS.md §3.1): stored verbatim under
`Settings.data_root/{project_id}/`, filenames sanitized to a bare basename
(no path traversal, no absolute paths, nothing empty) before anything is
written. Nothing downstream ever rewrites or moves them -- `ingest_folder`
only reads them.

**Run tracking.** `PipelineRun` (`db.models`) is a small table purely for
this HTTP surface -- `run_project_pipeline` itself knows nothing about it.
One row per run attempt, `status` transitioning running -> done|error. A
partial unique index (`ix_pipeline_run_one_running_per_project`, see the
migration) makes "at most one running run per project" a database
invariant: the route's own pre-check query below is just the friendly-error
fast path, the index is what actually closes the race between two
concurrent `POST .../run` calls.

**Background execution, MVP scope.** The job runs via FastAPI
`BackgroundTasks` against its own sync `Session` (`SyncSessionFactoryDep` --
never the request's session, see `db/session.py`'s module docstring), so
`POST .../run` returns 202 immediately instead of blocking on minutes of
embedding/LLM calls. This is in-process, not a real queue: a run is tied to
the worker process that started it, so a process crash mid-run silently
orphans that run's row at `status=running` forever (no heartbeat, no
retry, no cross-process resumption). The concurrent-run guard itself *is*
correct even across multiple worker processes (it's a database constraint,
not an in-memory lock) -- it's only run *durability* that's the MVP
limitation. A real queue (RQ/Redis, the plan's documented v1 upgrade) is
the fix if that matters before then.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from truth_engine.analysis.report import current_report
from truth_engine.api.deps import (
    PipelineEmbeddingProviderDep,
    PipelineLLMProviderDep,
    ProjectDep,
    SyncSessionDep,
    SyncSessionFactoryDep,
)
from truth_engine.api.schemas import (
    ProjectStatusDTO,
    RunResponse,
    StageProgressDTO,
    UploadedFileDTO,
    UploadResponse,
)
from truth_engine.config import get_settings
from truth_engine.db.models import (
    Artifact,
    PipelineRun,
    PipelineRunStatus,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.pipeline import STAGE_ORDER, run_project_pipeline
from truth_engine.reasoning.providers import EmbeddingProvider, LLMProvider

router = APIRouter(prefix="/projects/{project_id}", tags=["pipeline"])


# --------------------------------------------------------------------------- #
# Upload                                                                      #
# --------------------------------------------------------------------------- #
def _sanitize_filename(raw: str) -> str:
    """Require a bare, safe basename and REJECT anything else. A traversal
    attempt like `../../etc/passwd` is a 422 — not a quietly basenamed
    `passwd` — so a caller never gets a surprising rename (or a collision
    with a real file of that basename). Rejects empty, `.`/`..`, and any name
    carrying a path separator."""
    if not raw or raw in (".", "..") or "/" in raw or "\\" in raw:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid filename: {raw!r}")
    return raw


def _write_upload(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"{dest.name} exceeds the {max_bytes}-byte limit",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    return size


@router.post("/files", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def upload_files(
    project: ProjectDep, session: SyncSessionDep, files: list[UploadFile] = File(...)
) -> UploadResponse:
    settings = get_settings()
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no files provided")
    if len(files) > settings.upload_max_files:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"at most {settings.upload_max_files} files per upload",
        )

    # Validate every filename before writing anything -- a path-traversal
    # attempt (or any other invalid name) anywhere in the batch rejects the
    # whole request, not just the offending file.
    names = [_sanitize_filename(f.filename or "") for f in files]

    project_dir = Path(settings.data_root) / str(project.id)
    project_dir.mkdir(parents=True, exist_ok=True)

    stored = [
        UploadedFileDTO(
            filename=name,
            size_bytes=_write_upload(upload, project_dir / name, settings.upload_max_file_bytes),
        )
        for upload, name in zip(files, names, strict=True)
    ]

    project.root_path = str(project_dir)
    session.commit()
    return UploadResponse(root_path=project.root_path, files=stored)


# --------------------------------------------------------------------------- #
# Run                                                                         #
# --------------------------------------------------------------------------- #
def _execute_pipeline_run(
    session_factory: Callable[[], AbstractContextManager[Session]],
    run_id: UUID,
    project_id: UUID,
    *,
    llm_provider: LLMProvider | None,
    embedding_provider: EmbeddingProvider | None,
) -> None:
    """Runs in the background (`BackgroundTasks`), against its own session
    (never the request's -- see module docstring). Every exception is caught:
    a background task that raises has nowhere useful to propagate to, so any
    failure -- including one `run_project_pipeline` itself didn't anticipate
    -- must land as `PipelineRun.status=error`, never a silently dropped task.
    """
    with session_factory() as session:
        run = session.get(PipelineRun, run_id)
        if run is None:  # defensive: the route just created this row
            return

        def on_stage_start(stage: Stage) -> None:
            run.current_stage = stage
            session.commit()

        try:
            result = run_project_pipeline(
                session,
                project_id,
                on_stage_start=on_stage_start,
                llm_provider=llm_provider,
                embedding_provider=embedding_provider,
            )
        except Exception as exc:  # noqa: BLE001 - background job: capture, never crash the worker
            session.rollback()  # discard any straggling uncommitted work from the failed stage
            run = session.get(PipelineRun, run_id)  # re-fetch: rollback expires prior references
            if run is not None:
                run.status = PipelineRunStatus.error
                run.error = str(exc)[:2000]
                run.current_stage = None
                run.finished_at = datetime.now(UTC)
                session.commit()
            return

        failed = result.failed
        run.status = PipelineRunStatus.error if failed else PipelineRunStatus.done
        run.error = f"{failed.stage.value}: {failed.error}" if failed else None
        run.current_stage = failed.stage if failed else None
        run.finished_at = datetime.now(UTC)
        session.commit()


@router.post("/run", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def run_pipeline(
    project: ProjectDep,
    session: SyncSessionDep,
    session_factory: SyncSessionFactoryDep,
    llm_provider: PipelineLLMProviderDep,
    embedding_provider: PipelineEmbeddingProviderDep,
    background_tasks: BackgroundTasks,
) -> RunResponse:
    existing = session.scalar(
        select(PipelineRun).where(
            PipelineRun.project_id == project.id, PipelineRun.status == PipelineRunStatus.running
        )
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pipeline already running for this project")

    run = PipelineRun(project_id=project.id, status=PipelineRunStatus.running)
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # Closes the race the pre-check above can't: two concurrent POSTs
        # both passing the check, both inserting -- the partial unique index
        # (see the migration) lets exactly one of them win.
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "pipeline already running for this project"
        ) from None
    session.refresh(run)

    background_tasks.add_task(
        _execute_pipeline_run,
        session_factory,
        run.id,
        project.id,
        llm_provider=llm_provider,
        embedding_provider=embedding_provider,
    )
    return RunResponse(run_id=run.id, status=run.status)


# --------------------------------------------------------------------------- #
# Status                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/status", response_model=ProjectStatusDTO)
def get_status(project: ProjectDep, session: SyncSessionDep) -> ProjectStatusDTO:
    run = session.scalar(
        select(PipelineRun)
        .where(PipelineRun.project_id == project.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(1)
    )

    artifact_count = (
        session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.project_id == project.id)
        )
        or 0
    )
    counts: dict[Stage, dict[StageStatus, int]] = defaultdict(dict)
    for stage, stage_status, n in session.execute(
        select(StageState.stage, StageState.status, func.count())
        .join(Artifact, Artifact.id == StageState.artifact_id)
        .where(Artifact.project_id == project.id)
        .group_by(StageState.stage, StageState.status)
    ).all():
        counts[stage][stage_status] = n

    stages: list[StageProgressDTO] = []
    for stage in STAGE_ORDER:
        if stage is Stage.report:
            # Project-level singleton, not per-artifact -- StageState isn't
            # written for this stage (see analysis/report.py).
            done = 1 if current_report(session, project.id) is not None else 0
            stages.append(
                StageProgressDTO(stage=stage, total=1, done=done, error=0, pending=1 - done)
            )
            continue
        done = counts[stage].get(StageStatus.done, 0)
        error = counts[stage].get(StageStatus.error, 0)
        stages.append(
            StageProgressDTO(
                stage=stage,
                total=artifact_count,
                done=done,
                error=error,
                pending=max(0, artifact_count - done - error),
            )
        )

    return ProjectStatusDTO(
        state=run.status if run is not None else PipelineRunStatus.idle,
        run_id=run.id if run is not None else None,
        current_stage=run.current_stage if run is not None else None,
        error=run.error if run is not None else None,
        started_at=run.started_at if run is not None else None,
        finished_at=run.finished_at if run is not None else None,
        stages=stages,
    )
