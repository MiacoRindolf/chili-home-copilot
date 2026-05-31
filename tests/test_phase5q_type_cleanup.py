from __future__ import annotations

import inspect


def test_execution_hooks_no_longer_imports_trade_orm_symbol() -> None:
    from app.services.trading.brain_work import execution_hooks

    source = inspect.getsource(execution_hooks)

    assert "from ....models.trading import PaperTrade, Trade" not in source
    assert "from ....models.trading import Trade" not in source
    assert "trade: Trade" not in source


def test_brain_work_docs_avoid_legacy_trade_label_for_close_events() -> None:
    from app.services.trading.brain_work.handlers import quality_score, regime_ledger

    assert "Trade-close" not in inspect.getsource(quality_score)
    assert "Trade-close" not in inspect.getsource(regime_ledger)
