"""Step 12 orchestration (PROJECTSPECS.md §3.7 "Self-Updating Report" / §3.8
"Incremental Processing"): a versioned, living Markdown `Report` composed
from five independently-fingerprinted **sections**, gated by a project-level
`Report.corpus_fingerprint_hash` -- the same "one project-level row, not
per-artifact `StageState`" idempotency shape `direction.py`'s
`DirectionSnapshot` uses, because a report (like a direction snapshot) is a
fact about the whole project, not about any one artifact.

**The five sections (§3.7's own list) and where each reads from:**

  * **Overview** (deterministic) -- `DomainClassification` (domain +
    confidence), the selected `PhaseTemplate` (via the same `_select_
    template_domain` rule `phases.py`/`gaps.py` use), a project-wide
    `Artifact` count, and the timeline span (earliest/latest *placement*
    `TimelineEvent`).
  * **Current direction** (LLM-partial, the one genuinely judgment-shaped
    section) -- `DirectionSnapshot.inferred_direction_summary` plus the
    `current`-labeled artifacts (via `DirectionLabel`), refined into a short
    report-voice narrative. This is a *second*, report-specific LLM call,
    distinct from the one `direction.py` already made to produce the
    snapshot narrative in the first place (PROJECTSPECS.md's own pipeline
    description lists report synthesis as its own step-12 LLM use, separate
    from step 9's) -- report-voice framing that names recent artifacts by
    their clean `ViewProjection` name is a different job than the terse
    snapshot summary itself.
  * **Recent activity** (deterministic) -- the most recent `Settings.
    report_recent_activity_count` placement `TimelineEvent`s (`source` like
    `"placement:%"` -- an artifact's own chronological position, not a
    content-described date mention), rendered with each artifact's clean
    `ViewProjection` name.
  * **Open gaps** (deterministic) -- `Gap` rows with `status=open`, grouped
    structural vs. promised-unfulfilled, each with its own confidence
    (mirrors `gaps.py`'s own confidence-band separation).
  * **Flagged stale artifacts** (deterministic) -- `DirectionLabel` rows with
    `label=superseded`, each with its rationale and clean name.

**Incrementality is the whole point of this stage -- the mechanism, concretely.**
Each section has its own **input fingerprint**, computed from exactly the
analysis rows that feed it (a hash function per section, deliberately
decoupled from that section's *rendering* -- see below). `Report.sections` is
a JSONB map `{name: {content, fingerprint, model, model_version, rationale}}`;
`Report.corpus_fingerprint_hash` is the hash of the union of all five section
fingerprints, mirroring `DirectionSnapshot.corpus_fingerprint_hash`'s role.
Regeneration proceeds in two passes:

  1. **Cheap fast path.** Compute all five section fingerprints (plain
     deterministic queries + hashing -- no LLM call, no markdown rendering)
     and combine them into `corpus_fingerprint_hash`. If it matches the
     current version's, this is a **true no-op**: return the existing
     current `Report` untouched. No new version, no section work at all, no
     LLM call -- not even for sections that would end up unchanged anyway.
  2. **Selective regeneration**, only entered when the combined hash differs.
     For each section: if its own fingerprint still matches the *prior*
     version's stored fingerprint for that section, carry the prior
     `{content, fingerprint, model, model_version, rationale}` entry forward
     **verbatim** -- no recompute, and critically, no LLM call for the
     current-direction section when its own inputs (the direction snapshot,
     the current-labeled artifact set, the LLM model, or the enable flag --
     see `_direction_fingerprint`) haven't changed. Only sections whose
     fingerprint changed are re-rendered. A new `Report` version is written
     with `version = prior.version + 1`, `is_current=True`; the prior
     current row's `is_current` flips to `False` -- full history retained,
     nothing deleted (mirrors `ViewProjection`'s version-chain reversibility
     intent, not `DirectionLabel`'s update-in-place one, because unlike a
     per-artifact label there is no single owner who could "confirm" a
     report version and every past version is independently useful browsing
     history).

This two-pass shape (fingerprint everything cheaply first, only then decide
what's worth doing expensive work for) is the same trade-off `direction.py`
makes between `label_project_direction` and `build_project_direction_snapshot`,
and the same reason `_direction_fingerprint` and `_render_direction` both call
`_direction_inputs` independently rather than threading state between a
single fingerprint+render pass -- the fingerprint pass must stay cheap and
side-effect-free even when the render pass ultimately isn't needed.

**Why the current-direction section's fingerprint excludes `Gap`.** It reads
only `DirectionSnapshot` + `DirectionLabel` + the LLM model/enable-flag (the
last two mirroring `view.py`'s `naming_model` idiom: `provider.model if
enabled else "disabled"`, folded in so a model swap invalidates the cached
narrative the same way `phases.py`'s `_artifact_input_hash` folds in
`provider.model`). A change that only touches `Gap` rows (e.g. a gap
dismissed or a new promised-unfulfilled gap detected) therefore leaves this
section's fingerprint byte-identical, so it's carried forward verbatim and
the LLM is never re-invoked for it -- this is the concrete mechanism behind
this module's "feel real rather than batch-y" incrementality tests.

**Deterministic core, LLM only for narrative synthesis.** Every section other
than current-direction is 100% deterministic, always inspectable, and never
calls the LLM. The current-direction section itself degrades gracefully: no
`DirectionSnapshot` yet -> an honest "not yet inferred" sentence; `Settings.
report_llm_direction_enabled=False` -> the snapshot's own narrative rendered
directly (no LLM call); a malformed/empty LLM response -> the same
deterministic rendering (`_direction_fallback`). The report itself is always
produced -- never empty, never a crash -- exactly mirroring `view.py`'s
"deterministic fallback always available" contract for its own LLM-partial
field.

**Auditability.** One `DecisionAudit(actor=system)` per *new* report version
(not per section -- a report version is one coherent editorial act, and four
of five sections are self-documenting deterministic assembly, the same
reasoning `gaps.py` gives for not auditing structural gaps and `graph.py`
gives for not auditing edges). `new_value` records which sections changed vs.
were reused verbatim, plus each section's basis (`model`/`model_version`).
The audit row's own top-level `model`/`model_version` name the real LLM
provider only when the current-direction section was *actually re-synthesized
this run* (`SECTION_DIRECTION in changed_sections`, not merely present in
`new_sections` -- a reused section's stored `model` would otherwise
misleadingly look like this run made an LLM call when it didn't); otherwise
they name `_REPORT_RULESET`/`REPORT_VERSION`, mirroring `direction.py`'s
`_LABEL_RULESET` precedent for a row that's an assembly decision, not an LLM
one.

**A deliberately deferred limitation, not an oversight:** section
fingerprints do not fold in threshold-style `Settings` values (e.g.
`domain_confidence_threshold`, which feeds the overview's phase-template
selection) -- only the query *results* those settings shape (row sets,
caps applied via `.limit()`/slicing) are hashed, the same choice `direction.py`
makes for its own combining-rule thresholds. Changing such a setting alone,
with no other corpus change, will not retroactively invalidate a cached
section; bump `REPORT_VERSION` (as with `DIRECTION_VERSION`/`GAP_VERSION`
elsewhere) if a settings/rule change should force a full rebuild.

`ProcessingState` and per-artifact `StageState` are untouched by this module,
for the same reason `direction.py`'s project-level snapshot half leaves them
untouched: there is no per-artifact notion of "has the report run for this
artifact" -- `Report.corpus_fingerprint_hash` on the single current row is
this stage's entire source of truth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    AuditActor,
    DecisionAudit,
    DirectionLabel,
    DirectionLabelValue,
    DirectionSnapshot,
    DomainClassification,
    Gap,
    GapStatus,
    GapType,
    PhaseTemplate,
    Project,
    Report,
    ResolvedDate,
    TimelineEvent,
    ViewProjection,
)
from truth_engine.reasoning.json_extract import extract_json
from truth_engine.reasoning.providers import LLMProvider, get_llm_provider

# Bump when the section-derivation/composition rules in this module change
# materially enough that already-computed sections should be rebuilt.
REPORT_VERSION = "1"

# DecisionAudit.model for the report-version row when the current-direction
# section wasn't (re)synthesized by an LLM this run -- mirrors direction.py's
# `_LABEL_RULESET` / gaps.py's `_PROMISE_RULESET` / view.py's `_NAME_RULESET`.
_REPORT_RULESET = "report-ruleset"
_OVERVIEW_RULESET = "report-overview-ruleset"
_DIRECTION_RULESET = "report-direction-ruleset"
_ACTIVITY_RULESET = "report-activity-ruleset"
_GAPS_RULESET = "report-gaps-ruleset"
_STALE_RULESET = "report-stale-ruleset"

_DIRECTION_SYSTEM_PROMPT = (
    "You are a project-status report writer. Respond with ONLY a single JSON "
    "object -- no prose, no markdown fences."
)

SECTION_OVERVIEW = "overview"
SECTION_DIRECTION = "current_direction"
SECTION_ACTIVITY = "recent_activity"
SECTION_GAPS = "open_gaps"
SECTION_STALE = "stale_artifacts"
SECTION_ORDER: tuple[str, ...] = (
    SECTION_OVERVIEW,
    SECTION_DIRECTION,
    SECTION_ACTIVITY,
    SECTION_GAPS,
    SECTION_STALE,
)
SECTION_TITLES: dict[str, str] = {
    SECTION_OVERVIEW: "Overview",
    SECTION_DIRECTION: "Current Direction",
    SECTION_ACTIVITY: "Recent Activity",
    SECTION_GAPS: "Open Gaps",
    SECTION_STALE: "Flagged Stale Artifacts",
}


@dataclass(frozen=True, slots=True)
class _SectionContent:
    content: str
    model: str
    model_version: str
    rationale: str


@dataclass
class ReportResult:
    report: Report | None = None
    is_new_version: bool = False
    changed_sections: list[str] = field(default_factory=list)
    reused_sections: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shared helpers (deliberately duplicated per-module, matching direction.py's/ #
# gaps.py's/view.py's own small copies of the same helpers)                   #
# --------------------------------------------------------------------------- #
def _chosen_dates(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    rows = session.execute(
        select(ResolvedDate.artifact_id, ResolvedDate.candidate_date)
        .join(Artifact, Artifact.id == ResolvedDate.artifact_id)
        .where(Artifact.project_id == project_id, ResolvedDate.is_chosen.is_(True))
    ).all()
    return dict(rows)


def _select_template_domain(classification: DomainClassification, settings: Settings) -> str:
    """Same rule as phases.py's/gaps.py's function of the same name: which
    PhaseTemplate.domain the overview names as the selected phase model."""
    return (
        classification.domain
        if classification.confidence >= settings.domain_confidence_threshold
        else "generic"
    )


def _phase_templates_for_domain(session: Session, domain: str) -> list[PhaseTemplate]:
    return list(
        session.scalars(
            select(PhaseTemplate)
            .where(PhaseTemplate.domain == domain)
            .order_by(PhaseTemplate.ordinal)
        ).all()
    )


def _current_projections(
    session: Session, artifact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ViewProjection]:
    if not artifact_ids:
        return {}
    rows = session.scalars(
        select(ViewProjection).where(
            ViewProjection.artifact_id.in_(artifact_ids), ViewProjection.superseded_by.is_(None)
        )
    ).all()
    return {row.artifact_id: row for row in rows}


def _clean_name(artifact: Artifact, projection: ViewProjection | None) -> str:
    """The artifact's clean, human-facing name -- its current ViewProjection
    suggestion when one exists, the raw original_filename otherwise (view
    hasn't run yet, or genuinely produced nothing)."""
    if projection is not None and projection.suggested_name:
        return projection.suggested_name
    return artifact.original_filename


def _snippet(content: ArtifactContent | None, chars: int) -> str:
    if content is None or not content.raw_text:
        return ""
    return " ".join(content.raw_text.split())[:chars]


# A tz-aware sentinel, not `datetime.min` (naive) -- `_chosen_dates` returns
# tz-aware `DateTime(timezone=True)` values, and Python raises on comparing
# an aware and a naive datetime. `direction.py`'s equivalent recency sort
# uses `datetime.min` directly; that's safe there only because every artifact
# it sorts is drawn from `current_members`, which (by construction of
# `_cluster_signal_a`) can include a dateless cluster member. We don't rely
# on an equivalent guarantee here, so we use an aware sentinel instead of
# assuming one.
_MIN_AWARE = datetime.min.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Overview -- deterministic                                                  #
# --------------------------------------------------------------------------- #
_OverviewInputs = tuple[
    DomainClassification | None,
    str | None,
    list[PhaseTemplate],
    int,
    datetime | None,
    datetime | None,
]


def _overview_inputs(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> _OverviewInputs:
    classification = session.scalar(
        select(DomainClassification).where(DomainClassification.project_id == project_id)
    )
    template_domain = _select_template_domain(classification, settings) if classification else None
    phases = _phase_templates_for_domain(session, template_domain) if template_domain else []
    artifact_count = (
        session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id)
        )
        or 0
    )
    earliest, latest = session.execute(
        select(func.min(TimelineEvent.event_date), func.max(TimelineEvent.event_date)).where(
            TimelineEvent.project_id == project_id, TimelineEvent.source.like("placement:%")
        )
    ).one()
    return classification, template_domain, phases, artifact_count, earliest, latest


