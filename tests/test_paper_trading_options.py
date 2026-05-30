from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.trading import PaperTrade


OPTION_META = {
    "underlying": "SPY",
    "expiration": "2026-06-19",
    "strike": 729.0,
    "option_type": "call",
    "limit_price": 1.25,
}


def _option_signal() -> dict:
    return {
        "asset_type": "options",
        "options_path": True,
        "option_meta": dict(OPTION_META),
    }


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def count(self):
        return 0

    def first(self):
        return None


class _RowsQuery(_FakeQuery):
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeDb:
    def __init__(self, rows=None):
        self.added = []
        self.commits = 0
        self._rows = list(rows or [])

    def query(self, _model):
        if self._rows:
            return _RowsQuery(self._rows)
        return _FakeQuery()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        for idx, row in enumerate(self.added, start=1):
            row.id = idx

    def commit(self):
        self.commits += 1
        return None


def _paper_rows(db: _FakeDb) -> list[PaperTrade]:
    return [row for row in db.added if isinstance(row, PaperTrade)]


def test_autotrader_paper_entry_context_uses_option_premium() -> None:
    from app.services.trading.auto_trader import _paper_entry_context_for_alert

    alert = SimpleNamespace(entry_price=9.99)
    entry_price, signal = _paper_entry_context_for_alert(
        alert,
        px=729.0,
        snap={"options_path": True, "option_meta": dict(OPTION_META)},
    )

    assert entry_price == pytest.approx(1.25)
    assert signal["asset_type"] == "options"
    assert signal["options_path"] is True
    assert signal["option_meta"]["strike"] == 729.0
    assert signal["underlying_price_at_entry"] == pytest.approx(729.0)


def test_autotrader_paper_entry_context_refuses_underlying_fallback_without_premium() -> None:
    from app.services.trading.auto_trader import _paper_entry_context_for_alert

    option_meta = dict(OPTION_META)
    option_meta.pop("limit_price")
    alert = SimpleNamespace(entry_price=None)

    entry_price, signal = _paper_entry_context_for_alert(
        alert,
        px=729.0,
        snap={"options_path": True, "option_meta": option_meta},
    )

    assert entry_price is None
    assert signal["asset_type"] == "options"
    assert signal["paper_entry_price_error"] == "missing_option_premium"
    assert signal["underlying_price_at_entry"] == pytest.approx(729.0)


def test_autotrader_paper_entry_context_refuses_nonfinite_underlying() -> None:
    from app.services.trading.auto_trader import _paper_entry_context_for_alert

    alert = SimpleNamespace(entry_price=1.25)

    entry_price, signal = _paper_entry_context_for_alert(
        alert,
        px=float("nan"),
        snap={"options_path": True, "option_meta": dict(OPTION_META)},
    )

    assert entry_price is None
    assert signal["asset_type"] == "options"
    assert signal["paper_entry_price_error"] == "invalid_underlying_price"
    assert "underlying_price_at_entry" not in signal


def test_paper_shadow_option_without_premium_does_not_open_underlying_priced_trade(
    monkeypatch,
) -> None:
    from app.services.trading import auto_trader as at_mod
    from app.services.trading import paper_trading

    option_meta = dict(OPTION_META)
    option_meta.pop("limit_price")
    db = _FakeDb()
    calls = {"open": 0}

    def _open_should_not_run(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("missing option premium must not paper-open from underlying spot")

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_janitor_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_same_alert_reason_family",
        False,
        raising=False,
    )
    monkeypatch.setattr(paper_trading, "open_paper_trade", _open_should_not_run)

    at_mod._maybe_open_paper_shadow(
        db,
        uid=1,
        alert=SimpleNamespace(
            id=501,
            ticker="SPY",
            scan_pattern_id=901,
            entry_price=None,
        ),
        qty=1.0,
        px=729.0,
        snap={"options_path": True, "option_meta": option_meta},
        decision="placed",
    )

    assert calls["open"] == 0
    assert _paper_rows(db) == []


