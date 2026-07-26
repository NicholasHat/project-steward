"""Step 11 orchestration (PROJECTSPECS.md §3.6, Non-Destructive View/Index
Layer): per artifact, a **projection over the raw file** — never a mutation
of it — stored as `ViewProjection` metadata: `suggested_name`,
`suggested_category`, `virtual_path`. Gated by `StageState(stage=view)`.

**NON-DESTRUCTIVE is the whole point of this stage (CLAUDE.md's paramount
invariant).** This module never opens an artifact's file on disk, never
writes one, and never touches `Artifact.current_path`. Every read is from
already-extracted DB rows (`ArtifactContent.raw_text`, `Entity`/
`EntityMention`, `PhaseAssignment`, `ResolvedDate`) — the same discipline
`phases.py`/`gaps.py` use for their prompts. The only writes this module ever
makes are `ViewProjection` and `DecisionAudit` rows.

**Three fields, two derivation strategies (the deterministic/LLM split
CLAUDE.md calls load-bearing).**

  * `suggested_category` — **100% deterministic.** The artifact's
    highest-confidence `PhaseAssignment` (tie-broken toward a human-sourced
    assignment, then the template's own `ordinal`, then `PhaseTemplate.id`
    for a fully stable pick) names the category directly —
    `PhaseTemplate.phase_name`. An artifact with no `PhaseAssignment` at all
    (phases hasn't run yet, or it genuinely didn't match any phase) gets
    `Settings.view_generic_category` — the "domain-generic bucket" §3.6
    asks for, never a guess.
  * `virtual_path` — **100% deterministic**, built from the same category
    plus a `YYYY-MM` folder from the artifact's chosen `ResolvedDate` (or
    `Settings.view_undated_folder` if none) plus `suggested_name`:
    `{category}/{YYYY-MM}/{suggested_name}`. A pure string computed from
    already-persisted rows — never a filesystem path that gets created (see
    `_virtual_path`, and the non-destructive tests that assert no file I/O
    occurs at all).
  * `suggested_name` — **LLM-partial**, the one genuinely judgment-shaped
    field (a good name requires understanding *what a document is about*,
    not just counting/thresholding). A batched pass (`Settings.
    view_name_batch_size`, mirroring `phases.py`'s phase-assignment batching)
    asks the model for a short descriptive slug from each artifact's
    filename, headings, a content snippet, its chosen date, and its top
    entities — never a raw-text dump. The **date and file extension are
    deliberately never the model's job**: composed deterministically
    (`_compose_name`) around whatever slug is chosen, so a hallucinated date
    can never end up in a suggested name. Any slug that's missing, fails to
    parse, or normalizes to fewer than `Settings.view_name_min_slug_chars`
    characters after `_slugify` degrades to the **deterministic fallback**
    (date + cleaned original filename + the single top entity, per §3.6's
    own description of this fallback) — never a crash, never an empty name
    (`_compose_name`'s budget arithmetic always leaves room for at least the
    literal `"artifact"` if the slug trims to nothing).

**Human overrides — preserved across regeneration (the mechanism this module
had to choose, per the task).** `ViewProjection` gains a `source` column
(`AssignmentSource`, reused rather than inventing a new enum — mirrors
`PhaseAssignment.source` and the exact "wholesale-replace only the auto row"
precedent `phases.py`'s `_replace_phase_assignments` already establishes).
Before doing any hash/LLM work for an artifact, this module looks at its
**current** projection (`current_projection` — see below); if it exists and
`source == human`, regeneration is skipped outright for that artifact, full
stop — no new version, no LLM call, not even a fallback recompute. A human's
decision is never silently superseded by a fresh auto suggestion. (Applying
an override itself — e.g. from a future dashboard/API — is out of this
module's scope, same as `direction.py`/`phases.py` never build the
"confirm" UI action either; it only needs to *insert a `source=human` row and
supersede the prior one*, which is exactly what this module's own versioning
helper already does for auto rows.)

**Versioned + reversible (§3.6 + §4) — the version chain.** `ViewProjection`
is never updated in place and never deleted: each (re)computation inserts a
**new** row at `version = previous.version + 1` (or `1` if there is none) and
sets the *previous* current row's `superseded_by` to the new row's `id`. The
full history — every version this module (or a human) ever produced — stays
queryable and reversible: rolling back means pointing `superseded_by` back to
`NULL` on an older row (an operation this module doesn't need to perform
itself, but the chain is designed so it's trivial and lossless when a
dashboard adds it). **"Current" is defined as the latest row with
`superseded_by IS NULL`** for that artifact (`current_projection`) — a single
indexed-friendly predicate, no need to track "latest version" separately
since the two coincide by construction (only ever one row per artifact has a
`NULL` `superseded_by` at a time).

**Auditability.** One `DecisionAudit(actor=system)` per newly-created
`ViewProjection` version, carrying the old/new `(suggested_name,
suggested_category, virtual_path)` triple and — mirroring `gaps.py`'s
promised-unfulfilled precedent — the *actual* basis of the name decision:
`model`/`model_version` name the real provider when an LLM slug was used, or
`_NAME_RULESET`/`VIEW_VERSION` (mirroring `direction.py`'s `_LABEL_RULESET`)
when the deterministic fallback fired. Deterministic `suggested_category`/
`virtual_path` get **no separate audit row** — same reasoning `gaps.py` gives
for structural gaps and `graph.py` gives for edges: a phase-coverage lookup
and a string template are self-documenting via the `ViewProjection` row
itself (and the version chain *is* their reversibility record), not a fuzzy
judgment call worth a second audited artifact.

**Idempotency.** Per-artifact `StageState(stage=view)`, whose `input_hash`
folds in `artifact.content_hash`, the selected category (`phase_id` or the
generic-bucket marker), the chosen `ResolvedDate` (iso, or a fixed
"undated" marker), the artifact's top entity values, the LLM naming model
(or a fixed marker when disabled), and `VIEW_VERSION`. **`DirectionLabel` is
also folded in** even though today's category/path derivation never branches
on it — forward-compatible bookkeeping so a drift re-label doesn't leave a
stale-but-hash-matching projection cached if a future version of this module
starts consuming it (see PROJECTSPECS.md's own listing of direction as one of
the "inputs" to this stage); it costs nothing today and documents the
decision rather than silently omitting it. Unchanged → no-op (no new
version, no LLM call, matching every other `analysis` stage's contract). A
human-current artifact's `StageState` is still refreshed to `done` at the
current hash, the same "settle without recomputing" bookkeeping
`direction.py`'s `confirmed_by_user` path uses.

**Async bridge + failure isolation** mirror `phases.py`: one `asyncio.run(...
gather(...))` per batch of naming prompts, all persistence synchronous
afterward; one artifact's error rolls back and records `StageState(status=
error)` without blocking its batch-mates (per-artifact `session.commit()`,
same as `phases.py`/`direction.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    AssignmentSource,
    AuditActor,
    DecisionAudit,
    DirectionLabel,
    Entity,
    EntityMention,
    PhaseAssignment,
    PhaseTemplate,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    ViewProjection,
)
from truth_engine.reasoning.json_extract import extract_json
from truth_engine.reasoning.providers import LLMProvider, get_llm_provider

# Bump when the derivation/composition rules in this module change materially
# enough that already-computed (auto) projections should be regenerated.
VIEW_VERSION = "1"

# DecisionAudit.model for a suggested_name reached without a usable LLM slug
# (naming disabled, or the response was unparseable/too-short) -- mirrors
# direction.py's `_LABEL_RULESET` / gaps.py's `_PROMISE_RULESET`.
_NAME_RULESET = "view-name-ruleset"

_NAME_SYSTEM_PROMPT = (
    "You are a document-naming assistant. Respond with ONLY a single JSON "
    "object -- no prose, no markdown fences."
)

_STRUCTURE_LABEL_KEYS = ("headings", "sheet_names", "slide_titles")


@dataclass
class ViewResult:
    generated: int = 0
    skipped: int = 0
    human_skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _NameCandidate:
    slug: str
    rationale: str


# --------------------------------------------------------------------------- #
# Shared helpers (deliberately duplicated per-module, matching phases.py's /  #
# direction.py's / gaps.py's own small copies of the same helpers)            #
# --------------------------------------------------------------------------- #
def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _chosen_dates(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    rows = session.execute(
        select(ResolvedDate.artifact_id, ResolvedDate.candidate_date)
        .join(Artifact, Artifact.id == ResolvedDate.artifact_id)
        .where(Artifact.project_id == project_id, ResolvedDate.is_chosen.is_(True))
    ).all()
    return dict(rows)


def _direction_labels(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = session.execute(
        select(DirectionLabel.artifact_id, DirectionLabel.label)
        .join(Artifact, Artifact.id == DirectionLabel.artifact_id)
        .where(Artifact.project_id == project_id)
    ).all()
    return {artifact_id: label.value for artifact_id, label in rows}


def _structure_labels(structure: dict[str, object] | None) -> list[str]:
    if not structure:
        return []
    labels: list[str] = []
    for key in _STRUCTURE_LABEL_KEYS:
        value = structure.get(key)
        if isinstance(value, list):
            labels.extend(str(v) for v in value)
    return labels


def _top_entities_by_artifact(
    session: Session, artifact_ids: list[uuid.UUID], limit: int
) -> dict[uuid.UUID, list[str]]:
    """Per-artifact top entity *values*, ranked by mention count within that
    artifact (tiebroken by Entity.id for determinism, same reasoning as
    `phases.py`'s project-level `_top_entities`). Used both in the naming
    prompt (up to `limit`) and by the deterministic fallback (only the
    first)."""
    if not artifact_ids:
        return {}
    rows = session.execute(
        select(EntityMention.artifact_id, Entity.value, func.count(EntityMention.id))
        .join(Entity, Entity.id == EntityMention.entity_id)
        .where(EntityMention.artifact_id.in_(artifact_ids))
        .group_by(EntityMention.artifact_id, Entity.id, Entity.value)
        .order_by(EntityMention.artifact_id, func.count(EntityMention.id).desc(), Entity.id)
    ).all()
    result: dict[uuid.UUID, list[str]] = {}
    for artifact_id, value, _count in rows:
        bucket = result.setdefault(artifact_id, [])
        if len(bucket) < limit:
            bucket.append(value)
    return result


# --------------------------------------------------------------------------- #
# suggested_category -- deterministic                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _CategoryChoice:
    category: str
    phase_id: uuid.UUID | None


def _choose_category(
    session: Session, artifact_id: uuid.UUID, settings: Settings
) -> _CategoryChoice:
    """The artifact's highest-confidence PhaseAssignment names the category
    directly. Ties broken toward a human-sourced assignment (a human
    correction is at least as trustworthy a categorization signal as an
    automated one -- same reasoning gaps.py gives for counting both sources
    toward structural coverage), then the template's own ordinal, then
    PhaseTemplate.id for a fully stable pick. No assignment at all ->
    `Settings.view_generic_category`, the domain-generic bucket §3.6 asks
    for."""
    rows = session.execute(
        select(PhaseAssignment, PhaseTemplate)
        .join(PhaseTemplate, PhaseTemplate.id == PhaseAssignment.phase_id)
        .where(PhaseAssignment.artifact_id == artifact_id)
    ).all()
    if not rows:
        return _CategoryChoice(settings.view_generic_category, None)

    def _sort_key(pair: tuple[PhaseAssignment, PhaseTemplate]) -> tuple[float, bool, int, str]:
        assignment, template = pair
        return (
            assignment.confidence,
            assignment.source == AssignmentSource.human,
            -template.ordinal,
            str(template.id),
        )

    _assignment, best_template = max(rows, key=_sort_key)
    return _CategoryChoice(best_template.phase_name, best_template.id)


# --------------------------------------------------------------------------- #
# virtual_path -- deterministic                                              #
# --------------------------------------------------------------------------- #
def _path_segment(text: str) -> str:
    """Defensive only -- virtual_path is metadata text, never a real
    filesystem path that gets created, but a stray '/' in a category or
    filename would otherwise silently add a phantom path level."""
    return text.replace("/", "-").strip()


def _virtual_path(
    category: str, chosen_date: datetime | None, suggested_name: str, settings: Settings
) -> str:
    date_folder = chosen_date.strftime("%Y-%m") if chosen_date else settings.view_undated_folder
    return "/".join(_path_segment(part) for part in (category, date_folder, suggested_name))


# --------------------------------------------------------------------------- #
# suggested_name -- LLM-partial, deterministic composition + fallback        #
# --------------------------------------------------------------------------- #
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    lowered = text.strip().lower()
    return _SLUG_INVALID_RE.sub("-", lowered).strip("-")


def _split_filename(filename: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(filename)
    return stem, ext.lower()


def _compose_name(date_str: str, slug: str, ext: str, max_length: int) -> str:
    """Date prefix and extension are never truncated; the slug is trimmed to
    whatever budget remains, falling back to the literal 'artifact' if
    nothing survives the trim -- so this always returns a non-empty name,
    regardless of how pathological `slug`/`ext` are."""
    prefix = f"{date_str}_"
    budget = max(1, max_length - len(prefix) - len(ext))
    trimmed = slug[:budget].strip("-") or "artifact"
    return f"{prefix}{trimmed}{ext}"


def _date_str(chosen_date: datetime | None, settings: Settings) -> str:
    return chosen_date.strftime("%Y-%m-%d") if chosen_date else settings.view_undated_folder


def _fallback_name(
    artifact: Artifact, chosen_date: datetime | None, top_entity: str | None, settings: Settings
) -> str:
    """§3.6's own fallback recipe: date + cleaned original filename + top
    entity. Never crashes, never empty (see `_compose_name`)."""
    stem, ext = _split_filename(artifact.original_filename)
    slug = _slugify(stem) or "artifact"
    if top_entity:
        entity_slug = _slugify(top_entity)
        if entity_slug and entity_slug not in slug:
            slug = f"{slug}-{entity_slug}"
    return _compose_name(_date_str(chosen_date, settings), slug, ext, settings.view_name_max_length)


def _llm_name(
    artifact: Artifact, chosen_date: datetime | None, slug: str, settings: Settings
) -> str:
    _stem, ext = _split_filename(artifact.original_filename)
    return _compose_name(_date_str(chosen_date, settings), slug, ext, settings.view_name_max_length)


def _artifact_naming_line(
    artifact: Artifact,
    content: ArtifactContent | None,
    chosen_date: datetime | None,
    top_entities: list[str],
    settings: Settings,
) -> str:
    parts = [artifact.original_filename]
    if content is not None:
        headings = _structure_labels(content.structure)
        if headings:
            parts.append("headings: " + ", ".join(headings))
        if content.raw_text:
            snippet = " ".join(content.raw_text.split())[: settings.view_name_snippet_chars]
            if snippet:
                parts.append(f'"{snippet}"')
    if chosen_date is not None:
        parts.append(f"date: {chosen_date:%Y-%m-%d}")
    if top_entities:
        parts.append("entities: " + ", ".join(top_entities))
    return " | ".join(parts)


def _name_prompt(batch: list[tuple[int, str]]) -> str:
    example = {
        "names": [
            {
                "artifact_index": 0,
                "slug": "lab-meeting-catalyst-b-screening",
                "rationale": "meeting note about catalyst B screening results",
            }
        ]
    }
    artifact_lines = "\n".join(f"[{idx}] {line}" for idx, line in batch)
    return (
        "Suggest a short, descriptive slug (lowercase words separated by hyphens, "
        "no dates, no file extensions, at most 8 words) for each numbered artifact "
        "below, based on its filename, headings, content snippet, date, and top "
        "entities.\n\n"
        f"Artifacts:\n{artifact_lines}\n\n"
        "Respond with exactly one JSON object shaped like this example "
        "(artifact_index refers to the [N] markers above):\n"
        f"{json.dumps(example)}"
    )


def _parse_name_response(
    raw: str, batch_size: int, min_slug_chars: int
) -> dict[int, _NameCandidate]:
    """Defensively parse a batch's naming response. Any artifact index that's
    missing, malformed, or normalizes to fewer than `min_slug_chars`
    characters contributes nothing -- the caller degrades that artifact to
    the deterministic fallback rather than crashing or leaving it unnamed."""
    parsed = extract_json(raw)
    result: dict[int, _NameCandidate] = {}
    if not isinstance(parsed, dict):
        return result
    names = parsed.get("names")
    if not isinstance(names, list):
        return result

    for entry in names:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("artifact_index")
        if not isinstance(idx, int) or not (0 <= idx < batch_size):
            continue
        slug = _slugify(str(entry.get("slug") or ""))
        if len(slug) < min_slug_chars:
            continue
        result[idx] = _NameCandidate(slug=slug, rationale=str(entry.get("rationale") or ""))
    return result


async def _complete_name_batches(provider: LLMProvider, prompts: list[str]) -> list[str]:
    return await asyncio.gather(
        *(provider.complete(p, system=_NAME_SYSTEM_PROMPT) for p in prompts)
    )


# --------------------------------------------------------------------------- #
# Idempotency                                                                #
# --------------------------------------------------------------------------- #
def _artifact_input_hash(
    artifact: Artifact,
    category_choice: _CategoryChoice,
    chosen_date: datetime | None,
    direction_label: str | None,
    top_entities: list[str],
    naming_model: str | None,
) -> str:
    raw = (
        f"{VIEW_VERSION}:{artifact.content_hash}:{category_choice.phase_id}:"
        f"{chosen_date.isoformat() if chosen_date else 'undated'}:"
        f"{direction_label or 'none'}:{','.join(top_entities)}:{naming_model or 'disabled'}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _upsert_stage_state_done(
    session: Session, artifact_id: uuid.UUID, input_hash: str
) -> None:
    state = _stage_state(session, artifact_id, Stage.view)
    if state is None:
        state = StageState(artifact_id=artifact_id, stage=Stage.view, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None


def _record_error(session: Session, artifact: Artifact, input_hash: str, message: str) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.view)
    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.view, input_hash=input_hash)
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    session.commit()


# --------------------------------------------------------------------------- #
# Version chain: "current" == the row with superseded_by IS NULL            #
# --------------------------------------------------------------------------- #
def current_projection(session: Session, artifact_id: uuid.UUID) -> ViewProjection | None:
    """The artifact's current `ViewProjection` — the latest version, whether
    `auto` or a human override. By construction at most one row per artifact
    has `superseded_by IS NULL` at a time; `order_by(version.desc())` is a
    defensive tiebreak, not load-bearing."""
    return session.scalar(
        select(ViewProjection)
        .where(ViewProjection.artifact_id == artifact_id, ViewProjection.superseded_by.is_(None))
        .order_by(ViewProjection.version.desc())
        .limit(1)
    )


def _write_projection(
    session: Session,
    artifact_id: uuid.UUID,
    current: ViewProjection | None,
    *,
    suggested_name: str,
    suggested_category: str,
    virtual_path: str,
    model: str | None,
    model_version: str | None,
    rationale: str,
) -> ViewProjection:
    new_version = ViewProjection(
        artifact_id=artifact_id,
        suggested_name=suggested_name,
        suggested_category=suggested_category,
        virtual_path=virtual_path,
        version=(current.version + 1) if current is not None else 1,
        source=AssignmentSource.auto,
    )
    session.add(new_version)
    session.flush()  # populate new_version.id, both as the audit target and for superseded_by

    old_value = None
    if current is not None:
        old_value = {
            "suggested_name": current.suggested_name,
            "suggested_category": current.suggested_category,
            "virtual_path": current.virtual_path,
        }
        current.superseded_by = new_version.id

    session.add(
        DecisionAudit(
            decision_type="view_projection_name",
            target_id=new_version.id,
            old_value=old_value,
            new_value={
                "suggested_name": suggested_name,
                "suggested_category": suggested_category,
                "virtual_path": virtual_path,
            },
            actor=AuditActor.system,
            model=model,
            model_version=model_version,
            rationale=rationale,
        )
    )
    return new_version


# --------------------------------------------------------------------------- #
# Orchestration                                                              #
# --------------------------------------------------------------------------- #
def run_project_view(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> ViewResult:
    """Compute (or reuse) a `ViewProjection` for every artifact in the
    project. Never touches an artifact whose current projection has
    `source == human`. Otherwise regenerates (a new version, superseding the
    prior `auto` row) exactly the artifacts whose derivation inputs changed
    since the last run; unchanged artifacts are a true no-op."""
    settings = get_settings()
    provider = provider or get_llm_provider()
    result = ViewResult()

    rows = session.execute(
        select(Artifact, ArtifactContent)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.ingested_at, Artifact.id)
    ).all()
    if not rows:
        return result

    artifact_ids = [a.id for a, _ in rows]
    dates_by_artifact = _chosen_dates(session, project_id)
    labels_by_artifact = _direction_labels(session, project_id)
    entities_by_artifact = _top_entities_by_artifact(
        session, artifact_ids, settings.view_name_top_entities
    )
    naming_model = provider.model if settings.view_llm_naming_enabled else None

    # Per-artifact category + input hash, up front (cheap: no LLM/file I/O) --
    # mirrors phases.py's "compute every artifact's input_hash before
    # deciding the pending batch" structure.
    pending: list[
        tuple[Artifact, ArtifactContent | None, _CategoryChoice, datetime | None, list[str], str]
    ] = []
    for artifact, content in rows:
        current = current_projection(session, artifact.id)
        if current is not None and current.source == AssignmentSource.human:
            # A human's decision is never silently superseded by a fresh auto
            # suggestion -- skip entirely, but still settle StageState so
            # unrelated corpus churn doesn't re-flag this artifact forever.
            category_choice = _choose_category(session, artifact.id, settings)
            chosen_date = dates_by_artifact.get(artifact.id)
            top_entities = entities_by_artifact.get(artifact.id, [])
            input_hash = _artifact_input_hash(
                artifact,
                category_choice,
                chosen_date,
                labels_by_artifact.get(artifact.id),
                top_entities,
                naming_model,
            )
            _upsert_stage_state_done(session, artifact.id, input_hash)
            session.commit()
            result.human_skipped += 1
            continue

        category_choice = _choose_category(session, artifact.id, settings)
        chosen_date = dates_by_artifact.get(artifact.id)
        top_entities = entities_by_artifact.get(artifact.id, [])
        input_hash = _artifact_input_hash(
            artifact,
            category_choice,
            chosen_date,
            labels_by_artifact.get(artifact.id),
            top_entities,
            naming_model,
        )
        state = _stage_state(session, artifact.id, Stage.view)
        if state and state.status == StageStatus.done and state.input_hash == input_hash:
            result.skipped += 1
            continue
        pending.append((artifact, content, category_choice, chosen_date, top_entities, input_hash))

    if not pending:
        return result

    # LLM naming pass (batched), if enabled -- otherwise every pending
    # artifact goes straight to the deterministic fallback, zero LLM calls.
    llm_names: dict[uuid.UUID, _NameCandidate] = {}
    if settings.view_llm_naming_enabled:
        batches = [
            pending[i : i + settings.view_name_batch_size]
            for i in range(0, len(pending), settings.view_name_batch_size)
        ]
        prompts = [
            _name_prompt(
                [
                    (i, _artifact_naming_line(a, c, d, e, settings))
                    for i, (a, c, _cat, d, e, _h) in enumerate(batch)
                ]
            )
            for batch in batches
        ]
        raw_responses = asyncio.run(_complete_name_batches(provider, prompts))
        for batch, raw in zip(batches, raw_responses, strict=True):
            parsed = _parse_name_response(raw, len(batch), settings.view_name_min_slug_chars)
            for i, (artifact, *_rest) in enumerate(batch):
                if i in parsed:
                    llm_names[artifact.id] = parsed[i]

    for artifact, _content, category_choice, chosen_date, top_entities, input_hash in pending:
        try:
            candidate = llm_names.get(artifact.id)
            if candidate is not None:
                suggested_name = _llm_name(artifact, chosen_date, candidate.slug, settings)
                model, model_version = provider.model, provider.model_version
                rationale = candidate.rationale or "LLM-suggested descriptive slug."
            else:
                top_entity = top_entities[0] if top_entities else None
                suggested_name = _fallback_name(artifact, chosen_date, top_entity, settings)
                model, model_version = _NAME_RULESET, VIEW_VERSION
                rationale = (
                    "deterministic fallback (date + cleaned original filename + top entity); "
                    + (
                        "LLM naming disabled."
                        if not settings.view_llm_naming_enabled
                        else "LLM response was unusable or too short."
                    )
                )

            virtual_path = _virtual_path(
                category_choice.category, chosen_date, suggested_name, settings
            )
            current = current_projection(session, artifact.id)
            _write_projection(
                session,
                artifact.id,
                current,
                suggested_name=suggested_name,
                suggested_category=category_choice.category,
                virtual_path=virtual_path,
                model=model,
                model_version=model_version,
                rationale=rationale,
            )
            _upsert_stage_state_done(session, artifact.id, input_hash)
            session.commit()
            result.generated += 1
        except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
            _record_error(session, artifact, input_hash, str(exc))
            result.errors.append((artifact, str(exc)))

    return result


def main() -> None:
    """CLI: compute (or refresh) a `ViewProjection` for every artifact in a
    project.

        uv run python -m truth_engine.analysis.view --project-id <uuid>

    Non-destructive: only writes `ViewProjection`/`DecisionAudit`/
    `StageState` rows, never touches a file on disk or `Artifact.
    current_path`. Reads `PhaseAssignment` (step 7), chosen `ResolvedDate`
    (step 3/extract), `DirectionLabel` (steps 8-9, folded into the input hash
    only) and top `Entity`/`EntityMention` -- run any time after `extract`;
    richer categories/paths appear once `phases` has run too. A rerun over an
    unchanged corpus is a no-op; a projection a human has overridden
    (`source=human`) is never recomputed or overwritten.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_project_view(session, args.project_id)

    print(
        f"view: {result.generated} generated, {result.skipped} skipped, "
        f"{result.human_skipped} human-overridden (untouched), {len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
