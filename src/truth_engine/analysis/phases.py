"""Stage 7 orchestration: domain classification + phase-template mapping,
the two-stage design of PROJECTSPECS.md §3.3 — **the first stage that
legitimately calls the LLM** (CLAUDE.md's deterministic/LLM boundary; steps
1-6 never do).

**Stage A — domain classification (project-level, one LLM call).** Classify
the project's field from a *corpus-level fingerprint* — filenames, parsed
document structure (headings/slide titles/sheet names), a short leading
snippet per artifact, and the project's most-mentioned entities — never the
raw corpus text (`_corpus_fingerprint`). The candidate domain set is
**data-driven**: it's the distinct non-`generic` domains present in
`phase_templates` (`_candidate_domains`), so a new vertical is added by
seeding a template (`db/seed.py`), not by editing this module. Result is
persisted to the project's single `DomainClassification` row along with the
LLM's `model` and a `DecisionAudit` row carrying its rationale.

**Stage B — phase assignment (per artifact, batched LLM calls).** The
selected template's phases are mapped against each artifact's own compact
fingerprint; an artifact may match more than one phase (e.g. a meeting note
spanning planning and execution). Artifacts are grouped into batches
(`Settings.phase_assignment_batch_size`) — one LLM call per batch, not per
artifact — to bound total call count; batches run concurrently via
`asyncio.gather`. Each resulting `PhaseAssignment` gets its own
`DecisionAudit` row (mirrors `extract.service`'s per-entity-mention
precedent, not `embed.service`'s no-audit one — this stage infers new
facts).

**Low-confidence / no-good-fit fallback (§3.3.4):** `DomainClassification`
always records the LLM's honest best guess and confidence, even when that
confidence is low — never faking certainty. Which template phase assignment
maps artifacts against is a *separate* decision (`_select_template_domain`):
below `Settings.domain_confidence_threshold`, it's the `generic` template,
regardless of what the classifier guessed. This is the "flag to the user"
signal PROJECTSPECS.md asks for, surfaced later by the human-confirmation
checkpoint (§3.4) reading `DomainClassification.confidence` — never forcing
a bad domain-specific template onto a low-confidence corpus.

**Human-confirmation invariant:** a `DomainClassification` with
`confirmed_by_user=True` is never recomputed or overwritten — checked before
any LLM call or fingerprint work. The same protection applies per artifact
to human-sourced `PhaseAssignment` rows (`source=human`): rebuilding an
artifact's assignments wholesale-replaces only the `source=auto` rows.

**Robustness against flaky local-model JSON:** every LLM response is parsed
via `reasoning.json_extract.extract_json` (tolerates code fences/preamble)
and then validated against the allowed domain set / template phase names.
Anything unparseable or invalid degrades gracefully — domain classification
falls back to `generic` at confidence `0.0` with an honest rationale; a
phase-assignment batch entry that doesn't validate simply contributes no
`PhaseAssignment` rows for that artifact — never a crash.

**Async bridge:** `LLMProvider.complete()` is async; this module (like the
rest of `analysis`) is sync end-to-end (sync `Session`). Each stage makes
its own `asyncio.run(...)` call at a clean boundary — once for the single
domain-classification call, once with `asyncio.gather(...)` for a batch's
worth of concurrent phase-assignment calls — and all persistence happens
synchronously afterward. The two never interleave: no `await` occurs while
a `Session` operation is in flight.

**Idempotency — two different granularities:**
  * Domain classification is project-level and has no per-artifact
    `StageState` row to key off; `DomainClassification.corpus_fingerprint_hash`
    plays that role directly on the (single, unique-per-project) row —
    recompute if absent or the hash changed, skip otherwise, never touch a
    confirmed one.
  * Phase assignment is per-artifact via `StageState(artifact_id,
    stage=phases)`, whose `input_hash` folds in the artifact's
    `content_hash`, the *selected* template's domain + a hash of its phase
    rows (so an edited template invalidates cached state), the LLM model,
    and `PHASE_VERSION`. A second run over unchanged content and an
    unchanged selected domain is a no-op; a domain change (e.g. the
    classifier flips domains, or a user confirms a different one) rebuilds
    every artifact's assignments.

`ProcessingState` is left untouched in both directions, for the same reason
`timeline.py` leaves it untouched: no member names this stage precisely, and
`StageState(stage=phases)` alone is this stage's source of truth.

Failure isolation and the "replace this artifact's auto rows wholesale on
rebuild" pattern mirror `extract.service` / `embed.service` / `timeline.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    AssignmentSource,
    AuditActor,
    DecisionAudit,
    DomainClassification,
    Entity,
    EntityMention,
    PhaseAssignment,
    PhaseTemplate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.reasoning.json_extract import extract_json
from truth_engine.reasoning.providers import LLMProvider, get_llm_provider

# Bump either when the assembly/validation rules in this module change
# materially enough that already-computed results should be recomputed.
DOMAIN_VERSION = "1"
PHASE_VERSION = "1"

_DOMAIN_SYSTEM_PROMPT = (
    "You are a document-classification assistant. Respond with ONLY a single "
    "JSON object -- no prose, no markdown fences."
)
_PHASE_SYSTEM_PROMPT = (
    "You are a project-phase classification assistant. Respond with ONLY a "
    "single JSON object -- no prose, no markdown fences."
)

_STRUCTURE_LABEL_KEYS = ("headings", "sheet_names", "slide_titles")


@dataclass
class PhasesResult:
    domain: str = ""
    domain_confidence: float = 0.0
    assigned: int = 0
    skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


@dataclass
class _DomainResult:
    domain: str
    confidence: float
    rationale: str


@dataclass
class _PhaseCandidate:
    phase_name: str
    confidence: float
    rationale: str


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #
def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _structure_labels(structure: dict[str, object] | None) -> list[str]:
    if not structure:
        return []
    labels: list[str] = []
    for key in _STRUCTURE_LABEL_KEYS:
        value = structure.get(key)
        if isinstance(value, list):
            labels.extend(str(v) for v in value)
    return labels


def _artifact_signal_line(
    artifact: Artifact, content: ArtifactContent | None, snippet_chars: int
) -> str:
    """A single compact line summarizing one artifact: filename, any
    structure headings/titles, and a short leading text snippet. Shared by
    both the corpus-level fingerprint and the per-artifact phase-assignment
    prompt -- the same cheap signals, just aggregated at different scopes."""
    parts = [artifact.original_filename]
    if content is not None:
        headings = _structure_labels(content.structure)
        if headings:
            parts.append("headings: " + ", ".join(headings))
        if content.raw_text:
            snippet = " ".join(content.raw_text.split())[:snippet_chars]
            if snippet:
                parts.append(f'"{snippet}"')
    return " | ".join(parts)


def _top_entities(
    session: Session, project_id: uuid.UUID, limit: int
) -> list[tuple[str, str, int]]:
    # Tiebreak by Entity.id: ties on mention count are common (e.g. two
    # entities each mentioned once), and without a deterministic tiebreak
    # which entities land in the top `limit` -- and in what order -- could
    # vary run to run over unchanged data, the same hash-stability concern
    # as `_corpus_fingerprint`'s `(ingested_at, id)` ordering above.
    rows = session.execute(
        select(Entity.type, Entity.value, func.count(EntityMention.id))
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .where(Entity.project_id == project_id)
        .group_by(Entity.id, Entity.type, Entity.value)
        .order_by(func.count(EntityMention.id).desc(), Entity.id)
        .limit(limit)
    ).all()
    return [(str(etype), value, count) for etype, value, count in rows]


# --------------------------------------------------------------------------- #
# Stage A -- domain classification                                           #
# --------------------------------------------------------------------------- #
def _candidate_domains(session: Session) -> list[str]:
    """The classifiable domain set, derived from `phase_templates` rather
    than hardcoded -- a new vertical is added by seeding a template
    (`db/seed.py`), not by editing this module."""
    domains = session.scalars(
        select(PhaseTemplate.domain).where(PhaseTemplate.domain != "generic").distinct()
    ).all()
    return sorted(domains)


def _corpus_fingerprint(session: Session, project_id: uuid.UUID, settings: Settings) -> str:
    """Cheap corpus-level signals only -- filenames, structure headings,
    short snippets, and top recurring entities -- never a raw-text dump.

    Ordered by `(ingested_at, id)`, not `ingested_at` alone: a whole-folder
    ingest commits every artifact in one transaction, and Postgres's `now()`
    is constant within a transaction, so ties on `ingested_at` are the
    common case, not an edge case, here. `id` (a UUID, unrelated to insertion
    order) breaks the tie deterministically so two runs over byte-identical
    data always produce the same fingerprint text and hash -- otherwise
    `classify_project_domain`'s "recompute only if the corpus changed" check
    could spuriously fire on a corpus that didn't change at all. Same
    reasoning as `_top_entities`'s tiebreak below and `timeline.py`'s
    `_input_hash` docstring ("sorted so DB/insertion row order never
    perturbs the hash")."""
    rows = session.execute(
        select(Artifact, ArtifactContent)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.ingested_at, Artifact.id)
        .limit(settings.domain_fingerprint_max_artifacts)
    ).all()
    lines = [
        _artifact_signal_line(artifact, content, settings.domain_fingerprint_snippet_chars)
        for artifact, content in rows
    ]

    entity_lines = [
        f"{etype}: {value} ({count}x)"
        for etype, value, count in _top_entities(
            session, project_id, settings.domain_fingerprint_max_entities
        )
    ]

    parts = ["Artifacts:"] + (lines or ["(none)"])
    if entity_lines:
        parts += ["", "Recurring entities:"] + entity_lines
    return "\n".join(parts)


