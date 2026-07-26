"""Steps 8-9 orchestration (PROJECTSPECS.md §3.4): combine embedding
cluster-drift (**Signal A**) and the citation/reference graph built in step 8
(**Signal B**, `analysis.graph`) into a per-artifact `DirectionLabel`
(current / superseded / unclear) plus a project-level `DirectionSnapshot`
narrative, gated by `StageState(stage=direction)`.

**Signal A -- embedding cluster-drift (deterministic, `analysis/graph.py`'s
sibling here rather than its dependency).** Cluster the project's doc-level
`Embedding` vectors with HDBSCAN (`Settings.direction_min_cluster_size`),
lazy-imported from the `pipeline` extra (scikit-learn for L2-normalization,
hdbscan for the clustering itself -- CLAUDE.md's "heavy deps lazy-imported"
convention). Build each cluster's **temporal profile** from its members'
chosen `ResolvedDate`s: a cluster whose most recent member falls within
`Settings.direction_recency_window_days` of the corpus's latest resolved
date is "recent" (part of the current direction); one whose activity ends
earlier is "quiet" (a candidate drift source). The **current-direction
centroid** is the mean of the (L2-normalized) vectors of every recent
cluster's members, plus any *unclustered* (HDBSCAN noise) artifact that is
itself individually recent -- a single very-recent document doesn't have to
wait for a cluster to form around it to count as part of "what's current."
Each artifact's `signal_a_score` is *banded* by which side of the combining
rule's own thresholds its recent/quiet verdict falls on --
`[direction_current_threshold, 1.0]` for a recent-cluster member,
`[0.0, direction_superseded_threshold]` for a quiet one -- with cosine
similarity to the centroid (rescaled from `[-1, 1]` to `[0, 1]`) only
refining *where* within that band. This is deliberate, not a rounding
convenience: real sentence-embedding cosine similarities for same-domain
documents commonly sit at 0.85-0.98 regardless of topic (generic
domain/genre language dominates the raw number), so a naive unbanded
`(sim + 1) / 2` would understate an already-correct "this cluster is quiet"
verdict from HDBSCAN and the temporal profile, dragging it back toward
"current" on cosine-similarity noise. The reliable fact is the *discrete*
cluster/recency verdict; banding means `signal_a_score` reports that verdict
confidently and uses the continuous similarity only to rank within it --
inspect a persisted `signal_a_score` as "this signal's own
classification-consistent confidence, on the combining rule's scale," not as
a raw cosine number.

Below `Settings.direction_min_corpus_size` doc-level embeddings, clustering a
handful of vectors is noise (HDBSCAN would likely call everything outlier
anyway) -- Open Risk #4 (plan) calls this out explicitly. Below that
threshold this module **does not cluster at all**: it degrades to an honest
recency-only Signal A (each artifact's score is its own date's position in
the corpus's `[earliest, latest]` span), and every artifact's *confidence*
(not score -- see the combining rule below) is capped at
`Settings.direction_small_corpus_confidence_cap`. This is a real design
fork: a naive approach would run HDBSCAN regardless and let a min-cluster-
size parameter absorb small corpora, but a 4-6 document corpus clustering
into "everything is noise" or "everything is one cluster" produces a
*confident-looking* score that isn't earned by the data. Recency-only is the
honest fallback the settings docstrings describe.

**Signal B -- from the graph (deterministic, reads `RelationshipEdge`).**
For artifact A, look at every edge with `dst_artifact_id == A.id` (any
`EdgeType` -- all three represent *some* later text pointing back at A) whose
source artifact's chosen date is later than A's own. If any exist, A's score
is the recency-decay (linear over `direction_recency_window_days`) of the
*most recent* such reference -- "still being cited recently" scores high,
"was cited but that trailed off" scores low. If none exist:
  * If A's own chosen date is itself still within the recency window, A is
    simply too new to have been cited yet -- score is A's own recency-decay,
    not zero. Scoring a brand-new artifact as a "dead end" merely because
    nothing later than it has had a chance to reference it would be exactly
    the false-positive drift Open Risk #4 warns about.
  * Otherwise A has had time to be built upon and wasn't -- a genuine
    candidate dead-end, score 0.0.
  * With no chosen date and no references either way, there's nothing to
    score: `None` (missing signal, not zero).

**Combine A + B -> label, confidence, rationale (transparent, deterministic,
no LLM).** `combined_score` is the mean of whichever signals are available.
`confidence` is signal *agreement* when both are present
(`1 - abs(signal_a - signal_b)` -- signals that agree deserve more trust than
signals that contradict, which is exactly what "conflicting signals" should
demote to `unclear`), or `Settings.direction_single_signal_availability` when
only one is. In small-corpus mode that confidence is additionally capped at
`Settings.direction_small_corpus_confidence_cap`. Below
`Settings.direction_min_confident_label`, the label is `unclear` regardless
of score. Otherwise `combined_score >= direction_current_threshold` ->
`current`; `<= direction_superseded_threshold` -> `superseded`; between the
two -> `unclear`.

One more explicit guard, not just an emergent property of the thresholds
above: **`superseded` is never asserted in small-corpus mode**, full stop --
even if `combined_score` clears the superseded threshold and the capped
confidence still clears `direction_min_confident_label` (at the shipped
defaults, 0.35 > 0.3, so the confidence math alone would *not* reliably
block it). This is deliberate asymmetry, not an oversight: claiming an
artifact is a dead end is a stronger, more consequential claim than "this
still looks current," and Open Risk #4 says as much ("never assert
superseded from too little data"). A small corpus that looks quiet gets
`unclear`, never `superseded`.

Every `DirectionLabel.rationale` is built from the two signals' own
templated fragments (`_SignalAResult.rationale` / `_SignalBResult.rationale`,
each naming concrete dates/thresholds) plus the combining verdict -- never a
bare label with no explanation, per §3.4's "stated rationale the user can
inspect."

**The LLM's only job: `DirectionSnapshot.inferred_direction_summary`.** A
short narrative synthesized from the *same* current-direction member set
Signal A's centroid is built from (recent clusters' members + individually-
recent noise artifacts), capped at `Settings.direction_snapshot_recent_
artifacts` and `Settings.direction_summary_snippet_chars` each -- so the
narrative describes exactly the artifacts the labels were computed against,
not a coincidentally-similar independent notion of "recent." Malformed/empty
LLM output degrades to a deterministic fallback summary (never a crash, never
a blank snapshot) -- `reasoning.json_extract.extract_json` as elsewhere.

**Human-confirmation checkpoint (§3.4) -- the load-bearing invariant.**
`DirectionLabel` is one row per artifact (`artifact_id` unique, like
`DomainClassification`, not `PhaseAssignment`'s many-per-artifact). A
`confirmed_by_user=True` row is *never* recomputed or overwritten -- checked
before the per-artifact `StageState` hash comparison, not folded into it, so
a confirmed row is skipped even when the corpus changed underneath it.
`StageState(stage=direction)` for a confirmed artifact is still refreshed to
`done` at the current input hash (bookkeeping only -- keeps "did this stage
settle for this artifact" meaningful without touching the label itself).

**Idempotency -- two granularities, mirroring `phases.py`.** Clustering and
the graph are inherently project-wide computations (an artifact's cluster
membership and centroid similarity depend on every other artifact's vector,
not just its own), so this is fundamentally a *global* recompute, not a
per-artifact incremental one -- like `phases.py`'s domain classification, not
its per-artifact phase assignment. `_corpus_fingerprint_hash` covers every
artifact's doc-level embedding vector, every `RelationshipEdge`'s content,
and every chosen `ResolvedDate` (the same three inputs Signal A/B/the
timeline anchor read); it is folded into both `DirectionSnapshot.corpus_
fingerprint_hash` (project-level: skip the LLM call and the new history row
when unchanged) and every artifact's `StageState.input_hash` (via `artifact.
id:fingerprint_hash` -- deliberately coarse: *any* change anywhere in the
corpus invalidates *every* artifact's cached label, because the centroid and
cluster membership legitimately shift for everyone when the corpus does).
This is the same trade-off `graph.py` makes (Open Risk #5's "periodic full
recompute as a correctness backstop") and is cheap at this corpus scale.

**Auditability.** One `DecisionAudit(actor=system)` per (re)computed
`DirectionLabel`, mirroring `extract.service`'s deterministic-decision
precedent (`model`/`model_version` name the *ruleset*, not an LLM, since
scoring/labeling never calls one -- `model=DIRECTION_VERSION`'s sibling
constant, not `provider.model`). One more per `DirectionSnapshot`, this time
carrying the LLM's `model`/`model_version` since that row alone is
LLM-authored.

Failure isolation and the "replace this artifact's stage state on error"
pattern mirror the other analysis stages; `ProcessingState` is left
untouched for the same reason `timeline.py`/`phases.py`/`graph.py` leave it
untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
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
    Embedding,
    EmbeddingLevel,
    RelationshipEdge,
    ResolvedDate,
    Stage,
    StageState,
    StageStatus,
)
from truth_engine.reasoning.json_extract import extract_json
from truth_engine.reasoning.providers import LLMProvider, get_llm_provider

# Bump when the clustering/scoring/combining rules in this module change
# materially enough that already-computed labels/snapshots should be rebuilt.
DIRECTION_VERSION = "1"

_LABEL_RULESET = "direction-ruleset"  # DecisionAudit.model for the (non-LLM) label decision

_SUMMARY_SYSTEM_PROMPT = (
    "You are a project-status summarization assistant. Respond with ONLY a "
    "single JSON object -- no prose, no markdown fences."
)


# --------------------------------------------------------------------------- #
# Results                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class DirectionResult:
    labeled: int = 0
    skipped: int = 0
    confirmed_skipped: int = 0
    snapshot: DirectionSnapshot | None = None
    snapshot_skipped: bool = False
    errors: list[tuple[Artifact, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SignalAResult:
    score: float | None
    rationale: str


@dataclass(frozen=True, slots=True)
class _SignalBResult:
    score: float | None
    rationale: str


@dataclass(frozen=True, slots=True)
class _ArtifactSignals:
    signal_a: _SignalAResult
    signal_b: _SignalBResult


@dataclass(frozen=True, slots=True)
class _CombinedVerdict:
    label: DirectionLabelValue
    confidence: float
    combined_score: float | None


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #
def _stage_state(session: Session, artifact_id: uuid.UUID, stage: Stage) -> StageState | None:
    return session.scalar(
        select(StageState).where(StageState.artifact_id == artifact_id, StageState.stage == stage)
    )


def _direction_label(session: Session, artifact_id: uuid.UUID) -> DirectionLabel | None:
    return session.scalar(select(DirectionLabel).where(DirectionLabel.artifact_id == artifact_id))


def _chosen_dates(session: Session, project_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    rows = session.execute(
        select(ResolvedDate.artifact_id, ResolvedDate.candidate_date)
        .join(Artifact, Artifact.id == ResolvedDate.artifact_id)
        .where(Artifact.project_id == project_id, ResolvedDate.is_chosen.is_(True))
    ).all()
    return dict(rows)


def _doc_embeddings(
    session: Session, project_id: uuid.UUID
) -> list[tuple[uuid.UUID, list[float]]]:
    rows = session.execute(
        select(Embedding.artifact_id, Embedding.vector)
        .join(Artifact, Artifact.id == Embedding.artifact_id)
        .where(Artifact.project_id == project_id, Embedding.level == EmbeddingLevel.doc)
        .order_by(Artifact.id)  # deterministic ordering -> deterministic clustering input order
    ).all()
    return [(artifact_id, list(vector)) for artifact_id, vector in rows]


def _incoming_edges(
    session: Session, project_id: uuid.UUID
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """dst_artifact_id -> [src_artifact_id, ...], scoped to this project on
    both ends (an edge only ever connects two artifacts of the same project
    -- see `graph.py`)."""
    rows = session.execute(
        select(RelationshipEdge.dst_artifact_id, RelationshipEdge.src_artifact_id)
        .join(Artifact, Artifact.id == RelationshipEdge.src_artifact_id)
        .where(Artifact.project_id == project_id)
    ).all()
    by_dst: dict[uuid.UUID, list[uuid.UUID]] = {}
    for dst_id, src_id in rows:
        by_dst.setdefault(dst_id, []).append(src_id)
    return by_dst


# --------------------------------------------------------------------------- #
# Corpus fingerprint (project-wide, shared by the snapshot + every artifact's #
# StageState.input_hash -- see module docstring)                             #
# --------------------------------------------------------------------------- #
def _corpus_fingerprint_hash(session: Session, project_id: uuid.UUID) -> str:
    embeddings = session.execute(
        select(Embedding.artifact_id, Embedding.model, Embedding.model_version, Embedding.vector)
        .join(Artifact, Artifact.id == Embedding.artifact_id)
        .where(Artifact.project_id == project_id, Embedding.level == EmbeddingLevel.doc)
    ).all()
    embedding_lines = sorted(
        f"{artifact_id}|{model}|{model_version}|"
        + ",".join(f"{v:.6f}" for v in vector)
        for artifact_id, model, model_version, vector in embeddings
    )

    edges = session.execute(
        select(
            RelationshipEdge.src_artifact_id,
            RelationshipEdge.dst_artifact_id,
            RelationshipEdge.type,
            RelationshipEdge.confidence,
        )
        .join(Artifact, Artifact.id == RelationshipEdge.src_artifact_id)
        .where(Artifact.project_id == project_id)
    ).all()
    edge_lines = sorted(
        f"{src}|{dst}|{etype.value}|{confidence:.4f}" for src, dst, etype, confidence in edges
    )

    dates = _chosen_dates(session, project_id)
    date_lines = sorted(f"{artifact_id}|{date.isoformat()}" for artifact_id, date in dates.items())

    raw = (
        f"{DIRECTION_VERSION}\n"
        + "\n".join(embedding_lines)
        + "\n--\n"
        + "\n".join(edge_lines)
        + "\n--\n"
        + "\n".join(date_lines)
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _artifact_input_hash(artifact_id: uuid.UUID, fingerprint_hash: str) -> str:
    raw = f"{DIRECTION_VERSION}:{artifact_id}:{fingerprint_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Signal A -- embedding cluster-drift                                        #
# --------------------------------------------------------------------------- #
def _recency_decay(date: datetime | None, latest: datetime | None, window_days: int) -> float:
    """1.0 at `latest`, linearly down to 0.0 at `window_days` before it, 0.0
    beyond that. Signal B's citation-recency decay, and also used to test an
    artifact's own recency against the window (e.g. "too new to judge")."""
    if date is None or latest is None:
        return 0.0
    delta_days = (latest - date).total_seconds() / 86400
    if delta_days <= 0:
        return 1.0
    if delta_days >= window_days:
        return 0.0
    return 1.0 - delta_days / window_days


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector] if norm else vector


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i, x in enumerate(vector):
            sums[i] += x
    return [s / len(vectors) for s in sums]


