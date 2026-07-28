"""The date-resolution subsystem (PROJECTSPECS.md §3.2) — its own module
because it is the load-bearing input to everything downstream (timeline,
phases, drift), not a one-line lookup.

Three signals, each producing zero or more `DateCandidate`s:

  1. content   — dates/relative references found in `raw_text` (spaCy DATE
     entities resolved via `dateparser`, highest trust).
  2. doc_meta  — creation/modified timestamps embedded in the file format
     itself (Office core properties, PDF info dict, EXIF), medium trust.
  3. filesystem — `Artifact.fs_created`/`.fs_modified`, lowest trust,
     cross-check only.

**The chosen-date rule** (`choose`): the highest-*ranked signal* with any
candidate wins; confidence only breaks ties *within* that signal. This is a
deliberate reading of §3.2's "highest-trust signal wins, ties broken
sensibly" — the alternative (a single global max-confidence pick across all
signals) would let a lucky-but-shaky content guess lose to a suspiciously
precise filesystem timestamp, or worse, let filesystem "confidence" (which is
really just a fixed prior, not evidence) outrank a genuine in-text date. Signal
rank encodes *how much we trust the kind of evidence*; confidence encodes
*how sure we are within that kind*. Keeping them as separate axes, applied in
that order, is what keeps the choice inspectable — you can always answer
"why this date" with "it's the best content-derived candidate", never with an
opaque score comparison across unlike things.

**Relative-date anchoring** (open risk #2 in the plan): a relative reference
("last week's run") is meaningless without a fixed point to resolve against.
The anchor is the best *absolute* date already available for this artifact —
computed by running the same `choose` rule over the non-content candidates
(doc-meta beats filesystem, exactly as it would for the final choice, minus
content since that's what we're resolving). Every artifact ingested via
`ingest.service` always has filesystem timestamps, so an anchor is always
available in practice; the `None` case below is a defensive fallback, not an
expected path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from truth_engine.db.models import DateSignalSource
from truth_engine.extract._common import context_window

CONTENT_EXTRACTOR = "spacy+dateparser"
DOC_META_EXTRACTOR = "doc_meta"
FILESYSTEM_EXTRACTOR = "filesystem"

# Confidence is a fixed prior per source/field, not a statistical estimate —
# it encodes how much export/conversion/sync tends to corrupt each kind of
# timestamp (per §3.2), not a per-instance quality score.
_CONTENT_EXPLICIT_CONFIDENCE = 0.85
_CONTENT_RELATIVE_CONFIDENCE = 0.60  # depends on both NER + anchor accuracy
_DOC_META_CREATED_CONFIDENCE = 0.70
_DOC_META_MODIFIED_CONFIDENCE = 0.55
_FS_CREATED_CONFIDENCE = 0.35
_FS_MODIFIED_CONFIDENCE = 0.25

# Defaults mirror Settings.extract_min_year / extract_max_year — the extract
# service always passes the configured values; these keep direct callers (and
# tests) working without wiring config through. See content_candidates.
_DEFAULT_MIN_YEAR = 1970
_DEFAULT_MAX_YEAR = 2100

_SIGNAL_RANK = {
    DateSignalSource.content: 3,
    DateSignalSource.doc_meta: 2,
    DateSignalSource.filesystem: 1,
}

_RELATIVE_MARKERS = re.compile(
    r"\b(last|next|this|past|ago|yesterday|today|tomorrow|recent(ly)?|coming|upcoming)\b",
    re.IGNORECASE,
)

_CREATED_KEY_HINTS = ("created", "creationdate")
_MODIFIED_KEY_HINTS = ("modified", "moddate", "lastmodified")

_PDF_DATE_RE = re.compile(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class DateCandidate:
    candidate_date: datetime  # always tz-aware, UTC
    signal_source: DateSignalSource
    confidence: float
    evidence_text: str | None
    extractor: str
    anchor_date: datetime | None = None  # set only for anchored relative references


# --------------------------------------------------------------------------- #
# Signal 3 — filesystem (lowest trust, cross-check only)                      #
# --------------------------------------------------------------------------- #
def filesystem_candidates(
    fs_created: datetime | None, fs_modified: datetime | None
) -> list[DateCandidate]:
    candidates = []
    if fs_created is not None:
        candidates.append(
            DateCandidate(
                candidate_date=fs_created,
                signal_source=DateSignalSource.filesystem,
                confidence=_FS_CREATED_CONFIDENCE,
                evidence_text="Artifact.fs_created",
                extractor=FILESYSTEM_EXTRACTOR,
            )
        )
    if fs_modified is not None:
        candidates.append(
            DateCandidate(
                candidate_date=fs_modified,
                signal_source=DateSignalSource.filesystem,
                confidence=_FS_MODIFIED_CONFIDENCE,
                evidence_text="Artifact.fs_modified",
                extractor=FILESYSTEM_EXTRACTOR,
            )
        )
    return candidates


# --------------------------------------------------------------------------- #
# Signal 2 — document-internal metadata (medium trust)                       #
# --------------------------------------------------------------------------- #
def doc_meta_candidates(embedded_metadata: dict | None) -> list[DateCandidate]:
    """Scan `ArtifactContent.embedded_metadata` for created/modified
    timestamps. Field names vary by parser (docx/pptx core properties, PDF
    info dict, EXIF tags nested under "exif"), so this matches by key-name
    hint rather than hardcoding one schema per format — new parsers need no
    changes here as long as their date fields say "created"/"modified"."""
    if not embedded_metadata:
        return []
    candidates: list[DateCandidate] = []
    _scan_metadata_dict(embedded_metadata, candidates)
    exif = embedded_metadata.get("exif")
    if isinstance(exif, dict):
        _scan_metadata_dict(exif, candidates, key_hint_override=_CREATED_KEY_HINTS)
    return candidates


def _scan_metadata_dict(
    metadata: dict,
    candidates: list[DateCandidate],
    *,
    key_hint_override: tuple[str, ...] | None = None,
) -> None:
    for key, value in metadata.items():
        if not isinstance(value, str):
            continue
        key_lower = key.lower()
        if key_hint_override is not None:
            # EXIF tags (e.g. "EXIF DateTimeOriginal", "Image DateTime") don't
            # cleanly split into created/modified — treat any datetime tag as
            # a creation-equivalent signal.
            if "datetime" not in key_lower and "date" not in key_lower:
                continue
            confidence = _DOC_META_CREATED_CONFIDENCE
        elif any(hint in key_lower for hint in _CREATED_KEY_HINTS):
            confidence = _DOC_META_CREATED_CONFIDENCE
        elif any(hint in key_lower for hint in _MODIFIED_KEY_HINTS):
            confidence = _DOC_META_MODIFIED_CONFIDENCE
        else:
            continue

        parsed = _parse_metadata_datetime(value)
        if parsed is None:
            continue
        candidates.append(
            DateCandidate(
                candidate_date=parsed,
                signal_source=DateSignalSource.doc_meta,
                confidence=confidence,
                evidence_text=f"{key}={value}",
                extractor=DOC_META_EXTRACTOR,
            )
        )


def _parse_metadata_datetime(value: str) -> datetime | None:
    """Parse a doc-metadata timestamp without depending on the host's local
    timezone (dateparser's fallback for tz-less strings is "assume local
    machine time", which would make this non-deterministic across
    environments) — handle the known formats explicitly and fall back to
    strict ISO-8601 parsing, treating a missing offset as UTC."""
    value = value.strip()

    pdf_match = _PDF_DATE_RE.match(value)
    if pdf_match:
        return _parse_pdf_date(pdf_match)

    try:
        return datetime.strptime(value, _EXIF_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_pdf_date(match: re.Match[str]) -> datetime | None:
    # PDF date strings ("D:20230312101500-05'00'") may omit any trailing
    # component; default month/day to 1 and time fields to 0. The timezone
    # suffix is intentionally not parsed — this is a medium-trust signal
    # already, and the date/time components are what matter for resolution.
    year, month, day, hour, minute, second = (
        int(g) if g else default
        for g, default in zip(match.groups(), (None, 1, 1, 0, 0, 0), strict=True)
    )
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Signal 1 — content-embedded dates (highest trust)                           #
# --------------------------------------------------------------------------- #
def content_candidates(
    raw_text: str | None,
    spacy_doc: object,
    anchor: datetime | None,
    *,
    min_year: int = _DEFAULT_MIN_YEAR,
    max_year: int = _DEFAULT_MAX_YEAR,
) -> list[DateCandidate]:
    """Resolve every spaCy DATE entity in `raw_text` via `dateparser`,
    anchoring relative references ("last week") against `anchor`. spaCy finds
    *where* a date-like phrase is; dateparser decides *what date it means*.

    Content dates whose year falls outside `[min_year, max_year]` are dropped
    as parsing artifacts: spaCy tags any bare 4-digit token as a DATE, so a
    product/model number ("Nvidia 5090"), an address, or a large quantity
    would otherwise become a max-trust content date — and, as the corpus's
    latest date, poison every recency-window comparison downstream. Only this
    highest-trust *content* signal is clamped; doc-meta/filesystem timestamps
    are real dates by construction.

    Two spaCy quirks get cleaned up before handing the span to dateparser
    (both confirmed empirically against `en_core_web_sm`, not theoretical):
    it sometimes splits one date across adjacent entities ("March 12" +
    "2024" for "March 12, 2024"), and it includes a leading possessive/article
    ("our March 12") that makes `dateparser.parse` fail outright on the whole
    span. `_merged_date_spans` rejoins adjacent DATE entities; leading noise
    words are stripped before parsing.
    """
    if not raw_text or spacy_doc is None:
        return []
    import dateparser  # lazy: pipeline extra

    anchor_naive = anchor.replace(tzinfo=None) if anchor is not None else None
    candidates: list[DateCandidate] = []
    for span_text, start, end in _merged_date_spans(spacy_doc, raw_text):
        cleaned = _LEADING_NOISE_RE.sub("", span_text).strip()
        if not cleaned:
            continue
        evidence = context_window(raw_text, start, end)

        year_match = _BARE_YEAR_RE.match(cleaned)
        if year_match:
            year = int(cleaned)
            if not (min_year <= year <= max_year):
                continue  # a model/part number or quantity, not a year
            # A bare year ("2022") has no day/month — filling those from the
            # anchor would fabricate precision that was never in the text.
            candidates.append(
                DateCandidate(
                    candidate_date=datetime(year, 1, 1, tzinfo=UTC),
                    signal_source=DateSignalSource.content,
                    confidence=_CONTENT_YEAR_ONLY_CONFIDENCE,
                    evidence_text=evidence,
                    extractor=CONTENT_EXTRACTOR,
                )
            )
            continue

        is_relative = bool(_RELATIVE_MARKERS.search(cleaned))
        settings = {"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False}
        if anchor_naive is not None:
            settings["RELATIVE_BASE"] = anchor_naive
        parsed = dateparser.parse(cleaned, settings=settings, languages=["en"])
        if parsed is None:
            continue
        parsed_utc = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        if not (min_year <= parsed_utc.year <= max_year):
            continue  # implausible year — a parsing artifact, not a real date

        candidates.append(
            DateCandidate(
                candidate_date=parsed_utc,
                signal_source=DateSignalSource.content,
                confidence=_CONTENT_RELATIVE_CONFIDENCE
                if is_relative
                else _CONTENT_EXPLICIT_CONFIDENCE,
                evidence_text=evidence,
                extractor=CONTENT_EXTRACTOR,
                anchor_date=anchor if (is_relative and anchor is not None) else None,
            )
        )
    return candidates


_ADJACENCY_GAP_RE = re.compile(r"^[,\s]{0,3}$")
_LEADING_NOISE_RE = re.compile(
    r"^(our|my|your|his|her|their|the|this|that|a|an)\s+", re.IGNORECASE
)
_BARE_YEAR_RE = re.compile(r"^\d{4}$")
_CONTENT_YEAR_ONLY_CONFIDENCE = 0.45  # lower than even a relative reference: no day/month at all


def _merged_date_spans(spacy_doc: object, raw_text: str) -> list[tuple[str, int, int]]:
    """Adjacent spaCy DATE entities (separated only by whitespace/comma) are
    joined into one span before parsing — see `content_candidates` docstring."""
    date_ents = [ent for ent in spacy_doc.ents if ent.label_ == "DATE"]
    spans: list[list[int]] = []
    for ent in date_ents:
        if spans and _ADJACENCY_GAP_RE.match(raw_text[spans[-1][1] : ent.start_char]):
            spans[-1][1] = ent.end_char
        else:
            spans.append([ent.start_char, ent.end_char])
    return [(raw_text[s:e].strip(), s, e) for s, e in spans]


# --------------------------------------------------------------------------- #
# Selection                                                                    #
# --------------------------------------------------------------------------- #
def best_anchor(non_content_candidates: list[DateCandidate]) -> datetime | None:
    """Best available *absolute* date to resolve relative content references
    against — the same precedence rule used for the final choice, applied to
    everything except `content` (which is what we're trying to resolve)."""
    chosen = choose(non_content_candidates)
    return chosen.candidate_date if chosen is not None else None


def choose(candidates: list[DateCandidate]) -> DateCandidate | None:
    """The chosen-date rule: highest signal rank wins; confidence breaks ties
    within that rank. See module docstring for why this is two ordered axes
    rather than a single flattened score."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: (_SIGNAL_RANK[c.signal_source], c.confidence))
