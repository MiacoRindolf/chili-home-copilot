from __future__ import annotations

import inspect
from datetime import datetime

from app.routers import trading


def test_stop_decisions_uses_envelope_helper_not_trade_join() -> None:
    source = inspect.getsource(trading.api_stop_decisions)

    assert "load_stop_decision_envelope_rows" in source
    assert "StopDecision" not in source
    assert " Trade" not in source
    assert ".join(" not in source
    assert '"decisions": result' in source


def test_stop_decision_rows_preserve_public_contract() -> None:
    rows = trading._stop_decision_rows([
        {
            "id": 1,
            "trade_id": 2,
            "as_of_ts": datetime(2026, 5, 30, 18, 15, 30),
            "state": "tighten",
            "old_stop": 10.0,
            "new_stop": 10.5,
            "trigger": "atr_trail",
            "reason": "trail tightened",
            "executed": True,
        },
        {
            "id": 3,
            "trade_id": 4,
            "as_of_ts": None,
            "state": "hold",
            "old_stop": None,
            "new_stop": None,
            "trigger": None,
            "reason": None,
            "executed": False,
        },
    ])

    assert rows == [
        {
            "id": 1,
            "trade_id": 2,
            "as_of_ts": "2026-05-30T18:15:30",
            "state": "tighten",
            "old_stop": 10.0,
            "new_stop": 10.5,
            "trigger": "atr_trail",
            "reason": "trail tightened",
            "executed": True,
        },
        {
            "id": 3,
            "trade_id": 4,
            "as_of_ts": None,
            "state": "hold",
            "old_stop": None,
            "new_stop": None,
            "trigger": None,
            "reason": None,
            "executed": False,
        },
    ]