def _overview_fingerprint(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> str:
    classification, template_domain, phases, artifact_count, earliest, latest = _overview_inputs(
        session, project_id, settings
    )
    phase_line = "|".join(p.phase_name for p in phases)
    raw = (
        f"{REPORT_VERSION}:{classification.domain if classification else ''}:"
        f"{classification.confidence if classification else ''}:"
        f"{classification.confirmed_by_user if classification else ''}:"
        f"{template_domain or ''}:{phase_line}:{artifact_count}:"
        f"{earliest.isoformat() if earliest else ''}:{latest.isoformat() if latest else ''}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _render_overview(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> _SectionContent:
    classification, template_domain, phases, artifact_count, earliest, latest = _overview_inputs(
        session, project_id, settings
    )
    lines = []
    if classification is not None:
        confirmed = " (confirmed)" if classification.confirmed_by_user else ""
        lines.append(
            f"- **Domain:** {classification.domain} (confidence {classification.confidence:.2f})"
            f"{confirmed}"
        )
    else:
        lines.append("- **Domain:** not yet classified.")

    if phases:
        lines.append(
            f"- **Phase model:** {template_domain} — " + " → ".join(p.phase_name for p in phases)
        )
    else:
        lines.append("- **Phase model:** not yet selected.")

    lines.append(f"- **Artifacts:** {artifact_count}")

    if earliest is not None and latest is not None:
        span_days = (latest - earliest).days
        lines.append(
            f"- **Timeline span:** {earliest:%Y-%m-%d} to {latest:%Y-%m-%d} ({span_days} days)"
        )
    else:
        lines.append("- **Timeline span:** no dated artifacts yet.")

    return _SectionContent(
        "\n".join(lines),
        _OVERVIEW_RULESET,
        REPORT_VERSION,
        "deterministic overview assembled from domain classification, the selected phase "
        "template, and the placement timeline span.",
    )


# --------------------------------------------------------------------------- #
# Current direction -- LLM-partial (the only LLM call in this module)        #
# --------------------------------------------------------------------------- #
# (artifact_id, content_hash, projection_version, clean_name, content_snippet)
_DirectionEntry = tuple[uuid.UUID, str, int, str, str]


def _direction_inputs(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> tuple[DirectionSnapshot | None, list[_DirectionEntry]]:
    snapshot = session.scalar(
        select(DirectionSnapshot)
        .where(DirectionSnapshot.project_id == project_id)
        .order_by(DirectionSnapshot.computed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return None, []

    rows = session.execute(
        select(Artifact, ArtifactContent)
        .join(DirectionLabel, DirectionLabel.artifact_id == Artifact.id)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(
            Artifact.project_id == project_id,
            DirectionLabel.label == DirectionLabelValue.current,
        )
    ).all()
    if not rows:
        return snapshot, []

    dates_by_artifact = _chosen_dates(session, project_id)
    projections = _current_projections(session, [a.id for a, _ in rows])
    ordered = sorted(
        rows, key=lambda pair: dates_by_artifact.get(pair[0].id, _MIN_AWARE), reverse=True
    )[: settings.report_direction_max_current_artifacts]

    entries: list[_DirectionEntry] = [
        (
            artifact.id,
            artifact.content_hash,
            projections[artifact.id].version if artifact.id in projections else 0,
            _clean_name(artifact, projections.get(artifact.id)),
            _snippet(content, settings.report_direction_snippet_chars),
        )
        for artifact, content in ordered
    ]
    return snapshot, entries


def _direction_fingerprint(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> str:
    snapshot, entries = _direction_inputs(session, project_id, settings)
    if snapshot is None:
        return hashlib.sha256(f"{REPORT_VERSION}:no-snapshot".encode()).hexdigest()

    # Mirrors view.py's `naming_model` idiom: folding the LLM model (or a
    # fixed "disabled" marker) into the fingerprint means a model swap, or
    # toggling the enable flag, invalidates the cached narrative -- same
    # reasoning phases.py's `_artifact_input_hash` folds in `provider.model`.
    model_marker = provider.model if settings.report_llm_direction_enabled else "disabled"
    entry_lines = sorted(f"{aid}|{chash}|{pv}" for aid, chash, pv, _name, _snip in entries)
    raw = (
        f"{REPORT_VERSION}:{snapshot.corpus_fingerprint_hash}:{snapshot.inferred_direction_summary}:"
        f"{model_marker}\n" + "\n".join(entry_lines)
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _direction_fallback(snapshot: DirectionSnapshot, names: list[str]) -> str:
    lines = [snapshot.inferred_direction_summary]
    if names:
        lines.append("")
        lines.append("Artifacts currently part of this direction:")
        lines.extend(f"- {n}" for n in names)
    return "\n".join(lines)


def _direction_prompt(direction_summary: str, current_lines: list[str]) -> str:
    lines = "\n".join(f"- {line}" for line in current_lines) if current_lines else "(none)"
    return (
        "Write a short (2-4 sentence) 'Current Direction' section for a living project "
        "report, in a neutral report-writing voice. Base it on the project's inferred "
        "direction summary and the artifacts currently considered part of that direction, "
        "below.\n\n"
        f"Inferred direction summary: {direction_summary}\n\n"
        f"Artifacts currently part of this direction:\n{lines}\n\n"
        'Respond with exactly one JSON object of this form: {"narrative": "<2-4 sentences>"}'
    )


def _parse_direction_response(raw: str) -> str | None:
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return None
    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return None
    return narrative.strip()


def _render_direction(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> _SectionContent:
    snapshot, entries = _direction_inputs(session, project_id, settings)
    if snapshot is None:
        return _SectionContent(
            "Direction has not yet been inferred for this project.",
            _DIRECTION_RULESET,
            REPORT_VERSION,
            "direction analysis (steps 8-9) has not produced a DirectionSnapshot yet.",
        )

    names = [name for _aid, _chash, _pv, name, _snip in entries]
    if not settings.report_llm_direction_enabled:
        return _SectionContent(
            _direction_fallback(snapshot, names),
            _DIRECTION_RULESET,
            REPORT_VERSION,
            "LLM direction synthesis disabled; rendered the direction snapshot narrative directly.",
        )

    prompt_lines = [
        f'{name}: "{snippet}"' if snippet else name for _aid, _chash, _pv, name, snippet in entries
    ]
    raw = asyncio.run(
        provider.complete(
            _direction_prompt(snapshot.inferred_direction_summary, prompt_lines),
            system=_DIRECTION_SYSTEM_PROMPT,
        )
    )
    narrative = _parse_direction_response(raw)
    if narrative is None:
        return _SectionContent(
            _direction_fallback(snapshot, names),
            _DIRECTION_RULESET,
            REPORT_VERSION,
            "LLM response was not usable JSON; degraded to a deterministic rendering of the "
            "direction snapshot.",
        )

    content = narrative
    if names:
        content += "\n\nArtifacts currently part of this direction:\n" + "\n".join(
            f"- {n}" for n in names
        )
    return _SectionContent(
        content,
        provider.model,
        provider.model_version,
        "synthesized from the direction snapshot and current-labeled artifacts.",
    )


# --------------------------------------------------------------------------- #
# Recent activity -- deterministic                                          #
# --------------------------------------------------------------------------- #
def _activity_events(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> list[TimelineEvent]:
    return list(
        session.scalars(
            select(TimelineEvent)
            .where(TimelineEvent.project_id == project_id, TimelineEvent.source.like("placement:%"))
            .order_by(TimelineEvent.event_date.desc(), TimelineEvent.id)
            .limit(settings.report_recent_activity_count)
        ).all()
    )


def _activity_fingerprint(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> str:
    events = _activity_events(session, project_id, settings)
    artifact_ids = [e.artifact_id for e in events if e.artifact_id]
    projections = _current_projections(session, artifact_ids)
    # Fetch order is already deterministic (event_date desc, id asc tiebreak
    # -- same tiebreak reasoning as phases.py's `_corpus_fingerprint`), so no
    # extra sort is needed for hash stability here.
    lines = [
        f"{e.id}|{e.event_date.isoformat()}|{e.confidence:.4f}|{e.source}|"
        f"{projections[e.artifact_id].version if e.artifact_id in projections else 0}"
        for e in events
    ]
    raw = f"{REPORT_VERSION}\n" + "\n".join(lines)
    return hashlib.sha256(raw.encode()).hexdigest()


def _render_activity(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> _SectionContent:
    events = _activity_events(session, project_id, settings)
    if not events:
        return _SectionContent(
            "No timeline activity recorded yet.",
            _ACTIVITY_RULESET,
            REPORT_VERSION,
            "no placement TimelineEvent rows for this project.",
        )

    artifact_ids = [e.artifact_id for e in events if e.artifact_id]
    artifacts = (
        session.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids))).all()
        if artifact_ids
        else []
    )
    artifacts_by_id = {a.id: a for a in artifacts}
    projections = _current_projections(session, artifact_ids)

    lines = []
    for event in events:
        artifact = artifacts_by_id.get(event.artifact_id) if event.artifact_id else None
        name = (
            _clean_name(artifact, projections.get(event.artifact_id))
            if artifact is not None
            else event.description
        )
        lines.append(f"- {event.event_date:%Y-%m-%d} — {name} (confidence {event.confidence:.2f})")

    return _SectionContent(
        "\n".join(lines),
        _ACTIVITY_RULESET,
        REPORT_VERSION,
        f"{len(events)} most recent placement event(s).",
    )


# --------------------------------------------------------------------------- #
# Open gaps -- deterministic                                                 #
# --------------------------------------------------------------------------- #
def _open_gaps(session: Session, project_id: uuid.UUID) -> list[Gap]:
    return list(
        session.scalars(
            select(Gap)
            .where(Gap.project_id == project_id, Gap.status == GapStatus.open)
            .order_by(Gap.type, Gap.confidence.desc(), Gap.id)
        ).all()
    )


def _gaps_fingerprint(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> str:
    gaps = _open_gaps(session, project_id)
    lines = sorted(f"{g.id}|{g.type.value}|{g.confidence:.4f}|{g.description}" for g in gaps)
    raw = f"{REPORT_VERSION}\n" + "\n".join(lines)
    return hashlib.sha256(raw.encode()).hexdigest()


def _render_gaps(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> _SectionContent:
    gaps = _open_gaps(session, project_id)
    if not gaps:
        return _SectionContent(
            "No open gaps detected.",
            _GAPS_RULESET,
            REPORT_VERSION,
            "no Gap rows with status=open for this project.",
        )

    structural = [g for g in gaps if g.type == GapType.structural]
    promised = [g for g in gaps if g.type == GapType.promised_unfulfilled]
    lines = []
    if structural:
        lines.append("**Structural:**")
        lines.extend(f"- {g.description} (confidence {g.confidence:.2f})" for g in structural)
    if promised:
        if structural:
            lines.append("")
        lines.append("**Promised, unfulfilled:**")
        lines.extend(f"- {g.description} (confidence {g.confidence:.2f})" for g in promised)

    return _SectionContent(
        "\n".join(lines),
        _GAPS_RULESET,
        REPORT_VERSION,
        f"{len(structural)} structural, {len(promised)} promised-unfulfilled open gap(s).",
    )


# --------------------------------------------------------------------------- #
# Flagged stale artifacts -- deterministic                                   #
# --------------------------------------------------------------------------- #
def _stale_labels(session: Session, project_id: uuid.UUID) -> list[tuple[Artifact, DirectionLabel]]:
    rows = session.execute(
        select(Artifact, DirectionLabel)
        .join(DirectionLabel, DirectionLabel.artifact_id == Artifact.id)
        .where(
            Artifact.project_id == project_id,
            DirectionLabel.label == DirectionLabelValue.superseded,
        )
        .order_by(DirectionLabel.confidence.desc(), Artifact.id)
    ).all()
    return list(rows)


def _stale_fingerprint(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> str:
    rows = _stale_labels(session, project_id)
    projections = _current_projections(session, [a.id for a, _ in rows])
    lines = sorted(
        f"{a.id}|{a.content_hash}|{projections[a.id].version if a.id in projections else 0}|"
        f"{label.rationale}|{label.confidence:.4f}"
        for a, label in rows
    )
    raw = f"{REPORT_VERSION}\n" + "\n".join(lines)
    return hashlib.sha256(raw.encode()).hexdigest()


def _render_stale(
    session: Session, project_id: uuid.UUID, settings: Settings, provider: LLMProvider
) -> _SectionContent:
    rows = _stale_labels(session, project_id)
    if not rows:
        return _SectionContent(
            "No artifacts currently flagged as stale.",
            _STALE_RULESET,
            REPORT_VERSION,
            "no DirectionLabel rows with label=superseded for this project.",
        )

    projections = _current_projections(session, [a.id for a, _ in rows])
    lines = [
        f"- {_clean_name(a, projections.get(a.id))} — {label.rationale} "
        f"(confidence {label.confidence:.2f})"
        for a, label in rows
    ]
    return _SectionContent(
        "\n".join(lines),
        _STALE_RULESET,
        REPORT_VERSION,
        f"{len(rows)} artifact(s) labeled superseded.",
    )


# --------------------------------------------------------------------------- #
# Section dispatch tables                                                    #
# --------------------------------------------------------------------------- #
_FingerprintFn = Callable[[Session, uuid.UUID, Settings, LLMProvider], str]
_RenderFn = Callable[[Session, uuid.UUID, Settings, LLMProvider], _SectionContent]

_FINGERPRINT_FNS: dict[str, _FingerprintFn] = {
    SECTION_OVERVIEW: _overview_fingerprint,
    SECTION_DIRECTION: _direction_fingerprint,
    SECTION_ACTIVITY: _activity_fingerprint,
    SECTION_GAPS: _gaps_fingerprint,
    SECTION_STALE: _stale_fingerprint,
}
_RENDER_FNS: dict[str, _RenderFn] = {
    SECTION_OVERVIEW: _render_overview,
    SECTION_DIRECTION: _render_direction,
    SECTION_ACTIVITY: _render_activity,
    SECTION_GAPS: _render_gaps,
    SECTION_STALE: _render_stale,
}


# --------------------------------------------------------------------------- #
# Composition + orchestration                                                #
# --------------------------------------------------------------------------- #
def _combine_fingerprint(section_fingerprints: dict[str, str]) -> str:
    raw = f"{REPORT_VERSION}\n" + "\n".join(
        f"{name}:{fp}" for name, fp in sorted(section_fingerprints.items())
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _compose_markdown(
    project: Project, version: int, generated_at: datetime, sections: dict[str, dict]
) -> str:
    parts = [
        f"# Project Report — {project.name}",
        f"_Version {version}, generated {generated_at:%Y-%m-%d %H:%M UTC}_",
        "",
    ]
    for name in SECTION_ORDER:
        parts.append(f"## {SECTION_TITLES[name]}")
        parts.append(sections[name]["content"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _audit_model(
    new_sections: dict[str, dict], changed_sections: list[str], provider: LLMProvider
) -> tuple[str, str]:
    """The report-version audit row's own model/model_version: the real LLM
    provider only if the current-direction section was actually
    (re)synthesized by it *this run* -- gated on `changed_sections`, not
    merely on `new_sections`, so a reused (carried-forward) section's stored
    `model` from a prior run never misreports this run as having made an LLM
    call it didn't make."""
    if SECTION_DIRECTION in changed_sections:
        direction = new_sections[SECTION_DIRECTION]
        if direction["model"] == provider.model:
            return provider.model, direction["model_version"]
    return _REPORT_RULESET, REPORT_VERSION


def current_report(session: Session, project_id: uuid.UUID) -> Report | None:
    """The project's current report -- the row with `is_current=True`. By
    construction at most one row per project has `is_current=True` at a
    time."""
    return session.scalar(
        select(Report).where(Report.project_id == project_id, Report.is_current.is_(True))
    )


def run_project_report(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> ReportResult:
    """(Re)generate the project's self-updating report. A true no-op (no new
    version, no section work, no LLM call) when the combined section
    fingerprint is unchanged since the current version; otherwise a new
    version is written, carrying forward verbatim any section whose own
    fingerprint didn't change (see module docstring)."""
    settings = get_settings()
    provider = provider or get_llm_provider()
    result = ReportResult()

    project = session.get(Project, project_id)
    if project is None:
        return result

    section_fingerprints = {
        name: fn(session, project_id, settings, provider) for name, fn in _FINGERPRINT_FNS.items()
    }
    corpus_fingerprint_hash = _combine_fingerprint(section_fingerprints)

    latest = current_report(session, project_id)
    if latest is not None and latest.corpus_fingerprint_hash == corpus_fingerprint_hash:
        result.report = latest
        return result  # true no-op

    prior_sections: dict = latest.sections if latest is not None else {}
    new_sections: dict[str, dict] = {}
    for name in SECTION_ORDER:
        fingerprint = section_fingerprints[name]
        prior = prior_sections.get(name)
        if prior is not None and prior.get("fingerprint") == fingerprint:
            new_sections[name] = prior
            result.reused_sections.append(name)
            continue

        rendered = _RENDER_FNS[name](session, project_id, settings, provider)
        new_sections[name] = {
            "content": rendered.content,
            "fingerprint": fingerprint,
            "model": rendered.model,
            "model_version": rendered.model_version,
            "rationale": rendered.rationale,
        }
        result.changed_sections.append(name)

    version = (latest.version + 1) if latest is not None else 1
    report = Report(
        project_id=project_id,
        version=version,
        content="",
        sections=new_sections,
        corpus_fingerprint_hash=corpus_fingerprint_hash,
        is_current=True,
    )
    session.add(report)
    session.flush()  # populate report.id + generated_at for the markdown header and audit target

    report.content = _compose_markdown(project, report.version, report.generated_at, new_sections)

    if latest is not None:
        latest.is_current = False

    audit_model, audit_model_version = _audit_model(new_sections, result.changed_sections, provider)
    session.add(
        DecisionAudit(
            decision_type="report",
            target_id=report.id,
            old_value={"version": latest.version} if latest is not None else None,
            new_value={
                "version": report.version,
                "changed_sections": result.changed_sections,
                "reused_sections": result.reused_sections,
                "sections": {
                    name: {"model": s["model"], "model_version": s["model_version"]}
                    for name, s in new_sections.items()
                },
            },
            actor=AuditActor.system,
            model=audit_model,
            model_version=audit_model_version,
            rationale=(
                f"Regenerated report v{report.version}: changed "
                f"{', '.join(result.changed_sections) or '(none)'}; reused "
                f"{', '.join(result.reused_sections) or '(none)'}."
            ),
        )
    )

    result.report = report
    result.is_new_version = True
    # Self-commit, like the other analysis stages (direction/phases/gaps/view).
    # Report was the one stage that left this to the caller, so a standalone
    # run_project_pipeline() (not the API path, which commits afterward) would
    # silently drop the report.
    session.commit()
    return result


def main() -> None:
    """CLI: (re)generate a project's self-updating report.

        uv run python -m truth_engine.analysis.report --project-id <uuid>

    Composed from five independently-fingerprinted sections (overview,
    current direction, recent activity, open gaps, flagged stale artifacts).
    A section whose inputs are unchanged since the current version is reused
    verbatim -- in particular, the LLM-synthesized current-direction section
    is not re-invoked unless the direction snapshot or its current-labeled
    artifact set actually changed. A fully unchanged corpus is a true no-op:
    no new version, no LLM call. Run any time after the other analysis
    stages; sections whose upstream stage hasn't produced data yet degrade to
    an honest "not yet available" rendering rather than erroring.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_project_report(session, args.project_id)
        session.commit()

        if result.report is None:
            print("report: no such project")
        elif not result.is_new_version:
            print(f"report: no-op, v{result.report.version} unchanged (current)")
        else:
            print(
                f"report: v{result.report.version} generated; changed="
                f"{','.join(result.changed_sections) or '(none)'} reused="
                f"{','.join(result.reused_sections) or '(none)'}"
            )


if __name__ == "__main__":
    main()
