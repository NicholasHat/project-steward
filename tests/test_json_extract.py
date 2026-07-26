from __future__ import annotations

from truth_engine.reasoning.json_extract import extract_json


def test_extract_json_parses_clean_object() -> None:
    assert extract_json('{"domain": "research", "confidence": 0.8}') == {
        "domain": "research",
        "confidence": 0.8,
    }


def test_extract_json_strips_markdown_code_fence() -> None:
    text = '```json\n{"domain": "research", "confidence": 0.8}\n```'
    assert extract_json(text) == {"domain": "research", "confidence": 0.8}


def test_extract_json_strips_fence_without_language_tag() -> None:
    text = '```\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}


def test_extract_json_ignores_preamble_and_trailing_prose() -> None:
    text = (
        'Sure, here is the classification:\n{"domain": "research", "confidence": 0.8}\n'
        "Hope that helps!"
    )
    assert extract_json(text) == {"domain": "research", "confidence": 0.8}


def test_extract_json_handles_nested_braces_and_string_content() -> None:
    text = '{"rationale": "mentions {curly} braces and a \\"quote\\"", "n": 1}'
    assert extract_json(text) == {"rationale": 'mentions {curly} braces and a "quote"', "n": 1}


def test_extract_json_parses_array() -> None:
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_returns_none_for_garbage() -> None:
    assert extract_json("I cannot help with that request.") is None


def test_extract_json_returns_none_for_empty_or_none() -> None:
    assert extract_json("") is None
    assert extract_json(None) is None


def test_extract_json_returns_none_for_unbalanced_braces() -> None:
    assert extract_json('{"domain": "research"') is None