def test_paper_shadow_option_with_nonfinite_underlying_does_not_open(
    monkeypatch,
) -> None:
    from app.services.trading import auto_trader as at_mod
    from app.services.trading import paper_trading

    db = _FakeDb()
    calls = {"open": 0}

    def _open_should_not_run(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("nonfinite underlying must not open option paper shadow")

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_janitor_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_dedupe_same_alert_reason_family",
        False,
        raising=False,
    )
    monkeypatch.setattr(paper_trading, "open_paper_trade", _open_should_not_run)

    at_mod._maybe_open_paper_shadow(
        db,
        uid=1,
        alert=SimpleNamespace(
            id=503,
            ticker="SPY",
            scan_pattern_id=903,
            entry_price=1.25,
        ),
        qty=1.0,
        px=float("nan"),
        snap={"options_path": True, "option_meta": dict(OPTION_META)},
        decision="placed",
    )

    assert calls["open"] == 0
    assert _paper_rows(db) == []


def test_option_entry_rejects_nonfinite_underlying_before_paper_open(
    monkeypatch,
) -> None:
    from app.services.trading import auto_trader as at_mod
    from app.services.trading import paper_trading

    audits: list[dict] = []
    db = _FakeDb()

    def _open_should_not_run(*_args, **_kwargs):
        raise AssertionError("nonfinite underlying price must not open option paper rows")

    monkeypatch.setattr(
        at_mod,
        "_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )
    monkeypatch.setattr(at_mod, "_autotrader_tick_note", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paper_trading, "open_paper_trade", _open_should_not_run)

    out = {"skipped": 0, "placed": 0}
    at_mod._execute_new_entry(
        db,
        uid=1,
        alert=SimpleNamespace(
            id=502,
            ticker="SPY",
            scan_pattern_id=902,
            entry_price=1.25,
        ),
        px=float("nan"),
        snap={"options_path": True, "option_meta": dict(OPTION_META)},
        llm_snap=None,
        live=False,
        out=out,
    )

    assert out["skipped"] == 1
    assert audits[-1]["reason"] == "bad_px"
    assert _paper_rows(db) == []


def test_open_paper_trade_option_defaults_to_premium_levels(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_stop_pct",
        50.0,
        raising=False,
    )
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_tp_pct",
        100.0,
        raising=False,
    )

    with patch(
        "app.services.trading.paper_trading._compute_atr_levels",
        side_effect=AssertionError("option paper rows should not use underlying ATR levels"),
    ):
        trade = paper_trading.open_paper_trade(
            _FakeDb(),
            user_id=1,
            ticker="SPY",
            entry_price=1.25,
            quantity=2.0,
            signal_json=_option_signal(),
        )

    assert trade is not None
    assert trade.entry_price == pytest.approx(1.25)
    assert trade.stop_price == pytest.approx(0.625)
    assert trade.target_price == pytest.approx(2.50)
    assert trade.signal_json["_paper_meta"]["contract_multiplier"] == 100.0


