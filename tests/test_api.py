"""API layer tests: FastAPI `TestClient` against the real `create_app()`,
with a real-shaped authenticated user (the `current_active_user` dependency
is overridden, same idea as registering + logging in, but without going
through the async auth engine on a connection the test's SAVEPOINT rollback
can't reach -- see `db/session.py`'s module docstring for why the API has
two engines in the first place).

The sync data/action dependency (`get_sync_session`) is overridden to the
same transactional `db_session` fixture every other test module uses, so
everything a test seeds and everything a request does share one rolled-back
transaction -- no rows ever escape into the dev DB.

Owner-scoping (CLAUDE.md's #1 multi-tenant invariant) is the most important
thing under test here: every project-scoped and sub-resource-scoped read/
write must 404 for a user who isn't the owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.analysis.direction import label_project_direction
from truth_engine.analysis.gaps import run_project_gaps
from truth_engine.analysis.phases import classify_project_domain
from truth_engine.analysis.view import current_projection, run_project_view
from truth_engine.api.app import create_app
from truth_engine.auth.users import current_active_user
from truth_engine.db.models import (
    EMBEDDING_DIM,
    ArtifactContent,
    AuditActor,
    DateSignalSource,
    DecisionAudit,
    DirectionLabel,
    DirectionLabelValue,
    DirectionSnapshot,
    DomainClassification,
    EdgeType,
    Embedding,
    EmbeddingLevel,
    Entity,
    EntityMention,
    EntityType,
    Gap,
    GapStatus,
    GapType,
    PhaseAssignment,
    PhaseTemplate,
    ProcessingState,
    Project,
    RelationshipEdge,
    Report,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    TimelineEvent,
    User,
    ViewProjection,
)
from truth_engine.db.models import Artifact as ArtifactModel
from truth_engine.db.session import get_sync_session

_BASE_DATE = datetime(2024, 3, 12, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
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


@pytest.fixture
def client(db_session: Session, user: User) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_sync_session] = lambda: db_session
    app.dependency_overrides[current_active_user] = lambda: user
    return TestClient(app)


def _client_as(db_session: Session, acting_user: User) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_sync_session] = lambda: db_session
    app.dependency_overrides[current_active_user] = lambda: acting_user
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Seed helpers (mirror the conventions test_report.py/test_view.py/          #
# test_phases.py already established)                                        #
# --------------------------------------------------------------------------- #
def _make_artifact(
    db_session: Session,
    project: Project,
    *,
    filename: str = "notes.txt",
    chosen_date: datetime | None = None,
) -> ArtifactModel:
    artifact = ArtifactModel(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=str(uuid.uuid4()),
        current_path=f"/tmp/{filename}",
        original_filename=filename,
        file_type="txt",
        size_bytes=1,
        processing_state=ProcessingState.extracted,
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactContent(
            artifact_id=artifact.id,
            raw_text="a plain document with no promise markers in it.",
            structure=None,
            embedded_metadata=None,
            parser_name="test",
            parser_version="1",
        )
    )
    if chosen_date is not None:
        db_session.add(
            ResolvedDate(
                artifact_id=artifact.id,
                candidate_date=chosen_date,
                signal_source=DateSignalSource.content,
                confidence=0.8,
                evidence_text="test",
                extractor="test",
                is_chosen=True,
            )
        )
    db_session.flush()
    return artifact


def _phase_template(
    db_session: Session, domain: str = "research", ordinal: int = 0
) -> PhaseTemplate:
    return db_session.scalar(
        select(PhaseTemplate).where(
            PhaseTemplate.domain == domain, PhaseTemplate.ordinal == ordinal
        )
    )


def _make_view_projection(
    db_session: Session, artifact: ArtifactModel, *, suggested_name: str
) -> ViewProjection:
    projection = ViewProjection(
        artifact_id=artifact.id,
        suggested_name=suggested_name,
        suggested_category="Execution",
        virtual_path=f"Execution/2024-03/{suggested_name}",
        version=1,
    )
    db_session.add(projection)
    db_session.flush()
    return projection


# --------------------------------------------------------------------------- #
# Projects: CRUD + owner-scoping                                             #
# --------------------------------------------------------------------------- #
def test_create_list_get_delete_project(client: TestClient, user: User) -> None:
    created = client.post("/projects", json={"name": "Lab Folder", "root_path": "/data/lab"})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Lab Folder"
    project_id = body["id"]

    listed = client.get("/projects")
    assert listed.status_code == 200
    assert any(p["id"] == project_id for p in listed.json())

    fetched = client.get(f"/projects/{project_id}")
    assert fetched.status_code == 200
    assert fetched.json()["root_path"] == "/data/lab"

    deleted = client.delete(f"/projects/{project_id}")
    assert deleted.status_code == 204
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_owner_scoping_project(db_session: Session, project: Project, other_user: User) -> None:
    """The #1 invariant: user B cannot see or act on user A's project."""
    other_client = _client_as(db_session, other_user)
    assert other_client.get(f"/projects/{project.id}").status_code == 404
    assert other_client.delete(f"/projects/{project.id}").status_code == 404
    assert other_client.get(f"/projects/{project.id}/artifacts").status_code == 404
    assert other_client.get(f"/projects/{project.id}/timeline").status_code == 404
    assert other_client.get(f"/projects/{project.id}/direction").status_code == 404
    assert other_client.get(f"/projects/{project.id}/gaps").status_code == 404
    assert other_client.get(f"/projects/{project.id}/phases").status_code == 404
    assert other_client.get(f"/projects/{project.id}/report").status_code == 404
    assert other_client.patch(f"/projects/{project.id}/domain", json={}).status_code == 404


