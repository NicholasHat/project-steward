"""Step 2 — Parse (deterministic, per-format).

Dispatch each artifact to a format-specific extractor (see `parse.registry`)
and produce a normalized `ParsedDocument`: raw text, document structure
(headings/slide titles/sheet names), embedded metadata (EXIF, Office/PDF
props), and structured tables kept as structured data (not flattened into
prose).

Extractors: pdfplumber, python-docx, openpyxl/pandas, python-pptx,
Pillow+exifread, plus a plain-text/markdown reader — see `parse.handlers`.

Failure modes designed against: encrypted/password files and malformed
Office files (raised, caught, and recorded per-artifact by `parse.service`
without aborting the batch), scanned PDFs (flagged via
`structure.likely_scanned` rather than silently returning empty text),
encoding issues (replaced, not raised).
"""

from __future__ import annotations

from truth_engine.parse.service import ParseResult, parse_artifact, parse_project

__all__ = ["ParseResult", "parse_artifact", "parse_project"]
