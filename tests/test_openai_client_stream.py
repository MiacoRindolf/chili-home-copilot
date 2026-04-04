"""Regression: streaming must cascade when a tier yields no tokens (matches non-stream chat())."""
from unittest.mock import patch

from app.openai_client import _SECONDARY_MODEL, chat_stream


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
