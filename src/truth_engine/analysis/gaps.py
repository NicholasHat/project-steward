"""Stage 10 orchestration (PROJECTSPECS.md §3.5, Gap Detection): two kinds of
`Gap`, deliberately distinguished in type *and* confidence band, gated by
`StageState(stage=gaps)`.

**§5's non-goal governs this whole module:** "not attempting perfect
automated gap detection -- the goal is a strong first-pass draft a human
reviews and corrects." Every gap this module writes starts `status=open`
(`GapStatus`) and is a *candidate*, never a verdict.

**Kind 1 -- structural (deterministic, higher confidence).** Using the
domain's selected `PhaseTemplate` (step 7, the same `_select_template_domain`
rule `phases.py` uses -- see below) and `PhaseAssignment` rows: a phase
covered by fewer than `Settings.gap_structural_few_threshold` distinct
artifacts is a candidate gap. Coverage counts *both* `auto` and `human`
`PhaseAssignment.source` rows -- a human-assigned artifact is at least as
trustworthy a signal of coverage as an automated one, so excluding it would
make a human correction actively worse for gap detection, not better. Zero
coverage and "a few" are both gaps but banded apart
(`gap_structural_confidence_zero` vs. `_few`) -- a discrete, deterministic
count needs no fuzzier scoring than that. `phase_id` is always set;
`evidence` states the raw coverage counts so a human can verify the claim
without re-deriving it.

**Kind 2 -- promised-but-unfulfilled (content-level, lower/fuzzier
confidence).** Two-step, deliberately split at the deterministic/LLM
boundary the same way `phases.py`/`direction.py` split theirs:

  1. *Candidate detection is 100% deterministic* -- a curated, recall-
     favoring regex marker set (`_PROMISE_PATTERNS`, covering PROJECTSPECS
     §3.5's own examples plus the task's "next step:"/"to be determined")
     scanned once per artifact's `raw_text`. Nearby matches merge into one
     candidate (`_merge_spans`) so "we still need to run the control
     experiment, and the cost is TBD" doesn't produce two near-duplicate
     gaps for one sentence; a normalized-evidence dedupe collapses a marker
     repeated verbatim elsewhere in the same document.
  2. *Fulfillment judgment is the fuzzy part* (the plan calls gap detection
     LLM-partial). A deterministic keyword-overlap heuristic
     (`_deterministic_fulfillment`) runs first and is always available as a
     fallback: does any chronologically-*later* artifact's text contain
     enough of the promise's own significant vocabulary? If yes, the promise
     is fulfilled and **no gap is written at all** -- recall matters for
     surfacing real gaps, not for manufacturing gaps out of promises that
     were plainly kept. If `Settings.gap_promised_llm_enabled`, unresolved
     candidates get one batched LLM pass (`_llm_judge_unresolved`, mirroring
     `phases.py`'s per-batch `asyncio.gather` bridge) asking it to weigh the
     same "later artifacts" set with actual language understanding instead
     of token overlap. Any LLM response that's unparseable or names an
     out-of-range candidate index degrades to the deterministic verdict for
     *that candidate only* -- never a crash, never a silently dropped
     candidate (§3.5's "strong first-pass draft," not a fully autonomous
     one).

**Confidence bands, deliberately non-overlapping (§3.5's "different
confidence levels... presented differently").** Promised-unfulfilled tops
out at `gap_promised_confidence_llm_cap` (0.65 by default), strictly below
structural's floor at `gap_structural_confidence_few` (0.7) -- a human
scanning gaps by confidence never sees a fuzzy content-level guess outrank a
deterministic phase-coverage count. Within the promised band itself: "no
later artifact exists at all" (`_confidence_no_later_artifacts`) is the
simplest, least ambiguous unfulfilled case and scores highest; a bare
keyword-overlap miss (`_confidence_deterministic`) is the fuzziest and scores
lowest; an LLM verdict sits at its own (capped) reported confidence.

**Stable gap identity, without a schema change.** `Gap` (see `db.models`)
has no artifact/hash column -- and per the task, none is expected. Identity
is instead *computed at match time* from the columns that already exist:
  * structural: `(project_id, type=structural, phase_id)` -- `phase_id` is
    already a real column, already unique per phase.
  * promised: `(project_id, type=promised_unfulfilled, normalized(description))`
    -- `description` is deterministically built from the source artifact's
    filename plus the promise evidence (`_promise_description`), so
    normalizing it folds "source artifact" and "the promise text" into one
    stable string with no new column. Unchanged artifact text -> byte-
    identical evidence -> byte-identical normalized description -> the same
    identity on every rerun. If the underlying text genuinely changes, the
    identity correctly changes too -- that's *correct* incremental behavior
    (a new candidate for review), not a bug: a hash column pinned to old
    content would either stay stale or need its own invalidation logic this
    approach gets for free.

**Human review -- never clobbered (`_reconcile_gaps`).** For each freshly
computed draft: no existing row at that identity -> insert `status=open`; an
existing `status=open` row at that identity -> update in place (regenerate,
matching `phases.py`'s "wholesale-replace only the auto rows" precedent); an
existing row already acted on by a human (`confirmed`/`dismissed`/
`resolved`) -> left completely untouched, not even its `evidence`/
`confidence` refreshed. Symmetrically, an existing `open` gap whose identity
*isn't* reproduced this run (the phase gained coverage; the promise is now
judged fulfilled) is deleted -- stale auto-drafts don't linger, but a human's
`dismissed`/`confirmed`/`resolved` verdict is permanent history and is never
deleted either, even if its underlying condition no longer holds.

**Auditability decision.** Structural gaps get **no** `DecisionAudit` row --
mirroring `graph.py`'s precedent (deterministic count, self-documenting via
`evidence`, no old/new transition beyond what wholesale-replace already
captures) rather than `phases.py`'s. Promised-unfulfilled gaps **do** get one
`DecisionAudit` per created-or-updated row, always, whether the verdict came
from the LLM (`model`/`model_version` = the provider's) or the deterministic
fallback (`model=_PROMISE_RULESET`, `model_version=GAP_VERSION`, mirroring
`direction.py`'s `_LABEL_RULESET`/`DIRECTION_VERSION` for its own
deterministic-but-judged `DirectionLabel` rows). The reasoning: unlike a
graph edge or a phase-coverage count, "is this promise actually unfulfilled"
is a genuine judgment call over fuzzy content -- exactly the kind of
inferred fact CLAUDE.md's auditability invariant exists for -- and it's the
single row type in this module a human is most likely to want to interrogate
("why did the system think this wasn't addressed?"). Auditing it
unconditionally (not just the LLM-judged subset) keeps one code path instead
of a branch on judgment source, and costs nothing extra since these rows
already carry a `rationale` either way. The same reasoning extends to the
*reverse* transition: a promised gap disappearing because it's no longer
reproduced (judged fulfilled, or the marker text is gone) is just as much an
inferred verdict as one appearing, so `_reconcile_gaps`'s prune step
(`_audit_promised_prune`) writes one `DecisionAudit` (`old_value` the gap's
last state, `new_value=None`) for every pruned *promised* gap -- pruned
structural gaps stay unaudited, symmetric with the create/update decision
above.

**Idempotency -- project-wide, mirroring `direction.py`/`graph.py`.**
Structural gaps depend on *every* artifact's `PhaseAssignment`; a promise's
fulfillment depends on *every* other artifact's text and chosen date. Neither
is an incremental per-artifact computation, so this module folds one
project-wide fingerprint (`_corpus_fingerprint_hash`: phase assignments +
the selected template's phase rows + every artifact's `content_hash` + every
chosen `ResolvedDate`) into every artifact's `StageState.input_hash`, same
formula as `direction.py`'s `_artifact_input_hash`. Unlike `direction.py`
there's no per-artifact `confirmed_by_user` row that can independently be
"settled" ahead of a fingerprint change (gap review status lives on `Gap`,
not on an artifact), so the check collapses to one question: are *all*
artifacts already `done` at the current fingerprint-derived hash? If so this
is a full no-op (no queries beyond the check, no LLM call). If not, the
*entire* structural + promised computation reruns and every artifact's
`StageState` is written `done` at the new hash together -- there is no
meaningful "some artifacts pending, some settled" state for a computation
this globally coupled.

`ProcessingState` is left untouched, for the same reason every other
`analysis` module leaves it untouched: `StageState(stage=gaps)` alone is
this stage's source of truth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
    AuditActor,
    DecisionAudit,
    DomainClassification,
    Gap,
    GapStatus,
    GapType,
    PhaseAssignment,
    PhaseTemplate,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.reasoning.json_extract import extract_json
from truth_engine.reasoning.providers import LLMProvider, get_llm_provider

# Bump when the detection/fulfillment/identity rules in this module change
# materially enough that already-computed (open) gaps should be rebuilt.
GAP_VERSION = "1"

# DecisionAudit.model for a promised-unfulfilled verdict reached without an
# LLM (no later artifacts to check, LLM disabled, or its response was
# unusable) -- mirrors direction.py's `_LABEL_RULESET`.
_PROMISE_RULESET = "gap-promise-ruleset"

_PROMISE_SYSTEM_PROMPT = (
    "You are an assistant that judges whether a project artifact's promised or "
    "pending item was later addressed by another artifact. Respond with ONLY a "
    "single JSON object -- no prose, no markdown fences."
)

# Curated, recall-favoring markers for a future/pending item referenced in
# text -- PROJECTSPECS §3.5's own examples ("we still need to run the control
# experiment," "cost estimate TBD," "waiting on results from Group X") plus
# the task's "next step:" / "to be determined". Deliberately not "plan to" /
# "will" / "in progress" -- those false-positive heavily on routine future-
# tense prose rather than a genuinely flagged-as-outstanding item.
_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bstill need(?:s|ed)?\b",
        r"\bwe need to\b",
        r"\bTBD\b",
        r"\bto be determined\b",
        r"\bto be confirmed\b",
        r"\bwaiting on\b",
        r"\bwaiting for\b",
        r"\bpending\b",
        r"\bnext steps?\s*:",
        r"\byet to be\b",
        r"\bhave yet to\b",
        r"\bremains?\s+to\s+be\b",
        r"\bto[- ]?do\s*:",
        r"\bfuture work\b",
    )
)

# Boilerplate marker vocabulary excluded from the deterministic fulfillment
# heuristic's token-overlap check -- these words appear in nearly every
# promise candidate by construction (they're what matched `_PROMISE_PATTERNS`
# in the first place), so requiring them in a *later* artifact's text would
# be checking for the marker phrase, not the promised content, and would
# systematically undercount real fulfillment. Otherwise a standard small
# English stopword list.
_STOPWORDS = frozenset(
    {
        "this", "that", "these", "those", "with", "from", "have", "will",
        "been", "were", "what", "when", "where", "which", "while", "about",
        "into", "over", "under", "there", "their", "they", "would", "could",
        "should", "after", "before", "still", "need", "needs", "needed",
        "wait", "waiting", "pending", "determine", "determined", "confirm",
        "confirmed", "yet", "next", "step", "steps", "remain", "remains",
        "future", "work",
    }
)  # fmt: skip


@dataclass
class GapsResult:
    structural: int = 0
    promised: int = 0
    created: int = 0
    updated: int = 0
    preserved: int = 0
    pruned: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _GapDraft:
    type: GapType
    phase_id: uuid.UUID | None
    description: str
    evidence: str
    confidence: float
    rationale: str = ""
    model: str | None = None
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class _PromiseCandidate:
    artifact_id: uuid.UUID
    evidence: str  # the marker's containing sentence(s) -- see `_sentence_span`
    normalized: str  # within-run dedupe key


# --------------------------------------------------------------------------- #
# Shared helpers (deliberately duplicated per-module -- see phases.py/        #
# direction.py/graph.py's own copies of the same small helpers)               #
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


def _normalize_snippet(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _select_template_domain(classification: DomainClassification, threshold: float) -> str:
    """Same rule as `phases.py`'s function of the same name: which
    `PhaseTemplate.domain` structural coverage is checked against, distinct
    from the classifier's own honest (possibly low-confidence) guess."""
    return classification.domain if classification.confidence >= threshold else "generic"


