"""Regression: streaming must cascade when a tier yields no tokens, and the
non-streaming fallback must NOT fire when a permanent / rate-limited error
already surfaced (Phase A)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import RateLimitError

from app.openai_client import (
    _SECONDARY_MODEL,
    _is_permanent_openai_error,
    _is_weak_response,
    chat_stream,
)


# ── _is_permanent_openai_error ──────────────────────────────────────────

def _mk_rate_limit(code: str) -> RateLimitError:
    body = {"error": {"message": "quota", "type": code, "code": code}}
    resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    return RateLimitError(message="429", response=resp, body=body)


def test_is_permanent_openai_error_insufficient_quota():
    assert _is_permanent_openai_error(_mk_rate_limit("insufficient_quota")) == (
        True,
        "insufficient_quota",
    )


def test_is_permanent_openai_error_invalid_api_key():
    assert _is_permanent_openai_error(_mk_rate_limit("invalid_api_key"))[0] is True


def test_is_permanent_openai_error_model_not_found():
    assert _is_permanent_openai_error(_mk_rate_limit("model_not_found"))[0] is True


def test_is_permanent_openai_error_transient_429_is_not_permanent():
    body = {"error": {"message": "slow down", "type": "rate_limit_exceeded"}}
    resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    exc = RateLimitError(message="429", response=resp, body=body)
    assert _is_permanent_openai_error(exc) == (False, "")


def test_is_permanent_openai_error_plain_exception_is_not_permanent():
    assert _is_permanent_openai_error(RuntimeError("nope")) == (False, "")


# ── _is_weak_response + strict_escalation flag ─────────────────────────

def test_weak_response_empty_is_always_weak():
    assert _is_weak_response("", "anything", strict_escalation=False) is True
    assert _is_weak_response("", "anything", strict_escalation=True) is True


def test_weak_response_short_json_not_weak_when_non_strict():
    reply = '{"action":"hold","confidence":0.7}'
    user = "long question " * 20  # > 100 chars
    assert _is_weak_response(reply, user, strict_escalation=False) is False


def test_weak_response_short_reply_weak_when_strict_and_long_user_msg():
    reply = "yes."
    user = "long question " * 20
    assert _is_weak_response(reply, user, strict_escalation=True) is True


def test_weak_response_refusal_weak_in_both_modes():
    reply = "I'm sorry, but I can't help with that."
    user = "anything"
    assert _is_weak_response(reply, user, strict_escalation=False) is True
    assert _is_weak_response(reply, user, strict_escalation=True) is True


# ── chat_stream cascade + non-stream fallback gate ──────────────────────

@patch("app.openai_client._openai_official_configured", return_value=False)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._near_daily_limit", return_value=False)
@patch("app.openai_client._stream_provider")
def test_chat_stream_tries_secondary_when_primary_yields_no_tokens(mock_sp, *_):
    calls = []

    def side_effect(*args, **kwargs):
        model = args[2]
        calls.append(model)
        if len(calls) == 1:
            return iter([])
        return iter([("fallback ", _SECONDARY_MODEL), ("ok", _SECONDARY_MODEL)])

    mock_sp.side_effect = side_effect

    out = list(
        chat_stream(
            [{"role": "user", "content": "hi"}],
            system_prompt="sys",
            trace_id="test-stream",
            user_message="hi",
            max_tokens=128,
        )
    )

    assert len(calls) == 2
    assert out == [("fallback ", _SECONDARY_MODEL), ("ok", _SECONDARY_MODEL)]


@patch("app.openai_client._openai_official_configured", return_value=False)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
@patch(
    "app.openai_client._stream_provider",
    side_effect=lambda *a, **k: iter([]),
)
@patch("app.openai_client.chat")
def test_chat_stream_non_streaming_fallback_when_all_streams_empty(mock_chat, *_):
    """All tiers silent → non-stream fallback runs, result yielded."""
    mock_chat.return_value = {"reply": "non-stream body", "model": "gpt-4o-mini"}
    out = list(
        chat_stream(
            [{"role": "user", "content": "hi"}],
            system_prompt="sys",
            trace_id="test-fallback",
            user_message="hi",
            max_tokens=128,
        )
    )
    assert out == [("non-stream body", "gpt-4o-mini")]
    mock_chat.assert_called_once()


@patch("app.openai_client._openai_official_configured", return_value=False)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
@patch("app.openai_client._stream_provider")
@patch("app.openai_client.chat")
def test_chat_stream_skips_nonstream_fallback_on_permanent_error(
    mock_chat, mock_sp, *_
):
    """Permanent error in any tier → non-stream fallback must NOT run."""

    def side_effect(*args, **kwargs):
        raise _mk_rate_limit("insufficient_quota")

    mock_sp.side_effect = side_effect

    out = list(
        chat_stream(
            [{"role": "user", "content": "hi"}],
            system_prompt="sys",
            trace_id="test-perm",
            user_message="hi",
            max_tokens=128,
        )
    )
    assert out == []
    mock_chat.assert_not_called()


@patch("app.openai_client._openai_official_configured", return_value=False)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
@patch("app.openai_client._stream_provider")
@patch("app.openai_client.chat")
def test_chat_stream_skips_nonstream_fallback_on_transient_429(
    mock_chat, mock_sp, *_
):
    """Transient 429 exhausted at a tier → skip non-stream fallback too."""

    def _transient_429():
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
        return RateLimitError(
            message="slow down",
            response=resp,
            body={"error": {"message": "slow down", "type": "rate_limit_exceeded"}},
        )

    mock_sp.side_effect = lambda *a, **k: (_ for _ in ()).throw(_transient_429())

    out = list(
        chat_stream(
            [{"role": "user", "content": "hi"}],
            system_prompt="sys",
            trace_id="test-429",
            user_message="hi",
            max_tokens=128,
        )
    )
    assert out == []
    mock_chat.assert_not_called()
