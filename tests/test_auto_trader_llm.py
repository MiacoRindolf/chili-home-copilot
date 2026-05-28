from __future__ import annotations

from app.models.trading import BreakoutAlert
from app.services.trading import auto_trader_llm as mod


def _alert() -> BreakoutAlert:
    return BreakoutAlert(
        id=101,
        ticker="LLMT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.91,
        entry_price=100.0,
        stop_loss=96.0,
        target_price=108.0,
        indicator_snapshot={"imminent_scorecard": {"signal_lane": "standard"}},
    )


def test_revalidation_empty_reply_is_llm_unavailable(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(mod, "_load_system_prompt", lambda: "system")

    def fake_call_llm(*args, **kwargs):
        calls.update(kwargs)
        return {"reply": "", "gateway_log_id": 42}

    monkeypatch.setattr(mod, "call_llm", fake_call_llm)

    viable, snap = mod.run_revalidation_llm(_alert(), current_price=101.0)

    assert viable is False
    assert calls["return_meta"] is True
    assert snap["error"] == "llm_unavailable"
    assert snap["raw_preview"] == ""
    assert snap["gateway_log_id"] == 42


def test_revalidation_malformed_nonempty_reply_is_parse_failed(monkeypatch):
    monkeypatch.setattr(mod, "_load_system_prompt", lambda: "system")
    monkeypatch.setattr(
        mod,
        "call_llm",
        lambda *args, **kwargs: {"reply": "not-json", "gateway_log_id": 7},
    )

    viable, snap = mod.run_revalidation_llm(_alert(), current_price=101.0)

    assert viable is False
    assert snap["error"] == "parse_failed"
    assert snap["raw_preview"] == "not-json"
    assert snap["gateway_log_id"] == 7


def test_revalidation_valid_json_preserves_gateway_metadata(monkeypatch):
    monkeypatch.setattr(mod, "_load_system_prompt", lambda: "system")
    monkeypatch.setattr(
        mod,
        "call_llm",
        lambda *args, **kwargs: {
            "reply": '{"viable": true, "confidence": 0.82, "reason": "clean setup"}',
            "gateway_log_id": 9,
        },
    )

    viable, snap = mod.run_revalidation_llm(_alert(), current_price=101.0)

    assert viable is True
    assert snap["confidence"] == 0.82
    assert snap["reason"] == "clean setup"
    assert snap["gateway_log_id"] == 9
