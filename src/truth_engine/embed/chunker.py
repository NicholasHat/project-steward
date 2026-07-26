"""Deterministic, dependency-free word-window chunking (`chunk_words`).

This module only ever sees `ArtifactContent.raw_text` — it never touches
`StructuredTable`. That's what keeps chunking from shredding structured
tables (PROJECTSPECS.md open risk #6) without any format-specific
special-casing here: Parse already leaves `raw_text=None` for the formats
where a table *is* the content (xlsx/csv — see `parse.handlers.spreadsheet`)
and for scanned PDFs; and where prose and tables coexist in one artifact
(docx, pptx), each handler's `raw_text` is built only from body
paragraphs / slide text, never from table cells (see `parse.handlers.docx`,
`parse.handlers.pptx`). A spreadsheet's rows stay queryable in
`structured_tables`, exactly as before.
"""

from __future__ import annotations


def chunk_words(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """Split `text` into overlapping windows of ~`chunk_size` whitespace-
    delimited words, stepping by `chunk_size - overlap` (clamped to at least
    1 word so a misconfigured overlap can't loop forever).

    Returns `[]` for empty/whitespace-only text — callers treat that as "no
    chunks for this artifact" (e.g. a scanned PDF or an image with no EXIF
    text) rather than an error.
    """
    words = text.split()
    if not words:
        return []

    step = max(1, chunk_size - overlap)
    chunks: list[str] = []
    start = 0
    while True:
        window = words[start : start + chunk_size]
        chunks.append(" ".join(window))
        if start + chunk_size >= len(words):
            break
        start += step
    return chunks
