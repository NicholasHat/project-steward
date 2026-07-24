"""The one place `import spacy` / `spacy.load` happens — cached so the model
is parsed from disk once per process, not once per artifact."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from truth_engine.config import get_settings

if TYPE_CHECKING:
    from spacy.language import Language


@lru_cache(maxsize=1)
def get_nlp() -> Language:
    import spacy  # lazy: pipeline extra

    model_name = get_settings().spacy_model
    try:
        return spacy.load(model_name)
    except OSError as exc:
        raise RuntimeError(
            f"spaCy model {model_name!r} is not installed. Run: "
            f"uv run python -m spacy download {model_name}"
        ) from exc
