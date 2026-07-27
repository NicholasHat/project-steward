"""Upload + run + status (`api/routers/pipeline.py`): the HTTP loop that lets
a user drive the whole pipeline without touching a CLI.

**Why the background job needs its own session-factory override.**
`POST .../run` schedules `_execute_pipeline_run` via FastAPI `BackgroundTasks`
against a *fresh* sync session (`SyncSessionFactoryDep`), deliberately not the
request's own session (see `db/session.py`'s module docstring). In production
that's a brand-new connection to the real database; in this test suite every
other fixture shares one uncommitted, SAVEPOINT-per-commit transaction
(`db_session`, see `tests/conftest.py`) that a genuinely new connection can't
see. So `client` here overrides `get_sync_session_factory` to hand the
background job the *same* `db_session`, wrapped in a no-op context manager
(never closed -- the test still needs it afterward) -- this keeps every
write, from the route and from the background job alike, inside the one
transaction that gets rolled back when the test ends.

**Why the background job actually finishes before `client.post()` returns.**
Starlette runs `BackgroundTasks` as part of the same ASGI call that produces
the response; `TestClient`'s ASGI transport drives that whole call
synchronously before handing the response back. So by the time
`client.post(".../run")` returns, the pipeline has already run to completion
(or failure) -- no polling loop needed to observe the *first* status change,
though `GET .../status` is still exercised directly, as the dashboard would.

**Why the LLM/embedding providers are overridden too.** `run_project_pipeline`
threads `llm_provider`/`embedding_provider` through to every stage that
accepts one; `get_pipeline_llm_provider`/`get_pipeline_embedding_provider`
(api/deps.py) are the seam the background job reads them from, overridden
here to hermetic fakes so a full run never calls a real Ollama.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.api.app import create_app
from truth_engine.api.deps import (
    get_pipeline_embedding_provider,
    get_pipeline_llm_provider,
    get_sync_session_factory,
)
from truth_engine.auth.users import current_active_user
from truth_engine.config import get_settings
from truth_engine.db.models import (
    EMBEDDING_DIM,
    Artifact,
    PipelineRun,
    PipelineRunStatus,
    ProcessingState,
    Project,
    Stage,
    User,
)
from truth_engine.db.session import get_sync_session
from truth_engine.reasoning.providers import EmbeddingProvider, LLMProvider

SETTINGS = get_settings()


# --------------------------------------------------------------------------- #
# Fakes: hermetic, offline, no network call.                                 #
# --------------------------------------------------------------------------- #
class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, unit-normalized (matches the real provider's
    contract) -- same technique as test_embed.py's."""

    def __init__(self, model: str = "fake-embed", dim: int = EMBEDDING_DIM) -> None:
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int(hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest(), 16)
        values = []
        for i in range(self.dim):
            seed = (seed * 1_103_515_245 + 12_345 + i) & 0xFFFFFFFF
            values.append((seed / 0xFFFFFFFF) * 2 - 1)
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


class UnparseableLLMProvider(LLMProvider):
    """Always returns a non-JSON string. Every LLM-judgment stage in this
    codebase is documented (and separately tested, per-module) to degrade to
    a deterministic fallback on unparseable output rather than crash -- see
    `reasoning/json_extract.py`'s module docstring. That makes this the
    right fake for an orchestrator test: it proves the chain runs end to end
    over every stage without having to hand-craft five different stages'
    worth of valid response JSON just to reach a terminal state."""

    def __init__(self, model: str = "fake-llm") -> None:
        self.model = model
        self.calls = 0

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.calls += 1
        return "not json"


class RaisingLLMProvider(LLMProvider):
    """Raises on every call -- for the stage-error-surfaces-cleanly test."""

    model = "raising-fake-llm"

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        raise RuntimeError("llm provider unreachable")


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@contextmanager
def _borrowed_session(session: Session):
    """Hands the background job the test's own session instead of a fresh
    connection -- see module docstring. Never closes it: the test keeps
    using `db_session` afterward to assert on what the job wrote."""
    yield session


@pytest.fixture
def other_user(db_session: Session) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db_session.add(u)
    db_session.flush()
    return u