def _phase_templates_for_domain(session: Session, domain: str) -> list[PhaseTemplate]:
    return list(
        session.scalars(
            select(PhaseTemplate)
            .where(PhaseTemplate.domain == domain)
            .order_by(PhaseTemplate.ordinal)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Idempotency: project-wide fingerprint folded into every artifact's         #
# StageState.input_hash (see module docstring)                               #
# --------------------------------------------------------------------------- #
def _corpus_fingerprint_hash(session: Session, project_id: uuid.UUID, settings: Settings) -> str:
    assignment_rows = session.execute(
        select(PhaseAssignment.artifact_id, PhaseAssignment.phase_id)
        .join(Artifact, Artifact.id == PhaseAssignment.artifact_id)
        .where(Artifact.project_id == project_id)
    ).all()
    assignment_lines = sorted(f"{aid}|{pid}" for aid, pid in assignment_rows)

    classification = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project_id)
    )
    phase_lines: list[str] = []
    if classification is not None:
        template_domain = _select_template_domain(
            classification, settings.domain_confidence_threshold
        )
        phases = _phase_templates_for_domain(session, template_domain)
        phase_lines = sorted(f"{p.id}|{p.phase_name}|{p.ordinal}" for p in phases)

    content_rows = session.execute(
        select(Artifact.id, Artifact.content_hash).where(Artifact.project_id == project_id)
    ).all()
    content_lines = sorted(f"{aid}|{chash}" for aid, chash in content_rows)

    date_lines = sorted(
        f"{aid}|{d.isoformat()}" for aid, d in _chosen_dates(session, project_id).items()
    )

    raw = (
        f"{GAP_VERSION}\n"
        + "\n".join(assignment_lines)
        + "\n--\n"
        + "\n".join(phase_lines)
        + "\n--\n"
        + "\n".join(content_lines)
        + "\n--\n"
        + "\n".join(date_lines)
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _artifact_input_hash(artifact_id: uuid.UUID, fingerprint_hash: str) -> str:
    raw = f"{GAP_VERSION}:{artifact_id}:{fingerprint_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_up_to_date(session: Session, artifacts: list[Artifact], fingerprint_hash: str) -> bool:
    for artifact in artifacts:
        state = _stage_state(session, artifact.id, Stage.gaps)
        expected = _artifact_input_hash(artifact.id, fingerprint_hash)
        if state is None or state.status != StageStatus.done or state.input_hash != expected:
            return False
    return True


def _upsert_stage_state_done(session: Session, artifact_id: uuid.UUID, input_hash: str) -> None:
    state = _stage_state(session, artifact_id, Stage.gaps)
    if state is None:
        state = StageState(artifact_id=artifact_id, stage=Stage.gaps, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None


# --------------------------------------------------------------------------- #
# Kind 1 -- structural gaps (deterministic)                                  #
# --------------------------------------------------------------------------- #
def _detect_structural_gaps(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> list[_GapDraft]:
    """Empty if domain classification hasn't run yet (§3.5 structural gaps
    are meaningless without a selected phase template) -- run `phases` first;
    this degrades gracefully rather than erroring, like every other
    cross-stage dependency in `analysis`."""
    classification = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project_id)
    )
    if classification is None:
        return []

    template_domain = _select_template_domain(classification, settings.domain_confidence_threshold)
    phases = _phase_templates_for_domain(session, template_domain)
    if not phases:
        return []

    coverage = dict(
        session.execute(
            select(PhaseAssignment.phase_id, func.count(func.distinct(PhaseAssignment.artifact_id)))
            .join(Artifact, Artifact.id == PhaseAssignment.artifact_id)
            .where(
                Artifact.project_id == project_id,
                PhaseAssignment.phase_id.in_([p.id for p in phases]),
            )
            .group_by(PhaseAssignment.phase_id)
        ).all()
    )
    total_artifacts = (
        session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id)
        )
        or 0
    )

    threshold = settings.gap_structural_few_threshold
    drafts: list[_GapDraft] = []
    for phase in phases:
        count = coverage.get(phase.id, 0)
        if count >= threshold:
            continue
        if count == 0:
            description = f"No artifacts detected for the '{phase.phase_name}' phase."
            confidence = settings.gap_structural_confidence_zero
        else:
            description = (
                f"Only {count} artifact(s) detected for the '{phase.phase_name}' phase "
                f"(expected at least {threshold})."
            )
            confidence = settings.gap_structural_confidence_few
        evidence = (
            f"{count} of {total_artifacts} project artifact(s) mapped to phase "
            f"'{phase.phase_name}' via PhaseAssignment (threshold: {threshold})."
        )
        drafts.append(_GapDraft(GapType.structural, phase.id, description, evidence, confidence))
    return drafts