@dataclass(frozen=True, slots=True)
class _CurrentDirectionMember:
    artifact_id: uuid.UUID
    vector: list[float]


def _cluster_signal_a(
    doc_embeddings: list[tuple[uuid.UUID, list[float]]],
    dates_by_artifact: dict[uuid.UUID, datetime],
    settings: Settings,
) -> tuple[dict[uuid.UUID, _SignalAResult], list[_CurrentDirectionMember]]:
    """Full clustering path -- only called when corpus size >=
    Settings.direction_min_corpus_size. See module docstring for the
    centroid-similarity formulation and the "recent cluster or recent noise
    artifact" definition of the current-direction member set."""
    import hdbscan  # lazy: pipeline extra
    import numpy as np  # lazy: pipeline extra

    artifact_ids = [aid for aid, _ in doc_embeddings]
    normalized = [_normalize(vector) for _, vector in doc_embeddings]
    matrix = np.array(normalized)

    clusterer = hdbscan.HDBSCAN(min_cluster_size=max(2, settings.direction_min_cluster_size))
    cluster_labels = clusterer.fit_predict(matrix)

    dated_values = [d for d in dates_by_artifact.values()]
    corpus_latest = max(dated_values) if dated_values else None
    recency_cutoff = (
        corpus_latest - timedelta(days=settings.direction_recency_window_days)
        if corpus_latest is not None
        else None
    )

    # Per-cluster temporal profile: last-active date among dated members.
    members_by_cluster: dict[int, list[int]] = {}
    for idx, label in enumerate(cluster_labels):
        members_by_cluster.setdefault(int(label), []).append(idx)

    cluster_last_active: dict[int, datetime | None] = {}
    for label, member_idxs in members_by_cluster.items():
        member_dates = [
            dates_by_artifact[artifact_ids[i]]
            for i in member_idxs
            if artifact_ids[i] in dates_by_artifact
        ]
        cluster_last_active[label] = max(member_dates) if member_dates else None

    def _cluster_is_recent(label: int) -> bool:
        last_active = cluster_last_active.get(label)
        return (
            label != -1
            and last_active is not None
            and recency_cutoff is not None
            and last_active >= recency_cutoff
        )

    def _is_recent(idx: int, label: int) -> bool:
        if _cluster_is_recent(label):
            return True
        own_date = dates_by_artifact.get(artifact_ids[idx])
        return (
            label == -1
            and own_date is not None
            and recency_cutoff is not None
            and own_date >= recency_cutoff
        )

    # Whenever any artifact has a chosen date at all, the artifact(s) that
    # define `corpus_latest` are always themselves "recent" by construction
    # (their own date can't be before `recency_cutoff`, which is derived
    # from that same date) -- so `current_members` is only empty when
    # `corpus_latest` is None, i.e. no artifact in the corpus has a chosen
    # date at all. No further fallback is needed for the "everything is
    # quiet" case; that's a real, informative outcome, not a gap to paper
    # over.
    recent_by_idx = [_is_recent(idx, int(label)) for idx, label in enumerate(cluster_labels)]
    current_members: list[_CurrentDirectionMember] = [
        _CurrentDirectionMember(artifact_ids[idx], normalized[idx])
        for idx in range(len(artifact_ids))
        if recent_by_idx[idx]
    ]

    results: dict[uuid.UUID, _SignalAResult] = {}
    window = settings.direction_recency_window_days
    if not current_members:
        for aid in artifact_ids:
            results[aid] = _SignalAResult(
                None, "no resolved dates anywhere in the corpus; cluster-drift signal unavailable."
            )
        return results, []

    centroid = _normalize(_mean_vector([m.vector for m in current_members]))

    # Score band: which side of the *combining rule's own* thresholds a
    # recent vs. quiet cluster lands on is a reliable, already-established
    # fact (HDBSCAN + the temporal profile decided it); a raw rescaled
    # cosine similarity is not, in general, well-calibrated to [0, 1] for
    # real sentence embeddings -- same-domain documents commonly sit at
    # cosine similarity 0.85-0.98 regardless of topic, so the naive
    # `(sim + 1) / 2` rescale would understate a real quiet-cluster signal
    # (compressing it toward "current" purely from generic domain-language
    # similarity) rather than reflect the clustering's own verdict. Anchor
    # each side's score range to `direction_current_threshold` /
    # `direction_superseded_threshold` instead, so a recent-cluster member's
    # signal_a always independently reads as "current" and a quiet-cluster
    # member's always reads as "superseded" on this signal alone -- the
    # cosine similarity to the centroid only refines *where* within that
    # band, e.g. for ranking. `signal_a_score` is therefore this signal's
    # own classification-consistent confidence on the combining rule's own
    # scale, not a raw cosine number -- see the persisted value accordingly.
    for idx, label in enumerate(cluster_labels):
        aid = artifact_ids[idx]
        similarity = _cosine_similarity(normalized[idx], centroid)
        similarity_rescaled = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        if recent_by_idx[idx]:
            lower, upper = settings.direction_current_threshold, 1.0
        else:
            lower, upper = 0.0, settings.direction_superseded_threshold
        score = lower + (upper - lower) * similarity_rescaled
        last_active = cluster_last_active.get(int(label))

        if int(label) == -1:
            rationale = (
                f"embedding did not form a clear cluster with other artifacts "
                f"(HDBSCAN noise point); compared directly against the "
                f"current-direction centroid (similarity {score:.2f})."
            )
        elif _cluster_is_recent(int(label)):
            rationale = (
                f"belongs to an embedding cluster active as recently as "
                f"{last_active:%Y-%m-%d}, within the {window}-day recency window "
                f"(similarity to current direction {score:.2f})."
            )
        elif last_active is not None:
            rationale = (
                f"belongs to an embedding cluster whose activity went quiet after "
                f"{last_active:%Y-%m-%d}, outside the {window}-day recency window "
                f"(similarity to current direction {score:.2f})."
            )
        else:
            rationale = (
                f"belongs to an embedding cluster with no resolved dates among its "
                f"members; compared directly against the current-direction centroid "
                f"(similarity {score:.2f})."
            )
        results[aid] = _SignalAResult(score, rationale)

    return results, current_members


