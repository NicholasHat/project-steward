"""Full-pipeline orchestrator (PROJECTSPECS.md §2): chains every stage
service, in dependency order, into one call -- ingest -> parse -> extract ->
embed -> timeline -> phases -> graph -> direction -> gaps -> view -> report.

Each stage function already exists, is independently idempotent via its own
`StageState` gating (or, for `view`/`direction`, its human-override check),
and already isolates per-*artifact* failures internally (one bad file never
sinks the rest of a stage). This module adds nothing to that -- it just
calls the eleven public stage entrypoints in the order the later ones'
inputs require, so a caller (today: the API's `POST /projects/{id}/run`
background job; potentially a future single-command CLI) doesn't have to
hand-chain them itself.

**Stage-level failure handling.** A stage function raising (as opposed to
recording a per-artifact error in its own result object) means that stage's
own precondition failed outright -- an unreachable embedding provider, a
missing project, a folder that no longer exists. Every downstream stage
depends on this one's output, so the chain stops at the first such failure
rather than ploughing on against data the failed stage never produced;
earlier stages' work is untouched (each already committed before returning).
`PipelineResult.failed` names exactly which stage and why.

**Providers.** `llm_provider`/`embedding_provider` are threaded through to
every stage that accepts one, mirroring `run_project_phases`'s own
`provider: LLMProvider | None = None` convention one level up: `None` means
"let the stage resolve its own default" (the real Ollama-backed providers).
An explicit provider is how a caller -- tests, principally -- substitutes a
hermetic fake for the whole chain without monkeypatching module internals.

**No run-tracking here.** Persisting "a pipeline is running" / "which stage
is it on" / "did it error" is an API-layer concern
(`db.models.PipelineRun`), not this module's -- see
`api/routers/pipeline.py`. Keeping this function free of that lets a future
CLI call it directly with no run row involved. The optional
`on_stage_start` hook is this module's only concession to that caller: a
place to observe progress (e.g. to persist `PipelineRun.current_stage`)
without this module knowing what, if anything, is listening.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from truth_engine.analysis.direction import run_project_direction
from truth_engine.analysis.gaps import run_project_gaps
from truth_engine.analysis.graph import build_project_graph
from truth_engine.analysis.phases import run_project_phases
from truth_engine.analysis.report import run_project_report
from truth_engine.analysis.timeline import assemble_project_timeline
from truth_engine.analysis.view import run_project_view
from truth_engine.db.models import Project, Stage
from truth_engine.embed.service import embed_project
from truth_engine.extract.service import extract_project
from truth_engine.ingest.service import ingest_folder
from truth_engine.parse.service import parse_project
from truth_engine.reasoning.providers import EmbeddingProvider, LLMProvider

# Dependency order (PROJECTSPECS.md §2): each stage reads what the ones
# before it here wrote. `api/routers/pipeline.py`'s status endpoint reuses
# this as the canonical stage ordering for its per-stage progress list.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.ingest,
    Stage.parse,
    Stage.extract,
    Stage.embed,
    Stage.timeline,
    Stage.phases,
    Stage.graph,
    Stage.direction,
    Stage.gaps,
    Stage.view,
    Stage.report,
)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    stage: Stage
    summary: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class PipelineResult:
    outcomes: list[StageOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.outcomes) == len(STAGE_ORDER) and all(o.ok for o in self.outcomes)

    @property
    def failed(self) -> StageOutcome | None:
        """The first (and, by construction, only) failed outcome, or `None`
        if every stage ran to completion."""
        return next((o for o in self.outcomes if not o.ok), None)


def _summarize(result: object) -> str:
    """Compact one-line rendering of a stage result dataclass. Every stage
    result in this pipeline already *is* a dataclass of counts plus an
    `errors` list (`ParseResult`, `EmbedResult`, `GraphResult`, ...) -- one
    generic renderer covers all eleven instead of a bespoke formatter per
    stage. Non-scalar fields (a `DirectionSnapshot`/`Report` row, a list of
    changed section names) are skipped; they're not needed for a progress
    line and the dataclasses that carry them expose the interesting counts
    as scalar fields alongside them."""
    if not is_dataclass(result):
        return str(result)
    parts = []
    for key, value in vars(result).items():
        if key == "errors":
            value = len(value)
        elif not isinstance(value, int | float | str | bool | type(None)):
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def run_project_pipeline(
    session: Session,
    project_id: uuid.UUID,
    *,
    on_stage_start: Callable[[Stage], None] | None = None,
    llm_provider: LLMProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> PipelineResult:
    """Run every pipeline stage for `project_id`, in dependency order.

    Idempotent and incremental by construction: each stage skips artifacts
    (or the whole project-wide computation) whose inputs haven't changed
    since it last ran, via its own `StageState` gating. Re-running this over
    an already-processed, unchanged project is a safe, cheap no-op.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no project {project_id}")

    result = PipelineResult()

    def run_stage(stage: Stage, fn: Callable[[], object]) -> bool:
        if on_stage_start is not None:
            on_stage_start(stage)
        try:
            stage_result = fn()
        except Exception as exc:  # noqa: BLE001 - convert to a captured stage-level failure
            result.outcomes.append(StageOutcome(stage, summary="", error=str(exc)))
            return False
        result.outcomes.append(StageOutcome(stage, summary=_summarize(stage_result)))
        return True

    steps: list[tuple[Stage, Callable[[], object]]] = [
        (Stage.ingest, lambda: ingest_folder(session, project, Path(project.root_path))),
        (Stage.parse, lambda: parse_project(session, project_id)),
        (Stage.extract, lambda: extract_project(session, project_id)),
        (Stage.embed, lambda: embed_project(session, project_id, provider=embedding_provider)),
        (Stage.timeline, lambda: assemble_project_timeline(session, project_id)),
        (Stage.phases, lambda: run_project_phases(session, project_id, provider=llm_provider)),
        (Stage.graph, lambda: build_project_graph(session, project_id)),
        (
            Stage.direction,
            lambda: run_project_direction(session, project_id, provider=llm_provider),
        ),
        (Stage.gaps, lambda: run_project_gaps(session, project_id, provider=llm_provider)),
        (Stage.view, lambda: run_project_view(session, project_id, provider=llm_provider)),
        (Stage.report, lambda: run_project_report(session, project_id, provider=llm_provider)),
    ]

    for stage, fn in steps:
        if not run_stage(stage, fn):
            break

    return result
