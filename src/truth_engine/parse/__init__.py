"""Step 2 — Parse (deterministic, per-format).

Dispatch each artifact to a format-specific extractor and produce a normalized
ParsedDocument: raw text, document structure (headings/slide titles/sheet
names), embedded metadata (EXIF, Office/PDF props), and structured tables kept
as structured data (not flattened into prose).

Extractors: pdfplumber (+ ocrmypdf/tesseract fallback for scanned PDFs),
python-docx, openpyxl/pandas, python-pptx, Pillow+exifread.

Failure modes: scanned-vs-text PDFs, encrypted/password files, malformed Office
files, tables misread as prose, encoding issues.

Pipeline logic is intentionally not implemented yet — this is scaffolding.
"""
