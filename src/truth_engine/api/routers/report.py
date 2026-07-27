"""The self-updating report (PROJECTSPECS.md §3.7/§13) — a pure read, no
human action defined for it in this increment. `current_report` (`analysis.
report`) is the exact "is_current" lookup the module already establishes;
this router doesn't reimplement it."""

from __future__ import annotations

from fastapi import APIRouter

from truth_engine.analysis.report import current_report
from truth_engine.api.deps import ProjectDep, SyncSessionDep
from truth_engine.api.schemas import ReportDTO, ReportResponse

router = APIRouter(prefix="/projects/{project_id}/report", tags=["report"])


@router.get("", response_model=ReportResponse)
def get_report(project: ProjectDep, session: SyncSessionDep) -> ReportResponse:
    report = current_report(session, project.id)
    return ReportResponse(report=ReportDTO.model_validate(report) if report else None)
