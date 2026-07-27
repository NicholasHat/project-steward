"""FastAPI application factory.

Wires auth routes (register + JWT login), a health check, and the data +
human-action routers that expose the pipeline's outputs to the dashboard
(PROJECTSPECS.md §2 step 13): artifact browser, timeline, direction/drift,
gaps, phases/domain, and report — see `api/routers/*.py`. Upload and
pipeline-run orchestration over HTTP are out of scope for this increment; the
CLIs (`uv run python -m truth_engine.<stage>`) run the pipeline, and these
routers read/act on what it produced.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from truth_engine.api.routers import artifacts, direction, gaps, phases, projects, report, timeline
from truth_engine.auth.schemas import UserCreate, UserRead
from truth_engine.auth.users import auth_backend, current_active_user, fastapi_users
from truth_engine.config import get_settings
from truth_engine.db.models import User


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.0.1")

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
