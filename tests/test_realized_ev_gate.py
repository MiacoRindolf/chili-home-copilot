from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.realized_ev_gate import evaluate_realized_ev


def _patch_gate_settings(monkeypatch, *, min_trades: int = 5, raw_fallback: bool = True):
    monkeypatch.setattr(
        "app.services.trading.realized_ev_gate._settings_get",
        lambda key, default: {
            "chili_realized_ev_min_avg_return_pct": 0.0,
            "chili_realized_ev_min_win_rate": 0.0,
            "chili_realized_ev_min_trades": min_trades,
            "chili_realized_ev_gate_allow_raw_fallback": raw_fallback,
            "chili_realized_ev_gate_enabled": True,
        }.get(key, default),
    )


def test_realized_ev_gate_uses_raw_realized_fallback_for_thin_corrected_sample(monkeypatch):
    _patch_gate_settings(monkeypatch, min_trades=5)
    pat = SimpleNamespace(
        corrected_trade_count=None,
        corrected_win_rate=None,
        corrected_avg_return_pct=None,
        trade_count=0,
        win_rate=None,
        avg_return_pct=None,
        raw_realized_trade_count=5,
        raw_realized_win_rate=0.6,
        raw_realized_avg_return_pct=1.25,
    )

    result = evaluate_realized_ev(pat)

    assert result.passed is True
    assert result.snapshot["stats_source"] == "raw_realized_fallback"
    assert result.snapshot["raw_realized_fallback_used"] is True
    assert result.snapshot["trade_count"] == 5


def test_realized_ev_gate_does_not_let_raw_fallback_override_corrected_live_loss(monkeypatch):
    _patch_gate_settings(monkeypatch, min_trades=5)
    pat = SimpleNamespace(
        corrected_trade_count=1,
        corrected_win_rate=0.0,
        corrected_avg_return_pct=-2.0,
        trade_count=1,
        win_rate=0.0,
        avg_return_pct=-2.0,
        raw_realized_trade_count=8,
        raw_realized_win_rate=0.75,
        raw_realized_avg_return_pct=3.0,
    )

    result = evaluate_realized_ev(pat)

    assert result.passed is False
    assert result.snapshot["stats_source"] == "corrected_or_legacy"
    assert result.snapshot["raw_realized_fallback_used"] is False
    assert (
        result.snapshot["raw_realized_fallback_blocked_reason"]
        == "corrected_live_loss_takes_precedence"
    )