def _fingerprint_hash(fingerprint: str) -> str:
    raw = f"{DOMAIN_VERSION}\n{fingerprint}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _domain_prompt(fingerprint: str, candidate_domains: list[str]) -> str:
    domains = ", ".join(candidate_domains)
    return (
        "Classify the general field/domain of a project from the corpus signals "
        "below (filenames, document headings/titles, short leading excerpts, and "
        "recurring entities).\n\n"
        f"Candidate domains: {domains}\n"
        "Choose the single best-fitting candidate even if the fit is imperfect -- "
        "reflect any uncertainty in the confidence score rather than omitting a "
        "domain.\n\n"
        f"{fingerprint}\n\n"
        "Respond with exactly one JSON object of this form:\n"
        f'{{"domain": "<one of: {domains}>", "confidence": <0.0-1.0>, '
        '"rationale": "<one sentence>"}'
    )


def _parse_domain_response(raw: str, candidate_domains: list[str]) -> _DomainResult:
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return _DomainResult("generic", 0.0, "LLM response was not parseable JSON.")

    domain = parsed.get("domain")
    confidence = parsed.get("confidence")
    rationale = str(parsed.get("rationale") or "")

    if domain not in candidate_domains:
        return _DomainResult(
            "generic", 0.0, f"LLM proposed domain {domain!r}, not in the candidate set."
        )
    if not isinstance(confidence, int | float):
        return _DomainResult("generic", 0.0, "LLM response was missing a numeric confidence.")

    return _DomainResult(domain, max(0.0, min(1.0, float(confidence))), rationale)