# --------------------------------------------------------------------------- #
# Kind 2 -- promised-but-unfulfilled gaps                                    #
# --------------------------------------------------------------------------- #
def _merge_spans(spans: list[tuple[int, int]], gap_chars: int) -> list[tuple[int, int]]:
    """Marker matches within `gap_chars` of each other collapse into one
    candidate span -- see module docstring."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + gap_chars:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


_SENTENCE_ENDERS = (".", "!", "?")


def _sentence_span(text: str, start: int, end: int, *, max_radius: int) -> tuple[int, int]:
    """Expand `(start, end)` out to the sentence(s) it falls within, rather
    than a fixed character radius. A marker like "next step:" carries no
    information on its own -- the promised item is the clause that follows
    it in the same sentence -- so evidence must include that clause. But a
    raw character radius (the earlier approach) just as easily pulls in
    *unrelated* neighboring sentences, diluting both the human-facing
    snippet and (more consequentially) the deterministic token-overlap
    fulfillment check with words that have nothing to do with the promise.
    Bounded by the nearest sentence-ending punctuation or blank line on each
    side; `max_radius` is a safety valve only, for text with no punctuation
    at all (e.g. a table dump) where "the sentence" would otherwise be the
    entire document."""
    left_limit = max(0, start - max_radius)
    right_limit = min(len(text), end + max_radius)

    left_boundary = max(
        [text.rfind(ch, left_limit, start) for ch in _SENTENCE_ENDERS]
        + [text.rfind("\n\n", left_limit, start)]
    )
    left = left_boundary + 1 if left_boundary >= 0 else left_limit

    right_candidates = [
        pos
        for pos in (
            [text.find(ch, end, right_limit) for ch in _SENTENCE_ENDERS]
            + [text.find("\n\n", end, right_limit)]
        )
        if pos != -1
    ]
    right = min(right_candidates) + 1 if right_candidates else right_limit

    while left < end and text[left].isspace():
        left += 1
    return left, right


def _artifact_promise_candidates(
    artifact: Artifact, raw_text: str, settings: Settings
) -> list[_PromiseCandidate]:
    spans = [m.span() for pattern in _PROMISE_PATTERNS for m in pattern.finditer(raw_text)]
    if not spans:
        return []

    candidates: list[_PromiseCandidate] = []
    seen: set[str] = set()
    for start, end in _merge_spans(spans, settings.gap_promised_merge_gap_chars):
        sent_start, sent_end = _sentence_span(
            raw_text, start, end, max_radius=settings.gap_promised_evidence_radius
        )
        evidence = raw_text[sent_start:sent_end].strip()
        normalized = _normalize_snippet(evidence)
        if normalized in seen:
            continue  # exact-ish repeat of the same marker elsewhere in this document
        seen.add(normalized)
        candidates.append(
            _PromiseCandidate(artifact_id=artifact.id, evidence=evidence, normalized=normalized)
        )
    return candidates


def _significant_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _deterministic_fulfillment(
    evidence: str, later_texts: list[str], overlap_threshold: float
) -> bool:
    """Cheap, always-available fallback: does any later artifact's text
    contain enough of the promise's own significant (non-boilerplate)
    vocabulary? `evidence` is already sentence-scoped (`_sentence_span`), not
    a raw character radius, so its tokens are the promise's own clause
    rather than diluted by adjacent unrelated sentences. A real hit here
    means the promise is fulfilled -- no gap is written at all, regardless
    of whether the LLM pass is enabled."""
    tokens = _significant_tokens(evidence)
    if not tokens:
        return False
    for text in later_texts:
        lowered = text.lower()
        hits = sum(1 for token in tokens if token in lowered)
        if hits / len(tokens) >= overlap_threshold:
            return True
    return False


def _later_artifacts(
    source_id: uuid.UUID, all_ids: list[uuid.UUID], dates_by_artifact: dict[uuid.UUID, datetime]
) -> tuple[list[uuid.UUID], bool]:
    """`(candidate_ids, ordering_known)`. When the source artifact has a
    chosen date, only artifacts with a *later* chosen date qualify --
    matching §3.5's literal "no later-dated artifact addresses it." When it
    doesn't, chronological ordering can't be established at all; rather than
    treat that as vacuously unfulfilled, every other artifact in the project
    is checked best-effort (favor recall, per the task), and the caller notes
    the ordering uncertainty in the gap's rationale."""
    own_date = dates_by_artifact.get(source_id)
    if own_date is not None:
        later = [
            aid
            for aid in all_ids
            if aid != source_id and (d := dates_by_artifact.get(aid)) is not None and d > own_date
        ]
        return later, True
    return [aid for aid in all_ids if aid != source_id], False


