"""Importing this package registers every built-in format handler.

Each submodule only imports its heavy dependency (pdfplumber, python-docx,
...) lazily inside the handler function, so importing this package — and
therefore `truth_engine.parse` — never requires the `pipeline` extra.
"""

from __future__ import annotations

from truth_engine.parse.handlers import (  # noqa: F401
    docx,
    image,
    pdf,
    pptx,
    spreadsheet,
    text,
)

__all__ = ["docx", "image", "pdf", "pptx", "spreadsheet", "text"]
