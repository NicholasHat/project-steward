"""Stage 8 orchestration (PROJECTSPECS.md §3.4, **Signal B only** — cluster
drift/Signal A, direction labels, and gaps are later increments): a
deterministic citation/reference graph between artifacts of the *same*
project, persisted as `RelationshipEdge`, gated by `StageState(stage=graph)`.

**No LLM.** Three deterministic, precision-favoring match signals (Open Risk
#3 in the plan: unstructured-text reference detection is fuzzy, so this
module is built to under-emit rather than over-emit):

  * **Filename match** (confidence 0.90) — artifact A's `raw_text` contains
    artifact B's `original_filename` verbatim. The strongest signal: a full
    filename appearing in prose is essentially never a coincidence.
  * **Title/heading match** (confidence 0.75) — A's `raw_text` contains a
    string from B's `ArtifactContent.structure` (heading / slide title /
    sheet name). Slightly weaker than a filename — titles are shorter and
    more likely to echo generic project vocabulary — but still a named,
    structural reference to a specific document.
  * **Shared distinctive entity** (confidence 0.55) — A and B both mention
    the same `Entity`, restricted to types that carry a specific identifier
    by construction (`experiment`, `hypothesis`, `citation` — "Experiment
    7", "Hypothesis 2", a DOI) rather than `person`/`group`/`tool`/`cost`,
    which recur across most of a project's corpus (the PI's name, "Python",
    a recurring budget figure) and would turn nearly every artifact pair
    into an edge. Even within the eligible types, an entity mentioned in
    more than `Settings.graph_max_shared_entity_artifacts` distinct
    artifacts is treated as common rather than distinctive and contributes
    no edges — the same precision-over-recall guard, applied per entity
    rather than per type.

Both the filename and title signals are looked up through one shared
**match-key index**, built once per project (`_build_match_index`): every
candidate key (filename / heading string, `>= Settings.graph_min_match_key_chars`
long) maps to the single artifact that owns it — a key claimed by more than
one artifact is ambiguous and dropped entirely rather than guessed at.
Surviving keys are compiled into **one** alternation regex (longest key
first, so a longer key wins over a shorter one it contains), and each
artifact's text is scanned against it exactly once — O(total corpus text),
not O(artifacts²·text) (see CLAUDE.md's complexity note; mirrors
`extract.entities`'s single-pass gazetteer regex, e.g. `_TOOL_RE`). The
shared-entity signal needs no text scanning at all — `EntityMention` already
indexes artifact membership, so it's a single grouped query.

**Temporal edge-typing** (PROJECTSPECS.md §3.4: "a later artifact that
references an earlier one is builds_on"), using each artifact's chosen
`ResolvedDate`:
  * Filename/title match: directional for free (src = artifact whose text
    contains the mention, dst = the artifact named). Typed `builds_on` when
    src's chosen date is strictly after dst's; otherwise `references`
    (includes: no chosen date on one/both sides, or src is not later —
    still a valid citation, just not asserted as *extending* dst's work).
  * Shared entity: no directional text to anchor on, so orientation comes
    entirely from dates. Only entities with >= 2 distinctly-dated mentions
    are usable; those mentions are date-sorted and chained pairwise
    (mention[i] -> `mentioned_by` -> mention[i-1]), never all-pairs, to keep
    edge count linear in mentions rather than quadratic. A tie (equal dates)
    between consecutive mentions is skipped rather than given an arbitrary
    direction — precision over a guessed orientation.

**Dedupe:** per source artifact, at most one edge per destination — if more
than one signal (or more than one matched key) points at the same dst, the
highest-confidence candidate wins (`_keep_best`). No self-edges (checked at
construction and again, defensively, at persistence). A run wholesale-
replaces one artifact's *outgoing* edges only (`src_artifact_id == artifact.id`)
— mirrors `extract`/`embed`/`timeline`/`phases`'s "replace this row set on
rebuild" pattern; another artifact's edges into this one are untouched by
this artifact's rebuild (they're that other artifact's outgoing edges).

**Auditability decision:** no `DecisionAudit` rows are written here. Unlike
`extract`'s date/entity inferences or `phases`'s domain/phase judgments —
where the audit row is the *only* place the decision's rationale is
recorded — `RelationshipEdge` already carries its own `confidence` and
`evidence` (the matching filename/title/entity + surrounding snippet) on
the row itself, and rows are wholesale-replaced rather than amended, so
there is no old-value/new-value transition to capture either. A
`DecisionAudit` row would duplicate exactly the fields already on
`relationship_edges` with nothing added. This mirrors `timeline.py`'s and
`embed.service`'s precedent (derived data, no audit row) rather than
`extract.service`'s / `phases.py`'s (genuinely new inferred fact, gets one).

**Idempotency.** `StageState(artifact_id, stage=graph).input_hash` folds in
the artifact's own `content_hash` **and** a project-wide "target
fingerprint" hash (`_target_fingerprint_hash`) covering every artifact's
filename + structure headings + eligible-entity-type memberships + chosen
`ResolvedDate` — the same corpus-level-fingerprint pattern `phases.py` uses
for domain classification. The chosen date is included because edge *type*
(`references` vs `builds_on`) is decided from it (`_orient_textual_edge`),
so a re-extract that re-chooses a different date without changing the
artifact's text must still invalidate cached edges. Any change to *any*
artifact's candidate-match surface (a filename, a heading, an entity
mention, a chosen date) changes the project-wide fingerprint, which
invalidates every artifact's `input_hash` and rebuilds its outgoing edges —
deliberately coarser than diffing exactly which artifacts' candidate targets
an artifact was actually matched against, in exchange for a single simple,
correct invariant instead of fine-grained dependency tracking. At this
corpus scale (hundreds-low-thousands of docs) a full graph rebuild is cheap
(the match index and entity groups are each one query), so the simpler rule
wins — the same trade-off the plan's Open Risk #5 anticipates ("periodic
full recompute as a correctness backstop"). A second run over an unchanged
corpus makes no DB writes beyond StageState is a no-op for the writes.

`GRAPH_VERSION` is folded into both hashes so a future change to the
matching/typing rules above invalidates cached state, mirroring
`RULESET_VERSION` / `CHUNKER_VERSION` / `TIMELINE_VERSION` / `PHASE_VERSION`.

`ProcessingState` is left untouched, for the same reason `timeline.py` and
`phases.py` leave it untouched: no member names this stage precisely, and
`StageState(stage=graph)` alone is this stage's source of truth.

Failure isolation and per-artifact error recording mirror the other
analysis stages.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from truth_engine.config import Settings, get_settings
from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    EdgeType,
    Entity,
    EntityMention,
    EntityType,
    RelationshipEdge,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.extract._common import context_window

# Bump when the matching/typing rules in this module change materially
# enough that already-built graphs should be rebuilt.
GRAPH_VERSION = "1"

_STRUCTURE_LABEL_KEYS = ("headings", "sheet_names", "slide_titles")

# Entity types eligible for the shared-entity signal: these carry a specific
# identifier by construction (an experiment number, a hypothesis number, a
# citation key) rather than being reused generically across a project's
# corpus — see module docstring.
_ENTITY_SIGNAL_TYPES = (EntityType.experiment, EntityType.hypothesis, EntityType.citation)

FILENAME_CONFIDENCE = 0.90
TITLE_CONFIDENCE = 0.75
ENTITY_CONFIDENCE = 0.55

_KIND_FILENAME = "filename"
_KIND_TITLE = "title"


@dataclass(frozen=True, slots=True)
class _MatchKey:
    artifact_id: uuid.UUID
    kind: str  # _KIND_FILENAME | _KIND_TITLE
    confidence: float


@dataclass(frozen=True, slots=True)
class _CandidateEdge:
    dst_artifact_id: uuid.UUID
    type: EdgeType
    confidence: float
    evidence: str


@dataclass
class GraphResult:
    rebuilt: int = 0
    skipped: int = 0
    edges: int = 0
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


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


# --------------------------------------------------------------------------- #
# Idempotency: project-wide target fingerprint folded into every artifact's  #
# StageState.input_hash (see module docstring)                               #
# --------------------------------------------------------------------------- #
def _target_fingerprint_hash(session: Session, project_id: uuid.UUID) -> str:
    # Chosen dates are joined in here too, not just filenames/structure: an
    # edge's type (`references` vs `builds_on`) is decided by comparing
    # `dates_by_artifact[src]` vs `[dst]` (see `_orient_textual_edge`), so a
    # date changing without the artifact's text changing (e.g. extract is
    # re-run with an upgraded ruleset/model and re-chooses a different
    # candidate date) must still invalidate cached edges -- mirrors why
    # `extract.service` folds `RULESET_VERSION` into its own input_hash.
    rows = session.execute(
        select(
            Artifact.id,
            Artifact.original_filename,
            ArtifactContent.structure,
            ResolvedDate.candidate_date,
        )
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .join(
            ResolvedDate,
            (ResolvedDate.artifact_id == Artifact.id) & (ResolvedDate.is_chosen.is_(True)),
            isouter=True,
        )
        .where(Artifact.project_id == project_id)
    ).all()
    artifact_lines = sorted(
        f"{artifact_id}|{filename}|{','.join(sorted(_structure_labels(structure)))}|"
        f"{chosen_date.isoformat() if chosen_date else ''}"
        for artifact_id, filename, structure, chosen_date in rows
    )

    entity_rows = session.execute(
        select(Entity.id, Entity.type, Entity.normalized_value, EntityMention.artifact_id)
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .join(Artifact, Artifact.id == EntityMention.artifact_id)
        .where(
            Entity.project_id == project_id,
            Entity.type.in_(_ENTITY_SIGNAL_TYPES),
            Artifact.project_id == project_id,
        )
    ).all()
    groups: dict[uuid.UUID, tuple[str, set[uuid.UUID]]] = {}
    for entity_id, etype, normalized_value, artifact_id in entity_rows:
        label, artifact_ids = groups.setdefault(
            entity_id, (f"{etype.value}|{normalized_value}", set())
        )
        artifact_ids.add(artifact_id)
    entity_lines = sorted(
        f"{label}|{','.join(sorted(str(a) for a in ids))}" for label, ids in groups.values()
    )

    raw = f"{GRAPH_VERSION}\n" + "\n".join(artifact_lines) + "\n--\n" + "\n".join(entity_lines)
    return hashlib.sha256(raw.encode()).hexdigest()


def _input_hash(artifact: Artifact, target_fingerprint_hash: str) -> str:
    raw = f"{GRAPH_VERSION}:{artifact.content_hash}:{target_fingerprint_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Match-key index (filename + title signals), built once per project        #
# --------------------------------------------------------------------------- #
def _build_match_index(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> tuple[re.Pattern[str] | None, dict[str, _MatchKey]]:
    rows = session.execute(
        select(Artifact.id, Artifact.original_filename, ArtifactContent.structure)
        .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
        .where(Artifact.project_id == project_id)
    ).all()

    owners: dict[str, set[uuid.UUID]] = defaultdict(set)
    kinds: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}

    def register(text: str, artifact_id: uuid.UUID, kind: str) -> None:
        text = text.strip()
        if len(text) < settings.graph_min_match_key_chars:
            return
        key = text.lower()
        owners[key].add(artifact_id)
        kinds[key].add(kind)
        display.setdefault(key, text)

    for artifact_id, filename, structure in rows:
        register(filename, artifact_id, _KIND_FILENAME)
        for label in _structure_labels(structure):
            register(label, artifact_id, _KIND_TITLE)

    index: dict[str, _MatchKey] = {}
    for key, ids in owners.items():
        if len(ids) != 1:
            continue  # ambiguous key shared by >1 artifact -- drop for precision
        kind = _KIND_FILENAME if _KIND_FILENAME in kinds[key] else _KIND_TITLE
        confidence = FILENAME_CONFIDENCE if kind == _KIND_FILENAME else TITLE_CONFIDENCE
        index[key] = _MatchKey(artifact_id=next(iter(ids)), kind=kind, confidence=confidence)

    if not index:
        return None, index

    # Longest key first: at a given start position the regex engine takes the
    # first alternative that matches, so a longer key must be tried before a
    # shorter key it contains (e.g. "experiment_log.txt" before "log.txt").
    ordered = sorted(index, key=lambda k: len(display[k]), reverse=True)
    alternation = "|".join(re.escape(display[k]) for k in ordered)
    # \w-based boundary (not bare \b) so an adjacent underscore -- common in
    # filenames -- still counts as "part of a larger token" and blocks a
    # spurious match, e.g. "old_experiment_log.txt" must not match
    # "experiment_log.txt".
    pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)
    return pattern, index


def _orient_textual_edge(
    src_id: uuid.UUID, dst_id: uuid.UUID, dates_by_artifact: dict[uuid.UUID, datetime]
) -> EdgeType:
    src_date = dates_by_artifact.get(src_id)
    dst_date = dates_by_artifact.get(dst_id)
    if src_date is not None and dst_date is not None and src_date > dst_date:
        return EdgeType.builds_on
    return EdgeType.references


def _scan_artifact_text(
    source_id: uuid.UUID,
    raw_text: str,
    pattern: re.Pattern[str],
    index: dict[str, _MatchKey],
    dates_by_artifact: dict[uuid.UUID, datetime],
) -> list[_CandidateEdge]:
    found: dict[uuid.UUID, _CandidateEdge] = {}
    for match in pattern.finditer(raw_text):
        key = index.get(match.group(0).lower())
        if key is None or key.artifact_id == source_id:
            continue  # unindexed (shouldn't happen) or a self-mention -- no self-edges
        edge_type = _orient_textual_edge(source_id, key.artifact_id, dates_by_artifact)
        evidence = context_window(raw_text, match.start(), match.end())
        candidate = _CandidateEdge(key.artifact_id, edge_type, key.confidence, evidence)
        existing = found.get(key.artifact_id)
        if existing is None or candidate.confidence > existing.confidence:
            found[key.artifact_id] = candidate
    return list(found.values())


# --------------------------------------------------------------------------- #
# Shared-entity signal                                                       #
# --------------------------------------------------------------------------- #
def _entity_signal_edges(
    session: Session,
    project_id: uuid.UUID,
    settings: Settings,
    dates_by_artifact: dict[uuid.UUID, datetime],
) -> dict[uuid.UUID, list[_CandidateEdge]]:
    rows = session.execute(
        select(
            Entity.id, Entity.type, Entity.value, EntityMention.artifact_id, EntityMention.context
        )
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .join(Artifact, Artifact.id == EntityMention.artifact_id)
        .where(
            Entity.project_id == project_id,
            Entity.type.in_(_ENTITY_SIGNAL_TYPES),
            Artifact.project_id == project_id,
        )
        .order_by(Entity.id, EntityMention.artifact_id)
    ).all()

    by_entity: dict[uuid.UUID, dict[str, object]] = {}
    for entity_id, entity_type, value, artifact_id, mention_context in rows:
        group = by_entity.setdefault(
            entity_id, {"type": entity_type, "value": value, "mentions": {}}
        )
        mentions: dict[uuid.UUID, str | None] = group["mentions"]  # type: ignore[assignment]
        mentions.setdefault(artifact_id, mention_context)

    edges_by_src: dict[uuid.UUID, list[_CandidateEdge]] = defaultdict(list)
    for group in by_entity.values():
        mentions: dict[uuid.UUID, str | None] = group["mentions"]  # type: ignore[assignment]
        if not (2 <= len(mentions) <= settings.graph_max_shared_entity_artifacts):
            # Not shared at all, or common/generic enough to appear across
            # much of the corpus -- neither is a precision-favoring signal.
            continue
        dated = sorted(
            ((aid, dates_by_artifact[aid]) for aid in mentions if aid in dates_by_artifact),
            key=lambda pair: (pair[1], pair[0]),
        )
        if len(dated) < 2:
            continue  # can't reliably orient without dates on at least two mentions

        for (earlier_id, earlier_date), (later_id, later_date) in zip(
            dated, dated[1:], strict=False
        ):
            if earlier_date >= later_date:
                continue  # tied dates -- no basis for a direction, skip rather than guess
            mention_context = mentions.get(later_id) or mentions.get(earlier_id) or ""
            evidence = f'shared {group["type"].value} "{group["value"]}": {mention_context}'.strip()
            edges_by_src[later_id].append(
                _CandidateEdge(earlier_id, EdgeType.mentioned_by, ENTITY_CONFIDENCE, evidence)
            )
    return edges_by_src


# --------------------------------------------------------------------------- #
# Project-wide lookups                                                       #
# --------------------------------------------------------------------------- #
def _chosen_dates(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    rows = session.execute(
        select(ResolvedDate.artifact_id, ResolvedDate.candidate_date)
        .join(Artifact, Artifact.id == ResolvedDate.artifact_id)
        .where(Artifact.project_id == project_id, ResolvedDate.is_chosen.is_(True))
    ).all()
    return dict(rows)


def _raw_text_by_artifact(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = session.execute(
        select(ArtifactContent.artifact_id, ArtifactContent.raw_text)
        .join(Artifact, Artifact.id == ArtifactContent.artifact_id)
        .where(Artifact.project_id == project_id)
    ).all()
    return {artifact_id: text for artifact_id, text in rows if text}


# --------------------------------------------------------------------------- #
# Persistence                                                                #
# --------------------------------------------------------------------------- #
def _keep_best(
    candidates: dict[uuid.UUID, _CandidateEdge], edges: Iterable[_CandidateEdge]
) -> None:
    for edge in edges:
        existing = candidates.get(edge.dst_artifact_id)
        if existing is None or edge.confidence > existing.confidence:
            candidates[edge.dst_artifact_id] = edge


def _replace_edges(session: Session, artifact: Artifact, edges: list[_CandidateEdge]) -> None:
    session.execute(delete(RelationshipEdge).where(RelationshipEdge.src_artifact_id == artifact.id))
    for edge in edges:
        if edge.dst_artifact_id == artifact.id:
            continue  # defensive: no self-edges
        session.add(
            RelationshipEdge(
                src_artifact_id=artifact.id,
                dst_artifact_id=edge.dst_artifact_id,
                type=edge.type,
                confidence=edge.confidence,
                evidence=edge.evidence,
            )
        )


def _upsert_stage_state_done(
    session: Session, artifact_id: uuid.UUID, input_hash: str
) -> None:
    state = _stage_state(session, artifact_id, Stage.graph)
    if state is None:
        state = StageState(artifact_id=artifact_id, stage=Stage.graph, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None


def _record_error(session: Session, artifact: Artifact, input_hash: str, message: str) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.graph)
    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.graph, input_hash=input_hash)
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    session.commit()


# --------------------------------------------------------------------------- #
# Orchestration                                                              #
# --------------------------------------------------------------------------- #
def build_project_graph(session: Session, project_id: uuid.UUID) -> GraphResult:
    """(Re)build the citation/reference graph for every artifact in a
    project whose match-index inputs changed since the last successful run.
    Edges only ever connect two artifacts of this project (every lookup is
    scoped by `project_id`)."""
    result = GraphResult()
    settings = get_settings()

    artifacts = list(
        session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()
    )
    if not artifacts:
        return result

    target_fingerprint_hash = _target_fingerprint_hash(session, project_id)

    pending: list[tuple[Artifact, str]] = []
    for artifact in artifacts:
        input_hash = _input_hash(artifact, target_fingerprint_hash)
        state = _stage_state(session, artifact.id, Stage.graph)
        if state and state.status == StageStatus.done and state.input_hash == input_hash:
            result.skipped += 1
            continue
        pending.append((artifact, input_hash))

    if not pending:
        return result

    # Built once for the whole project, then scanned against per artifact --
    # see module docstring for the complexity rationale.
    pattern, index = _build_match_index(session, project_id, settings)
    dates_by_artifact = _chosen_dates(session, project_id)
    entity_edges_by_src = _entity_signal_edges(session, project_id, settings, dates_by_artifact)
    raw_text_by_artifact = _raw_text_by_artifact(session, project_id)

    for artifact, input_hash in pending:
        try:
            candidates: dict[uuid.UUID, _CandidateEdge] = {}
            raw_text = raw_text_by_artifact.get(artifact.id)
            if raw_text and pattern is not None:
                _keep_best(
                    candidates,
                    _scan_artifact_text(artifact.id, raw_text, pattern, index, dates_by_artifact),
                )
            _keep_best(candidates, entity_edges_by_src.get(artifact.id, []))

            edges = [
                edge
                for edge in candidates.values()
                if edge.confidence >= settings.graph_confidence_floor
            ]
            _replace_edges(session, artifact, edges)
            _upsert_stage_state_done(session, artifact.id, input_hash)
            session.commit()
            result.rebuilt += 1
            result.edges += len(edges)
        except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
            _record_error(session, artifact, input_hash, str(exc))
            result.errors.append((artifact, str(exc)))

    return result


def main() -> None:
    """CLI: (re)build the citation/reference graph for every artifact in a project.

        uv run python -m truth_engine.analysis.graph --project-id <uuid>

    Only artifacts whose match-index inputs changed since the last
    successful run are reprocessed -- `StageState` gates idempotency. Run
    after `python -m truth_engine.extract` (reads filenames/structure/
    entities/resolved dates); does not require embeddings.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = build_project_graph(session, args.project_id)

    print(
        f"graph: {result.rebuilt} rebuilt ({result.edges} edges), "
        f"{result.skipped} skipped, {len(result.errors)} errors"
    )
    for artifact, err in result.errors:
        print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