def _client_as(
    db_session: Session,
    acting_user: User,
    *,
    llm_provider: LLMProvider | None,
    embedding_provider: EmbeddingProvider | None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_sync_session] = lambda: db_session
    app.dependency_overrides[current_active_user] = lambda: acting_user
    app.dependency_overrides[get_sync_session_factory] = lambda: (
        lambda: _borrowed_session(db_session)
    )
    app.dependency_overrides[get_pipeline_llm_provider] = lambda: llm_provider
    app.dependency_overrides[get_pipeline_embedding_provider] = lambda: embedding_provider
    return TestClient(app)


@pytest.fixture
def client(db_session: Session, user: User) -> TestClient:
    return _client_as(
        db_session,
        user,
        llm_provider=UnparseableLLMProvider(),
        embedding_provider=FakeEmbeddingProvider(),
    )


@pytest.fixture(autouse=True)
def _tmp_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test in this module uploads through `data_root` -- point it at a
    throwaway directory so no test ever writes into the real data dir (see
    test_view.py's precedent for mutating the shared `SETTINGS` singleton
    in place)."""
    root = tmp_path / "data-root"
    monkeypatch.setattr(SETTINGS, "data_root", str(root))
    return root


def _artifacts(session: Session, project: Project) -> list[Artifact]:
    return list(session.scalars(select(Artifact).where(Artifact.project_id == project.id)).all())


# --------------------------------------------------------------------------- #
# Upload                                                                      #
# --------------------------------------------------------------------------- #
def test_upload_stores_files_with_sanitized_names_and_sets_root_path(
    client: TestClient, project: Project, _tmp_data_root: Path
) -> None:
    resp = client.post(
        f"/projects/{project.id}/files",
        files=[
            ("files", ("notes.txt", b"hello world", "text/plain")),
            ("files", ("readme.md", b"# Title\n\nbody", "text/markdown")),
        ],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["root_path"] == str(_tmp_data_root / str(project.id))
    names = {f["filename"] for f in body["files"]}
    assert names == {"notes.txt", "readme.md"}

    stored_dir = _tmp_data_root / str(project.id)
    assert (stored_dir / "notes.txt").read_bytes() == b"hello world"
    assert (stored_dir / "readme.md").read_bytes() == b"# Title\n\nbody"


def test_upload_rejects_path_traversal_and_writes_nothing(
    client: TestClient, project: Project, _tmp_data_root: Path
) -> None:
    resp = client.post(
        f"/projects/{project.id}/files",
        files=[
            ("files", ("safe.txt", b"fine", "text/plain")),
            ("files", ("../../etc/passwd", b"evil", "text/plain")),
        ],
    )
    assert resp.status_code == 422

    stored_dir = _tmp_data_root / str(project.id)
    # Whole request rejected -- not even the valid file in the same batch
    # was written, and nothing escaped the project directory.
    assert not stored_dir.exists() or list(stored_dir.iterdir()) == []
    assert not (_tmp_data_root / "etc").exists()


def test_upload_rejects_empty_filename(client: TestClient, project: Project) -> None:
    resp = client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("", b"data", "text/plain"))],
    )
    assert resp.status_code == 422


def test_upload_owner_scoping(
    db_session: Session, project: Project, other_user: User
) -> None:
    other_client = _client_as(
        db_session,
        other_user,
        llm_provider=UnparseableLLMProvider(),
        embedding_provider=FakeEmbeddingProvider(),
    )
    resp = other_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Run + status: the full offline pipeline loop                               #
# --------------------------------------------------------------------------- #
def _upload_tiny_corpus(client: TestClient, project: Project) -> None:
    resp = client.post(
        f"/projects/{project.id}/files",
        files=[
            (
                "files",
                (
                    "meeting-notes.txt",
                    b"Meeting on March 3, 2024 with Alice about the catalyst experiment.",
                    "text/plain",
                ),
            ),
            (
                "files",
                (
                    "followup.md",
                    b"# Follow-up\n\nAfter the March meeting we ran experiment 2.",
                    "text/markdown",
                ),
            ),
        ],
    )
    assert resp.status_code == 201


def test_full_pipeline_run_drives_artifacts_to_terminal_state_and_status_transitions(
    client: TestClient, db_session: Session, project: Project
) -> None:
    idle = client.get(f"/projects/{project.id}/status")
    assert idle.status_code == 200
    assert idle.json()["state"] == "idle"
    assert idle.json()["run_id"] is None

    _upload_tiny_corpus(client, project)
    original_bytes = {
        p.name: p.read_bytes() for p in Path(project.root_path).iterdir()  # refreshed below
    }

    run_resp = client.post(f"/projects/{project.id}/run")
    assert run_resp.status_code == 202
    run_body = run_resp.json()
    assert run_body["status"] == "running"
    run_id = run_body["run_id"]

    # BackgroundTasks already ran synchronously within the POST above (see
    # module docstring) -- status is already terminal.
    status_resp = client.get(f"/projects/{project.id}/status")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["run_id"] == run_id
    assert status_body["state"] == "done", status_body
    assert status_body["error"] is None
    assert status_body["finished_at"] is not None

    stage_names = [s["stage"] for s in status_body["stages"]]
    assert stage_names == [s.value for s in Stage]
    for stage in status_body["stages"]:
        assert stage["error"] == 0, stage

    artifacts = _artifacts(db_session, project)
    assert len(artifacts) == 2
    for artifact in artifacts:
        assert artifact.processing_state == ProcessingState.embedded

    report_resp = client.get(f"/projects/{project.id}/report")
    assert report_resp.status_code == 200
    assert report_resp.json()["report"] is not None

    # Non-destructive: the uploaded originals are byte-identical after a
    # full run through every stage (PROJECTSPECS.md §3.1).
    project_dir = Path(project.root_path)
    refreshed = {p.name: p.read_bytes() for p in project_dir.iterdir()}
    assert refreshed == original_bytes

    # Re-run: safe, near-no-op -- every stage's own StageState gating skips
    # unchanged artifacts, and the run still reaches `done`.
    rerun_resp = client.post(f"/projects/{project.id}/run")
    assert rerun_resp.status_code == 202
    rerun_status = client.get(f"/projects/{project.id}/status")
    assert rerun_status.json()["state"] == "done"
    assert rerun_status.json()["run_id"] != run_id  # a second, distinct run row
    assert refreshed == {p.name: p.read_bytes() for p in project_dir.iterdir()}


def test_concurrent_run_is_rejected_with_409(
    client: TestClient, db_session: Session, project: Project
) -> None:
    db_session.add(PipelineRun(project_id=project.id, status=PipelineRunStatus.running))
    db_session.commit()

    resp = client.post(f"/projects/{project.id}/run")
    assert resp.status_code == 409


def test_stage_error_surfaces_as_error_status_not_a_crash(
    db_session: Session, project: Project, user: User
) -> None:
    client = _client_as(
        db_session,
        user,
        llm_provider=RaisingLLMProvider(),
        embedding_provider=FakeEmbeddingProvider(),
    )
    _upload_tiny_corpus(client, project)

    run_resp = client.post(f"/projects/{project.id}/run")
    assert run_resp.status_code == 202  # accepted immediately regardless of eventual outcome

    status_body = client.get(f"/projects/{project.id}/status").json()
    assert status_body["state"] == "error"
    assert status_body["error"] is not None
    assert "phases" in status_body["error"]  # first stage that actually calls the LLM

    # Deterministic stages before the failure still completed cleanly.
    by_stage = {s["stage"]: s for s in status_body["stages"]}
    assert by_stage["embed"]["done"] == 2
    assert by_stage["embed"]["error"] == 0
    # Nothing past the failure point ran.
    assert by_stage["report"]["done"] == 0


def test_run_owner_scoping(db_session: Session, project: Project, other_user: User) -> None:
    other_client = _client_as(
        db_session,
        other_user,
        llm_provider=UnparseableLLMProvider(),
        embedding_provider=FakeEmbeddingProvider(),
    )
    assert other_client.post(f"/projects/{project.id}/run").status_code == 404
    assert other_client.get(f"/projects/{project.id}/status").status_code == 404