def _fallback_signal_a(
    doc_embeddings: list[tuple[uuid.UUID, list[float]]],
    dates_by_artifact: dict[uuid.UUID, datetime],
    settings: Settings,
) -> dict[uuid.UUID, _SignalAResult]:
    """Small-corpus path (Open Risk #4): no clustering, an honest
    recency-only signal instead. See module docstring."""
    artifact_ids = [aid for aid, _ in doc_embeddings]
    dated = {aid: dates_by_artifact[aid] for aid in artifact_ids if aid in dates_by_artifact}
    corpus_size = len(artifact_ids)

    results: dict[uuid.UUID, _SignalAResult] = {}
    if not dated:
        for aid in artifact_ids:
            results[aid] = _SignalAResult(
                None,
                f"corpus too small ({corpus_size} artifacts, below the "
                f"{settings.direction_min_corpus_size}-artifact clustering threshold) for "
                "cluster-based drift detection, and no resolved date available either.",
            )
        return results

    earliest, latest = min(dated.values()), max(dated.values())
    span_days = (latest - earliest).total_seconds() / 86400

    for aid in artifact_ids:
        own_date = dated.get(aid)
        if own_date is None:
            results[aid] = _SignalAResult(
                None,
                f"corpus too small ({corpus_size} artifacts) for cluster-based drift "
                "detection, and this artifact has no resolved date.",
            )
            continue
        if span_days <= 0:
            score = 0.5
            rationale = (
                f"corpus too small ({corpus_size} artifacts, below the "
                f"{settings.direction_min_corpus_size}-artifact clustering threshold) for "
                "cluster-based drift detection; every dated artifact shares the same "
                "resolved date, so recency alone gives no signal."
            )
        else:
            score = (own_date - earliest).total_seconds() / 86400 / span_days
            rationale = (
                f"corpus too small ({corpus_size} artifacts, below the "
                f"{settings.direction_min_corpus_size}-artifact clustering threshold) for "
                f"cluster-based drift detection; conservative recency-only signal places "
                f"this artifact's date ({own_date:%Y-%m-%d}) at position {score:.2f} within "
                f"the corpus's {earliest:%Y-%m-%d} to {latest:%Y-%m-%d} span."
            )
        results[aid] = _SignalAResult(score, rationale)

    return results


