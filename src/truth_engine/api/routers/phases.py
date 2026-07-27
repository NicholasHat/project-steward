"""Domain/phase view (PROJECTSPECS.md §3.3/§13) and the domain-confirmation
action. `analysis/phases.py`'s `classify_project_domain` treats a
`confirmed_by_user=True` `DomainClassification` as inviolate, checked before
any fingerprint/LLM work — `patch_domain` sets exactly that flag."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from truth_engine.analysis.phases import _phase_templates_for_domain, _select_template_domain
from truth_engine.api.assemble import phase_assignment_dto
from truth_engine.api.deps import CurrentUser, PageParamsDep, ProjectDep, SyncSessionDep
from truth_engine.api.schemas import (
    DomainClassificationDTO,
    DomainPatchRequest,
    Page,
    PhaseAssignmentDTO,
    PhasesOverviewDTO,
    PhaseTemplateCoverageDTO,
)
from truth_engine.config import get_settings
from truth_engine.db.models import (
    Artifact,
    AuditActor,
    DecisionAudit,
    DomainClassification,
    PhaseAssignment,
    PhaseTemplate,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["phases"])


@router.get("/phases", response_model=PhasesOverviewDTO)
def get_phases(
    project: ProjectDep, session: SyncSessionDep, page: PageParamsDep
) -> PhasesOverviewDTO:
    settings = get_settings()
    classification = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project.id)
    )
    template_domain = (
        _select_template_domain(classification, settings.domain_confidence_threshold)
        if classification
        else None
    )
    templates = _phase_templates_for_domain(session, template_domain) if template_domain else []

    coverage: dict[uuid.UUID, int] = {}
    if templates:
        coverage = dict(
            session.execute(
                select(
                    PhaseAssignment.phase_id, func.count(func.distinct(PhaseAssignment.artifact_id))
                )
                .join(Artifact, Artifact.id == PhaseAssignment.artifact_id)
                .where(
                    Artifact.project_id == project.id,
                    PhaseAssignment.phase_id.in_([t.id for t in templates]),
                )
                .group_by(PhaseAssignment.phase_id)
            ).all()
        )
    phases = [
        PhaseTemplateCoverageDTO(
            id=t.id,
            phase_name=t.phase_name,
            ordinal=t.ordinal,
            description=t.description,
            artifact_count=coverage.get(t.id, 0),
        )
        for t in templates
    ]

    query = (
        select(PhaseAssignment, PhaseTemplate.phase_name)
        .join(PhaseTemplate, PhaseTemplate.id == PhaseAssignment.phase_id)
        .join(Artifact, Artifact.id == PhaseAssignment.artifact_id)
        .where(Artifact.project_id == project.id)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.execute(
        query.order_by(PhaseAssignment.confidence.desc()).limit(page.limit).offset(page.offset)
    ).all()
    assignments: list[PhaseAssignmentDTO] = [phase_assignment_dto(pa, name) for pa, name in rows]

    return PhasesOverviewDTO(
        domain_classification=(
            DomainClassificationDTO.model_validate(classification) if classification else None
        ),
        template_domain=template_domain,
        phases=phases,
        assignments=Page(items=assignments, total=total, limit=page.limit, offset=page.offset),
    )


@router.patch("/domain", response_model=DomainClassificationDTO)
def patch_domain(
    project: ProjectDep, body: DomainPatchRequest, user: CurrentUser, session: SyncSessionDep
) -> DomainClassification:
    """Confirm or override the project's `DomainClassification` (PROJECTSPECS.md
    §3.3's "option for the user to confirm/override"). Always sets
    `confirmed_by_user`; optionally overrides `domain`. Once set,
    `analysis.phases.classify_project_domain` never recomputes this row."""
    classification = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project.id)
    )
    if classification is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no domain classification for this project yet"
        )

    if body.domain is not None:
        valid_domains = set(session.scalars(select(PhaseTemplate.domain).distinct()).all())
        if body.domain not in valid_domains:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown domain {body.domain!r}"
            )

    old = {
        "domain": classification.domain,
        "confidence": classification.confidence,
        "confirmed_by_user": classification.confirmed_by_user,
    }
    note = ""
    if body.domain is not None:
        classification.domain = body.domain
        note = f" Overridden to {body.domain!r}."
    classification.confirmed_by_user = True

    session.add(
        DecisionAudit(
            decision_type="domain_classification",
            target_id=classification.id,
            old_value=old,
            new_value={
                "domain": classification.domain,
                "confidence": classification.confidence,
                "confirmed_by_user": True,
            },
            actor=AuditActor.user,
            model=None,
            model_version=None,
            rationale=f"Confirmed by {user.email}.{note}",
        )
    )
    session.commit()
    session.refresh(classification)
    return classification
