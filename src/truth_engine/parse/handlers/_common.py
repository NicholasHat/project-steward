"""Small helpers shared across format handlers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_safe(value: Any) -> Any:
    """Coerce a cell value into something JSONB can store — JSONB serializes
    via `json.dumps`, which doesn't know `datetime`/`Decimal`."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


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
