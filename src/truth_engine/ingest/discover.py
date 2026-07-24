"""Recursive file discovery for a project folder.

Skips hidden entries (dotfiles/dotdirs, e.g. `.git`, `.DS_Store`) and other
common non-artifact noise — these are never project content.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_SKIP_DIR_NAMES = {"__pycache__", "node_modules"}


def discover_files(root: Path) -> Iterator[Path]:
    """Yield every regular file under `root`, deepest-first-safe, in a stable
    (sorted) order so repeated runs over an unchanged folder behave identically.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") or part in _SKIP_DIR_NAMES for part in rel_parts):
            continue
        yield path
