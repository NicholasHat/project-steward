"""Streaming content hashing — the basis of artifact identity.

Identity is content_hash + UUID, never filename/path, so hashing is the one
piece of ingest logic every other invariant (dedupe, moved-file matching)
depends on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024  # 1 MiB, keeps memory flat regardless of file size


def hash_file(path: Path, *, algorithm: str = "sha256") -> str:
    """Hex digest of a file's contents, read in fixed-size chunks."""
    digest = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