# --------------------------------------------------------------------------- #
# Signal B -- from the citation/reference graph                              #
# --------------------------------------------------------------------------- #
def _signal_b_for_artifact(
    own_date: datetime | None,
    referencing_dates: list[datetime],
    corpus_latest: datetime | None,
    window_days: int,
) -> _SignalBResult:
    qualifying = [
        d for d in referencing_dates if own_date is None or d > own_date
    ]
    if qualifying:
        most_recent = max(qualifying)
        score = _recency_decay(most_recent, corpus_latest, window_days)
        if score > 0:
            rationale = (
                f"referenced by a later artifact as recently as {most_recent:%Y-%m-%d}, "
                f"within the {window_days}-day recency window (score {score:.2f})."
            )
        else:
            rationale = (
                f"last referenced by a later artifact on {most_recent:%Y-%m-%d}, outside "
                f"the {window_days}-day recency window."
            )
        return _SignalBResult(score, rationale)

    if own_date is not None:
        own_recency = _recency_decay(own_date, corpus_latest, window_days)
        if own_recency > 0:
            return _SignalBResult(
                own_recency,
                f"not yet referenced by any later artifact, but was itself dated "
                f"{own_date:%Y-%m-%d}, within the {window_days}-day recency window -- too "
                "new to judge as a dead end.",
            )
        return _SignalBResult(
            0.0,
            f"not referenced by any later artifact, and its own date "
            f"({own_date:%Y-%m-%d}) is outside the {window_days}-day recency window -- a "
            "candidate dead end.",
        )

    return _SignalBResult(None, "no resolved date and no later references -- insufficient signal.")