def _promise_description(artifact: Artifact, evidence: str) -> str:
    return (
        f"'{artifact.original_filename}' appears to promise a future/pending item that no "
        f'later artifact addresses: "{evidence}"'
    )


def _promise_gap_draft(
    artifact: Artifact,
    candidate: _PromiseCandidate,
    confidence: float,
    rationale: str,
    *,
    model: str,
    model_version: str,
) -> _GapDraft:
    return _GapDraft(
        type=GapType.promised_unfulfilled,
        phase_id=None,
        description=_promise_description(artifact, candidate.evidence),
        evidence=candidate.evidence,
        confidence=confidence,
        rationale=rationale,
        model=model,
        model_version=model_version,
    )


def _promise_prompt(entries: list[tuple[int, _PromiseCandidate, list[str]]]) -> str:
    blocks = []
    for idx, candidate, excerpts in entries:
        excerpt_block = (
            "\n".join(f"  - {e}" for e in excerpts)
            if excerpts
            else "  (no later artifacts to compare against)"
        )
        blocks.append(
            f'[{idx}] Promise: "{candidate.evidence}"\n  Later artifacts:\n{excerpt_block}'
        )
    example = {
        "judgments": [
            {"candidate_index": 0, "fulfilled": False, "confidence": 0.5, "rationale": "..."}
        ]
    }
    return (
        "For each numbered promise below (a snippet from a project artifact that mentions a "
        "future or pending item), decide whether any of its listed later artifacts fulfills or "
        "addresses it. If unsure, prefer fulfilled=false and reflect the uncertainty in "
        "confidence rather than guessing fulfilled=true.\n\n"
        "Respond with exactly one JSON object shaped like this example (candidate_index refers "
        "to the [N] markers below):\n"
        f"{json.dumps(example)}\n\n" + "\n\n".join(blocks)
    )


