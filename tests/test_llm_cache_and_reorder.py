"""Phase B tests: content-hash LRU+TTL cache + free-tier-first cascade reorder."""
import threading
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
def test_call_llm_cache_singleflight_coalesces_concurrent_provider_calls(_cfg):
    provider_started = threading.Event()
    provider_release = threading.Event()
    start = threading.Barrier(6)
    calls = 0
    lock = threading.Lock()
    results: list[str] = []

    def fake_chat(**_kwargs):
        nonlocal calls
        with lock:
            calls += 1
        provider_started.set()
        assert provider_release.wait(5)
        return {"reply": "shared-reply", "model": "m1"}

    def worker():
        start.wait()
        reply = llm_caller.call_llm(
            messages=[{"role": "user", "content": "same deterministic prompt"}],
            max_tokens=100,
            cacheable=True,
        )
        with lock:
            results.append(reply)

    with patch("app.openai_client.chat", side_effect=fake_chat), patch(
        "app.openai_client.is_configured", return_value=True
    ):
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        start.wait()
        assert provider_started.wait(5)
        time.sleep(0.1)
        provider_release.set()
        for thread in threads:
            thread.join(timeout=5)

    assert calls == 1
    assert results == ["shared-reply"] * 5
    stats = llm_caller.get_cache_stats()
    assert stats["coalesced"] >= 1


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
def test_call_llm_cache_key_includes_purpose_model_policy(_cfg, monkeypatch):
    gateway = MagicMock(side_effect=[
        {"reply": "mini-policy-reply", "model": "gpt-5.4-mini", "gateway_log_id": 11},
        {"reply": "nano-policy-reply", "model": "gpt-5.4-nano", "gateway_log_id": 12},
    ])
    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    msgs = [{"role": "user", "content": "deterministic code search"}]
    monkeypatch.setattr(
        "app.config.settings.chili_llm_purpose_model_overrides_json",
        '{"code_search":"gpt-5.4-mini"}',
    )
    r1 = llm_caller.call_llm(
        messages=msgs,
        max_tokens=100,
        cacheable=True,
        purpose="code_search",
    )

    monkeypatch.setattr(
        "app.config.settings.chili_llm_purpose_model_overrides_json",
        '{"code_search":"gpt-5.4-nano"}',
    )
    r2 = llm_caller.call_llm(
        messages=msgs,
        max_tokens=100,
        cacheable=True,
        purpose="code_search",
    )

    assert r1 == "mini-policy-reply"
    assert r2 == "nano-policy-reply"
    assert gateway.call_count == 2
    stats = llm_caller.get_cache_stats()
    assert stats["misses"] == 2
    assert stats["hits"] == 0


def test_call_llm_with_purpose_gateway_failure_skips_direct_openai(monkeypatch):
    gateway = MagicMock(side_effect=RuntimeError("gateway unavailable"))
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.openai_client.chat", direct)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    reply = llm_caller.call_llm(
        messages=[{"role": "user", "content": "deterministic extraction"}],
        max_tokens=100,
        purpose="trade_plan_extract",
        trace_id="llm-caller-cost-test",
    )

    assert reply == ""
    assert gateway.call_count == 1
    direct.assert_not_called()


def test_call_llm_with_purpose_gateway_failure_return_meta_skips_direct_openai(monkeypatch):
    gateway = MagicMock(side_effect=RuntimeError("gateway unavailable"))
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.openai_client.chat", direct)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    result = llm_caller.call_llm(
        messages=[{"role": "user", "content": "autotrader revalidation"}],
        max_tokens=100,
        purpose="autotrader_revalidation",
        trace_id="llm-caller-cost-test",
        return_meta=True,
    )

    assert result == {"reply": "", "gateway_log_id": None}
    assert gateway.call_count == 1
    direct.assert_not_called()


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


@patch("app.services.llm_caller._cache_config", return_value=(256, 600))
def test_call_llm_autodetects_project_web_research_before_cache_key(_cfg, monkeypatch):
    captured_purposes: list[str | None] = []
    original_cache_key = llm_caller._cache_key

    def spy_cache_key(messages, max_tokens, system_prompt, purpose=None):
        captured_purposes.append(purpose)
        return original_cache_key(messages, max_tokens, system_prompt, purpose)

    gateway = MagicMock(
        return_value={
            "reply": '{"summary":"ok","sources":[],"relevance_score":0.9}',
            "model": "gpt-5.5",
            "gateway_log_id": 77,
        }
    )
    monkeypatch.setattr(llm_caller, "_cache_key", spy_cache_key)
    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    from app.services.project_brain.web_research import _summarize_raw

    first = _summarize_raw("cacheable research", "[]", "trace")
    second = _summarize_raw("cacheable research", "[]", "trace")

    assert first and first["summary"] == "ok"
    assert second and second["summary"] == "ok"
    assert captured_purposes == ["project_web_research", "project_web_research"]
    assert gateway.call_count == 1
    assert gateway.call_args.kwargs["purpose"] == "project_web_research"


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