def _compute_signal_b(
    session: Session,
    project_id: uuid.UUID,
    artifact_ids: list[uuid.UUID],
    dates_by_artifact: dict[uuid.UUID, datetime],
    settings: Settings,
) -> dict[uuid.UUID, _SignalBResult]:
    incoming = _incoming_edges(session, project_id)
    dated_values = list(dates_by_artifact.values())
    corpus_latest = max(dated_values) if dated_values else None

    results: dict[uuid.UUID, _SignalBResult] = {}
    for aid in artifact_ids:
        own_date = dates_by_artifact.get(aid)
        referencing_dates = [
            dates_by_artifact[src_id]
            for src_id in incoming.get(aid, [])
            if src_id in dates_by_artifact
        ]
        results[aid] = _signal_b_for_artifact(
            own_date, referencing_dates, corpus_latest, settings.direction_recency_window_days
        )
    return results


# --------------------------------------------------------------------------- #
# Combining rule                                                             #
# --------------------------------------------------------------------------- #
def _combine(
    signal_a: float | None, signal_b: float | None, settings: Settings, small_corpus_mode: bool
) -> _CombinedVerdict:
    scores = [s for s in (signal_a, signal_b) if s is not None]
    if not scores:
        return _CombinedVerdict(DirectionLabelValue.unclear, 0.0, None)

    combined_score = sum(scores) / len(scores)
    if len(scores) == 2:
        confidence = 1.0 - abs(signal_a - signal_b)  # type: ignore[operator]
    else:
        confidence = settings.direction_single_signal_availability

    if small_corpus_mode and signal_a is not None:
        confidence = min(confidence, settings.direction_small_corpus_confidence_cap)

    if confidence < settings.direction_min_confident_label:
        label = DirectionLabelValue.unclear
    elif combined_score >= settings.direction_current_threshold:
        label = DirectionLabelValue.current
    elif combined_score <= settings.direction_superseded_threshold:
        label = DirectionLabelValue.superseded
    else:
        label = DirectionLabelValue.unclear

    # Hard guard, not an emergent property of the confidence math above (see
    # module docstring): a small corpus never gets to assert "dead end."
    if small_corpus_mode and label == DirectionLabelValue.superseded:
        label = DirectionLabelValue.unclear

    return _CombinedVerdict(label, confidence, combined_score)