def _parse_promise_response(raw: str, batch_size: int) -> dict[int, tuple[bool, float, str]]:
    """Defensively parse a batch's fulfillment-judgment response. Any
    candidate index that's missing, malformed, or out of range contributes
    nothing -- the caller degrades that one candidate to the deterministic
    verdict rather than crashing or dropping it."""
    parsed = extract_json(raw)
    result: dict[int, tuple[bool, float, str]] = {}
    if not isinstance(parsed, dict):
        return result
    judgments = parsed.get("judgments")
    if not isinstance(judgments, list):
        return result

    for entry in judgments:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("candidate_index")
        if not isinstance(idx, int) or not (0 <= idx < batch_size):
            continue
        fulfilled = entry.get("fulfilled")
        confidence = entry.get("confidence")
        if not isinstance(fulfilled, bool) or not isinstance(confidence, int | float):
            continue
        rationale = str(entry.get("rationale") or "")
        result[idx] = (fulfilled, max(0.0, min(1.0, float(confidence))), rationale)
    return result


async def _complete_promise_batches(provider: LLMProvider, prompts: list[str]) -> list[str]:
    return await asyncio.gather(
        *(provider.complete(p, system=_PROMISE_SYSTEM_PROMPT) for p in prompts)
    )


def _prompt_entries(
    batch: list[tuple[_PromiseCandidate, list[uuid.UUID], bool]],
    raw_text_by_artifact: dict[uuid.UUID, str],
    dates_by_artifact: dict[uuid.UUID, datetime],
    settings: Settings,
) -> list[tuple[int, _PromiseCandidate, list[str]]]:
    entries = []
    for local_idx, (candidate, later_ids, _ordering_known) in enumerate(batch):
        ranked = sorted(
            later_ids, key=lambda aid: (dates_by_artifact.get(aid) or datetime.min, aid)
        )
        ranked = ranked[: settings.gap_promised_later_artifacts_per_candidate]
        excerpts = [
            " ".join(raw_text_by_artifact[aid].split())[: settings.gap_promised_snippet_chars]
            for aid in ranked
            if aid in raw_text_by_artifact
        ]
        entries.append((local_idx, candidate, excerpts))
    return entries


