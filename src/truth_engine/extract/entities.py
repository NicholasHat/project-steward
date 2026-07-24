"""Non-date entity extraction (PROJECTSPECS.md §3.2): people, groups/tools,
cost figures, named experiments/hypotheses, and citations.

spaCy's general-purpose NER covers PERSON and ORG (mapped to `person` /
`group`) reasonably well out of the box. It has no label for the other
categories the spec asks for — and worse, its generic labels false-positive
on them (`en_core_web_sm` tags "Hypothesis 2" as ORG). So those categories
are deterministic rule/gazetteer matches, applied *before* spaCy so a
specific rule match wins a span over a generic mislabel, not the reverse.
Still fully rule-based/model-based — no LLM anywhere in this stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from truth_engine.db.models import EntityType
from truth_engine.extract._common import context_window

SPACY_EXTRACTOR = "spacy_ner"
RULE_EXTRACTOR = "rule"

# spaCy label -> (EntityType, fixed confidence). en_core_web_sm doesn't expose
# a per-entity confidence score, so this is a calibrated prior per label, not
# a measured probability — PERSON is generally reliable, ORG (standing in for
# "group/team" per §3.2) noisier, hence the lower value.
_SPACY_LABEL_MAP: dict[str, tuple[EntityType, float]] = {
    "PERSON": (EntityType.person, 0.75),
    "ORG": (EntityType.group, 0.65),
}

# Starting gazetteer for "tools/software mentioned" — extend this list as new
# tools show up in real projects; no code changes needed elsewhere.
TOOL_GAZETTEER: tuple[str, ...] = (
    "python", "r studio", "rstudio", "matlab", "excel", "spss", "origin",
    "labview", "git", "github", "docker", "aws", "tensorflow", "pytorch",
    "graphpad", "prism", "imagej", "jupyter", "sql", "jmp", "minitab", "sas",
    "comsol", "autocad", "solidworks", "matplotlib", "numpy", "pandas",
)  # fmt: skip
_TOOL_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in TOOL_GAZETTEER) + r")\b", re.IGNORECASE
)

_EXPERIMENT_RE = re.compile(r"\b(Experiment|Trial|Run)\s+#?([A-Za-z0-9][\w-]*)", re.IGNORECASE)
_HYPOTHESIS_RE = re.compile(r"\bHypothesis\s+#?([A-Za-z0-9][\w-]*)", re.IGNORECASE)
_COST_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|dollars)\b"
)
_CITATION_RE = re.compile(
    r"\([A-Z][A-Za-z-]+(?:\s+et al\.)?,?\s+\d{4}\)"  # (Smith et al., 2022)
    r"|\b10\.\d{4,9}/[^\s,;)]+"  # DOI
    r"|\[\d+\]"  # [1]
)

# Rules run in this order, before spaCy — see module docstring.
_RULES: tuple[tuple[re.Pattern[str], EntityType, float], ...] = (
    (_TOOL_RE, EntityType.tool, 0.60),
    (_EXPERIMENT_RE, EntityType.experiment, 0.70),
    (_HYPOTHESIS_RE, EntityType.hypothesis, 0.70),
    (_COST_RE, EntityType.cost, 0.70),
    (_CITATION_RE, EntityType.citation, 0.70),
)


@dataclass(frozen=True, slots=True)
class EntityMentionCandidate:
    entity_type: EntityType
    value: str
    normalized_value: str
    span: str  # "start:end" char offsets, matches EntityMention.span
    context: str | None
    confidence: float
    extractor: str


def extract_entities(raw_text: str | None, spacy_doc: object) -> list[EntityMentionCandidate]:
    if not raw_text:
        return []

    mentions: list[EntityMentionCandidate] = []
    taken_spans: list[tuple[int, int]] = []

    for regex, entity_type, confidence in _RULES:
        for match in regex.finditer(raw_text):
            value = match.group(0).rstrip(".")
            start, end = match.start(), match.start() + len(value)
            mentions.append(
                _candidate(raw_text, entity_type, value, start, end, confidence, RULE_EXTRACTOR)
            )
            taken_spans.append((start, end))

    if spacy_doc is not None:
        for ent in spacy_doc.ents:
            mapping = _SPACY_LABEL_MAP.get(ent.label_)
            if mapping is None or _overlaps(ent.start_char, ent.end_char, taken_spans):
                continue
            entity_type, confidence = mapping
            mentions.append(
                _candidate(
                    raw_text,
                    entity_type,
                    ent.text,
                    ent.start_char,
                    ent.end_char,
                    confidence,
                    SPACY_EXTRACTOR,
                )
            )
            taken_spans.append((ent.start_char, ent.end_char))

    return mentions


def _candidate(
    raw_text: str,
    entity_type: EntityType,
    value: str,
    start: int,
    end: int,
    confidence: float,
    extractor: str,
) -> EntityMentionCandidate:
    value = value.strip()
    return EntityMentionCandidate(
        entity_type=entity_type,
        value=value,
        normalized_value=normalize_value(entity_type, value),
        span=f"{start}:{end}",
        context=context_window(raw_text, start, end),
        confidence=confidence,
        extractor=extractor,
    )


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < s_end and s_start < end for s_start, s_end in spans)


def normalize_value(entity_type: EntityType, value: str) -> str:
    """Canonical form used for `Entity.normalized_value` dedup (unique on
    `(type, normalized_value)`)."""
    if entity_type == EntityType.cost:
        return _normalize_cost(value)
    return " ".join(value.split()).strip().lower()


def _normalize_cost(value: str) -> str:
    """"$5k" / "$1,200" / "500 USD" -> a fixed-point decimal string ("5000.00")
    so the same figure mentioned in different notations dedupes to one
    Entity. Currency-agnostic (assumes a single implicit currency, USD for
    the initial R&D vertical) — a genuine multi-currency corpus is out of
    scope for this increment."""
    multiplier = 1.0
    if re.search(r"[kK]\b", value):
        multiplier = 1_000.0
    elif re.search(r"[mM]\b", value):
        multiplier = 1_000_000.0
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return value.strip().lower()
    return f"{float(digits) * multiplier:.2f}"