def classify_project_domain(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> DomainClassification:
    """Classify (or reuse) a project's domain. Never overwrites a
    `confirmed_by_user` row. Recomputes when absent or the corpus
    fingerprint changed since the last classification; otherwise reuses the
    existing row untouched, with no LLM call.
    """
    existing = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project_id)
    )
    if existing is not None and existing.confirmed_by_user:
        return existing

    settings = get_settings()
    candidate_domains = _candidate_domains(session)
    fingerprint = _corpus_fingerprint(session, project_id, settings)
    fingerprint_hash = _fingerprint_hash(fingerprint)
    if existing is not None and existing.corpus_fingerprint_hash == fingerprint_hash:
        return existing

    provider = provider or get_llm_provider()
    prompt = _domain_prompt(fingerprint, candidate_domains)
    raw = asyncio.run(provider.complete(prompt, system=_DOMAIN_SYSTEM_PROMPT))
    result = _parse_domain_response(raw, candidate_domains)

    if existing is None:
        existing = DomainClassification(project_id=project_id)
        session.add(existing)
    existing.domain = result.domain
    existing.confidence = result.confidence
    existing.model = provider.model
    existing.corpus_fingerprint_hash = fingerprint_hash
    existing.confirmed_by_user = False  # a freshly (re)computed row starts unconfirmed
    session.flush()  # populate existing.id for the audit target

    session.add(
        DecisionAudit(
            decision_type="domain_classification",
            target_id=existing.id,
            new_value={"domain": result.domain, "confidence": result.confidence},
            actor=AuditActor.system,
            model=provider.model,
            model_version=provider.model_version,
            rationale=result.rationale,
        )
    )
    return existing


def _select_template_domain(classification: DomainClassification, threshold: float) -> str:
    """Which `PhaseTemplate.domain` to map artifacts against -- distinct
    from `classification.domain`, which always records the LLM's honest
    best guess even below the confidence threshold (§3.3.4: never force a
    bad template, but don't fake certainty either)."""
    return classification.domain if classification.confidence >= threshold else "generic"


# --------------------------------------------------------------------------- #
# Stage B -- phase assignment                                                #
# --------------------------------------------------------------------------- #
def _phase_templates_for_domain(session: Session, domain: str) -> list[PhaseTemplate]:
    return list(
        session.scalars(
            select(PhaseTemplate)
            .where(PhaseTemplate.domain == domain)
            .order_by(PhaseTemplate.ordinal)
        ).all()
    )


