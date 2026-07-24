"""Small helpers shared across the extract submodules (mirrors
`parse.handlers._common`)."""

from __future__ import annotations

_CONTEXT_RADIUS = 40  # chars of surrounding text captured on each side of a span


def context_window(text: str, start: int, end: int, *, radius: int = _CONTEXT_RADIUS) -> str:
    """A short excerpt around `text[start:end]` for human-inspectable evidence
    (`EntityMention.context` / `ResolvedDate.evidence_text`)."""
    return text[max(0, start - radius) : min(len(text), end + radius)]