def _llm_judge_unresolved(
    unresolved: list[tuple[_PromiseCandidate, list[uuid.UUID], bool]],
    artifacts_by_id: dict[uuid.UUID, Artifact],
    raw_text_by_artifact: dict[uuid.UUID, str],
    dates_by_artifact: dict[uuid.UUID, datetime],
    settings: Settings,
    provider: LLMProvider,
) -> list[_GapDraft]:
    batches = [
        unresolved[i : i + settings.gap_promised_batch_size]
        for i in range(0, len(unresolved), settings.gap_promised_batch_size)
    ]
    prompts = [
        _promise_prompt(_prompt_entries(batch, raw_text_by_artifact, dates_by_artifact, settings))
        for batch in batches
    ]
    raw_responses = asyncio.run(_complete_promise_batches(provider, prompts))

    drafts: list[_GapDraft] = []
    for batch, raw in zip(batches, raw_responses, strict=True):
        parsed = _parse_promise_response(raw, len(batch))
        for local_idx, (candidate, _later_ids, _ordering_known) in enumerate(batch):
            artifact = artifacts_by_id[candidate.artifact_id]
            judged = parsed.get(local_idx)
            if judged is None:
                drafts.append(
                    _promise_gap_draft(
                        artifact,
                        candidate,
                        settings.gap_promised_confidence_deterministic,
                        "deterministic keyword-overlap check found no later artifact "
                        "addressing this item (the LLM fulfillment judgment was unusable, "
                        "degraded to the deterministic fallback).",
                        model=_PROMISE_RULESET,
                        model_version=GAP_VERSION,
                    )
                )
                continue
            fulfilled, confidence, rationale = judged
            if fulfilled:
                continue  # LLM judged this fulfilled -- no gap
            capped = max(0.0, min(settings.gap_promised_confidence_llm_cap, confidence))
            drafts.append(
                _GapDraft(
                    type=GapType.promised_unfulfilled,
                    phase_id=None,
                    description=_promise_description(artifact, candidate.evidence),
                    evidence=candidate.evidence,
                    confidence=capped,
                    rationale=rationale or "LLM judged this promise unfulfilled.",
                    model=provider.model,
                    model_version=provider.model_version,
                )
            )
    return drafts


