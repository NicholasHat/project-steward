"""Defensive JSON extraction from LLM completions.

`LLMProvider.complete()` returns a raw string — there's no structured-output
API (see `providers.py`). A local small model routinely wraps valid JSON in
markdown code fences, prepends a sentence of preamble ("Sure, here's the
classification:"), or occasionally emits something that isn't JSON at all.
Every LLM-judgment stage (domain classification and phase assignment today;
steps 9-12 as they're built) needs to parse a completion the same defensive
way, so the logic lives here once rather than being reinvented per stage.

Never raises: any malformed input yields `None`, and callers are expected to
treat that as "the model's output couldn't be trusted" and degrade
gracefully (fall back to a safe default, skip, mark low-confidence) rather
than crash the stage — a local model returning imperfect output is an
expected condition here, not an exceptional one.
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str | None) -> object | None:
    """Best-effort parse of a JSON value (object or array) out of `text`.

    Tries, in order: the whole string as-is; the contents of the first
    fenced code block; the first balanced `{...}` or `[...]` span in the
    text. Returns `None` — never raises — if none of these parse.
    """
    if not text:
        return None

    for candidate in _candidates(text):
        try:
            value: object = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        return value
    return None


def _candidates(text: str) -> list[str]:
    candidates = [text.strip()]

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    span = _first_balanced_span(text)
    if span:
        candidates.append(span)

    return candidates


def _first_balanced_span(text: str) -> str | None:
    """The first balanced `{...}` or `[...]` substring — whichever bracket
    type opens first — tolerating any preamble/trailing prose the model
    wrapped the actual JSON value in. String contents (including escaped
    quotes/braces) are skipped so a brace inside a quoted rationale doesn't
    throw off the depth count."""
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
