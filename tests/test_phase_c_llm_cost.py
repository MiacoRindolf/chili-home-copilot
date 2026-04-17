"""Phase C tests:
- analyze/stream SSE cache replays cached text as SSE chunks
- per-provider daily token budget blocks tier at threshold
- pattern_adjustment_advisor input gate reuses rec when inputs match
- app/prompts/agent_shared.txt loads at AgentBase import
"""
from unittest.mock import MagicMock, patch

import pytest

from app import openai_client as oc
from app.services.trading import pattern_adjustment_advisor as paa
from app.routers import trading as trading_router


# ── c1: analyze/stream SSE cache ────────────────────────────────────────

def test_analyze_stream_cache_hit_and_miss_and_ttl(monkeypatch):
    trading_router._analyze_stream_cache.clear()
    trading_router._analyze_stream_stats["hits"] = 0
    trading_router._analyze_stream_stats["misses"] = 0

    key = trading_router._analyze_stream_key(
        user_id=1, ticker="AAPL", interval="1d", ai_context="ctx-v1", user_msg="analyze"
    )
    assert trading_router._analyze_stream_cache_get(key) is None

    trading_router._analyze_stream_cache_put(key, "full analysis text", "gpt-4o-mini", "1d")
    got = trading_router._analyze_stream_cache_get(key)
    assert got is not None
    assert got[0] == "full analysis text"
    assert got[1] == "gpt-4o-mini"

    stats = trading_router._analyze_stream_cache_stats()
    assert stats["hits"] == 1
    assert stats["size"] == 1

    import time
    base = time.monotonic()
    monkeypatch.setattr(
        trading_router.time,
        "monotonic",
        lambda: base + trading_router._analyze_stream_ttl("1d") + 5,
    )
    assert trading_router._analyze_stream_cache_get(key) is None


def test_analyze_stream_cache_ttl_by_interval():
    assert trading_router._analyze_stream_ttl("1m") == 10
    assert trading_router._analyze_stream_ttl("1d") == 90
    assert trading_router._analyze_stream_ttl("unknown") == trading_router._ANALYZE_STREAM_TTL_DEFAULT


def test_analyze_stream_chunk_text_yields_all_bytes():
    body = "abcdefghij" * 5
    chunks = list(trading_router._analyze_stream_chunk_text(body, chunk_size=16))
    assert "".join(chunks) == body
    assert all(len(c) <= 16 for c in chunks)


# ── c2: per-provider daily token budget ────────────────────────────────

def test_per_provider_bucket_isolated():
    oc._daily_tokens.clear()
    oc._track_tokens(100, "https://api.groq.com/openai/v1")
    oc._track_tokens(200, "https://api.openai.com/v1")
    usage = oc.get_daily_token_usage()
    assert usage["providers"]["groq"]["used"] == 100
    assert usage["providers"]["openai"]["used"] == 200


def test_per_provider_near_daily_limit_openai(monkeypatch):
    oc._daily_tokens.clear()
    monkeypatch.setattr(oc.settings, "openai_daily_token_limit", 500)
    assert oc._near_daily_limit("https://api.openai.com/v1") is False
    oc._track_tokens(600, "https://api.openai.com/v1")
    assert oc._near_daily_limit("https://api.openai.com/v1") is True
    # Groq bucket is untouched
    assert oc._near_daily_limit("https://api.groq.com/openai/v1") is False


def test_per_provider_zero_limit_is_unlimited(monkeypatch):
    oc._daily_tokens.clear()
    monkeypatch.setattr(oc.settings, "openai_daily_token_limit", 0)
    oc._track_tokens(10_000_000, "https://api.openai.com/v1")
    assert oc._near_daily_limit("https://api.openai.com/v1") is False


def test_openai_tier_skipped_when_budget_exhausted(monkeypatch):
    oc._daily_tokens.clear()
    monkeypatch.setattr(oc.settings, "openai_daily_token_limit", 100)
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", False)
    oc._track_tokens(1000, oc.PAID_OPENAI_BASE_URL)

    mock_call = MagicMock()
    with patch("app.openai_client._openai_official_configured", return_value=True), \
         patch("app.openai_client._groq_stack_configured", return_value=False), \
         patch("app.openai_client._premium_configured", return_value=False), \
         patch("app.openai_client._call_provider", mock_call):
        result = oc.chat(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            trace_id="t-budget",
        )

    mock_call.assert_not_called()
    assert result["model"] == "error"


# ── c3: pattern_adjustment_advisor input gate ──────────────────────────

@pytest.fixture(autouse=True)
def _reset_gate():
    paa.reset_input_gate_cache()
    yield
    paa.reset_input_gate_cache()


def _make_inputs(**overrides):
    base = dict(
        ticker="AAPL",
        pattern_name="breakout",
        pattern_description="x",
        health_summary="ok",
        health_score=0.72,
        health_delta=0.01,
        current_price=100.00,
        entry_price=95.0,
        current_stop=92.0,
        current_target=110.0,
        pattern_stop=90.0,
        pattern_target=115.0,
        pnl_pct=5.3,
        trade_plan_health=None,
        trade_id=42,
    )
    base.update(overrides)
    return base


def test_input_gate_reuses_rec_on_tiny_price_wiggle():
    mock_llm = MagicMock(return_value='{"action":"hold","new_stop":null,"new_target":null,"confidence":0.7,"reasoning":"ok"}')
    with patch("app.services.llm_caller.call_llm", mock_llm):
        r1 = paa.get_adjustment(**_make_inputs())
        r2 = paa.get_adjustment(**_make_inputs(current_price=100.05))

    assert r1.action == r2.action
    assert mock_llm.call_count == 1
    stats = paa.get_input_gate_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_input_gate_misses_when_price_moves_outside_bucket():
    mock_llm = MagicMock(return_value='{"action":"hold","new_stop":null,"new_target":null,"confidence":0.5,"reasoning":"ok"}')
    with patch("app.services.llm_caller.call_llm", mock_llm):
        paa.get_adjustment(**_make_inputs())
        paa.get_adjustment(**_make_inputs(current_price=105.0))

    assert mock_llm.call_count == 2
    stats = paa.get_input_gate_stats()
    assert stats["misses"] == 2


def test_input_gate_misses_when_health_moves_outside_bucket():
    mock_llm = MagicMock(return_value='{"action":"hold","new_stop":null,"new_target":null,"confidence":0.5,"reasoning":"ok"}')
    with patch("app.services.llm_caller.call_llm", mock_llm):
        paa.get_adjustment(**_make_inputs(health_score=0.72))
        paa.get_adjustment(**_make_inputs(health_score=0.90))

    assert mock_llm.call_count == 2


# ── c4: agent_shared.txt loads ──────────────────────────────────────────

def test_agent_shared_prompt_loaded():
    from app.services.project_brain.base import AGENT_SHARED_PROMPT
    assert "ONLY valid JSON" in AGENT_SHARED_PROMPT
    assert "info" in AGENT_SHARED_PROMPT and "critical" in AGENT_SHARED_PROMPT
