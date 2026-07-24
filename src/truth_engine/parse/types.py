"""The normalized output every format handler produces.

Every handler, regardless of format, dispatches to one of these — this is
what makes "add a new format" an isolated addition instead of a change to the
orchestration logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """One spreadsheet sheet / document table, kept structured — never
    flattened into prose."""

    source: str  # sheet/tab/table name
    table_schema: list[str] | None
    rows: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    raw_text: str | None
    structure: dict[str, Any] | None  # headings / slide titles / sheet names
    embedded_metadata: dict[str, Any] | None  # EXIF / Office / PDF properties
    tables: list[ParsedTable] = field(default_factory=list)
    parser_name: str = ""
    parser_version: str = ""