def test_random_project_id_is_404_not_500(client: TestClient) -> None:
    assert client.get(f"/projects/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------- #
# Artifact browser + detail                                                  #
# --------------------------------------------------------------------------- #
def test_artifact_browser_shows_current_view_direction_and_phases(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    _make_view_projection(db_session, artifact, suggested_name="2024-03-12_lab-meeting.txt")
    db_session.add(
        DirectionLabel(
            artifact_id=artifact.id,
            label=DirectionLabelValue.current,
            rationale="recent activity",
            confidence=0.7,
        )
    )
    template = _phase_template(db_session)
    db_session.add(
        PhaseAssignment(
            artifact_id=artifact.id,
            phase_id=template.id,
            confidence=0.9,
            rationale="clearly planning",
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/artifacts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["view"]["suggested_name"] == "2024-03-12_lab-meeting.txt"
    assert row["direction"]["label"] == "current"
    assert row["direction"]["confidence"] == 0.7
    assert row["phases"][0]["phase_name"] == template.phase_name
    assert row["chosen_date"] is not None
    assert row["chosen_date_confidence"] == 0.8

    # filter by phase_id and by direction label
    by_phase = client.get(
        f"/projects/{project.id}/artifacts", params={"phase_id": str(template.id)}
    )
    assert by_phase.json()["total"] == 1
    by_wrong_direction = client.get(
        f"/projects/{project.id}/artifacts", params={"direction": "superseded"}
    )
    assert by_wrong_direction.json()["total"] == 0


def test_artifact_detail_includes_entities_dates_and_edges(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    other = _make_artifact(db_session, project, filename="earlier.txt")

    entity = Entity(
        project_id=project.id, type=EntityType.person, value="Alice", normalized_value="alice"
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        EntityMention(
            entity_id=entity.id,
            artifact_id=artifact.id,
            context="Alice ran the experiment",
            confidence=0.9,
            extractor="test",
        )
    )
    db_session.add(
        RelationshipEdge(
            src_artifact_id=artifact.id,
            dst_artifact_id=other.id,
            type=EdgeType.references,
            confidence=0.8,
            evidence="cites earlier.txt",
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/artifacts/{artifact.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entities"][0]["value"] == "Alice"
    assert len(body["resolved_dates"]) == 1
    assert body["resolved_dates"][0]["is_chosen"] is True
    assert body["edges"][0]["direction"] == "outgoing"
    assert body["edges"][0]["other_artifact_id"] == str(other.id)


def test_artifact_detail_surfaces_processing_note_for_unsupported(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, filename="clip.mov")
    db_session.add(
        StageState(
            artifact_id=artifact.id,
            stage=Stage.parse,
            status=StageStatus.skipped,
            input_hash="h",
            error="no parser for 'mov' — file retained, not analyzed",
        )
    )
    db_session.commit()

    body = client.get(f"/projects/{project.id}/artifacts/{artifact.id}").json()
    assert "no parser for 'mov'" in body["processing_note"]


def test_artifact_detail_has_no_processing_note_when_fully_processed(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.commit()
    body = client.get(f"/projects/{project.id}/artifacts/{artifact.id}").json()
    assert body["processing_note"] is None


def test_artifact_browser_pagination_slices_items_but_reports_full_total(
    client: TestClient, db_session: Session, project: Project
) -> None:
    for i in range(3):
        _make_artifact(db_session, project, filename=f"doc-{i}.txt")
    db_session.commit()

    first_page = client.get(f"/projects/{project.id}/artifacts", params={"limit": 2, "offset": 0})
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert first_body["total"] == 3
    assert len(first_body["items"]) == 2
    assert first_body["limit"] == 2
    assert first_body["offset"] == 0

    second_page = client.get(f"/projects/{project.id}/artifacts", params={"limit": 2, "offset": 2})
    second_body = second_page.json()
    assert second_body["total"] == 3
    assert len(second_body["items"]) == 1
    assert second_body["offset"] == 2

    # No overlap between the two pages, and together they cover every seeded artifact.
    first_ids = {row["id"] for row in first_body["items"]}
    second_ids = {row["id"] for row in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 3


def test_owner_scoping_artifact(
    db_session: Session, project: Project, other_user: User
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.commit()
    other_client = _client_as(db_session, other_user)
    assert other_client.get(f"/projects/{project.id}/artifacts/{artifact.id}").status_code == 404
    assert (
        other_client.put(
            f"/projects/{project.id}/artifacts/{artifact.id}/name", json={"suggested_name": "x.txt"}
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Timeline                                                                    #
# --------------------------------------------------------------------------- #
def test_timeline_surfaces_confidence_and_clean_name(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    _make_view_projection(db_session, artifact, suggested_name="2024-03-12_lab-meeting.txt")
    db_session.add(
        TimelineEvent(
            project_id=project.id,
            artifact_id=artifact.id,
            event_date=_BASE_DATE,
            description="artifact placement",
            confidence=0.85,
            source="placement:content",
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["confidence"] == 0.85
    assert body["items"][0]["artifact_name"] == "2024-03-12_lab-meeting.txt"


# --------------------------------------------------------------------------- #
# Direction: read + human confirmation                                       #
# --------------------------------------------------------------------------- #
def test_direction_read_shows_snapshot_and_labels(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    db_session.add(
        DirectionSnapshot(
            project_id=project.id,
            inferred_direction_summary="Currently focused on catalyst screening.",
            corpus_fingerprint_hash="irrelevant",
        )
    )
    db_session.add(
        DirectionLabel(
            artifact_id=artifact.id,
            label=DirectionLabelValue.superseded,
            rationale="quiet cluster",
            signal_a_score=0.1,
            signal_b_score=0.2,
            confidence=0.6,
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/direction")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot"]["inferred_direction_summary"].startswith("Currently focused")
    assert body["labels"]["items"][0]["label"] == "superseded"
    assert body["labels"]["items"][0]["signal_a_score"] == 0.1


def test_direction_patch_confirms_and_writes_audit_and_survives_rerun(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    label = DirectionLabel(
        artifact_id=artifact.id,
        label=DirectionLabelValue.unclear,
        rationale="ambiguous",
        confidence=0.4,
    )
    db_session.add(label)
    # label_project_direction only iterates artifacts with a doc-level embedding.
    db_session.add(
        Embedding(
            artifact_id=artifact.id,
            level=EmbeddingLevel.doc,
            vector=[0.01] * EMBEDDING_DIM,
            model="test",
            model_version="1",
        )
    )
    db_session.commit()

    resp = client.patch(
        f"/projects/{project.id}/direction/{artifact.id}", json={"label": "current"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "current"
    assert body["confirmed_by_user"] is True

    audits = db_session.scalars(
        select(DecisionAudit).where(
            DecisionAudit.decision_type == "direction_label", DecisionAudit.target_id == label.id
        )
    ).all()
    assert any(a.actor == AuditActor.user for a in audits)

    # Non-clobber: re-running the direction stage must not touch a confirmed label.
    result = label_project_direction(db_session, project.id)
    assert result.confirmed_skipped == 1
    db_session.refresh(label)
    assert label.label == DirectionLabelValue.current
    assert label.confirmed_by_user is True


def test_direction_patch_missing_label_is_404(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.commit()
    resp = client.patch(f"/projects/{project.id}/direction/{artifact.id}", json={})
    assert resp.status_code == 404


def test_owner_scoping_direction_patch(
    db_session: Session, project: Project, other_user: User
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.add(
        DirectionLabel(artifact_id=artifact.id, label=DirectionLabelValue.unclear, confidence=0.4)
    )
    db_session.commit()
    other_client = _client_as(db_session, other_user)
    resp = other_client.patch(f"/projects/{project.id}/direction/{artifact.id}", json={})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Gaps: read + human review                                                  #
# --------------------------------------------------------------------------- #
def test_gaps_read_filterable_by_status_and_type(
    client: TestClient, db_session: Session, project: Project
) -> None:
    db_session.add(
        Gap(
            project_id=project.id,
            type=GapType.structural,
            description="No artifacts for Analysis phase.",
            evidence="0 of 3 artifacts",
            confidence=0.9,
            status=GapStatus.open,
        )
    )
    db_session.add(
        Gap(
            project_id=project.id,
            type=GapType.promised_unfulfilled,
            description="promised control experiment never delivered",
            evidence="we still need to run the control experiment",
            confidence=0.5,
            status=GapStatus.dismissed,
        )
    )
    db_session.commit()

    all_gaps = client.get(f"/projects/{project.id}/gaps")
    assert all_gaps.json()["total"] == 2

    open_only = client.get(f"/projects/{project.id}/gaps", params={"status": "open"})
    assert open_only.json()["total"] == 1
    assert open_only.json()["items"][0]["type"] == "structural"

    promised_only = client.get(
        f"/projects/{project.id}/gaps", params={"type": "promised_unfulfilled"}
    )
    assert promised_only.json()["total"] == 1


def test_gap_patch_sets_status_writes_audit_and_survives_rerun(
    client: TestClient, db_session: Session, project: Project
) -> None:
    # An artifact with no promise markers and no domain classification: run_project_gaps
    # below finds zero structural/promised drafts and must leave this gap untouched.
    _make_artifact(db_session, project)
    gap = Gap(
        project_id=project.id,
        type=GapType.structural,
        description="No artifacts for Analysis phase.",
        evidence="0 of 3 artifacts",
        confidence=0.9,
        status=GapStatus.open,
    )
    db_session.add(gap)
    db_session.commit()

    resp = client.patch(f"/projects/{project.id}/gaps/{gap.id}", json={"status": "confirmed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    audits = db_session.scalars(
        select(DecisionAudit).where(
            DecisionAudit.decision_type == "gap_status", DecisionAudit.target_id == gap.id
        )
    ).all()
    assert any(a.actor == AuditActor.user for a in audits)

    # Non-clobber: a human-reviewed gap (status != open) is left untouched by a rerun.
    run_project_gaps(db_session, project.id)
    db_session.refresh(gap)
    assert gap.status == GapStatus.confirmed


def test_owner_scoping_gap_patch(db_session: Session, project: Project, other_user: User) -> None:
    gap = Gap(
        project_id=project.id,
        type=GapType.structural,
        description="No artifacts for Analysis phase.",
        confidence=0.9,
        status=GapStatus.open,
    )
    db_session.add(gap)
    db_session.commit()
    other_client = _client_as(db_session, other_user)
    resp = other_client.patch(f"/projects/{project.id}/gaps/{gap.id}", json={"status": "dismissed"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Phases / domain: read + human confirmation                                 #
# --------------------------------------------------------------------------- #
def test_phases_read_shows_classification_coverage_and_assignments(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project)
    db_session.add(
        DomainClassification(
            project_id=project.id,
            domain="research",
            confidence=0.9,
            model="fake-llm",
            corpus_fingerprint_hash="irrelevant",
        )
    )
    template = _phase_template(db_session, domain="research", ordinal=0)
    db_session.add(
        PhaseAssignment(
            artifact_id=artifact.id, phase_id=template.id, confidence=0.8, rationale="fit"
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/phases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain_classification"]["domain"] == "research"
    assert body["template_domain"] == "research"
    covered = next(p for p in body["phases"] if p["phase_name"] == template.phase_name)
    assert covered["artifact_count"] == 1
    assert body["assignments"]["total"] == 1


def test_domain_patch_confirms_overrides_writes_audit_and_survives_rerun(
    client: TestClient, db_session: Session, project: Project
) -> None:
    classification = DomainClassification(
        project_id=project.id,
        domain="research",
        confidence=0.4,
        model="fake-llm",
        corpus_fingerprint_hash="irrelevant",
    )
    db_session.add(classification)
    db_session.commit()

    resp = client.patch(f"/projects/{project.id}/domain", json={"domain": "engineering"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "engineering"
    assert body["confirmed_by_user"] is True

    audits = db_session.scalars(
        select(DecisionAudit).where(
            DecisionAudit.decision_type == "domain_classification",
            DecisionAudit.target_id == classification.id,
        )
    ).all()
    assert any(a.actor == AuditActor.user for a in audits)

    # Non-clobber: classify_project_domain never recomputes a confirmed row.
    reused = classify_project_domain(db_session, project.id)
    assert reused.domain == "engineering"
    assert reused.confirmed_by_user is True


def test_domain_patch_rejects_unknown_domain(
    client: TestClient, db_session: Session, project: Project
) -> None:
    db_session.add(
        DomainClassification(
            project_id=project.id,
            domain="research",
            confidence=0.4,
            corpus_fingerprint_hash="irrelevant",
        )
    )
    db_session.commit()
    resp = client.patch(f"/projects/{project.id}/domain", json={"domain": "astrology"})
    assert resp.status_code == 422


def test_domain_patch_missing_classification_is_404(client: TestClient, project: Project) -> None:
    resp = client.patch(f"/projects/{project.id}/domain", json={})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
def test_report_returns_current_version(
    client: TestClient, db_session: Session, project: Project
) -> None:
    db_session.add(
        Report(
            project_id=project.id,
            version=1,
            content="# Project Report\n\nAll going well.",
            sections={"overview": {"content": "..."}},
            corpus_fingerprint_hash="irrelevant",
            is_current=True,
        )
    )
    db_session.commit()

    resp = client.get(f"/projects/{project.id}/report")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report"]["content"].startswith("# Project Report")


def test_report_not_yet_generated_returns_null(client: TestClient, project: Project) -> None:
    resp = client.get(f"/projects/{project.id}/report")
    assert resp.status_code == 200
    assert resp.json()["report"] is None


# --------------------------------------------------------------------------- #
# Artifact name override: human ViewProjection version, non-clobber on rerun #
# --------------------------------------------------------------------------- #
def test_name_override_creates_human_version_and_survives_rerun(
    client: TestClient, db_session: Session, project: Project
) -> None:
    artifact = _make_artifact(db_session, project, chosen_date=_BASE_DATE)
    original = _make_view_projection(db_session, artifact, suggested_name="2024-03-12_notes.txt")
    db_session.commit()

    resp = client.put(
        f"/projects/{project.id}/artifacts/{artifact.id}/name",
        json={"suggested_name": "2024-03-12_catalyst-b-screening.txt"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_name"] == "2024-03-12_catalyst-b-screening.txt"
    assert body["source"] == "human"
    assert body["version"] == 2

    db_session.refresh(original)
    assert original.superseded_by == uuid.UUID(body["id"])

    audits = db_session.scalars(
        select(DecisionAudit).where(
            DecisionAudit.decision_type == "view_projection_name",
            DecisionAudit.target_id == uuid.UUID(body["id"]),
        )
    ).all()
    assert any(a.actor == AuditActor.user for a in audits)

    # Non-clobber: a human-sourced projection is never regenerated by a rerun.
    result = run_project_view(db_session, project.id)
    assert result.human_skipped == 1
    assert result.generated == 0
    current = current_projection(db_session, artifact.id)
    assert current.suggested_name == "2024-03-12_catalyst-b-screening.txt"
    assert current.source.value == "human"
