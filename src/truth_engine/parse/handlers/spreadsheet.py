"""XLSX (openpyxl) and CSV (pandas) — kept as structured tables, never
flattened into prose. No `raw_text`: a spreadsheet's content lives in
`StructuredTable`, and there's no lossless single-string form worth storing.
"""

from __future__ import annotations

from pathlib import Path

from truth_engine.parse.handlers._common import header_from_row, json_safe
from truth_engine.parse.registry import register
from truth_engine.parse.types import ParsedDocument, ParsedTable


@register("xlsx", "xlsm")
def parse_xlsx(path: Path) -> ParsedDocument:
    import openpyxl  # lazy: pipeline extra

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    tables: list[ParsedTable] = []
    for sheet_name in workbook.sheetnames:
        rows_iter = workbook[sheet_name].iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if header_row is None:
            continue
        header = header_from_row(header_row)
        rows = [
            {h: json_safe(v) for h, v in zip(header, row, strict=False)}
            for row in rows_iter
            if row is not None and any(v is not None for v in row)
        ]
        tables.append(ParsedTable(source=sheet_name, table_schema=header, rows=rows))
    workbook.close()

    return ParsedDocument(
        raw_text=None,
        structure={"sheet_names": workbook.sheetnames},
        embedded_metadata=None,
        tables=tables,
        parser_name="openpyxl",
        parser_version="1",
    )


@register("csv")
def parse_csv(path: Path) -> ParsedDocument:
    import pandas as pd  # lazy: pipeline extra

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    header = [str(c) for c in df.columns]
    table = ParsedTable(source=path.stem, table_schema=header, rows=df.to_dict(orient="records"))

    return ParsedDocument(
        raw_text=None,
        structure={"sheet_names": [path.stem]},
        embedded_metadata=None,
        tables=[table],
        parser_name="pandas",
        parser_version="1",
    )