def test_open_paper_trade_short_without_atr_uses_short_side_fallbacks(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    with patch(
        "app.services.trading.paper_trading._compute_atr_levels",
        return_value=(None, None, None),
    ):
        trade = paper_trading.open_paper_trade(
            _FakeDb(),
            user_id=1,
            ticker="SPY",
            entry_price=100.0,
            direction="short",
        )

    assert trade is not None
    assert trade.direction == "short"
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.stop_price == pytest.approx(103.0)
    assert trade.target_price == pytest.approx(94.0)
    assert trade.signal_json["_paper_meta"]["highest_price"] is None
    assert trade.signal_json["_paper_meta"]["lowest_price"] == pytest.approx(100.0)


def test_open_paper_trade_passes_direction_to_atr_levels(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    with patch(
        "app.services.trading.paper_trading._compute_atr_levels",
        return_value=(104.0, 94.0, 2.0),
    ) as compute_atr:
        trade = paper_trading.open_paper_trade(
            _FakeDb(),
            user_id=1,
            ticker="SPY",
            entry_price=100.0,
            direction="SHORT",
        )

    assert trade is not None
    assert trade.direction == "short"
    assert trade.stop_price == pytest.approx(104.0)
    assert trade.target_price == pytest.approx(94.0)
    assert compute_atr.call_args.args[2]["_paper_direction"] == "short"


def test_compute_atr_levels_respects_short_side_geometry() -> None:
    from app.services.trading import paper_trading

    class _Series:
        values = [100.0] * 20

    class _Df:
        def __len__(self):
            return 20

        def __getitem__(self, _key):
            return _Series()

    with patch(
        "app.services.trading.market_data.fetch_ohlcv_df",
        return_value=_Df(),
    ), patch(
        "app.services.trading.indicator_core.compute_atr",
        return_value=[2.0],
    ):
        stop, target, atr = paper_trading._compute_atr_levels(
            "SPY",
            100.0,
            {"atr_stop_mult": 2.0, "atr_target_mult": 3.0},
            direction="short",
        )

    assert stop == pytest.approx(104.0)
    assert target == pytest.approx(94.0)
    assert atr == pytest.approx(2.0)


def test_compute_atr_levels_rejects_malformed_multiplier() -> None:
    from app.services.trading import paper_trading

    class _Series:
        values = [100.0] * 20

    class _Df:
        def __len__(self):
            return 20

        def __getitem__(self, _key):
            return _Series()

    with patch(
        "app.services.trading.market_data.fetch_ohlcv_df",
        return_value=_Df(),
    ), patch(
        "app.services.trading.indicator_core.compute_atr",
        return_value=[2.0],
    ):
        assert paper_trading._compute_atr_levels(
            "SPY",
            100.0,
            {"atr_stop_mult": "bad", "atr_target_mult": 3.0},
        ) == (None, None, None)


def test_option_signal_honors_nested_options_path() -> None:
    from app.services.trading import paper_trading

    assert paper_trading._is_option_signal({"asset_kind": "option"})
    assert paper_trading._is_option_signal(
        {"breakout_alert": {"asset_kind": "options"}},
    )
    assert paper_trading._is_option_signal(
        {"breakout_alert": {"options_path": True}},
    )
    assert paper_trading._is_option_signal(
        {"breakout_alert": {"options_path": "yes"}},
    )
    assert not paper_trading._is_option_signal(
        {"options_path": "false", "breakout_alert": {"options_path": "false"}},
    )


def test_close_paper_trade_nested_options_path_uses_contract_multiplier(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json={"breakout_alert": {"options_path": True}},
    )

    paper_trading._close_paper_trade(trade, 1.45, "target")

    assert trade.pnl == pytest.approx(40.0)
    assert trade.pnl_pct == pytest.approx(16.0)


def test_close_paper_trade_asset_kind_signal_uses_contract_multiplier(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json={"asset_kind": "option"},
    )

    paper_trading._close_paper_trade(trade, 1.45, "target")

    assert trade.pnl == pytest.approx(40.0)
    assert trade.pnl_pct == pytest.approx(16.0)


def test_close_paper_trade_option_uses_contract_multiplier(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    paper_trading._close_paper_trade(trade, 1.45, "target")

    assert trade.pnl == pytest.approx(40.0)
    assert trade.pnl_pct == pytest.approx(16.0)


def test_close_paper_trade_rejects_nonfinite_exit_without_outcome(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with pytest.raises(ValueError, match="invalid paper close inputs"):
        paper_trading._close_paper_trade(trade, float("inf"), "target")

    assert trade.status == "open"
    assert trade.exit_price is None
    assert trade.exit_date is None
    assert trade.pnl is None
    assert trade.pnl_pct is None


def test_close_paper_trade_rejects_boolean_exit_without_outcome(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with pytest.raises(ValueError, match="invalid paper close inputs"):
        paper_trading._close_paper_trade(trade, True, "target")

    assert trade.status == "open"
    assert trade.exit_price is None
    assert trade.exit_date is None
    assert trade.pnl is None
    assert trade.pnl_pct is None


def test_paper_option_mark_uses_option_quote_not_underlying(monkeypatch) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option paper MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ) as quote:
        mark = paper_trading._paper_current_mark_price(trade)

    assert mark == pytest.approx(1.45)
    proxy = quote.call_args.args[0]
    assert proxy.indicator_snapshot["option_meta"]["limit_price"] == pytest.approx(1.25)


def test_paper_option_mark_rejects_nonfinite_option_quote() -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option paper MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": "Infinity",
            "mark_price": "Infinity",
            "source": "robinhood_options",
        },
    ):
        mark = paper_trading._paper_current_mark_price(trade)

    assert mark is None


def test_paper_option_mark_rejects_boolean_option_quote() -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option paper MTM must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": True,
            "mark_price": True,
            "source": "robinhood_options",
        },
    ):
        mark = paper_trading._paper_current_mark_price(trade)

    assert mark is None


