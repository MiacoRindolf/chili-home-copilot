from __future__ import annotations

import inspect

from app.services.trading import autotrader_desk


def test_autotrader_desk_live_loader_uses_envelope_helper_not_trade_query() -> None:
    source = inspect.getsource(autotrader_desk.list_pattern_linked_open_positions)

    assert "load_autotrader_desk_live_envelope_objects" in source
    assert "db.query(Trade)" not in source
    assert "filter_broker_stale_open_trades" in source
    assert "broker_stale_open_trade_snapshot" in source
    assert "list_position_overrides" in source
    assert '"controls_supported": True' in source
    assert '"close_supported": True' in source


def test_autotrader_desk_paper_path_stays_unchanged() -> None:
    source = inspect.getsource(autotrader_desk.list_pattern_linked_open_positions)

    assert "db.query(PaperTrade)" in source
    assert "PaperTrade.user_id == user_id" in source
    assert '"kind": "paper"' in source
    assert 'overrides_map.get(("paper", int(pt.id)))' in source

