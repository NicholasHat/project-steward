"""FastAPI application factory.

Wires auth routes (register + JWT login), a health check, and the data +
human-action routers that expose the pipeline's outputs to the dashboard
(PROJECTSPECS.md §2 step 13): artifact browser, timeline, direction/drift,
gaps, phases/domain, and report — see `api/routers/*.py`. `pipeline.router`
adds the upload/run/status surface (`api/routers/pipeline.py`) that lets a
user drive ingestion and the full pipeline over HTTP instead of only via the
CLIs (`uv run python -m truth_engine.<stage>`), which remain useful for
scripting/dev but are no longer the only way in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.api.routers import (
    artifacts,
    direction,
    gaps,
    phases,
    pipeline,
    projects,
    report,
    timeline,
)
from truth_engine.auth.schemas import UserCreate, UserRead
from truth_engine.auth.users import auth_backend, current_active_user, fastapi_users
from truth_engine.config import get_settings
from truth_engine.db.models import PipelineRun, PipelineRunStatus, User
from truth_engine.db.session import get_sync_sessionmaker


def reconcile_orphaned_runs(session: Session) -> int:
    """Mark any run still `running` as errored, returning how many were swept.

    A pipeline run is in-process (`api/routers/pipeline.py`): it's tied to the
    worker that started it, with no heartbeat or cross-process resumption. So
    on a fresh process start, anything still `running` in the table can't
    actually be running -- the worker that owned it is gone -- and would
    otherwise sit at `running` forever, blocking new runs via the one-running-
    per-project index. This assumes a single app process (which the deploy
    uses); with multiple workers a run another worker just started could be
    swept, so a real fix here is a job queue with heartbeats.
    """
    orphaned = session.scalars(
        select(PipelineRun).where(PipelineRun.status == PipelineRunStatus.running)
    ).all()
    now = datetime.now(UTC)
    for run in orphaned:
        run.status = PipelineRunStatus.error
        run.error = "run interrupted by a server restart"
        run.current_stage = None
        run.finished_at = now
    session.commit()
    return len(orphaned)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    with get_sync_sessionmaker()() as session:
        reconcile_orphaned_runs(session)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.0.1", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(
        fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"]
    )
    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"]
    )

    for router in (
        projects.router,
        artifacts.router,
        timeline.router,
        direction.router,
        gaps.router,
        phases.router,
        report.router,
        pipeline.router,
    ):
        app.include_router(router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me", tags=["auth"])
    async def whoami(user: User = Depends(current_active_user)) -> dict[str, str]:
        return {"id": str(user.id), "email": user.email}

    return app


app = create_app()
