from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.brain_neural_mesh import repository as repo
from app.services.trading.brain_neural_mesh import trade_context_aggregator as mod
from app.services.trading.brain_neural_mesh.graduation import empty_graduation_state


def _children() -> dict[str, dict[str, object]]:
    return {
        "nm_stop_eval": {
            "action": "hold",
            "urgency": "info",
            "ticker": "TST",
            "health_score": 0.7,
        }
    }


def _reset_cost_state(monkeypatch: pytest.MonkeyPatch) -> None:
    mod._daily_llm_calls.clear()
    mod._decision_cache.clear()
    monkeypatch.setattr(mod, "_today_str", lambda: "2026-06-04")


def test_mesh_trade_context_has_bounded_teacher_defaults() -> None:
    assert mod.DEFAULT_DAILY_LLM_CAP == 50
    assert hasattr(settings, "mesh_daily_llm_cap")
    assert hasattr(settings, "mesh_teacher_queue_pressure_block_fraction")


def test_teacher_llm_respects_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cost_state(monkeypatch)
    monkeypatch.setattr(settings, "mesh_daily_llm_cap", 1)
    mod._daily_llm_calls["2026-06-04"] = 1

    def fail_gateway(**_kwargs):
        raise AssertionError("daily cap should skip gateway_chat")

    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", fail_gateway)

    assert mod._call_teacher_llm(_children()) is None


def test_teacher_llm_uses_gateway_when_cap_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cost_state(monkeypatch)
    monkeypatch.setattr(settings, "mesh_daily_llm_cap", 1)
    calls: list[dict[str, object]] = []

    def fake_gateway(**kwargs):
        calls.append(kwargs)
        return {
            "reply": (
                '{"decision":"hold","urgency":"info","action":"hold",'
                '"confidence":0.62,"reasoning":"mechanical check"}'
            )
        }

    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", fake_gateway)

    decision = mod._call_teacher_llm(_children())

    assert decision is not None
    assert decision["method"] == "teacher_llm"
    assert len(calls) == 1
    assert mod._daily_llm_calls["2026-06-04"] == 1


def test_queue_pressure_blocks_teacher_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mesh_teacher_queue_pressure_block_fraction", 0.8)
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 10)
    monkeypatch.setattr(repo, "pending_queue_depth", lambda _db: 8)

    assert mod._teacher_blocked_by_queue_pressure(object()) is True


def test_handle_trade_context_uses_mechanical_decision_under_queue_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cost_state(monkeypatch)
    monkeypatch.setattr(mod, "_teacher_blocked_by_queue_pressure", lambda _db: True)

    def fail_teacher(_children_state):
        raise AssertionError("queue pressure should skip teacher LLM")

    monkeypatch.setattr(mod, "_call_teacher_llm", fail_teacher)
    state = SimpleNamespace(
        local_state={"graduation": empty_graduation_state()},
        updated_at=None,
    )

    decision = mod.handle_trade_context(
        object(),
        "nm_trade_context",
        state,
        {"children_state": _children()},
    )

    assert decision is not None
    assert decision["method"] == "mechanical"
    assert state.local_state["method"] == "mechanical"
