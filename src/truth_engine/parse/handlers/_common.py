"""Small helpers shared across format handlers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from truth_engine.parse.types import ParsedTable


def json_safe(value: Any) -> Any:
    """Coerce a cell value into something JSONB can store — JSONB serializes
    via `json.dumps`, which doesn't know `datetime`/`Decimal`."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def tables_as_text(
    title: str,
    tables: Sequence[ParsedTable],
    *,
    max_rows_per_table: int,
    max_chars: int,
) -> str | None:
    """Synthesize a compact textual projection of a spreadsheet's tables for
    the analysis stages (embed/NER/date/phase/direction), which all key off
    `raw_text`. The `StructuredTable` rows stay the lossless record; this is a
    lossy, analysis-only view — filename, then per sheet its name, column
    headers, and a bounded sample of rows (`col | col | col`). Row order is the
    table's own; cell order follows `table_schema`. Returns `None` when there's
    nothing but the title (an empty workbook), so the caller can treat it as a
    zero-text artifact rather than embedding a bare filename."""
    lines: list[str] = [title]
    for table in tables:
        lines.append("")
        lines.append(f"Sheet: {table.source}")
        if table.table_schema:
            lines.append("Columns: " + ", ".join(table.table_schema))
        emitted = 0
        for row in table.rows:
            if emitted >= max_rows_per_table:
                break
            cells = ["" if v is None else str(v) for v in row.values()]
            if any(cell.strip() for cell in cells):
                lines.append(" | ".join(cells))
                emitted += 1
    text = "\n".join(lines).strip()
    if text == title.strip():
        return None
    return text[:max_chars]


def header_from_row(row: tuple[Any, ...], *, prefix: str = "col") -> list[str]:
    """Build a non-empty, de-duplicated column-name list from a raw header row
    (blank cells and repeated names are common in real spreadsheets)."""
    seen: dict[str, int] = {}
    header: list[str] = []
    for i, cell in enumerate(row):
        name = str(cell).strip() if cell not in (None, "") else f"{prefix}_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        header.append(name)
    return header
