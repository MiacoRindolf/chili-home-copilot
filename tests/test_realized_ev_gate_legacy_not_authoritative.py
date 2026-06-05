"""The realized-EV gate must treat the conflated *legacy* stat columns as
NON-authoritative: only clean ``corrected_*`` evidence may pre-empt the clean
``raw_realized_*`` fallback.

Pattern 1246 is the live case this fixes -- ``corrected_*`` is NULL (every one
of its closed trades carries a dirty reconcile/sync-gone exit, so the cleaned
writer can't compute a corrected stat), the legacy column reads -0.14% (7 dirty
sync-gone rows whose real broker fills were positive), and ``raw_realized_*``
reads +2.66% over 12 clean rows. Before this fix the legacy -0.14% (n=7 >= 5)
counted as a "sufficient" sample and blocked the clean +2.66% fallback -- a
FALSE net-loser block. The clean corrected LIVE-loss path must still block
(safety preserved).
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.trading.realized_ev_gate as gate


def _fake_settings_get(name, default):
    return {
        "chili_realized_ev_gate_enabled": True,
        "chili_realized_ev_min_trades": 5,
        "chili_realized_ev_min_avg_return_pct": 0.0,
        "chili_realized_ev_min_win_rate": 0.0,
        "chili_realized_ev_gate_allow_raw_fallback": True,
    }.get(name, default)


def _pat(**kw):
    base = dict(
        id=1,
        corrected_trade_count=None, corrected_win_rate=None, corrected_avg_return_pct=None,
        trade_count=None, win_rate=None, avg_return_pct=None,
        raw_realized_trade_count=None, raw_realized_win_rate=None, raw_realized_avg_return_pct=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_legacy_only_loss_falls_back_to_clean_raw_realized(monkeypatch):
    # Pattern 1246: corrected NULL, legacy loss (n=7), clean raw +2.66% (n=12).
    monkeypatch.setattr(gate, "_settings_get", _fake_settings_get)
    pat = _pat(
        trade_count=7, win_rate=0.8571, avg_return_pct=-0.14,           # legacy (dirty)
        raw_realized_trade_count=12, raw_realized_win_rate=0.5833, raw_realized_avg_return_pct=2.66,
    )
    res = gate.evaluate_realized_ev(pat)
    assert res.passed is True, res.reasons
    assert res.snapshot["stats_source"] == "raw_realized_fallback"
    assert res.snapshot["sample_from_corrected"] is False
    assert res.snapshot["raw_realized_fallback_used"] is True


def test_clean_corrected_loss_still_blocks_despite_positive_raw(monkeypatch):
    # Safety: a genuine clean corrected LIVE loss is NOT overridden by raw/paper.
    monkeypatch.setattr(gate, "_settings_get", _fake_settings_get)
    pat = _pat(
        corrected_trade_count=6, corrected_win_rate=0.2, corrected_avg_return_pct=-3.0,
        trade_count=6, win_rate=0.2, avg_return_pct=-3.0,
        raw_realized_trade_count=10, raw_realized_win_rate=0.7, raw_realized_avg_return_pct=5.0,
    )
    res = gate.evaluate_realized_ev(pat)
    assert res.passed is False
    assert res.snapshot["sample_from_corrected"] is True
    assert res.snapshot["raw_realized_fallback_used"] is False
    assert any("realized_avg_return_not_positive" in r for r in res.reasons)


def test_legacy_loss_with_negative_raw_still_blocks(monkeypatch):
    # Falling back to raw must still BLOCK when the clean raw signal is negative.
    monkeypatch.setattr(gate, "_settings_get", _fake_settings_get)
    pat = _pat(
        trade_count=7, win_rate=0.4, avg_return_pct=-0.14,              # legacy
        raw_realized_trade_count=8, raw_realized_win_rate=0.25, raw_realized_avg_return_pct=-1.5,
    )
    res = gate.evaluate_realized_ev(pat)
    assert res.passed is False
    assert res.snapshot["stats_source"] == "raw_realized_fallback"
    assert any("realized_avg_return_not_positive" in r for r in res.reasons)


def test_clean_corrected_win_passes_without_touching_raw(monkeypatch):
    monkeypatch.setattr(gate, "_settings_get", _fake_settings_get)
    pat = _pat(
        corrected_trade_count=6, corrected_win_rate=0.6, corrected_avg_return_pct=2.0,
        trade_count=6, win_rate=0.6, avg_return_pct=2.0,
        raw_realized_trade_count=10, raw_realized_win_rate=0.1, raw_realized_avg_return_pct=-9.0,
    )
    res = gate.evaluate_realized_ev(pat)
    assert res.passed is True, res.reasons
    assert res.snapshot["stats_source"] == "corrected_or_legacy"
    assert res.snapshot["sample_from_corrected"] is True