def _detect_promised_gaps(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> list[_GapDraft]:
    rows = session.execute(
        select(Artifact, ArtifactContent.raw_text)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.ingested_at, Artifact.id)
    ).all()
    artifacts_by_id = {artifact.id: artifact for artifact, _ in rows}
    raw_text_by_artifact = {artifact.id: text for artifact, text in rows if text}
    all_ids = list(artifacts_by_id)
    dates_by_artifact = _chosen_dates(session, project_id)

    candidates: list[_PromiseCandidate] = []
    for artifact, text in rows:
        if not text:
            continue
        candidates.extend(_artifact_promise_candidates(artifact, text, settings))
    if not candidates:
        return []

    drafts: list[_GapDraft] = []
    unresolved: list[tuple[_PromiseCandidate, list[uuid.UUID], bool]] = []
    for candidate in candidates:
        later_ids, ordering_known = _later_artifacts(
            candidate.artifact_id, all_ids, dates_by_artifact
        )
        if not later_ids:
            uncertainty = (
                ""
                if ordering_known
                else "; this artifact has no resolved date, so chronological ordering could "
                "not be established either"
            )
            drafts.append(
                _promise_gap_draft(
                    artifacts_by_id[candidate.artifact_id],
                    candidate,
                    settings.gap_promised_confidence_no_later_artifacts,
                    "no later artifact exists in the project to check for fulfillment"
                    f"{uncertainty}.",
                    model=_PROMISE_RULESET,
                    model_version=GAP_VERSION,
                )
            )
            continue

        later_texts = [
            raw_text_by_artifact[aid] for aid in later_ids if aid in raw_text_by_artifact
        ]
        if _deterministic_fulfillment(
            candidate.evidence, later_texts, settings.gap_promised_fulfillment_token_overlap
        ):
            continue  # fulfilled -- no gap
        unresolved.append((candidate, later_ids, ordering_known))

    if not unresolved:
        return drafts

    if settings.gap_promised_llm_enabled:
        drafts.extend(
            _llm_judge_unresolved(
                unresolved,
                artifacts_by_id,
                raw_text_by_artifact,
                dates_by_artifact,
                settings,
                provider,
            )
        )
    else:
        for candidate, _later_ids, _ordering_known in unresolved:
            drafts.append(
                _promise_gap_draft(
                    artifacts_by_id[candidate.artifact_id],
                    candidate,
                    settings.gap_promised_confidence_deterministic,
                    "deterministic keyword-overlap check found no later artifact addressing "
                    "this item (LLM fulfillment judgment disabled).",
                    model=_PROMISE_RULESET,
                    model_version=GAP_VERSION,
                )
            )
    return drafts


# --------------------------------------------------------------------------- #
# Stable identity + reconciliation against human-reviewed state              #
# --------------------------------------------------------------------------- #
def _draft_identity(draft: _GapDraft) -> str:
    if draft.type == GapType.structural:
        return f"structural:{draft.phase_id}"
    return f"promised:{_normalize_snippet(draft.description)}"


def _existing_identity(gap: Gap) -> str:
    if gap.type == GapType.structural:
        return f"structural:{gap.phase_id}"
    return f"promised:{_normalize_snippet(gap.description)}"


@dataclass
class _ReconcileCounts:
    created: int = 0
    updated: int = 0
    preserved: int = 0
    pruned: int = 0


def _audit_if_promised(session: Session, gap: Gap, draft: _GapDraft, *, old: dict | None) -> None:
    if draft.type != GapType.promised_unfulfilled:
        return  # structural gaps are deterministic + self-documenting -- see module docstring
    session.add(
        DecisionAudit(
            decision_type="gap_promised_unfulfilled",
            target_id=gap.id,
            old_value=old,
            new_value={"description": draft.description, "confidence": draft.confidence},
            actor=AuditActor.system,
            model=draft.model,
            model_version=draft.model_version,
            rationale=draft.rationale,
        )
    )


def _audit_promised_prune(session: Session, gap: Gap) -> None:
    """A promised-unfulfilled gap disappearing (now judged fulfilled, or its
    marker text no longer present) is exactly as much a judgment call as one
    appearing -- a human who never touched this gap should still be able to
    see why it vanished. Structural prunes get no audit row, symmetric with
    `_audit_if_promised`'s create/update decision: a phase clearing coverage
    is a self-documenting count, not a fuzzy verdict."""
    if gap.type != GapType.promised_unfulfilled:
        return
    session.add(
        DecisionAudit(
            decision_type="gap_promised_unfulfilled",
            target_id=gap.id,
            old_value={
                "description": gap.description,
                "evidence": gap.evidence,
                "confidence": gap.confidence,
            },
            new_value=None,
            actor=AuditActor.system,
            model=_PROMISE_RULESET,
            model_version=GAP_VERSION,
            rationale="no longer reproduced by this run's promise-detection pass -- judged "
            "fulfilled, or the marker text is no longer present; gap removed.",
        )
    )


