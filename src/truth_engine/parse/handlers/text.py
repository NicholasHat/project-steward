"""Plain text / markdown — the trivial case, no library needed."""

from __future__ import annotations

from pathlib import Path

from truth_engine.parse.registry import register
from truth_engine.parse.types import ParsedDocument

PARSER_NAME = "text"
PARSER_VERSION = "1"


@register("txt", "md", "markdown")
def parse_text(path: Path) -> ParsedDocument:
    # errors="replace" rather than raising: encoding issues shouldn't sink an
    # otherwise-readable file, just degrade the offending characters.
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    return ParsedDocument(
        raw_text=raw_text or None,
        structure=None,
        embedded_metadata=None,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
