"""Step 3 — Extract entities & dates (multi-signal, confidence-scored).

Date resolution is its own subsystem: for each artifact, gather candidate dates
from (1) content-embedded dates/references [highest trust], (2) document-internal
metadata timestamps [medium], (3) filesystem timestamps [lowest, cross-check
only]. Each candidate carries a confidence score and a source label; one is
chosen. Also extract non-date entities: people, groups, tools, cost figures,
named experiments/hypotheses, referenced external sources.

Tools: spaCy NER + dateparser + rules.

Failure modes: relative dates ("last week") needing an anchor, timezone/format
ambiguity, NER false positives, doc-meta timestamps corrupted by export.

Pipeline logic is intentionally not implemented yet — this is scaffolding.
"""
