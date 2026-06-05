"""Unit tests for advisory-trading LLM stream cost stats (in-memory, no DB).

Re-implemented on main from os-deploy unique work
(backup/os-deploy-cleanup-20260605).
"""
from __future__ import annotations

from app.services.trading import stream_llm_cost as sc

_P = "advisory"


def setup_function(_func):
    sc.reset_trading_stream_llm_cost_stats()


def test_gateway_cache_hit_records_saved_not_spend():
    sc.record_stream_gateway_result(
        _P, {"cache_status": "gateway_cache_hit", "reply": "ok", "estimated_saved_usd": 0.01}
    )
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["gateway_stream_calls"] == 1
    assert s["gateway_stream_success"] == 1
    assert s["gateway_cache_hits"] == 1
    assert s["saved_responses"] == 1
    assert s["estimated_saved_usd"] == 0.01
    assert s["estimated_spend_usd"] == 0.0
    assert s["by_purpose"][_P]["gateway_cache_hits"] == 1


def test_gateway_miss_records_spend():
    sc.record_stream_gateway_result(
        _P, {"cache_status": "miss", "reply": "ok", "estimated_cost_usd": 0.05}
    )
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["gateway_cache_hits"] == 0
    assert s["saved_responses"] == 0
    assert s["estimated_spend_usd"] == 0.05
    assert s["gateway_stream_success"] == 1


def test_empty_reply_counts_as_failure():
    sc.record_stream_gateway_result(_P, {"cache_status": "miss", "reply": ""})
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["gateway_stream_failures"] == 1 and s["gateway_stream_success"] == 0


def test_llm_caller_cache_hit_uses_distinct_counter():
    sc.record_stream_gateway_result(_P, {"cache_status": "llm_caller_cache_hit", "reply": "ok"})
    assert sc.get_trading_stream_llm_cost_stats()["llm_caller_cache_hits"] == 1


def test_material_and_mechanical_records():
    sc.record_material_cache_hit(_P, estimated_saved_usd=0.02)
    sc.record_mechanical_response(_P, estimated_saved_usd=0.03)
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["material_cache_hits"] == 1
    assert s["mechanical_responses"] == 1
    assert s["saved_responses"] == 2
    assert s["estimated_saved_usd"] == 0.05


def test_explicit_failure_helper():
    sc.record_stream_gateway_failure(_P)
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["gateway_stream_calls"] == 1 and s["gateway_stream_failures"] == 1


def test_avoidance_rate_and_reset():
    sc.record_material_cache_hit(_P)  # saved + observed
    sc.record_stream_gateway_result(_P, {"cache_status": "miss", "reply": "ok", "estimated_cost_usd": 0.1})
    s = sc.get_trading_stream_llm_cost_stats()
    assert s["total_requests_observed"] == 2
    assert s["saved_responses"] == 1
    assert s["avoidance_rate"] == 0.5
    sc.reset_trading_stream_llm_cost_stats()
    s2 = sc.get_trading_stream_llm_cost_stats()
    assert s2["total_requests_observed"] == 0 and s2["by_purpose"] == {}
