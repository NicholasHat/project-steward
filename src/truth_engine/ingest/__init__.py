"""Step 1 — Ingest.

Discover files in a project folder, assign each a stable internal ID
(independent of filename/path), compute a content hash, and dedupe.

Failure modes to design against: duplicate/near-duplicate files, unreadable
or corrupt files, filename != identity, and re-ingest of moved files (match
on content hash, not path).

Pipeline logic is intentionally not implemented yet — this is scaffolding.
"""
