"""App-level startup behavior: orphaned-run reconciliation (app.reconcile_orphaned_runs)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from truth_engine.api.app import reconcile_orphaned_runs
from truth_engine.db.models import PipelineRun, PipelineRunStatus, Project


def test_reconcile_sweeps_running_but_leaves_finished_runs(
    db_session: Session, project: Project
) -> None:
    # A `running` row from a worker that has since died must be swept to error;
    # a `done` row is a real, finished run and must be left alone.
    running = PipelineRun(project_id=project.id, status=PipelineRunStatus.running)
    done = PipelineRun(
        project_id=project.id, status=PipelineRunStatus.done, finished_at=datetime.now(UTC)
    )
    db_session.add_all([running, done])
    db_session.commit()

    swept = reconcile_orphaned_runs(db_session)

    assert swept == 1
    db_session.refresh(running)
    db_session.refresh(done)
    assert running.status == PipelineRunStatus.error
    assert running.finished_at is not None
    assert running.error
    assert done.status == PipelineRunStatus.done  # untouched


def test_reconcile_is_a_noop_when_nothing_is_running(
    db_session: Session, project: Project
) -> None:
    assert reconcile_orphaned_runs(db_session) == 0
