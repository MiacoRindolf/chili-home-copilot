"""Comprehensive tests for the Trading module: models, service CRUD, and API routes."""
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
import pandas as pd
from app.models import User, Device
from app.models.trading import (
    WatchlistItem, Trade, JournalEntry, TradingInsight,
    ScanResult, BacktestResult, MarketSnapshot, LearningEvent,
    ScanPattern, PatternTradeRow,
)
from app.services import trading_service as ts
from app.pairing import DEVICE_COOKIE_NAME


def _make_paired(db):
    """Create a paired user+device and return (user, token)."""
    user = User(name="TradeUser")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = "trade-test-tok"
    db.add(Device(token=token, user_id=user.id, label="test", client_ip_last="127.0.0.1"))
    db.commit()
    return user, token


# ── Model Tests ──────────────────────────────────────────────────────────────


class TestTradingModels:
    def test_create_watchlist_item(self, db):
        item = WatchlistItem(user_id=1, ticker="AAPL")
        db.add(item)
        db.commit()
        db.refresh(item)
        assert item.id is not None
        assert item.ticker == "AAPL"
        assert item.added_at is not None

    def test_create_trade(self, db):
        trade = Trade(
            user_id=1, ticker="TSLA", direction="long",
            entry_price=200.0, quantity=10,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        assert trade.id is not None
        assert trade.status == "open"
        assert trade.pnl is None

    def test_create_short_trade(self, db):
        trade = Trade(
            user_id=1, ticker="SPY", direction="short",
            entry_price=450.0, quantity=5,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        assert trade.direction == "short"

    def test_create_journal_entry(self, db):
        entry = JournalEntry(user_id=1, content="Market looks bullish", trade_id=None)
        db.add(entry)
        db.commit()
        db.refresh(entry)
        assert entry.id is not None
        assert entry.created_at is not None

    def test_create_trading_insight(self, db):
        sp = ScanPattern(
            name="Test insight pattern",
            rules_json="{}",
            origin="test",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        insight = TradingInsight(
            user_id=1,
            scan_pattern_id=sp.id,
            pattern_description="RSI oversold bounce on AAPL",
            confidence=0.75,
            evidence_count=5,
        )
        db.add(insight)
        db.commit()
        db.refresh(insight)
        assert insight.id is not None
        assert insight.active is True

    def test_create_scan_result(self, db):
        scan = ScanResult(
            user_id=1, ticker="NVDA", score=8.5, signal="buy",
            entry_price=500.0, stop_loss=480.0, take_profit=550.0,
            risk_level="medium", rationale="Strong momentum",
        )
        db.add(scan)
        db.commit()
        db.refresh(scan)
        assert scan.id is not None

    def test_create_backtest_result(self, db):
        bt = BacktestResult(
            user_id=1, ticker="AAPL", strategy_name="sma_cross",
            return_pct=15.2, win_rate=0.6, max_drawdown=-8.3, trade_count=25,
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        assert bt.id is not None

    def test_create_market_snapshot(self, db):
        snap = MarketSnapshot(
            ticker="AAPL", snapshot_date=datetime.utcnow(),
            close_price=185.50,
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        assert snap.id is not None

    def test_create_learning_event(self, db):
        ev = LearningEvent(
            user_id=1, event_type="discovery",
            description="New bullish pattern found",
            confidence_before=0.5, confidence_after=0.65,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        assert ev.id is not None


# ── Service CRUD Tests ───────────────────────────────────────────────────────


class TestWatchlistService:
    def test_add_to_watchlist(self, db):
        item = ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        assert item.ticker == "AAPL"
        assert item.user_id == 1

    def test_add_duplicate_returns_existing(self, db):
        item1 = ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        item2 = ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        assert item1.id == item2.id

    def test_add_normalizes_ticker(self, db):
        item = ts.add_to_watchlist(db, user_id=1, ticker="aapl")
        assert item.ticker == "AAPL"

    def test_get_watchlist(self, db):
        ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        ts.add_to_watchlist(db, user_id=1, ticker="TSLA")
        items = ts.get_watchlist(db, user_id=1)
        assert len(items) == 2
        tickers = {i.ticker for i in items}
        assert "AAPL" in tickers
        assert "TSLA" in tickers

    def test_remove_from_watchlist(self, db):
        ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        removed = ts.remove_from_watchlist(db, user_id=1, ticker="AAPL")
        assert removed is True
        items = ts.get_watchlist(db, user_id=1)
        assert len(items) == 0

    def test_remove_nonexistent_returns_false(self, db):
        removed = ts.remove_from_watchlist(db, user_id=1, ticker="NOPE")
        assert removed is False

    def test_watchlist_isolation_per_user(self, db):
        ts.add_to_watchlist(db, user_id=1, ticker="AAPL")
        ts.add_to_watchlist(db, user_id=2, ticker="TSLA")
        assert len(ts.get_watchlist(db, user_id=1)) == 1
        assert len(ts.get_watchlist(db, user_id=2)) == 1


class TestTradeService:
    def test_create_trade(self, db):
        trade = ts.create_trade(
            db, user_id=1,
            ticker="AAPL", direction="long",
            entry_price=150.0, quantity=10,
        )
        assert trade.id is not None
        assert trade.status == "open"
        assert trade.ticker == "AAPL"

    def test_close_trade_long_profit(self, db):
        trade = ts.create_trade(
            db, user_id=1,
            ticker="AAPL", direction="long",
            entry_price=100.0, quantity=10,
        )
        closed = ts.close_trade(db, trade.id, user_id=1, exit_price=120.0)
        assert closed is not None
        assert closed.status == "closed"
        assert closed.pnl == pytest.approx(200.0, abs=0.01)

    def test_close_trade_long_loss(self, db):
        trade = ts.create_trade(
            db, user_id=1,
            ticker="AAPL", direction="long",
            entry_price=100.0, quantity=10,
        )
        closed = ts.close_trade(db, trade.id, user_id=1, exit_price=90.0)
        assert closed.pnl == pytest.approx(-100.0, abs=0.01)

    def test_close_trade_short_profit(self, db):
        trade = ts.create_trade(
            db, user_id=1,
            ticker="AAPL", direction="short",
            entry_price=100.0, quantity=10,
        )
        closed = ts.close_trade(db, trade.id, user_id=1, exit_price=80.0)
        assert closed.pnl == pytest.approx(200.0, abs=0.01)

    def test_close_already_closed_returns_none(self, db):
        trade = ts.create_trade(
            db, user_id=1, ticker="AAPL", direction="long",
            entry_price=100.0, quantity=10,
        )
        ts.close_trade(db, trade.id, user_id=1, exit_price=120.0)
        result = ts.close_trade(db, trade.id, user_id=1, exit_price=130.0)
        assert result is None

    def test_close_wrong_user_returns_none(self, db):
        trade = ts.create_trade(
            db, user_id=1, ticker="AAPL", direction="long",
            entry_price=100.0, quantity=10,
        )
        result = ts.close_trade(db, trade.id, user_id=999, exit_price=120.0)
        assert result is None

    def test_get_trades_all(self, db):
        ts.create_trade(db, user_id=1, ticker="AAPL", direction="long", entry_price=100.0, quantity=1)
        ts.create_trade(db, user_id=1, ticker="TSLA", direction="long", entry_price=200.0, quantity=1)
        trades = ts.get_trades(db, user_id=1)
        assert len(trades) == 2

    def test_get_trades_by_status(self, db):
        t = ts.create_trade(db, user_id=1, ticker="AAPL", direction="long", entry_price=100.0, quantity=1)
        ts.close_trade(db, t.id, user_id=1, exit_price=110.0)
        ts.create_trade(db, user_id=1, ticker="TSLA", direction="long", entry_price=200.0, quantity=1)

        open_trades = ts.get_trades(db, user_id=1, status="open")
        closed_trades = ts.get_trades(db, user_id=1, status="closed")
        assert len(open_trades) == 1
        assert open_trades[0].ticker == "TSLA"
        assert len(closed_trades) == 1
        assert closed_trades[0].ticker == "AAPL"


class TestJournalService:
    def test_add_journal_entry(self, db):
        entry = ts.add_journal_entry(db, user_id=1, content="Market analysis")
        assert entry.id is not None
        assert entry.content == "Market analysis"

    def test_add_journal_linked_to_trade(self, db):
        trade = ts.create_trade(
            db, user_id=1, ticker="AAPL", direction="long",
            entry_price=100.0, quantity=1,
        )
        entry = ts.add_journal_entry(db, user_id=1, content="Entry note", trade_id=trade.id)
        assert entry.trade_id == trade.id

    def test_get_journal(self, db):
        ts.add_journal_entry(db, user_id=1, content="Note 1")
        ts.add_journal_entry(db, user_id=1, content="Note 2")
        entries = ts.get_journal(db, user_id=1)
        assert len(entries) == 2

    def test_journal_isolation_per_user(self, db):
        ts.add_journal_entry(db, user_id=1, content="User 1 note")
        ts.add_journal_entry(db, user_id=2, content="User 2 note")
        assert len(ts.get_journal(db, user_id=1)) == 1
        assert len(ts.get_journal(db, user_id=2)) == 1


class TestTradeStats:
    def test_empty_stats(self, db):
        stats = ts.get_trade_stats(db, user_id=1)
        assert stats["total_trades"] == 0

    def test_stats_with_closed_trades(self, db):
        t1 = ts.create_trade(db, user_id=1, ticker="AAPL", direction="long", entry_price=100.0, quantity=10)
        ts.close_trade(db, t1.id, user_id=1, exit_price=120.0)

        t2 = ts.create_trade(db, user_id=1, ticker="TSLA", direction="long", entry_price=200.0, quantity=5)
        ts.close_trade(db, t2.id, user_id=1, exit_price=180.0)

        stats = ts.get_trade_stats(db, user_id=1)
        assert stats["total_trades"] == 2


class TestClampPeriod:
    def test_daily_any_period(self):
        assert ts._clamp_period("1d", "6mo") == "6mo"
        assert ts._clamp_period("1d", "1y") == "1y"

    def test_1m_clamped(self):
        result = ts._clamp_period("1m", "6mo")
        assert result in ("1d", "5d")

    def test_1h_valid(self):
        assert ts._clamp_period("1h", "3mo") == "3mo"


# ── API Route Tests ──────────────────────────────────────────────────────────


class TestTradingPageAPI:
    @patch("app.services.trading_service.should_run_learning", return_value=False)
    def test_trading_page_loads(self, mock_learn, client):
        resp = client.get("/trading")
        assert resp.status_code == 200
        assert "Trading" in resp.text


class TestWatchlistAPI:
    def test_get_empty_watchlist(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/watchlist")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_add_and_get_watchlist(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/trading/watchlist", json={"ticker": "AAPL"})
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "AAPL"

        resp2 = client.get("/api/trading/watchlist")
        assert len(resp2.json()["items"]) == 1

    def test_remove_from_watchlist(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        client.post("/api/trading/watchlist", json={"ticker": "AAPL"})
        resp = client.delete("/api/trading/watchlist?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestTradesAPI:
    def test_get_empty_trades(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/trades")
        assert resp.status_code == 200
        assert resp.json()["trades"] == []

    def test_create_trade(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/trading/trades", json={
            "ticker": "AAPL",
            "direction": "long",
            "entry_price": 150.0,
            "quantity": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["ticker"] == "AAPL"

    def test_close_trade(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/trading/trades", json={
            "ticker": "AAPL", "direction": "long",
            "entry_price": 100.0, "quantity": 10,
        })
        trade_id = resp.json()["id"]
        resp2 = client.post(f"/api/trading/trades/{trade_id}/close", json={
            "exit_price": 120.0,
        })
        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True
        assert resp2.json()["pnl"] == pytest.approx(200.0, abs=0.01)

    def test_close_nonexistent_trade(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/trading/trades/9999/close", json={"exit_price": 100.0})
        assert resp.status_code == 404


class TestJournalAPI:
    def test_get_empty_journal(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/journal")
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    def test_add_journal_entry(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/trading/journal", json={
            "content": "Market analysis note",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_get_journal_stats(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/journal/stats")
        assert resp.status_code == 200
        assert "total_trades" in resp.json()


class TestMarketDataAPI:
    @patch("app.services.trading_service._yf_history")
    def test_ohlcv_returns_data(self, mock_hist, client):
        mock_df = pd.DataFrame({
            "Open": [100.0], "High": [105.0], "Low": [99.0],
            "Close": [103.0], "Volume": [1000000],
        }, index=pd.to_datetime(["2026-03-01"]))
        mock_hist.return_value = mock_df

        resp = client.get("/api/trading/ohlcv?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["data"]) == 1
        assert data["data"][0]["close"] == 103.0

    @patch("app.services.trading_service._yf_history")
    def test_ohlcv_empty_data(self, mock_hist, client):
        mock_hist.return_value = pd.DataFrame()
        resp = client.get("/api/trading/ohlcv?ticker=NOPE")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    @patch("app.services.trading_service._yf_fast_info")
    def test_quote_returns_data(self, mock_info, client):
        mock_info.return_value = {
            "last_price": 185.50,
            "previous_close": 183.0,
            "day_high": 187.0,
            "day_low": 182.0,
            "volume": 5000000,
            "market_cap": 2800000000000,
        }
        resp = client.get("/api/trading/quote?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["price"] == 185.50

    @patch("app.services.trading_service._yf_fast_info")
    def test_quote_returns_null_for_unknown(self, mock_info, client):
        mock_info.return_value = None
        resp = client.get("/api/trading/quote?ticker=NOPE")
        assert resp.status_code == 200
        assert resp.json()["price"] is None


class TestInsightsAPI:
    def test_get_empty_insights(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/insights")
        assert resp.status_code == 200
        assert resp.json()["insights"] == []

    def test_get_insights_with_data(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        sp = ScanPattern(
            name="Insights API pat",
            rules_json="{}",
            origin="test",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        db.add(TradingInsight(
            user_id=user.id,
            scan_pattern_id=sp.id,
            pattern_description="RSI oversold bounce",
            confidence=0.8, evidence_count=3,
        ))
        db.commit()
        resp = client.get("/api/trading/insights")
        data = resp.json()
        assert len(data["insights"]) == 1
        assert data["insights"][0]["confidence"] == 0.8


class TestPortfolioAPI:
    def test_portfolio_empty(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/portfolio")
        assert resp.status_code == 200


class TestBrainAPI:
    def test_brain_stats(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/brain/stats")
        assert resp.status_code == 200

    def test_brain_activity(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/brain/activity")
        assert resp.status_code == 200
        assert "events" in resp.json()

    def test_brain_worker_status_includes_insight_counts(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/brain/worker/status")
        assert resp.status_code == 200
        j = resp.json()
        assert "trading_insights_null_user_count" in j
        assert "trading_insights_total_count" in j
        assert "brain_default_user_id" in j

    def test_learned_patterns(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/learn/patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "demoted" in data

    def test_tradeable_patterns_empty(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/brain/tradeable-patterns")
        assert resp.status_code == 200
        j = resp.json()
        assert j["ok"] is True
        assert j["patterns"] == []
        assert "filters" in j

    def test_tradeable_patterns_respects_gates(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        p_ok = ScanPattern(
            name="PromoOK",
            rules_json='{"conditions":[]}',
            origin="brain_discovered",
            promotion_status="promoted",
            active=True,
            oos_win_rate=55.0,
            oos_trade_count=10,
            backtest_count=10,
        )
        p_low_wr = ScanPattern(
            name="PromoWeakWR",
            rules_json='{"conditions":[]}',
            origin="brain_discovered",
            promotion_status="promoted",
            active=True,
            oos_win_rate=40.0,
            oos_trade_count=10,
        )
        p_legacy = ScanPattern(
            name="LegacyHighStats",
            rules_json='{"conditions":[]}',
            origin="user",
            promotion_status="legacy",
            active=True,
            oos_win_rate=99.0,
            oos_trade_count=99,
        )
        db.add_all([p_ok, p_low_wr, p_legacy])
        db.commit()
        db.refresh(p_ok)
        db.refresh(p_low_wr)
        db.refresh(p_legacy)
        resp = client.get("/api/trading/brain/tradeable-patterns?min_oos_wr=50&min_trades=5")
        assert resp.status_code == 200
        ids = [x["id"] for x in resp.json()["patterns"]]
        assert p_ok.id in ids
        assert p_low_wr.id not in ids
        assert p_legacy.id not in ids

    def test_learned_patterns_includes_global_null_user_insights(self, db, client):
        """Worker/scheduler insights with user_id NULL appear for logged-in users."""
        user, token = _make_paired(db)
        sp = ScanPattern(
            name="GlobalInsightPat",
            description="test",
            rules_json="{}",
            origin="user",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        ins = TradingInsight(
            user_id=None,
            scan_pattern_id=sp.id,
            pattern_description="GlobalInsightPat — from worker",
            confidence=0.72,
            evidence_count=1,
            active=True,
        )
        db.add(ins)
        db.commit()
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/learn/patterns")
        assert resp.status_code == 200
        row = next((p for p in resp.json()["active"] if p.get("id") == ins.id), None)
        assert row is not None
        assert row.get("insight_scope") == "global"

    def test_learned_patterns_includes_bt_aggregate_metrics(self, db, client):
        """Pattern list exposes summed simulated trades and worst drawdown across deduped backtests."""
        from datetime import datetime

        user, token = _make_paired(db)
        sp = ScanPattern(
            name="AggPatMetrics",
            description="test",
            rules_json="{}",
            origin="user",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        ins = TradingInsight(
            user_id=user.id,
            scan_pattern_id=sp.id,
            pattern_description="AggPatMetrics — bullish setup",
            confidence=0.75,
            evidence_count=2,
        )
        db.add(ins)
        db.commit()
        db.refresh(ins)
        now = datetime.utcnow()
        db.add(
            BacktestResult(
                user_id=user.id,
                ticker="SPY",
                strategy_name="dynamic_pattern",
                return_pct=2.0,
                win_rate=60.0,
                max_drawdown=-4.0,
                trade_count=5,
                related_insight_id=ins.id,
                scan_pattern_id=sp.id,
                ran_at=now,
            )
        )
        db.add(
            BacktestResult(
                user_id=user.id,
                ticker="QQQ",
                strategy_name="dynamic_pattern",
                return_pct=1.0,
                win_rate=55.0,
                max_drawdown=-8.5,
                trade_count=7,
                related_insight_id=ins.id,
                scan_pattern_id=sp.id,
                ran_at=now,
            )
        )
        db.commit()
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/learn/patterns")
        assert resp.status_code == 200
        data = resp.json()
        row = next((p for p in data["active"] if p.get("scan_pattern_id") == sp.id), None)
        assert row is not None
        assert row.get("bt_total_trades") == 12
        assert row.get("bt_worst_max_drawdown") == -8.5
        assert "oos_trade_count" in row

    @patch("app.services.backtest_service._fetch_ohlcv_df")
    def test_benchmark_walk_forward_evaluate_smoke(self, mock_fetch):
        import numpy as np
        import pandas as pd

        n = 200
        idx = pd.date_range("2020-01-01", periods=n, freq="D")
        mock_fetch.return_value = pd.DataFrame({
            "Open": np.full(n, 100.0),
            "High": np.full(n, 101.0),
            "Low": np.full(n, 99.0),
            "Close": np.linspace(99.0, 110.0, n),
            "Volume": np.full(n, 1e6),
        }, index=idx)
        from app.services.backtest_service import benchmark_walk_forward_evaluate

        out = benchmark_walk_forward_evaluate(
            conditions=[{"indicator": "price", "op": ">", "value": 0}],
            pattern_name="wf_smoke",
            exit_config=None,
            tickers=["SPY"],
            period="5y",
            interval="1d",
            n_windows=4,
            min_bars_per_window=35,
            min_positive_fold_ratio=0.01,
        )
        assert out.get("ok") is True
        assert "SPY" in out.get("tickers", {})
        assert "passes_gate" in out
        assert out["tickers"]["SPY"].get("n_windows", 0) >= 2

    def test_brain_apply_bench_promotion_gate(self, monkeypatch):
        from app import config
        from app.services.trading.learning import brain_apply_bench_promotion_gate

        monkeypatch.setattr(config.settings, "brain_bench_walk_forward_gate_enabled", True)
        s, ok = brain_apply_bench_promotion_gate(
            origin="brain_discovered",
            bench_summary={"ok": True, "passes_gate": False},
            current_promotion_status="promoted",
        )
        assert s == "rejected_bench"
        assert ok is False
        s2, ok2 = brain_apply_bench_promotion_gate(
            origin="brain_discovered",
            bench_summary={"ok": True, "passes_gate": True},
            current_promotion_status="promoted",
        )
        assert s2 is None
        assert ok2 is True
        s3, ok3 = brain_apply_bench_promotion_gate(
            origin="user",
            bench_summary={"ok": True, "passes_gate": False},
            current_promotion_status="promoted",
        )
        assert s3 is None
        assert ok3 is True

    def test_learned_patterns_bench_fold_summary(self, db, client):
        user, token = _make_paired(db)
        sp = ScanPattern(
            name="BenchPatCard",
            description="test",
            rules_json="{}",
            origin="user",
            bench_walk_forward_json={
                "passes_gate": True,
                "evaluated_at": "2025-01-01T00:00:00Z",
                "tickers": {
                    "SPY": {"positive_return_windows": 5, "n_windows": 8},
                    "QQQ": {"positive_return_windows": 4, "n_windows": 8},
                },
            },
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        ins = TradingInsight(
            user_id=user.id,
            scan_pattern_id=sp.id,
            pattern_description="BenchPatCard — bullish",
            confidence=0.8,
            evidence_count=1,
        )
        db.add(ins)
        db.commit()
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/learn/patterns")
        assert resp.status_code == 200
        row = next((p for p in resp.json()["active"] if p.get("scan_pattern_id") == sp.id), None)
        assert row is not None
        assert row.get("bench_fold_summary")
        assert "SPY" in row["bench_fold_summary"]
        assert row.get("bench_passes_gate") is True

    @patch("app.services.backtest_service._fetch_ohlcv_df")
    def test_pattern_backtest_with_insight_id_not_404(self, mock_fetch, db, client):
        """Using insight id in pattern backtest must not return 404 when insight exists and resolves."""
        import pandas as pd
        import numpy as np
        user, token = _make_paired(db)
        sp = ScanPattern(
            name="Test RSI pattern",
            description="RSI oversold",
            rules_json='{"conditions":[{"indicator":"rsi_14","op":"<","value":35}]}',
            origin="user",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        insight = TradingInsight(
            user_id=user.id,
            scan_pattern_id=sp.id,
            pattern_description="RSI oversold bounce",
            confidence=0.8,
            evidence_count=3,
        )
        db.add(insight)
        db.commit()
        db.refresh(insight)
        dates = pd.date_range("2024-01-01", periods=60, freq="D")
        mock_fetch.return_value = pd.DataFrame({
            "Open": np.random.uniform(180, 200, 60),
            "High": np.random.uniform(200, 210, 60),
            "Low": np.random.uniform(175, 185, 60),
            "Close": np.linspace(185, 205, 60),
            "Volume": np.random.randint(1_000_000, 5_000_000, 60),
        }, index=dates)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post(
            f"/api/trading/patterns/{insight.id}/backtest",
            params={"ticker": "AAPL", "period": "1y", "interval": "1d"},
            json={},
        )
        assert resp.status_code != 404, "Pattern not found when using insight id (UI sends insight:id)"
        data = resp.json()
        assert "ok" in data
        if data.get("ok"):
            assert "ticker" in data and "strategy" in data

    def test_stored_backtest_trades_get_and_post(self, db, client):
        """Chill compat: /api/trading-brain/.../trades and learn alias return JSON trades."""
        user, token = _make_paired(db)
        sp = ScanPattern(
            name="Stored BT pat",
            rules_json="{}",
            origin="test",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        insight = TradingInsight(
            user_id=user.id,
            scan_pattern_id=sp.id,
            pattern_description="Test pattern",
            confidence=0.7,
            evidence_count=2,
        )
        db.add(insight)
        db.commit()
        db.refresh(insight)
        bt = BacktestResult(
            user_id=user.id,
            ticker="NKT",
            strategy_name="dynamic_pattern",
            return_pct=5.0,
            win_rate=62.5,
            max_drawdown=-3.0,
            trade_count=2,
            related_insight_id=insight.id,
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        db.add(
            PatternTradeRow(
                user_id=user.id,
                related_insight_id=insight.id,
                backtest_result_id=bt.id,
                ticker="NKT",
                as_of_ts=datetime.utcnow(),
                timeframe="1d",
                features_json={"schema": "1", "entry_price": 10.0, "exit_price": 10.5},
            )
        )
        db.commit()

        client.cookies.set(DEVICE_COOKIE_NAME, token)
        for path in (
            f"/api/trading-brain/brain/backtest/{bt.id}/trades",
            f"/api/trading/learn/backtest/{bt.id}/trades",
        ):
            for method in ("get", "post"):
                resp = getattr(client, method)(path)
                assert resp.status_code == 200, f"{method.upper()} {path}: {resp.text}"
                assert resp.headers.get("content-type", "").startswith("application/json")
                data = resp.json()
                assert data.get("ok") is True
                assert data.get("backtest_id") == bt.id
                assert len(data.get("trades", [])) == 1
                assert data["trades"][0]["ticker"] == "NKT"
                assert data["trades"][0]["features"].get("entry_price") == 10.0


class TestTopPicksFreshness:
    """Tests for top picks freshness metadata and recheck."""

    def test_top_picks_returns_freshness_metadata(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/api/trading/top-picks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "picks" in data
        assert "as_of" in data
        assert "age_seconds" in data
        assert "is_stale" in data

    @patch("app.services.trading.market_data.fetch_quote")
    def test_pick_recheck_valid(self, mock_fetch, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        mock_fetch.return_value = {"price": 150.0}
        resp = client.post(
            "/api/trading/top-picks/recheck",
            json={"ticker": "AAPL", "entry_price": 150.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "valid"
        assert data["live_price"] == 150.0
        assert data["drift_pct"] == 0

    @patch("app.services.trading.market_data.fetch_quote")
    def test_pick_recheck_invalidated(self, mock_fetch, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        mock_fetch.return_value = {"price": 200.0}  # 33% drift from 150
        resp = client.post(
            "/api/trading/top-picks/recheck",
            json={"ticker": "AAPL", "entry_price": 150.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "invalidated"
        assert data["drift_pct"] == pytest.approx(33.33, abs=0.5)

    def test_pick_recheck_requires_ticker(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post(
            "/api/trading/top-picks/recheck",
            json={"entry_price": 150.0},
        )
        assert resp.status_code == 422  # validation error

    def test_pick_recheck_requires_entry_price(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post(
            "/api/trading/top-picks/recheck",
            json={"ticker": "AAPL"},
        )
        assert resp.status_code == 422


class TestProposalFreshness:
    """Tests for proposal freshness metadata and recheck."""

    def test_proposals_contain_freshness_fields(self, db, client):
        from app.models.trading import StrategyProposal
        from datetime import datetime, timedelta

        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        prop = StrategyProposal(
            user_id=user.id,
            ticker="AAPL",
            direction="long",
            status="pending",
            entry_price=150.0,
            stop_loss=140.0,
            take_profit=170.0,
            proposed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=1),
            projected_profit_pct=13.3,
            projected_loss_pct=-6.7,
            risk_reward_ratio=2.0,
            confidence=75,
            timeframe="swing",
            thesis="Test thesis",
        )
        db.add(prop)
        db.commit()

        resp = client.get("/api/trading/proposals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["proposals"]) >= 1
        p = data["proposals"][0]
        assert "age_seconds" in p
        assert "expires_in_seconds" in p
        assert "is_expired" in p
        assert "expiry_reason" in p

    @patch("app.services.trading.market_data.fetch_quote")
    def test_proposal_recheck_returns_drift(self, mock_fetch, db, client):
        from app.models.trading import StrategyProposal
        from datetime import datetime, timedelta

        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        prop = StrategyProposal(
            user_id=user.id,
            ticker="AAPL",
            direction="long",
            status="pending",
            entry_price=150.0,
            stop_loss=140.0,
            take_profit=170.0,
            proposed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=1),
            projected_profit_pct=13.3,
            projected_loss_pct=-6.7,
            risk_reward_ratio=2.0,
            confidence=75,
            timeframe="swing",
            thesis="Test",
        )
        db.add(prop)
        db.commit()
        proposal_id = prop.id

        mock_fetch.return_value = {"price": 155.0}  # ~3.3% drift
        resp = client.post(f"/api/trading/proposals/{proposal_id}/recheck")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "live_price" in data
        assert "drift_pct" in data
        assert "status" in data


# ── TCA (transaction cost / slippage) ───────────────────────────────────────


class TestTcaService:
    def test_entry_slippage_bps_long(self):
        from app.services.trading.tca_service import entry_slippage_bps

        assert entry_slippage_bps(100.0, 100.5, "long") == 50.0
        assert entry_slippage_bps(100.0, 99.5, "long") == -50.0

    def test_entry_slippage_bps_short(self):
        from app.services.trading.tca_service import entry_slippage_bps

        assert entry_slippage_bps(100.0, 99.5, "short") == 50.0
        assert entry_slippage_bps(100.0, 100.5, "short") == -50.0

    def test_apply_tca_prefers_avg_fill_price(self):
        from app.services.trading.tca_service import apply_tca_on_trade_fill

        t = MagicMock()
        t.tca_reference_entry_price = 100.0
        t.avg_fill_price = 100.1
        t.entry_price = 999.0
        t.direction = "long"
        apply_tca_on_trade_fill(t)
        assert t.tca_entry_slippage_bps == 10.0

    def test_tca_summary_guest_returns_empty(self, db):
        from app.services.trading.tca_service import tca_summary_by_ticker

        r = tca_summary_by_ticker(db, None, days=30)
        assert r["ok"] is True
        assert r["overall_fills"] == 0
        assert r["by_ticker"] == []
        assert r["exit_overall_closes"] == 0
        assert r["exit_by_ticker"] == []

    def test_exit_slippage_bps_long(self):
        from app.services.trading.tca_service import exit_slippage_bps

        assert exit_slippage_bps(100.0, 99.5, "long") == 50.0
        assert exit_slippage_bps(100.0, 100.5, "long") == -50.0

    def test_tca_summary_aggregates(self, db):
        from app.services.trading.tca_service import tca_summary_by_ticker

        user, _ = _make_paired(db)
        now = datetime.utcnow()
        for bps in (10.0, 30.0):
            tr = Trade(
                user_id=user.id,
                ticker="AAPL",
                direction="long",
                entry_price=150.0,
                quantity=1.0,
                entry_date=now,
                filled_at=now,
                tca_entry_slippage_bps=bps,
            )
            db.add(tr)
        db.commit()
        r = tca_summary_by_ticker(db, user.id, days=7)
        assert r["overall_fills"] == 2
        assert r["overall_avg_entry_slippage_bps"] == 20.0
        assert len(r["by_ticker"]) == 1
        assert r["by_ticker"][0]["ticker"] == "AAPL"


class TestTcaAPI:
    def test_tca_summary_endpoint(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        now = datetime.utcnow()
        db.add(
            Trade(
                user_id=user.id,
                ticker="MSFT",
                direction="long",
                entry_price=400.0,
                quantity=1.0,
                entry_date=now,
                filled_at=now,
                tca_entry_slippage_bps=5.0,
            )
        )
        db.commit()
        resp = client.get("/api/trading/tca/summary?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["overall_fills"] == 1
        assert data["by_ticker"][0]["ticker"] == "MSFT"
        assert "exit_overall_closes" in data


class TestAttributionAPI:
    def test_live_vs_research_endpoint(self, db, client):
        from app.models.trading import ScanPattern

        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        pat = ScanPattern(
            name="Test pat",
            rules_json="{}",
            origin="user",
            win_rate=55.0,
            oos_win_rate=60.0,
            promotion_status="promoted",
        )
        db.add(pat)
        db.commit()
        db.refresh(pat)
        now = datetime.utcnow()
        db.add(
            Trade(
                user_id=user.id,
                ticker="XOM",
                direction="long",
                entry_price=50.0,
                quantity=2.0,
                entry_date=now,
                exit_date=now,
                exit_price=55.0,
                status="closed",
                pnl=10.0,
                scan_pattern_id=pat.id,
                tca_entry_slippage_bps=2.0,
                tca_exit_slippage_bps=-1.0,
            )
        )
        db.commit()
        resp = client.get("/api/trading/attribution/live-vs-research?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["patterns"]) == 1
        row = data["patterns"][0]
        assert row["scan_pattern_id"] == pat.id
        assert row["live_closed_trades"] == 1
        assert row["live_win_rate_pct"] == 100.0
        assert row["research_oos_win_rate_pct"] == 60.0
