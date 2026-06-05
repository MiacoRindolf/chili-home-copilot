"""Unit tests for reasoning-brain LLM cost stats (in-memory, no DB).

Re-implemented on main from os-deploy unique work
(backup/os-deploy-cleanup-20260605).
"""
from __future__ import annotations

from app.services.reasoning_brain import cost_stats as cs

_P = "reasoning_user_model"


def setup_function(_func):
    cs.reset_reasoning_cost_stats()


def test_increment_aggregates_per_purpose_and_total():
    cs.increment_reasoning_cost_stat(_P, "fresh_cache_hits", 2)
    cs.increment_reasoning_cost_stat(_P, "fresh_cache_hits")
    cs.increment_reasoning_cost_stat("reasoning_anticipate", "llm_calls")
    stats = cs.get_reasoning_cost_stats()
    assert stats["by_purpose"][_P]["fresh_cache_hits"] == 3
    assert stats["fresh_cache_hits"] == 3  # rolled-up total
    assert stats["by_purpose"]["reasoning_anticipate"]["llm_calls"] == 1


def test_derived_avoidance_rate():
    cs.increment_reasoning_cost_stat(_P, "fresh_cache_hits", 3)
    cs.increment_reasoning_cost_stat(_P, "llm_calls", 1)
    p = cs.get_reasoning_cost_stats()["by_purpose"][_P]
    assert p["saved_responses"] == 3
    assert p["total_requests_observed"] == 4
    assert p["avoidance_rate"] == 0.75
    assert p["llm_call_rate"] == 0.25


def test_add_cost_usd_only_amount_counters_and_positive():
    cs.add_reasoning_cost_usd(_P, "estimated_spend_usd", 0.5)
    cs.add_reasoning_cost_usd(_P, "estimated_spend_usd", -1)  # non-positive ignored
    cs.add_reasoning_cost_usd(_P, "fresh_cache_hits", 9)      # not an amount counter -> ignored
    p = cs.get_reasoning_cost_stats()["by_purpose"][_P]
    assert p["estimated_spend_usd"] == 0.5
    assert p["fresh_cache_hits"] == 0


def test_material_fingerprint_deterministic():
    assert cs.reasoning_material_fingerprint({"q": "x"}) == cs.reasoning_material_fingerprint({"q": "x"})


def test_material_replay_roundtrip_counts_store_and_hit():
    fp = cs.reasoning_material_fingerprint({"q": "x"})
    assert cs.get_reasoning_material_replay(_P, fp) is None
    cs.set_reasoning_material_replay(_P, fp, {"reply": "cached"}, estimated_saved_usd=0.02)
    assert cs.get_reasoning_material_replay(_P, fp) == {"reply": "cached"}
    p = cs.get_reasoning_cost_stats()["by_purpose"][_P]
    assert p["material_cache_stores"] == 1
    assert p["material_cache_hits"] == 1
    assert p["estimated_saved_usd"] == 0.02


def test_inflight_leader_follower_coalesce():
    fp = cs.reasoning_material_fingerprint({"q": "y"})
    ev1, leader1 = cs.begin_reasoning_material_inflight(_P, fp)
    assert leader1 is True
    ev2, leader2 = cs.begin_reasoning_material_inflight(_P, fp)
    assert leader2 is False and ev2 is ev1
    # leader publishes to the replay cache, then signals the followers
    cs.set_reasoning_material_replay(_P, fp, {"reply": "done"})
    cs.finish_reasoning_material_inflight(_P, fp, {"reply": "done"})
    assert cs.wait_reasoning_material_inflight(_P, fp, ev2) == {"reply": "done"}


def test_record_gateway_cache_hit():
    cs.record_reasoning_gateway_cost(
        _P, {"cache_status": "gateway_cache_hit", "estimated_saved_usd": 0.03}
    )
    p = cs.get_reasoning_cost_stats()["by_purpose"][_P]
    assert p["gateway_cache_hits"] == 1
    assert p["estimated_saved_usd"] == 0.03


def test_reset_clears_counters_and_replay():
    cs.increment_reasoning_cost_stat(_P, "llm_calls", 5)
    fp = cs.reasoning_material_fingerprint({"q": "z"})
    cs.set_reasoning_material_replay(_P, fp, {"r": 1})
    cs.reset_reasoning_cost_stats()
    assert cs.get_reasoning_cost_stats()["by_purpose"][_P]["llm_calls"] == 0
    assert cs.get_reasoning_material_replay(_P, fp) is None
