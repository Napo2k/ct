"""LLM layer tests — JSON parsing, retry classification, retry loop (no API key)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.llm import (
    LLMError,
    _call_with_retries,
    _extract_json_object,
    _is_retryable,
    _parse_json_response,
)


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


def test_parse_raw_json():
    parsed = _parse_json_response('{"action": "HOLD", "pair": "EURUSD"}')
    assert parsed["action"] == "HOLD"


def test_parse_fenced_json():
    raw = "```json\n{\"action\": \"ENTER\", \"pair\": \"GBPUSD\"}\n```"
    parsed = _parse_json_response(raw)
    assert parsed["pair"] == "GBPUSD"


def test_parse_json_embedded_in_prose():
    raw = 'Here is my decision:\n{"action": "EXIT", "pair": "USDJPY"}\nThanks!'
    parsed = _parse_json_response(raw)
    assert parsed["action"] == "EXIT"


def test_parse_invalid_json_raises():
    with pytest.raises(LLMError):
        _parse_json_response("not json at all")


def test_parse_empty_raises():
    with pytest.raises(LLMError):
        _parse_json_response("   ")


def test_extract_json_object_ignores_braces_in_strings():
    text = 'prefix {"reasoning": "use {curly} braces", "n": 1} suffix'
    extracted = _extract_json_object(text)
    assert extracted == '{"reasoning": "use {curly} braces", "n": 1}'


def test_extract_json_object_none_when_absent():
    assert _extract_json_object("no object here") is None


def test_is_retryable_by_status_code():
    exc = Exception()
    exc.status_code = 429
    assert _is_retryable(exc) is True


def test_is_retryable_by_class_name():
    class RateLimitError(Exception):
        pass

    assert _is_retryable(RateLimitError()) is True


def test_is_not_retryable_for_generic_error():
    assert _is_retryable(ValueError("bad input")) is False


def test_call_with_retries_succeeds_after_transient_failures():
    attempts = {"count": 0}

    def create_message():
        attempts["count"] += 1
        if attempts["count"] < 3:
            exc = Exception("overloaded")
            exc.status_code = 529
            raise exc
        return _FakeMessage('{"action": "HOLD", "pair": "EURUSD"}')

    result = asyncio.run(
        _call_with_retries(create_message, max_retries=3, retry_base_delay=0)
    )
    assert result["action"] == "HOLD"
    assert attempts["count"] == 3


def test_call_with_retries_gives_up_after_max_retries():
    attempts = {"count": 0}

    def create_message():
        attempts["count"] += 1
        exc = Exception("rate limited")
        exc.status_code = 429
        raise exc

    with pytest.raises(LLMError):
        asyncio.run(_call_with_retries(create_message, max_retries=2, retry_base_delay=0))
    assert attempts["count"] == 2


def test_call_with_retries_does_not_retry_fatal_error():
    attempts = {"count": 0}

    def create_message():
        attempts["count"] += 1
        raise ValueError("bad request")

    with pytest.raises(LLMError):
        asyncio.run(_call_with_retries(create_message, max_retries=3, retry_base_delay=0))
    assert attempts["count"] == 1


def test_call_with_retries_reprompts_on_invalid_json():
    attempts = {"count": 0}

    def create_message():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return _FakeMessage("sorry, no JSON this time")
        return _FakeMessage('{"action": "HOLD", "pair": "EURUSD"}')

    result = asyncio.run(
        _call_with_retries(create_message, max_retries=3, retry_base_delay=0)
    )
    assert result["action"] == "HOLD"
    assert attempts["count"] == 2
