"""Tests for the project-brain agent LLM cost wrapper + the agent rewiring.

The 10 project-brain agents route their LLM calls through call_agent_llm
(via a per-agent functools.partial that injects purpose=project_<agent>), so
cost/cache telemetry is attributed per agent while the agents still receive a
plain reply string. Re-implemented on main from os-deploy unique work.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.project_brain.agents import llm_cost as lc


def setup_function(_func):
    lc.reset_project_agent_cost_stats()


def _fake_llm(*args, **kwargs):
    return {"reply": "hello", "cache_status": "miss", "estimated_cost_usd": 0.01}


def test_call_agent_llm_returns_string_and_records_per_purpose():
    with patch.object(lc, "_call_llm", side_effect=_fake_llm):
        out = lc.call_agent_llm(
            messages=[{"role": "user", "content": "give me a real answer please"}],
            purpose="project_architect",
        )
    assert out == "hello"  # plain string by default (agents rely on this)
    p = lc.get_project_agent_cost_stats()["by_purpose"]["project_architect"]
    assert p["llm_calls"] == 1
    assert p["llm_success"] == 1


def test_return_meta_yields_dict():
    with patch.object(lc, "_call_llm", side_effect=_fake_llm):
        out = lc.call_agent_llm(
            messages=[{"role": "user", "content": "x y z content"}],
            purpose="project_architect",
            return_meta=True,
        )
    assert isinstance(out, dict) and out["reply"] == "hello"


def test_material_cache_serves_repeat_cacheable_without_second_llm_call():
    msgs = [{"role": "user", "content": "stable material content for caching test"}]
    with patch.object(lc, "_call_llm", side_effect=_fake_llm) as m:
        a = lc.call_agent_llm(messages=msgs, purpose="project_architect", cacheable=True)
        b = lc.call_agent_llm(messages=msgs, purpose="project_architect", cacheable=True)
    assert a == "hello" and b == "hello"
    assert m.call_count == 1  # second call served from the material cache
    p = lc.get_project_agent_cost_stats()["by_purpose"]["project_architect"]
    assert p["material_cache_hits"] >= 1


def test_rewired_agent_module_routes_through_wrapper_with_its_purpose():
    # architect.call_llm is now functools.partial(call_agent_llm, purpose=project_architect)
    from app.services.project_brain.agents import architect

    with patch.object(lc, "_call_llm", side_effect=_fake_llm):
        out = architect.call_llm(
            messages=[{"role": "user", "content": "architect prompt content"}]
        )
    assert out == "hello"
    p = lc.get_project_agent_cost_stats()["by_purpose"]["project_architect"]
    assert p["llm_calls"] >= 1


def test_each_agent_module_has_distinct_purpose_partial():
    from app.services.project_brain.agents import qa_engineer, security_engineer

    with patch.object(lc, "_call_llm", side_effect=_fake_llm):
        qa_engineer.call_llm(messages=[{"role": "user", "content": "qa content here"}])
        security_engineer.call_llm(messages=[{"role": "user", "content": "sec content here"}])
    bp = lc.get_project_agent_cost_stats()["by_purpose"]
    assert bp["project_qa_engineer"]["llm_calls"] >= 1
    assert bp["project_security_engineer"]["llm_calls"] >= 1