def _template_hash(phases: list[PhaseTemplate]) -> str:
    parts = sorted(f"{p.id}|{p.phase_name}|{p.ordinal}|{p.description or ''}" for p in phases)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _artifact_input_hash(
    artifact: Artifact, template_domain: str, template_hash: str, provider: LLMProvider
) -> str:
    raw = (
        f"{artifact.content_hash}:{template_domain}:{template_hash}:"
        f"{provider.model}:{PHASE_VERSION}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _phase_prompt(batch: list[tuple[int, str]], phases: list[PhaseTemplate]) -> str:
    phase_lines = "\n".join(f"- {p.phase_name}: {p.description or ''}" for p in phases)
    artifact_lines = "\n".join(f"[{idx}] {line}" for idx, line in batch)
    example = {
        "assignments": [
            {
                "artifact_index": 0,
                "phases": [{"phase": phases[0].phase_name, "confidence": 0.8, "rationale": "..."}],
            }
        ]
    }
    return (
        "Map each numbered artifact below to the project phase(s) it most likely "
        "belongs to. An artifact MAY belong to more than one phase (e.g. a "
        "meeting note spanning planning and execution). Only use phase names "
        "from the list below, verbatim; if an artifact doesn't clearly fit any "
        "phase, omit it rather than guessing.\n\n"
        f"Phases:\n{phase_lines}\n\n"
        f"Artifacts:\n{artifact_lines}\n\n"
        "Respond with exactly one JSON object shaped like this example "
        "(artifact_index refers to the [N] markers above):\n"
        f"{json.dumps(example)}"
    )


def _parse_phase_response(
    raw: str, batch_size: int, phase_names_by_lower: dict[str, str]
) -> dict[int, list[_PhaseCandidate]]:
    """Defensively parse a batch's phase-assignment response. Any artifact
    index that's missing, malformed, or names phases outside the template
    contributes nothing -- the caller persists zero `PhaseAssignment` rows
    for it, an honest 'the model gave nothing usable' outcome, not a crash.
    """
    parsed = extract_json(raw)
    result: dict[int, list[_PhaseCandidate]] = {}
    if not isinstance(parsed, dict):
        return result
    assignments = parsed.get("assignments")
    if not isinstance(assignments, list):
        return result

    for entry in assignments:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("artifact_index")
        if not isinstance(idx, int) or not (0 <= idx < batch_size):
            continue
        phase_entries = entry.get("phases")
        if not isinstance(phase_entries, list):
            continue

        candidates = []
        for p in phase_entries:
            if not isinstance(p, dict):
                continue
            name = phase_names_by_lower.get(str(p.get("phase", "")).strip().lower())
            confidence = p.get("confidence")
            if name is None or not isinstance(confidence, int | float):
                continue
            candidates.append(
                _PhaseCandidate(
                    phase_name=name,
                    confidence=max(0.0, min(1.0, float(confidence))),
                    rationale=str(p.get("rationale") or ""),
                )
            )
        if candidates:
            result[idx] = candidates

    return result


async def _complete_batches(provider: LLMProvider, prompts: list[str]) -> list[str]:
    return await asyncio.gather(
        *(provider.complete(p, system=_PHASE_SYSTEM_PROMPT) for p in prompts)
    )


def _replace_phase_assignments(
    session: Session,
    artifact: Artifact,
    candidates: list[_PhaseCandidate],
    phases_by_name: dict[str, PhaseTemplate],
    provider: LLMProvider,
) -> None:
    # Wholesale-replace only this artifact's *auto* rows -- human-sourced
    # PhaseAssignment rows (source=human) are never touched.
    session.execute(
        delete(PhaseAssignment).where(
            PhaseAssignment.artifact_id == artifact.id,
            PhaseAssignment.source == AssignmentSource.auto,
        )
    )
    for candidate in candidates:
        phase = phases_by_name[candidate.phase_name]
        row = PhaseAssignment(
            artifact_id=artifact.id,
            phase_id=phase.id,
            confidence=candidate.confidence,
            rationale=candidate.rationale,
            source=AssignmentSource.auto,
        )
        session.add(row)
        session.flush()  # populate row.id for the audit target
        session.add(
            DecisionAudit(
                decision_type="phase_assignment",
                target_id=row.id,
                new_value={"phase": phase.phase_name, "confidence": candidate.confidence},
                actor=AuditActor.system,
                model=provider.model,
                model_version=provider.model_version,
                rationale=candidate.rationale,
            )
        )


def _upsert_stage_state_done(
    session: Session, artifact_id: uuid.UUID, stage: Stage, input_hash: str
) -> None:
    state = _stage_state(session, artifact_id, stage)
    if state is None:
        state = StageState(artifact_id=artifact_id, stage=stage, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None


def _record_phase_error(
    session: Session, artifact: Artifact, input_hash: str, message: str
) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.phases)
    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.phases, input_hash=input_hash)
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    session.commit()


