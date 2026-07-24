"""Step 1 — Ingest.

Discover files in a project folder, assign each a stable internal ID
(independent of filename/path), compute a content hash, and dedupe.

Failure modes designed against: duplicate/near-duplicate files (deduped by
content hash within a project), unreadable or corrupt files (recorded as
per-file errors, batch continues), filename != identity, and re-ingest of
moved files (matched on content hash, not path).
"""

from __future__ import annotations

from truth_engine.ingest.service import IngestResult, ingest_folder

__all__ = ["IngestResult", "ingest_folder"]
