from __future__ import annotations

import inspect

from app.services.trading import ai_context


def test_ai_context_recent_trade_context_uses_management_envelope_helper() -> None:
    source = inspect.getsource(ai_context.build_ai_context)

    assert "load_recent_ticker_envelope_rows" in source
    assert "db.query(Trade)" not in source


def test_ai_context_no_longer_imports_trade_orm_symbol() -> None:
    source = inspect.getsource(ai_context)

    assert "Trade," not in source
    assert "from ...models.trading import Trade" not in source


def test_journal_and_close_attribution_no_longer_import_trade_orm_symbol() -> None:
    from app.services.trading import journal
    from app.services.trading.brain_work import execution_attribution

    journal_source = inspect.getsource(journal)
    attribution_source = inspect.getsource(execution_attribution)

    assert "from ...models.trading import JournalEntry, Trade" not in journal_source
    assert "from ....models.trading import Trade" not in attribution_source
    assert "trade: Trade" not in journal_source
    assert "trade: Trade" not in attribution_source