def assign_project_phases(
    session: Session,
    project_id: uuid.UUID,
    classification: DomainClassification,
    *,
    provider: LLMProvider | None = None,
) -> PhasesResult:
    """Map every artifact in a project to phase(s) of the template selected
    for `classification`, skipping artifacts whose content, selected
    domain/template, and LLM model are all unchanged since the last run."""
    settings = get_settings()
    provider = provider or get_llm_provider()
    result = PhasesResult()

    template_domain = _select_template_domain(classification, settings.domain_confidence_threshold)
    phases = _phase_templates_for_domain(session, template_domain)
    if not phases:  # defensive: template_domain not seeded (shouldn't happen post db.seed)
        phases = _phase_templates_for_domain(session, "generic")
        template_domain = "generic"
    template_hash = _template_hash(phases)
    phases_by_name = {p.phase_name: p for p in phases}
    phase_names_by_lower = {p.phase_name.lower(): p.phase_name for p in phases}

    # (ingested_at, id) tiebreak: same reasoning as `_corpus_fingerprint`
    # above -- a whole-folder ingest commits every artifact with an
    # identical `ingested_at`, so batch composition/order would otherwise be
    # nondeterministic across runs over unchanged data.
    rows = session.execute(
        select(Artifact, ArtifactContent)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.ingested_at, Artifact.id)
    ).all()

    pending: list[tuple[Artifact, ArtifactContent | None, str]] = []
    for artifact, content in rows:
        input_hash = _artifact_input_hash(artifact, template_domain, template_hash, provider)
        state = _stage_state(session, artifact.id, Stage.phases)
        if state and state.status == StageStatus.done and state.input_hash == input_hash:
            result.skipped += 1
            continue
        pending.append((artifact, content, input_hash))

    if not pending:
        return result

    batches = [
        pending[i : i + settings.phase_assignment_batch_size]
        for i in range(0, len(pending), settings.phase_assignment_batch_size)
    ]
    prompts = [
        _phase_prompt(
            [
                (i, _artifact_signal_line(a, c, settings.phase_assignment_snippet_chars))
                for i, (a, c, _) in enumerate(batch)
            ],
            phases,
        )
        for batch in batches
    ]
    raw_responses = asyncio.run(_complete_batches(provider, prompts))

    for batch, raw in zip(batches, raw_responses, strict=True):
        parsed = _parse_phase_response(raw, len(batch), phase_names_by_lower)
        for i, (artifact, _content, input_hash) in enumerate(batch):
            try:
                _replace_phase_assignments(
                    session, artifact, parsed.get(i, []), phases_by_name, provider
                )
                _upsert_stage_state_done(session, artifact.id, Stage.phases, input_hash)
                session.commit()
                result.assigned += 1
            except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
                _record_phase_error(session, artifact, input_hash, str(exc))
                result.errors.append((artifact, str(exc)))

    return result


def run_project_phases(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> PhasesResult:
    """Classify (or reuse) the project's domain, then map every artifact to
    phase(s) of the selected template. The two stages share one provider
    instance so both audit trails record the same `model`."""
    provider = provider or get_llm_provider()
    classification = classify_project_domain(session, project_id, provider=provider)
    session.commit()

    result = assign_project_phases(session, project_id, classification, provider=provider)
    result.domain = classification.domain
    result.domain_confidence = classification.confidence
    return result


def main() -> None:
    """CLI: classify (or reuse) a project's domain, then map every artifact
    to phase(s) of the selected phase template.

        uv run python -m truth_engine.analysis.phases --project-id <uuid>

    Domain classification makes no LLM call when unchanged or when a prior
    classification has been confirmed by the user. Phase assignment is
    per-artifact and gated by StageState like the other analysis stages. Run
    after `python -m truth_engine.extract`; embeddings are not required.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_project_phases(session, args.project_id)

    print(
        f"phases: domain={result.domain} (confidence={result.domain_confidence:.2f}); "
        f"{result.assigned} assigned, {result.skipped} skipped, {len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
