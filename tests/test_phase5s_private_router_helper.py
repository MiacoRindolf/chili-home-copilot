from __future__ import annotations

import inspect

from app.routers.trading_sub import ai


def test_pattern_evidence_uses_management_envelope_helper() -> None:
    source = inspect.getsource(ai._api_pattern_evidence_response)

    assert "load_pattern_tagged_envelope_rows" in source
    assert "db.query(Trade)" not in source
    assert "Trade.user_id" not in source
    assert '"trades": trades_out' in source


def test_pattern_evidence_no_longer_imports_trade_orm_symbol() -> None:
    source = inspect.getsource(ai._api_pattern_evidence_response)

    assert "TradingHypothesis, Trade" not in source
    assert "from ...models.trading import LearningEvent, TradingHypothesis, Trade" not in source
