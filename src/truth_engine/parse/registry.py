"""Format-handler registry — the dispatch abstraction for step 2.

A handler is just `Callable[[Path], ParsedDocument]`. Adding a new format
means writing one function in `parse.handlers` and decorating it with
`@register(...)`; nothing else in the pipeline changes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from truth_engine.parse.types import ParsedDocument

Handler = Callable[[Path], ParsedDocument]

_REGISTRY: dict[str, Handler] = {}


class UnsupportedFormatError(Exception):
    """No registered handler for this file type."""


def register(*extensions: str) -> Callable[[Handler], Handler]:
    """Decorator: register a handler function for one or more extensions
    (without the leading dot, case-insensitive)."""

    def decorator(fn: Handler) -> Handler:
        for ext in extensions:
            _REGISTRY[ext.lower()] = fn
        return fn

    return decorator


def get_handler(file_type: str) -> Handler:
    try:
        return _REGISTRY[file_type.lower()]
    except KeyError:
        raise UnsupportedFormatError(file_type) from None


def supported_extensions() -> frozenset[str]:
    return frozenset(_REGISTRY)
