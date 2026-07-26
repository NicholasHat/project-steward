"""Stage 6 orchestration: deterministic assembly of `TimelineEvent` rows from
`ResolvedDate` rows (extract's output), gated by `StageState`.

No LLM, no embeddings, no network — pure derivation, per PROJECTSPECS.md
§3.2's confidence-scored date subsystem and the load-bearing UI requirement
that follows from it ("the UI/timeline should visibly distinguish confident
placements from inferred/uncertain ones rather than presenting a single flat
chronology as ground truth"). Every `TimelineEvent` this module writes
carries the `confidence` and `source` of the `ResolvedDate` row it came from,
unchanged — timeline never re-scores or upgrades a date's trust level.

**Two kinds of event, per artifact:**

  * **Placement** (at most one per artifact) — where the artifact sits on the
    project timeline: `ResolvedDate.is_chosen`'s date, confidence, and a
    `source` label of the form `"placement:<signal>"` (`placement:content` /
    `placement:doc_meta` / `placement:filesystem`) so a UI can distinguish a
    high-confidence content placement from a low-confidence filesystem-only
    one at a glance, without parsing free text. `description` is the
    artifact's `original_filename` — a clean rename/category is step 11's
    `ViewProjection`, not this stage's job, so the filename is the best
    available identity label — enriched with the chosen row's own
    `evidence_text` when the placement signal is content (see dedupe rule).
  * **Content-described** (zero or more per artifact) — every *other*
    `ResolvedDate` row with `signal_source == content`: a date the text
    itself refers to ("as discussed in our March 12 meeting"), not
    necessarily the artifact's own placement. `description` is that row's
    `evidence_text`; `source` is the fixed label `SOURCE_CONTENT_DESCRIBED`.

**Dedupe rule:** when the chosen date *is* a content-signal row, it is
represented once, by the placement event, and excluded from the
content-described set — emitting a second event for the same `ResolvedDate`
row would show the identical date/confidence/signal twice under two labels
for no informational gain. But "represented once" must not mean "the
evidence text is silently dropped": exactly this case (an artifact's own
placement date is a date the text explicitly describes) is the single most
informative one on the whole timeline — "which artifact" and "why this
date" are the same fact. So `_placement_description` folds the row's
`evidence_text` into the placement's description instead of discarding it
in favor of the filename (see that function). Every *other* content-signal
row — a date mentioned in the text that isn't where the artifact itself got
placed — is still surfaced as its own content-described event. Comparison
is by identity against the same `ResolvedDate` object used for the
placement event, not by value, so two rows that happen to share a timestamp
are never conflated.

**Auditability:** no new `DecisionAudit` rows are written here. Every field
on every `TimelineEvent` is a lossless copy of a `ResolvedDate` row's own
`candidate_date` / `confidence` / `evidence_text` / `signal_source` — fields
that are already fully inspectable on `resolved_dates` itself, and, for the
chosen row specifically, already have their own `decision_type="resolved_date"`
audit entry from `extract.service`. Timeline infers nothing new (which date
to trust was decided once, in extract); it only decides which already-
inspectable rows are worth surfacing as timeline entries and how to label
them, a fixed deterministic rule rather than a per-instance judgment. This
mirrors `embed.service`'s precedent (derived data, no audit row), not
`extract.service`'s (genuinely new inferred facts each get one).

**`ProcessingState` is untouched, in both directions:** `ProcessingState`'s
members (`pending`/`parsed`/`extracted`/`embedded`/`analyzed`/`error`) name
the deterministic per-artifact pipeline (steps 1-5); `analyzed` would
overclaim that all of steps 6-10 have run when this is only the first of
five, and there is no `timeline` member (adding one is a schema/migration
change, not warranted for this increment). Rather than reach for the
nearest existing value in either direction, timeline leaves
`Artifact.processing_state` exactly as the deterministic pipeline last set
it — on success *and* on failure alike, so it never asserts something this
stage isn't precise enough to mean. `StageState(stage=timeline)` is the
exact, symmetric source of truth for "did timeline run, and did it
succeed" for a given artifact; nothing else needs to double as a proxy for
it.

**Depends on extract, not embed:** this module never checks
`StageState(stage=extract)` explicitly — it just reads `ResolvedDate` rows
directly and degrades gracefully to zero rows (and thus the "no chosen
date" skip-but-done path) if extract hasn't run yet, the same way `embed`
degrades to zero chunks when `ArtifactContent` doesn't exist yet rather than
gating on `StageState(stage=parse)`.

Idempotency: `StageState(artifact_id, stage=timeline).input_hash` is a hash
of the artifact's *current* `ResolvedDate` row contents (not
`Artifact.content_hash`) — see `_input_hash` for why row content, not row
id, is what must be hashed. `TIMELINE_VERSION` is folded in so a future
change to the assembly/dedupe rule above invalidates cached state, mirroring
`RULESET_VERSION` / `CHUNKER_VERSION` in extract/embed.

Failure isolation and the "replace this artifact's rows wholesale on
re-run" pattern mirror `extract.service` / `embed.service`.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.db.models import (
    Artifact,
    DateSignalSource,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
    TimelineEvent,
)

# Bump when the assembly/dedupe rule above changes materially enough that
# already-assembled artifacts should be rebuilt.
TIMELINE_VERSION = "1"

# Source labels, centralized so the timeline UI can pattern-match on a small,
# stable vocabulary instead of parsing free text. "placement:*" answers
# "where does this artifact sit"; the suffix is the signal that placed it
# there, so confidence + source together let a UI visually distinguish a
# high-confidence content placement from a low-confidence filesystem-only
# one (PROJECTSPECS.md §3.2). "content_described" answers "what date does
# the text describe" and never represents an artifact's own placement.
SOURCE_PLACEMENT_CONTENT = "placement:content"
SOURCE_PLACEMENT_DOC_META = "placement:doc_meta"
SOURCE_PLACEMENT_FILESYSTEM = "placement:filesystem"
SOURCE_CONTENT_DESCRIBED = "content_described"

_PLACEMENT_SOURCE: dict[DateSignalSource, str] = {
    DateSignalSource.content: SOURCE_PLACEMENT_CONTENT,
    DateSignalSource.doc_meta: SOURCE_PLACEMENT_DOC_META,
    DateSignalSource.filesystem: SOURCE_PLACEMENT_FILESYSTEM,
}


@dataclass
class TimelineResult:
    assembled: int = 0
    skipped: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _resolved_dates(session: Session, artifact_id: uuid.UUID) -> list[ResolvedDate]:
    return list(
        session.scalars(
            select(ResolvedDate).where(ResolvedDate.artifact_id == artifact_id)
        ).all()
    )


def _input_hash(resolved: list[ResolvedDate]) -> str:
    """Hash of this artifact's `ResolvedDate` row *contents*, not their ids
    or `Artifact.content_hash`: extract wholesale-replaces `resolved_dates`
    (new ids) on every re-run, even a no-op one whose confidences/dates come
    out identical, so hashing ids would make timeline re-rebuild on every
    extract run regardless of whether anything actually changed. Sorted so
    DB/insertion row order never perturbs the hash.
    """
    parts = sorted(
        f"{r.signal_source.value}|{r.candidate_date.isoformat()}|{r.confidence:.6f}|"
        f"{r.evidence_text or ''}|{r.is_chosen}"
        for r in resolved
    )
    raw = f"{TIMELINE_VERSION}\n" + "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def assemble_artifact_timeline(session: Session, artifact: Artifact) -> bool:
    """Assemble timeline events for one artifact if its resolved-date inputs
    changed since the last successful run. Returns True if (re)assembled,
    False if skipped as already current.
    """
    resolved = _resolved_dates(session, artifact.id)
    input_hash = _input_hash(resolved)
    state = _stage_state(session, artifact.id, Stage.timeline)
    if state and state.status == StageStatus.done and state.input_hash == input_hash:
        return False

    _replace_timeline_events(session, artifact, resolved)

    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.timeline, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None
    return True


def assemble_project_timeline(session: Session, project_id: uuid.UUID) -> TimelineResult:
    """Assemble timeline events for every artifact in a project, skipping
    ones already up to date."""
    result = TimelineResult()
    artifacts = session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()

    for artifact in artifacts:
        try:
            changed = assemble_artifact_timeline(session, artifact)
        except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
            _record_error(session, artifact, str(exc))
            result.errors.append((artifact, str(exc)))
            continue

        session.commit()
        result.assembled += 1 if changed else 0
        result.skipped += 0 if changed else 1

    return result


def _placement_description(artifact: Artifact, chosen: ResolvedDate) -> str:
    """The placement event's description: the artifact's own identity
    (filename — a clean rename is step 11's `ViewProjection` job, not this
    stage's), enriched with the chosen row's `evidence_text` when the
    placement signal is content. Without this, the dedupe rule below would
    silently throw away exactly the evidence text that explains *why* the
    artifact landed on this date in the highest-trust case — the one
    instance where "which artifact" and "why this date" are the same fact.
    """
    if chosen.signal_source == DateSignalSource.content and chosen.evidence_text:
        return f"{artifact.original_filename} — {chosen.evidence_text}"
    return artifact.original_filename


def _replace_timeline_events(
    session: Session, artifact: Artifact, resolved: list[ResolvedDate]
) -> None:
    session.execute(delete(TimelineEvent).where(TimelineEvent.artifact_id == artifact.id))

    chosen = next((r for r in resolved if r.is_chosen), None)
    if chosen is not None:
        session.add(
            TimelineEvent(
                project_id=artifact.project_id,
                artifact_id=artifact.id,
                event_date=chosen.candidate_date,
                description=_placement_description(artifact, chosen),
                confidence=chosen.confidence,
                source=_PLACEMENT_SOURCE[chosen.signal_source],
            )
        )
    # else: no candidate date at all (shouldn't happen — filesystem always
    # yields one — but be defensive). Nothing to place; fall through and
    # still mark the stage done below via the caller.

    for candidate in resolved:
        if candidate.signal_source != DateSignalSource.content:
            continue
        if candidate is chosen:
            continue  # already represented by the placement event above
        session.add(
            TimelineEvent(
                project_id=artifact.project_id,
                artifact_id=artifact.id,
                event_date=candidate.candidate_date,
                description=candidate.evidence_text or "(no evidence text captured)",
                confidence=candidate.confidence,
                source=SOURCE_CONTENT_DESCRIBED,
            )
        )


def _record_error(session: Session, artifact: Artifact, message: str) -> None:
    session.rollback()
    resolved = _resolved_dates(session, artifact.id)
    state = _stage_state(session, artifact.id, Stage.timeline)
    if state is None:
        state = StageState(
            artifact_id=artifact.id, stage=Stage.timeline, input_hash=_input_hash(resolved)
        )
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    session.commit()


def main() -> None:
    """CLI: (re)assemble timeline events for every artifact in a project.

        uv run python -m truth_engine.analysis.timeline --project-id <uuid>

    Only artifacts whose resolved-date inputs changed since the last
    successful run are reprocessed — `StageState` gates idempotency. Run
    after `python -m truth_engine.extract` (reads `ResolvedDate`); does not
    require `python -m truth_engine.embed` to have run.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = assemble_project_timeline(session, args.project_id)

    print(
        f"timeline: {result.assembled} assembled, {result.skipped} skipped, "
        f"{len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