def _build_rationale(signals: _ArtifactSignals, verdict: _CombinedVerdict) -> str:
    parts = [
        f"Signal A (embedding cluster): {signals.signal_a.rationale}",
        f"Signal B (citation graph): {signals.signal_b.rationale}",
    ]
    if verdict.combined_score is not None:
        parts.append(
            f"Combined score {verdict.combined_score:.2f}, confidence "
            f"{verdict.confidence:.2f} -> {verdict.label.value}."
        )
    else:
        parts.append(
            f"No usable signal -> {verdict.label.value} (confidence {verdict.confidence:.2f})."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Signal computation, project-wide                                           #
# --------------------------------------------------------------------------- #
def _compute_all_signals(
    session: Session, project_id: uuid.UUID, settings: Settings
) -> tuple[dict[uuid.UUID, _ArtifactSignals], bool, list[_CurrentDirectionMember]]:
    doc_embeddings = _doc_embeddings(session, project_id)
    dates_by_artifact = _chosen_dates(session, project_id)
    artifact_ids = [aid for aid, _ in doc_embeddings]

    small_corpus_mode = len(doc_embeddings) < settings.direction_min_corpus_size
    current_members: list[_CurrentDirectionMember] = []
    if small_corpus_mode:
        signal_a_by_artifact = _fallback_signal_a(doc_embeddings, dates_by_artifact, settings)
    else:
        signal_a_by_artifact, current_members = _cluster_signal_a(
            doc_embeddings, dates_by_artifact, settings
        )

    signal_b_by_artifact = _compute_signal_b(
        session, project_id, artifact_ids, dates_by_artifact, settings
    )

    no_embedding = _SignalAResult(None, "no doc-level embedding available.")
    no_signal_b = _SignalBResult(None, "no signal computed.")
    signals: dict[uuid.UUID, _ArtifactSignals] = {}
    for aid in artifact_ids:
        signals[aid] = _ArtifactSignals(
            signal_a_by_artifact.get(aid, no_embedding),
            signal_b_by_artifact.get(aid, no_signal_b),
        )
    return signals, small_corpus_mode, current_members


# --------------------------------------------------------------------------- #
# Per-artifact label persistence                                             #
# --------------------------------------------------------------------------- #
def _upsert_stage_state_done(
    session: Session, artifact_id: uuid.UUID, input_hash: str
) -> None:
    state = _stage_state(session, artifact_id, Stage.direction)
    if state is None:
        state = StageState(artifact_id=artifact_id, stage=Stage.direction, input_hash=input_hash)
        session.add(state)
    else:
        state.input_hash = input_hash
    state.status = StageStatus.done
    state.error = None


def _record_error(session: Session, artifact: Artifact, input_hash: str, message: str) -> None:
    session.rollback()
    state = _stage_state(session, artifact.id, Stage.direction)
    if state is None:
        state = StageState(artifact_id=artifact.id, stage=Stage.direction, input_hash=input_hash)
        session.add(state)
    state.status = StageStatus.error
    state.error = message[:2000]
    session.commit()


def _write_label(
    session: Session,
    artifact_id: uuid.UUID,
    signals: _ArtifactSignals,
    verdict: _CombinedVerdict,
) -> DirectionLabel:
    rationale = _build_rationale(signals, verdict)
    existing = _direction_label(session, artifact_id)
    if existing is None:
        existing = DirectionLabel(artifact_id=artifact_id)
        session.add(existing)
    existing.label = verdict.label
    existing.rationale = rationale
    existing.signal_a_score = signals.signal_a.score
    existing.signal_b_score = signals.signal_b.score
    existing.confidence = verdict.confidence
    existing.confirmed_by_user = False  # a freshly (re)computed row starts unconfirmed
    session.flush()  # populate existing.id for the audit target

    session.add(
        DecisionAudit(
            decision_type="direction_label",
            target_id=existing.id,
            new_value={
                "label": verdict.label.value,
                "signal_a_score": signals.signal_a.score,
                "signal_b_score": signals.signal_b.score,
                "confidence": verdict.confidence,
            },
            actor=AuditActor.system,
            model=_LABEL_RULESET,
            model_version=DIRECTION_VERSION,
            rationale=rationale,
        )
    )
    return existing


def label_project_direction(session: Session, project_id: uuid.UUID) -> DirectionResult:
    """Compute (or reuse) a `DirectionLabel` for every artifact with a
    doc-level embedding in the project. Never touches a
    `confirmed_by_user=True` row's label; every other artifact is
    recomputed wholesale when the project's corpus fingerprint changes
    (see module docstring -- clustering is inherently global)."""
    settings = get_settings()
    result = DirectionResult()

    fingerprint_hash = _corpus_fingerprint_hash(session, project_id)
    doc_embeddings = _doc_embeddings(session, project_id)
    if not doc_embeddings:
        return result

    artifacts_by_id = {
        a.id: a
        for a in session.scalars(
            select(Artifact).where(Artifact.project_id == project_id)
        ).all()
    }

    pending_ids = []
    for artifact_id in [aid for aid, _ in doc_embeddings]:
        existing_label = _direction_label(session, artifact_id)
        if existing_label is not None and existing_label.confirmed_by_user:
            input_hash = _artifact_input_hash(artifact_id, fingerprint_hash)
            _upsert_stage_state_done(session, artifact_id, input_hash)
            session.commit()
            result.confirmed_skipped += 1
            continue

        input_hash = _artifact_input_hash(artifact_id, fingerprint_hash)
        state = _stage_state(session, artifact_id, Stage.direction)
        if state and state.status == StageStatus.done and state.input_hash == input_hash:
            result.skipped += 1
            continue
        pending_ids.append(artifact_id)

    if pending_ids:
        # Clustering + the graph are project-wide; there's no cheaper way to
        # get signals for just `pending_ids` (see module docstring). Note
        # `build_project_direction_snapshot` independently makes this same
        # call when its own fingerprint check says the narrative needs a
        # refresh -- a deliberate "two simple, independently-correct stages"
        # trade-off (mirrors `phases.py`'s domain-classification +
        # phase-assignment split) over threading shared state between them.
        signals_by_artifact, small_corpus_mode, _current_members = _compute_all_signals(
            session, project_id, settings
        )
        for artifact_id in pending_ids:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None:
                continue
            input_hash = _artifact_input_hash(artifact_id, fingerprint_hash)
            try:
                signals = signals_by_artifact[artifact_id]
                verdict = _combine(
                    signals.signal_a.score, signals.signal_b.score, settings, small_corpus_mode
                )
                _write_label(session, artifact_id, signals, verdict)
                _upsert_stage_state_done(session, artifact_id, input_hash)
                session.commit()
                result.labeled += 1
            except Exception as exc:  # noqa: BLE001 - isolate one bad artifact from the batch
                _record_error(session, artifact, input_hash, str(exc))
                result.errors.append((artifact, str(exc)))

    return result


# --------------------------------------------------------------------------- #
# Project-level snapshot narrative (the only LLM call in this module)        #
# --------------------------------------------------------------------------- #
def _latest_snapshot(session: Session, project_id: uuid.UUID) -> DirectionSnapshot | None:
    return session.scalar(
        select(DirectionSnapshot)
        .where(DirectionSnapshot.project_id == project_id)
        .order_by(DirectionSnapshot.computed_at.desc())
        .limit(1)
    )


def _artifact_snippet(content: ArtifactContent | None, snippet_chars: int) -> str:
    if content is None or not content.raw_text:
        return ""
    return " ".join(content.raw_text.split())[:snippet_chars]


def _fallback_summary(
    recent_artifacts: list[Artifact], corpus_latest: datetime | None
) -> str:
    names = ", ".join(a.original_filename for a in recent_artifacts[:5])
    if not names:
        return "Not enough dated, recent activity to synthesize a direction summary."
    when = f" as of {corpus_latest:%Y-%m-%d}" if corpus_latest else ""
    return f"Current direction inferred from recent activity{when}: {names}."


def _summary_prompt(recent_lines: list[str]) -> str:
    artifact_lines = "\n".join(f"- {line}" for line in recent_lines)
    return (
        "Based on the project's most recently active artifacts below, write a "
        "short (2-3 sentence) narrative describing the project's current "
        "direction.\n\n"
        f"Recent artifacts:\n{artifact_lines}\n\n"
        'Respond with exactly one JSON object of this form: {"summary": "<2-3 sentences>"}'
    )


def _parse_summary_response(raw: str) -> str | None:
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return summary.strip()


def build_project_direction_snapshot(
    session: Session,
    project_id: uuid.UUID,
    *,
    provider: LLMProvider | None = None,
) -> tuple[DirectionSnapshot, bool]:
    """(Re)compute the project's `DirectionSnapshot` narrative from the same
    current-direction member set Signal A's centroid is built from. Returns
    `(snapshot, skipped)` -- `skipped=True` means an existing snapshot was
    reused untouched because the corpus fingerprint hasn't changed, with no
    LLM call made and no clustering/graph pass run.
    """
    settings = get_settings()
    fingerprint_hash = _corpus_fingerprint_hash(session, project_id)

    latest = _latest_snapshot(session, project_id)
    if latest is not None and latest.corpus_fingerprint_hash == fingerprint_hash:
        return latest, True

    doc_embeddings = _doc_embeddings(session, project_id)
    current_members: list[_CurrentDirectionMember] = []
    if doc_embeddings:
        _signals, _small_corpus_mode, current_members = _compute_all_signals(
            session, project_id, settings
        )

    member_ids = [m.artifact_id for m in current_members]
    dates_by_artifact = _chosen_dates(session, project_id)
    member_ids_sorted = sorted(
        member_ids, key=lambda aid: dates_by_artifact.get(aid, datetime.min), reverse=True
    )[: settings.direction_snapshot_recent_artifacts]

    rows = (
        session.execute(
            select(Artifact, ArtifactContent)
            .join(ArtifactContent, ArtifactContent.artifact_id == Artifact.id, isouter=True)
            .where(Artifact.id.in_(member_ids_sorted))
        ).all()
        if member_ids_sorted
        else []
    )
    rows_by_id = {artifact.id: (artifact, content) for artifact, content in rows}
    recent_artifacts = [rows_by_id[aid][0] for aid in member_ids_sorted if aid in rows_by_id]
    recent_lines = [
        f"{rows_by_id[aid][0].original_filename}: "
        f"\"{_artifact_snippet(rows_by_id[aid][1], settings.direction_summary_snippet_chars)}\""
        for aid in member_ids_sorted
        if aid in rows_by_id
    ]

    corpus_latest = max(dates_by_artifact.values()) if dates_by_artifact else None
    provider = provider or get_llm_provider()
    model, model_version, summary = provider.model, provider.model_version, None
    rationale = "deterministic fallback (no recent artifacts to summarize)"

    if recent_lines:
        raw = asyncio.run(
            provider.complete(_summary_prompt(recent_lines), system=_SUMMARY_SYSTEM_PROMPT)
        )
        summary = _parse_summary_response(raw)
        rationale = (
            f"synthesized from {len(recent_lines)} recent artifact(s)"
            if summary
            else "LLM response was not usable JSON; degraded to a deterministic fallback summary"
        )

    if summary is None:
        summary = _fallback_summary(recent_artifacts, corpus_latest)

    snapshot = DirectionSnapshot(
        project_id=project_id,
        inferred_direction_summary=summary,
        corpus_fingerprint_hash=fingerprint_hash,
    )
    session.add(snapshot)
    session.flush()  # populate snapshot.id for the audit target

    session.add(
        DecisionAudit(
            decision_type="direction_snapshot",
            target_id=snapshot.id,
            new_value={"summary": summary},
            actor=AuditActor.system,
            model=model,
            model_version=model_version,
            rationale=rationale,
        )
    )
    return snapshot, False


def run_project_direction(
    session: Session, project_id: uuid.UUID, *, provider: LLMProvider | None = None
) -> DirectionResult:
    """Label every artifact's direction, then (re)compute the project-level
    snapshot narrative. Two independent stages sharing one fingerprint
    formula, called in sequence -- mirrors `phases.py`'s `run_project_phases`
    calling domain-classification then phase-assignment."""
    result = label_project_direction(session, project_id)
    session.commit()

    if _doc_embeddings(session, project_id):  # nothing to snapshot before embed has run at all
        snapshot, skipped = build_project_direction_snapshot(
            session, project_id, provider=provider or get_llm_provider()
        )
        session.commit()
        result.snapshot = snapshot
        result.snapshot_skipped = skipped

    return result


def main() -> None:
    """CLI: label every artifact's direction (current/superseded/unclear)
    and (re)compute the project's direction snapshot narrative.

        uv run python -m truth_engine.analysis.direction --project-id <uuid>

    Reads doc-level `Embedding` (step 5), `RelationshipEdge` (step 8), and
    `ResolvedDate` (step 3/extract); run after `embed` and `graph`. Confirmed
    labels (`confirmed_by_user=True`) are never recomputed or overwritten --
    this is a *draft for human review*, not a final verdict.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_project_direction(session, args.project_id)

        print(
            f"direction: {result.labeled} labeled, {result.skipped} skipped, "
            f"{result.confirmed_skipped} confirmed (untouched), {len(result.errors)} errors"
        )
        if result.snapshot is not None:
            tag = "reused" if result.snapshot_skipped else "computed"
            print(f"snapshot ({tag}): {result.snapshot.inferred_direction_summary}")
        for artifact, err in result.errors:
            print(f"  ERROR {artifact.original_filename}: {err}")


if __name__ == "__main__":
    main()
