"""Stage 3 orchestration: multi-signal date resolution (`extract.dates`) +
entity extraction (`extract.entities`), persisted as `ResolvedDate` /
`Entity` / `EntityMention`, gated by `StageState`.

**First stage to write `DecisionAudit`** — parse re-expresses a file's own
bytes; extract *infers* facts from them, so every inferred fact gets an audit
row: one per artifact for the chosen date (the fact that matters is "this is
the resolved date", not each rejected candidate — all candidates are already
inspectable via `resolved_dates` itself), and one per entity mention (each
occurrence is its own inferred fact, e.g. two mentions of "Alice" in
different artifacts are two separate judgments even though they resolve to
one `Entity` row).

Idempotency: `StageState(artifact_id, stage=extract).input_hash` folds in
`content_hash` **and** the spaCy model name + ruleset version, so bumping
either invalidates cached state — re-running extract with an upgraded model
or rule set reprocesses everything, unlike parse's `content_hash`-only key
(see `parse.service` docstring; this was flagged as a known gap there).
This still doesn't catch a same-named model being upgraded in place by pip
(e.g. `en_core_web_sm` 3.8.0 -> 3.9.0) — introspecting the installed model's
version would mean loading it before deciding whether to skip, which defeats
the point of skipping. Out of scope for this increment; pin the model
version in the deployment environment if this matters.

Failure isolation, per-artifact error recording, and the "replace this
artifact's rows wholesale on re-run" pattern all mirror `parse.service`.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    AuditActor,
    DecisionAudit,
    Entity,
    EntityMention,
    EntityType,
    ProcessingState,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.extract import dates, entities
from truth_engine.extract.dates import DateCandidate
from truth_engine.extract.entities import EntityMentionCandidate
from truth_engine.extract.nlp import get_nlp

# Bump when regex rules or confidence formulas in `dates`/`entities` change
# materially enough that already-extracted artifacts should be reprocessed.
RULESET_VERSION = "1"


@dataclass
class ExtractResult:
    extracted: int = 0
    skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _input_hash(artifact: Artifact, settings: Settings) -> str:
    raw = f"{artifact.content_hash}:{settings.spacy_model}:{RULESET_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_artifact(session: Session, artifact: Artifact) -> bool:
    """Extract dates and entities for one artifact if its content or the
    extractor version changed since the last successful run. Returns True if
    (re)extracted, False if skipped as already current.
    """
    settings = get_settings()
    input_hash = _input_hash(artifact, settings)
    state = _stage_state(session, artifact.id, Stage.extract)
    if state and state.status == StageStatus.done and state.input_hash == input_hash:
        return False

    content = session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    raw_text = content.raw_text if content else None
    embedded_metadata = content.embedded_metadata if content else None

    fs_candidates = dates.filesystem_candidates(artifact.fs_created, artifact.fs_modified)
    meta_candidates = dates.doc_meta_candidates(embedded_metadata)
    anchor = dates.best_anchor(fs_candidates + meta_candidates)

    spacy_doc = None
    if raw_text:
        spacy_doc = get_nlp()(raw_text[: settings.extract_max_text_chars])

    content_candidates = dates.content_candidates(
        raw_text,
        spacy_doc,
        anchor,
        min_year=settings.extract_min_year,
        max_year=settings.extract_max_year,
    )
    all_candidates = fs_candidates + meta_candidates + content_candidates
    chosen = dates.choose(all_candidates)

    _replace_resolved_dates(session, artifact, all_candidates, chosen)
    _replace_entity_mentions(session, artifact, entities.extract_entities(raw_text, spacy_doc))

    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.extract, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None

    # `unsupported` is terminal — an unsupported artifact still flows through
    # here (it gets a filesystem-dated ResolvedDate so it appears on the
    # timeline), but its classification must not be overwritten with a
    # lifecycle rung that implies content was processed.
    if artifact.processing_state != ProcessingState.unsupported:
        artifact.processing_state = ProcessingState.extracted
    return True


def extract_project(session: Session, project_id: uuid.UUID) -> ExtractResult:
    """Extract every artifact in a project, skipping ones already up to date."""
    result = ExtractResult()
    artifacts = session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()

    for artifact in artifacts:
        try:
            changed = extract_artifact(session, artifact)
        except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
            _record_error(session, artifact, str(exc))
            result.errors.append((artifact, str(exc)))
            continue

        session.commit()
        result.extracted += 1 if changed else 0
        result.skipped += 0 if changed else 1

    return result


def _replace_resolved_dates(
    session: Session,
    artifact: Artifact,
    candidates: list[DateCandidate],
    chosen: DateCandidate | None,
) -> None:
    session.execute(delete(ResolvedDate).where(ResolvedDate.artifact_id == artifact.id))

    chosen_row: ResolvedDate | None = None
    for candidate in candidates:
        row = ResolvedDate(
            artifact_id=artifact.id,
            candidate_date=candidate.candidate_date,
            signal_source=candidate.signal_source,
            confidence=candidate.confidence,
            evidence_text=candidate.evidence_text,
            extractor=candidate.extractor,
            is_chosen=candidate is chosen,
        )
        session.add(row)
        if candidate is chosen:
            chosen_row = row

    if chosen is None or chosen_row is None:
        return

    session.flush()  # populate chosen_row.id for the audit target
    rationale = (
        f"chosen via signal precedence: {chosen.signal_source.value} "
        f"(confidence={chosen.confidence:.2f})"
    )
    if chosen.anchor_date is not None:
        rationale += f"; relative content reference anchored to {chosen.anchor_date.isoformat()}"
    session.add(
        DecisionAudit(
            decision_type="resolved_date",
            target_id=chosen_row.id,
            new_value={
                "candidate_date": chosen.candidate_date.isoformat(),
                "signal_source": chosen.signal_source.value,
                "confidence": chosen.confidence,
            },
            actor=AuditActor.system,
            model=chosen.extractor,
            model_version=RULESET_VERSION,
            rationale=rationale,
        )
    )


def _replace_entity_mentions(
    session: Session, artifact: Artifact, mentions: list[EntityMentionCandidate]
) -> None:
    session.execute(delete(EntityMention).where(EntityMention.artifact_id == artifact.id))

    for mention in mentions:
        entity = _get_or_create_entity(
            session,
            artifact.project_id,
            mention.entity_type,
            mention.value,
            mention.normalized_value,
        )
        mention_row = EntityMention(
            entity_id=entity.id,
            artifact_id=artifact.id,
            span=mention.span,
            context=mention.context,
            confidence=mention.confidence,
            extractor=mention.extractor,
        )
        session.add(mention_row)
        session.flush()  # populate mention_row.id for the audit target
        session.add(
            DecisionAudit(
                decision_type="entity_mention",
                target_id=mention_row.id,
                new_value={
                    "entity_type": mention.entity_type.value,
                    "value": mention.value,
                    "normalized_value": mention.normalized_value,
                    "confidence": mention.confidence,
                },
                actor=AuditActor.system,
                model=mention.extractor,
                model_version=RULESET_VERSION,
                rationale=f"extracted via {mention.extractor}",
            )
        )


def _get_or_create_entity(
    session: Session,
    project_id: uuid.UUID,
    entity_type: EntityType,
    value: str,
    normalized_value: str,
) -> Entity:
    entity = session.scalar(
        select(Entity).where(
            Entity.project_id == project_id,
            Entity.type == entity_type,
            Entity.normalized_value == normalized_value,
        )
    )
    if entity is None:
        entity = Entity(
            project_id=project_id,
            type=entity_type,
            value=value,
            normalized_value=normalized_value,
        )
        session.add(entity)
        session.flush()
    return entity


def _record_error(session: Session, artifact: Artifact, message: str) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.extract)
    if state is None:
        state = StageState(
            artifact_id=artifact.id,
            stage=Stage.extract,
            input_hash=_input_hash(artifact, get_settings()),
        )
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    artifact.processing_state = ProcessingState.error
    session.commit()
