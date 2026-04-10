from __future__ import annotations

import json

from app.services.trading import brain_assistant


def test_brain_assistant_chat_structured_response(monkeypatch, db) -> None:
    monkeypatch.setattr(
        brain_assistant,
        "build_snapshot",
        lambda *args, **kwargs: {
            "snapshot_at": "2026-04-10T12:00:00Z",
            "automation_focus": {
                "operator_readiness": {"live_ready": False},
                "focus_session": {"data_fidelity": {"simulation_fidelity": "consolidated_quote_sim"}},
            },
        },
    )
    monkeypatch.setattr(
        brain_assistant,
        "call_llm",
        lambda *args, **kwargs: json.dumps(
            {
                "reply": "Momentum thesis is constructive, but execution is still gated.",
                "recommendations": [
                    {
                        "action": "buy",
                        "symbol": "NVDA",
                        "thesis": "Breakout continuation remains intact while readiness is monitored.",
                        "rationale": ["Relative strength is leading.", "No contrary risk event is visible in snapshot."],
                        "entry": "Break above 1000 on momentum confirmation",
                        "invalidation": "Lose breakout base",
                        "exit_logic": "Scale on exhaustion or trailing stop",
                        "timeframe": "intraday",
                        "confidence": 0.74,
                        "risk_note": "Do not bypass policy gates.",
                        "sizing_guidance": "Half risk until live readiness clears.",
                        "execution_readiness": {"status": "gated", "reason": "operator confirmation required"},
                        "what_would_change": "Breakout failure or readiness downgrade",
                    }
                ],
                "missing_context": ["account buying power"],
            }
        ),
    )

    out = brain_assistant.chat(
        db,
        user_id=1,
        messages=[{"role": "user", "content": "Top setup right now?"}],
    )

    assert out["ok"] is True
    assert out["reply"].startswith("Momentum thesis")
    assert out["snapshot_at"] == "2026-04-10T12:00:00Z"
    assert out["missing_context"] == ["account buying power"]
    assert len(out["recommendations"]) == 1
    rec = out["recommendations"][0]
    assert rec["action"] == "buy"
    assert rec["symbol"] == "NVDA"
    assert rec["execution_readiness"]["status"] == "gated"


def test_brain_assistant_chat_route_contract(paired_client, monkeypatch) -> None:
    client, _user = paired_client

    monkeypatch.setattr(
        "app.services.trading.brain_assistant.chat",
        lambda *args, **kwargs: {
            "ok": True,
            "reply": "Actionable recommendation ready.",
            "recommendations": [
                {
                    "action": "wait",
                    "symbol": "TSLA",
                    "thesis": "Wait for confirmation through resistance.",
                    "rationale": ["Resistance overhead remains active."],
                }
            ],
            "missing_context": ["current spread regime"],
            "snapshot_at": "2026-04-10T12:00:00Z",
        },
    )

    r = client.post(
        "/api/brain/trading/assistant/chat",
        json={"messages": [{"role": "user", "content": "What do you recommend?"}]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["reply"] == "Actionable recommendation ready."
    assert data["snapshot_at"] == "2026-04-10T12:00:00Z"
    assert data["recommendations"][0]["action"] == "wait"
    assert data["missing_context"] == ["current spread regime"]
