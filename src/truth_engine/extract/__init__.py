"""Step 3 — Extract entities & dates (multi-signal, confidence-scored).

Date resolution is its own subsystem (`extract.dates`): for each artifact,
gather candidate dates from (1) content-embedded dates/references [highest
trust], (2) document-internal metadata timestamps [medium], (3) filesystem
timestamps [lowest, cross-check only]. Each candidate carries a confidence
score and a source label; exactly one is chosen per artifact via a signal-
precedence rule (see `dates.choose`). Also extracts non-date entities
(`extract.entities`): people, groups, tools, cost figures, named
experiments/hypotheses, referenced external sources.

Tools: spaCy NER + dateparser + rules. Deterministic — no LLM, no network.

Failure modes designed against: relative dates ("last week") needing an
anchor (resolved against the best available absolute date, `dates.best_anchor`),
timezone/format ambiguity in doc-metadata timestamps (parsed without relying
on host-local timezone, `dates._parse_metadata_datetime`), NER false
positives on non-date categories (rule/gazetteer matches take precedence over
generic spaCy labels, see `entities` module docstring), doc-meta timestamps
corrupted by export/conversion (reflected as a lower confidence, never
treated as ground truth).
"""

from __future__ import annotations

from truth_engine.extract.service import ExtractResult, extract_artifact, extract_project

__all__ = ["ExtractResult", "extract_artifact", "extract_project"]
