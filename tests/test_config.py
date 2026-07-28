"""Production-safety validation on Settings (config._enforce_production_safety)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from truth_engine.config import DEFAULT_SECRET_KEY, Settings


def test_production_rejects_default_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", secret_key=DEFAULT_SECRET_KEY)


def test_production_accepts_a_real_secret() -> None:
    s = Settings(environment="production", secret_key="a-real-generated-secret")
    assert s.environment == "production"


def test_development_boots_with_the_placeholder_secret() -> None:
    # The out-of-the-box default: dev must not fail on the shipped placeholder.
    s = Settings(environment="development", secret_key=DEFAULT_SECRET_KEY)
    assert s.secret_key == DEFAULT_SECRET_KEY


def test_production_anthropic_requires_a_key() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            secret_key="a-real-secret",
            llm_provider="anthropic",
            anthropic_api_key=None,
        )