def test_paper_option_exit_quote_requires_executable_side() -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.55,
            "mark_price": 2.55,
            "executable_price": 2.05,
            "source": "robinhood_options",
        },
    ) as quote:
        exit_price = paper_trading._paper_current_mark_price(trade, purpose="exit")

    assert exit_price == pytest.approx(2.05)
    assert quote.call_args.kwargs["purpose"] == "exit"


def test_paper_option_exit_refuses_mark_without_executable_side() -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json=_option_signal(),
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.55,
            "mark_price": 2.55,
            "executable_price": None,
            "source": "robinhood_options",
        },
    ):
        exit_price = paper_trading._paper_current_mark_price(trade, purpose="exit")

    assert exit_price is None


def test_check_paper_exits_option_target_waits_for_executable_bid(monkeypatch) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        id=101,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            **_option_signal(),
            "_paper_meta": {"expiry_days": 5, "trailing_enabled": False},
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 2.65,
            "mark_price": 2.65,
            "executable_price": 2.10,
            "source": "robinhood_options",
        },
    ) as quote, patch(
        "app.services.trading.paper_trading._paper_dynamic_monitor_decision",
        return_value=None,
    ):
        result = paper_trading.check_paper_exits(db, user_id=1)

    assert result == {"checked": 1, "closed": 0, "trailing_updated": 0}
    assert trade.status == "open"
    assert db.commits == 0
    assert quote.call_args.kwargs["purpose"] == "exit"


def test_check_paper_exits_option_stop_uses_gapped_executable_bid(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        id=102,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            **_option_signal(),
            "_paper_meta": {"expiry_days": 5, "trailing_enabled": False},
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 0.45,
            "mark_price": 0.45,
            "executable_price": 0.45,
            "source": "robinhood_options",
        },
    ) as quote, patch(
        "app.services.trading.paper_trading._paper_dynamic_monitor_decision",
        return_value=None,
    ):
        result = paper_trading.check_paper_exits(db, user_id=1)

    assert result == {"checked": 1, "closed": 1, "trailing_updated": 0}
    assert trade.status == "closed"
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(0.45)
    assert trade.pnl == pytest.approx(-160.0)
    assert trade.pnl_pct == pytest.approx(-64.0)
    assert db.commits == 1
    assert quote.call_args.kwargs["purpose"] == "exit"


def test_check_paper_exits_option_expiry_no_quote_cancels_without_pnl(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(paper_trading.settings, "backtest_commission", 0.0, raising=False)
    trade = PaperTrade(
        id=103,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow() - timedelta(days=6),
        signal_json={
            **_option_signal(),
            "_paper_meta": {"expiry_days": 5, "trailing_enabled": False},
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 1.25,
            "mark_price": 1.25,
            "executable_price": None,
            "source": "robinhood_options_unavailable",
        },
    ), patch(
        "app.services.trading.paper_trading._apply_slippage",
        side_effect=AssertionError("unquoted expired options must not book a fill"),
    ):
        result = paper_trading.check_paper_exits(db, user_id=1)

    assert result == {"checked": 1, "closed": 0, "trailing_updated": 0}
    assert trade.status == paper_trading.PAPER_TRADE_STATUS_CANCELLED
    assert trade.exit_reason == paper_trading.PAPER_OPTION_EXPIRED_NO_QUOTE_REASON
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.pnl_pct is None
    assert trade.signal_json[paper_trading.PAPER_NO_QUOTE_EXIT_META_KEY][
        "pnl_recorded"
    ] is False
    assert db.commits == 1


def test_shadow_option_stale_janitor_cancels_without_pnl_when_no_executable_quote(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=201,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow() - timedelta(hours=3),
        signal_json={
            **_option_signal(),
            "auto_trader_v1": True,
            "paper_shadow": True,
        },
    )
    db = _FakeDb(rows=[trade])

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={
            "price": 1.25,
            "mark_price": 1.25,
            "executable_price": None,
            "source": "robinhood_options_unavailable",
        },
    ), patch(
        "app.services.trading.paper_trading._apply_slippage",
        side_effect=AssertionError("unquoted stale option shadows must not book a fill"),
    ):
        result = paper_trading.prune_autotrader_paper_shadow_capacity(
            db,
            user_id=1,
            max_open=100,
            max_age_hours=1,
            buffer=5,
        )

    assert result["closed"] == 1
    assert result["cancelled"] == 1
    assert result["stale_closed"] == 0
    assert result["stale_cancelled"] == 1
    assert trade.status == paper_trading.PAPER_TRADE_STATUS_CANCELLED
    assert trade.exit_reason == paper_trading.PAPER_SHADOW_STALE_NO_QUOTE_REASON
    assert trade.exit_price is None
    assert trade.pnl is None
    assert trade.pnl_pct is None
    assert trade.signal_json[paper_trading.PAPER_SHADOW_CAPACITY_EVICTION_META_KEY][
        "pnl_recorded"
    ] is False
    assert db.commits == 1


