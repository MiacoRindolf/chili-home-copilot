"""Phase B tests: content-hash LRU+TTL cache + free-tier-first cascade reorder."""
import time
from unittest.mock import patch, MagicMock

import httpx
import pytest
from openai import RateLimitError

from app.services import llm_caller
from app import openai_client as oc


@pytest.fixture(autouse=True)
def _reset_cache_and_order_flag():
    llm_caller.reset_cache()
    oc._ORDER_LOGGED = False
    yield
    llm_caller.reset_cache()


# ── cache ───────────────────────────────────────────────────────────────

@patch("app.services.llm_caller._cache_config", return_value=(256, 600))
def test_call_llm_cache_hit_skips_second_provider_call(_cfg):
    mock_chat = MagicMock(return_value={"reply": "cached-reply", "model": "m1"})
    with patch("app.openai_client.chat", mock_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        msgs = [{"role": "user", "content": "deterministic"}]

        r1 = llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)
        r2 = llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)

    assert r1 == r2 == "cached-reply"
    assert mock_chat.call_count == 1
    stats = llm_caller.get_cache_stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert 0.0 < stats["hit_rate"] <= 1.0


@patch("app.services.llm_caller._cache_config", return_value=(256, 600))
def test_call_llm_different_max_tokens_different_cache_keys(_cfg):
    mock_chat = MagicMock(return_value={"reply": "ok", "model": "m1"})
    with patch("app.openai_client.chat", mock_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        msgs = [{"role": "user", "content": "x"}]
        llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)
        llm_caller.call_llm(messages=msgs, max_tokens=200, cacheable=True)

    assert mock_chat.call_count == 2
    stats = llm_caller.get_cache_stats()
    assert stats["misses"] == 2
    assert stats["hits"] == 0


@patch("app.services.llm_caller._cache_config", return_value=(256, 600))
def test_call_llm_cacheable_false_never_caches(_cfg):
    mock_chat = MagicMock(return_value={"reply": "ok", "model": "m1"})
    with patch("app.openai_client.chat", mock_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        msgs = [{"role": "user", "content": "x"}]
        llm_caller.call_llm(messages=msgs, max_tokens=100)
        llm_caller.call_llm(messages=msgs, max_tokens=100)

    assert mock_chat.call_count == 2
    stats = llm_caller.get_cache_stats()
    assert stats["hits"] == 0 and stats["misses"] == 0


def test_call_llm_cache_ttl_expiry_causes_miss(monkeypatch):
    monkeypatch.setattr(llm_caller, "_cache_config", lambda: (256, 600))
    mock_chat = MagicMock(return_value={"reply": "ok", "model": "m1"})
    with patch("app.openai_client.chat", mock_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        msgs = [{"role": "user", "content": "x"}]
        llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)

        base = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: base + 10_000)

        llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)

    assert mock_chat.call_count == 2
    stats = llm_caller.get_cache_stats()
    assert stats["evictions"] >= 1


@patch("app.services.llm_caller._cache_config", return_value=(0, 0))
def test_call_llm_cache_disabled_when_max_entries_zero(_cfg):
    mock_chat = MagicMock(return_value={"reply": "ok", "model": "m1"})
    with patch("app.openai_client.chat", mock_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        msgs = [{"role": "user", "content": "x"}]
        llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)
        llm_caller.call_llm(messages=msgs, max_tokens=100, cacheable=True)

    assert mock_chat.call_count == 2


# ── free-tier-first reorder ────────────────────────────────────────────

def test_free_tier_first_enabled_requires_both_providers(monkeypatch):
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", True)
    with patch("app.openai_client._openai_official_configured", return_value=False), \
            patch("app.openai_client._groq_stack_configured", return_value=True):
        assert oc._free_tier_first_enabled() is False

    with patch("app.openai_client._openai_official_configured", return_value=True), \
            patch("app.openai_client._groq_stack_configured", return_value=False):
        assert oc._free_tier_first_enabled() is False

    with patch("app.openai_client._openai_official_configured", return_value=True), \
            patch("app.openai_client._groq_stack_configured", return_value=True):
        assert oc._free_tier_first_enabled() is True


def test_free_tier_first_off_returns_false_even_with_both(monkeypatch):
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", False)
    with patch("app.openai_client._openai_official_configured", return_value=True), \
            patch("app.openai_client._groq_stack_configured", return_value=True):
        assert oc._free_tier_first_enabled() is False


@patch("app.openai_client._openai_official_configured", return_value=True)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
def test_chat_free_tier_first_calls_groq_before_openai(
    _near, _prem, _groq_cfg, _openai_cfg, monkeypatch
):
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", True)
    call_order = []

    def _fake_call(api_key, base_url, model, *args, **kwargs):
        call_order.append(model)
        return {"reply": "this is a strong reply that is more than twenty chars long", "tokens_used": 10, "model": model}

    with patch("app.openai_client._call_provider", side_effect=_fake_call):
        result = oc.chat(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            trace_id="t-order",
        )

    assert call_order[0] == oc.settings.llm_model
    assert result["model"] == oc.settings.llm_model


@patch("app.openai_client._openai_official_configured", return_value=True)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
def test_weak_groq_escalates_to_openai_under_free_tier_first(
    _near, _prem, _groq_cfg, _openai_cfg, monkeypatch
):
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", True)
    call_order = []

    def _fake_call(api_key, base_url, model, *args, **kwargs):
        call_order.append(model)
        if model in (oc.settings.llm_model, oc._SECONDARY_MODEL):
            return {"reply": "no.", "tokens_used": 1, "model": model}
        return {
            "reply": "This is a rich OpenAI reply containing much detail and explanation.",
            "tokens_used": 40,
            "model": model,
        }

    with patch("app.openai_client._call_provider", side_effect=_fake_call):
        result = oc.chat(
            messages=[{"role": "user", "content": "tell me something detailed about your thinking process in long form"}],
            user_message="tell me something detailed about your thinking process in long form",
            trace_id="t-esc",
        )

    assert oc.settings.llm_model in call_order
    assert oc._SECONDARY_MODEL in call_order
    assert oc.PAID_OPENAI_MODEL in call_order
    assert result["model"] == oc.PAID_OPENAI_MODEL


@patch("app.openai_client._openai_official_configured", return_value=True)
@patch("app.openai_client._groq_stack_configured", return_value=True)
@patch("app.openai_client._premium_configured", return_value=False)
@patch("app.openai_client._near_daily_limit", return_value=False)
def test_legacy_order_tries_openai_before_groq(
    _near, _prem, _groq_cfg, _openai_cfg, monkeypatch
):
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", False)
    call_order = []

    def _fake_call(api_key, base_url, model, *args, **kwargs):
        call_order.append(model)
        return {
            "reply": "Adequate OpenAI reply long enough to pass weak check.",
            "tokens_used": 12,
            "model": model,
        }

    with patch("app.openai_client._call_provider", side_effect=_fake_call):
        oc.chat(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            trace_id="t-legacy",
        )

    assert call_order[0] == oc.PAID_OPENAI_MODEL
