"""Unit tests for app.services.trading_summary.build_trading_summary.

The three DB-query helpers are patched so the aggregation/shaping logic is tested
fast (no DB). Faithful Trade stand-ins via SimpleNamespace.
"""
from types import SimpleNamespace
from unittest.mock import patch

from app.services import trading_summary as tsum


def _trade(**kw):
    base = {
        "ticker": "X", "status": "closed", "pnl": 0.0, "scan_pattern_id": None,
        "exit_reason": "", "direction": "long",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _summary(closed, opens=None, names=None, user_id=1, window_hours=24):
    with patch.object(tsum, "_closed_trades", return_value=closed), \
         patch.object(tsum, "_open_trades", return_value=opens or []), \
         patch.object(tsum, "_pattern_names", return_value=names or {}):
        return tsum.build_trading_summary(object(), user_id, window_hours=window_hours)


class TestBuildTradingSummary:
    def test_no_user_returns_empty(self):
        assert tsum.build_trading_summary(object(), None) == {}

    def test_net_pnl_and_win_rate(self):
        closed = [_trade(ticker="A", pnl=10.0), _trade(ticker="B", pnl=-4.0),
                  _trade(ticker="C", pnl=6.0)]
        s = _summary(closed)
        assert s["net_pnl"] == 12.0
        # 2 of 3 winners -> 0.666...
        assert round(s["win_rate"], 3) == round(2 / 3, 3)
        assert len(s["closes"]) == 3
        assert s["closes"][0]["ticker"] == "A"

    def test_pattern_names_resolved_and_top_patterns_sorted(self):
        closed = [
            _trade(ticker="A", pnl=10.0, scan_pattern_id=585),
            _trade(ticker="B", pnl=5.0, scan_pattern_id=585),
            _trade(ticker="C", pnl=20.0, scan_pattern_id=537),
        ]
        s = _summary(closed, names={585: "Wedge", 537: "Reclaim"})
        # Closes show resolved names.
        assert s["closes"][0]["pattern"] == "Wedge"
        # Top patterns: 537 (20) before 585 (15), names resolved, trade counts.
        tp = s["top_patterns"]
        assert tp[0]["id"] == "Reclaim" and tp[0]["pnl"] == 20.0 and tp[0]["trades"] == 1
        assert tp[1]["id"] == "Wedge" and tp[1]["pnl"] == 15.0 and tp[1]["trades"] == 2

    def test_pattern_id_fallback_when_name_missing(self):
        closed = [_trade(ticker="A", pnl=1.0, scan_pattern_id=999)]
        s = _summary(closed, names={})
        assert s["closes"][0]["pattern"] == "999"

    def test_open_positions_listed_without_unrealized(self):
        opens = [_trade(ticker="EKSO", status="open", direction="long"),
                 _trade(ticker="BTC", status="open", direction="short")]
        s = _summary([], opens=opens)
        assert s["open_positions"] == [
            {"ticker": "EKSO", "side": "long"}, {"ticker": "BTC", "side": "short"}]

    def test_no_closes_nulls_pnl_and_winrate(self):
        s = _summary([])
        assert s["net_pnl"] is None
        assert s["win_rate"] is None
        assert s["closes"] == []
        assert s["top_patterns"] == []

    def test_null_pnl_excluded_from_aggregates(self):
        closed = [_trade(ticker="A", pnl=None), _trade(ticker="B", pnl=8.0)]
        s = _summary(closed)
        assert s["net_pnl"] == 8.0       # the None-pnl trade doesn't count
        assert s["win_rate"] == 1.0      # only the one with pnl, which won
        assert len(s["closes"]) == 2     # but both still listed as closes

    def test_top_patterns_capped_at_5(self):
        closed = [_trade(ticker=f"T{i}", pnl=float(i), scan_pattern_id=i)
                  for i in range(1, 9)]
        s = _summary(closed, names={i: f"P{i}" for i in range(1, 9)})
        assert len(s["top_patterns"]) == 5
        assert s["top_patterns"][0]["id"] == "P8"  # highest pnl first

    def test_short_window_omits_date(self):
        s = _summary([_trade(pnl=1.0)], window_hours=6)
        assert s["date"] is None