def test_check_paper_exits_trailing_rejects_wrong_side_long_stop(monkeypatch) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=801,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        stop_price=105.0,
        target_price=200.0,
        quantity=1.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            "_paper_meta": {
                "trailing_enabled": True,
                "atr_value": 1.0,
                "trailing_atr_mult": 2.0,
            }
        },
    )
    db = _FakeDb(rows=[trade])

    monkeypatch.setattr(
        paper_trading,
        "_paper_current_mark_price",
        lambda _pt, purpose="display": 110.0,
    )
    monkeypatch.setattr(paper_trading, "_paper_dynamic_monitor_decision", lambda *_args, **_kwargs: None)

    result = paper_trading.check_paper_exits(db, user_id=1)

    assert result["checked"] == 1
    assert result["closed"] == 0
    assert result["trailing_updated"] == 0
    assert "trailing_stop" not in trade.signal_json["_paper_meta"]
    assert db.commits == 0


def test_check_paper_exits_trailing_updates_with_directional_long_risk(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=802,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=200.0,
        quantity=1.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={
            "_paper_meta": {
                "trailing_enabled": True,
                "atr_value": 1.0,
                "trailing_atr_mult": 2.0,
            }
        },
    )
    db = _FakeDb(rows=[trade])

    monkeypatch.setattr(
        paper_trading,
        "_paper_current_mark_price",
        lambda _pt, purpose="display": 110.0,
    )
    monkeypatch.setattr(paper_trading, "_paper_dynamic_monitor_decision", lambda *_args, **_kwargs: None)

    result = paper_trading.check_paper_exits(db, user_id=1)

    assert result["checked"] == 1
    assert result["closed"] == 0
    assert result["trailing_updated"] == 1
    assert trade.signal_json["_paper_meta"]["trailing_stop"] == pytest.approx(108.0)
    assert db.commits == 1


def test_check_paper_exits_ignores_wrong_side_long_hard_stop(monkeypatch) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=803,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        stop_price=105.0,
        target_price=200.0,
        quantity=1.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={"_paper_meta": {}},
    )
    db = _FakeDb(rows=[trade])

    monkeypatch.setattr(
        paper_trading,
        "_paper_current_mark_price",
        lambda _pt, purpose="display": 104.0,
    )
    monkeypatch.setattr(paper_trading, "_paper_dynamic_monitor_decision", lambda *_args, **_kwargs: None)

    result = paper_trading.check_paper_exits(db, user_id=1)

    assert result["checked"] == 1
    assert result["closed"] == 0
    assert trade.status == "open"
    assert db.commits == 0


def test_check_paper_exits_ignores_wrong_side_long_target(monkeypatch) -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=804,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        target_price=90.0,
        quantity=1.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json={"_paper_meta": {}},
    )
    db = _FakeDb(rows=[trade])

    monkeypatch.setattr(
        paper_trading,
        "_paper_current_mark_price",
        lambda _pt, purpose="display": 100.0,
    )
    monkeypatch.setattr(paper_trading, "_paper_dynamic_monitor_decision", lambda *_args, **_kwargs: None)

    result = paper_trading.check_paper_exits(db, user_id=1)

    assert result["checked"] == 1
    assert result["closed"] == 0
    assert trade.status == "open"
    assert db.commits == 0


