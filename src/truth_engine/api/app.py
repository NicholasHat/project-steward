"""FastAPI application factory.

Wires auth routes (register + JWT login) and a health check. The four product
surfaces (artifact browser, timeline, direction/drift, report) will be added as
routers under this app as the pipeline lands.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

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

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me", tags=["auth"])
    async def whoami(user: User = Depends(current_active_user)) -> dict[str, str]:
        return {"id": str(user.id), "email": user.email}

    return app


app = create_app()
