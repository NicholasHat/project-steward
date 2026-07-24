"""PDF via pdfplumber (text layer + embedded tables).

Scanned (image-only) PDFs are detected — `structure.likely_scanned` — but OCR
fallback is deliberately out of scope for this increment; see module docstring
deferrals.
"""

from __future__ import annotations

from pathlib import Path

from truth_engine.parse.handlers._common import header_from_row, json_safe
from truth_engine.parse.registry import register
from truth_engine.parse.types import ParsedDocument, ParsedTable

PARSER_NAME = "pdfplumber"
PARSER_VERSION = "1"


@register("pdf")
def parse_pdf(path: Path) -> ParsedDocument:
    import pdfplumber  # lazy: pipeline extra

    page_texts: list[str] = []
    tables: list[ParsedTable] = []
    doc_meta: dict[str, str] = {}

    with pdfplumber.open(path) as pdf:
        doc_meta = {str(k): str(v) for k, v in (pdf.metadata or {}).items() if v}
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                page_texts.append(text)
            for table_num, raw_table in enumerate(page.extract_tables(), start=1):
                if not raw_table:
                    continue
                header_row, *rows = raw_table
                header = header_from_row(tuple(header_row))
                tables.append(
                    ParsedTable(
                        source=f"page_{page_num}_table_{table_num}",
                        table_schema=header,
                        rows=[
                            {h: json_safe(v) for h, v in zip(header, row, strict=False)}
                            for row in rows
                        ],
                    )
                )

    raw_text = "\n\n".join(page_texts) if page_texts else None
    structure = {"page_count": page_count, "likely_scanned": raw_text is None}
    return ParsedDocument(
        raw_text=raw_text,
        structure=structure,
        embedded_metadata=doc_meta or None,
        tables=tables,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