def test_shadow_capacity_janitor_counts_serialized_shadow_signal() -> None:
    from app.services.trading import paper_trading

    trade = PaperTrade(
        id=202,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        stop_price=0.60,
        target_price=2.50,
        quantity=2.0,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json=json.dumps({"auto_trader_v1": True, "paper_shadow": True}),
    )
    db = _FakeDb(rows=[trade])

    result = paper_trading.prune_autotrader_paper_shadow_capacity(
        db,
        user_id=1,
        max_open=1,
        max_age_hours=100,
        buffer=0,
    )

    assert result["checked"] == 1
    assert result["capacity_cancelled"] == 1
    assert result["cancelled"] == 1
    assert trade.status == paper_trading.PAPER_TRADE_STATUS_CANCELLED
    assert trade.exit_reason == paper_trading.PAPER_SHADOW_CAPACITY_EVICTED_REASON
    assert isinstance(trade.signal_json, dict)
    assert trade.signal_json[paper_trading.PAPER_SHADOW_CAPACITY_EVICTION_META_KEY][
        "pnl_recorded"
    ] is False
    assert db.commits == 1


def test_auto_enter_option_signal_uses_asset_gate_and_meta_contract_quantity(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_stop_pct",
        50.0,
        raising=False,
    )
    monkeypatch.setattr(
        paper_trading.settings,
        "chili_autotrader_options_exit_tp_pct",
        100.0,
        raising=False,
    )
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "confidence": 0.9,
        "option_meta": {**OPTION_META, "quantity": 2},
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ) as risk_gate, patch(
        "app.services.trading.portfolio_risk.size_position",
        side_effect=AssertionError("option contracts must not use share sizing"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 1
    risk_gate.assert_called_once()
    assert risk_gate.call_args.kwargs.get("asset_type") == "options"
    trade = _paper_rows(db)[0]
    assert trade.quantity == 2
    assert trade.entry_price == pytest.approx(1.25)
    assert trade.stop_price == pytest.approx(0.625)
    assert trade.target_price == pytest.approx(2.50)


def test_auto_enter_option_signal_rejects_fractional_contract_quantity(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "confidence": 0.9,
        "option_meta": {**OPTION_META, "quantity": "1.5"},
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.portfolio_risk.size_position",
        side_effect=AssertionError("option contracts must not use share sizing"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 0
    assert _paper_rows(db) == []


def test_option_contract_sizing_rejects_wrong_side_premium_stop() -> None:
    from app.services.trading import paper_trading

    qty = paper_trading._size_option_contracts(
        10_000.0,
        1.25,
        1.50,
        risk_pct=0.5,
    )

    assert qty == 0


def test_auto_enter_option_signal_rejects_wrong_side_premium_stop(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "stop_price": 1.50,
        "confidence": 0.9,
        "option_meta": dict(OPTION_META),
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 0
    assert _paper_rows(db) == []


def test_auto_enter_option_signal_rejects_unaffordable_contract_size(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "stop_price": 0.75,
        "confidence": 0.9,
        "option_meta": dict(OPTION_META),
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=100.0,
        )

    assert entered == 0
    assert _paper_rows(db) == []


def test_auto_enter_option_signal_sizes_contracts_with_multiplier(
    monkeypatch,
) -> None:
    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading.settings, "backtest_spread", 0.0, raising=False)
    signal = {
        **_option_signal(),
        "ticker": "SPY",
        "entry_price": 1.25,
        "stop_price": 0.75,
        "confidence": 0.9,
        "option_meta": dict(OPTION_META),
    }
    db = _FakeDb()

    with patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, "ok"),
    ), patch(
        "app.services.trading.net_edge_ranker.mode_is_active",
        return_value=False,
    ), patch(
        "app.services.trading.position_sizer_writer.mode_is_active",
        return_value=False,
    ):
        entered = paper_trading.auto_enter_from_signals(
            db,
            user_id=1,
            signals=[signal],
            capital=10_000.0,
        )

    assert entered == 1
    trade = _paper_rows(db)[0]
    assert trade.quantity == 1
    assert trade.signal_json["_paper_meta"]["contract_multiplier"] == 100.0