def _reconcile_gaps(
    session: Session, project_id: uuid.UUID, drafts: list[_GapDraft]
) -> _ReconcileCounts:
    counts = _ReconcileCounts()
    existing = list(session.scalars(select(Gap).where(Gap.project_id == project_id)).all())
    existing_by_identity = {_existing_identity(g): g for g in existing}
    seen: set[str] = set()

    for draft in drafts:
        identity = _draft_identity(draft)
        seen.add(identity)
        gap = existing_by_identity.get(identity)

        if gap is None:
            gap = Gap(
                project_id=project_id,
                type=draft.type,
                phase_id=draft.phase_id,
                description=draft.description,
                evidence=draft.evidence,
                confidence=draft.confidence,
                status=GapStatus.open,
            )
            session.add(gap)
            session.flush()  # populate gap.id for the audit target
            counts.created += 1
            _audit_if_promised(session, gap, draft, old=None)
        elif gap.status == GapStatus.open:
            old = {
                "description": gap.description,
                "evidence": gap.evidence,
                "confidence": gap.confidence,
            }
            gap.description = draft.description
            gap.evidence = draft.evidence
            gap.confidence = draft.confidence
            counts.updated += 1
            _audit_if_promised(session, gap, draft, old=old)
        else:
            counts.preserved += 1  # human-reviewed -- never touched, per the human-review invariant

    for gap in existing:
        if gap.status == GapStatus.open and _existing_identity(gap) not in seen:
            _audit_promised_prune(session, gap)
            session.delete(gap)  # condition no longer holds -- stale auto-draft, safe to drop
            counts.pruned += 1

    return counts


# --------------------------------------------------------------------------- #
# Orchestration                                                              #
# --------------------------------------------------------------------------- #
def run_project_gaps(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> GapsResult:
    """Detect (or refresh) structural and promised-unfulfilled gaps for every
    artifact-dependent computation this project's corpus supports. A no-op
    when every artifact's `StageState(stage=gaps)` already reflects the
    current project-wide fingerprint; otherwise the full structural +
    promised computation reruns and every artifact's stage state is
    refreshed together (see module docstring -- this is inherently a
    project-wide, not per-artifact, computation)."""
    result = GapsResult()
    settings = get_settings()

    artifacts = list(
        session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()
    )
    if not artifacts:
        return result

    fingerprint_hash = _corpus_fingerprint_hash(session, project_id, settings)
    if _is_up_to_date(session, artifacts, fingerprint_hash):
        result.skipped = len(artifacts)
        return result

    provider = provider or get_llm_provider()
    try:
        structural_drafts = _detect_structural_gaps(session, project_id, settings)
        promised_drafts = _detect_promised_gaps(session, project_id, settings, provider)
        counts = _reconcile_gaps(session, project_id, structural_drafts + promised_drafts)
        for artifact in artifacts:
            input_hash = _artifact_input_hash(artifact.id, fingerprint_hash)
            _upsert_stage_state_done(session, artifact.id, input_hash)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - one project-wide computation; retry wholesale next run
        session.rollback()
        result.errors.append(str(exc))
        return result

    result.structural = len(structural_drafts)
    result.promised = len(promised_drafts)
    result.created = counts.created
    result.updated = counts.updated
    result.preserved = counts.preserved
    result.pruned = counts.pruned
    return result


def main() -> None:
    """CLI: detect (or refresh) structural and promised-unfulfilled gaps for
    a project.

        uv run python -m truth_engine.analysis.gaps --project-id <uuid>

    Structural gaps read `PhaseAssignment` against the project's selected
    phase template (step 7) -- skipped gracefully (zero structural gaps,
    promised gaps still run) if `phases` hasn't run yet. Promised-unfulfilled
    gaps read `ArtifactContent.raw_text` and chosen `ResolvedDate`s directly
    and don't require phases, embeddings, or the graph. A rerun over an
    unchanged corpus is a no-op; gaps a human has confirmed, dismissed, or
    resolved are never recomputed or overwritten.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_project_gaps(session, args.project_id)

    print(
        f"gaps: {result.structural} structural, {result.promised} promised candidate(s) -> "
        f"{result.created} created, {result.updated} updated, {result.preserved} preserved "
        f"(human-reviewed), {result.pruned} pruned; {result.skipped} artifacts unchanged"
    )
    for err in result.errors:
        print(f"  ERROR: {err}")


if __name__ == "__main__":
    main()
