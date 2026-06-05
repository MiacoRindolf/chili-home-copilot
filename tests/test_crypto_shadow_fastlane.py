"""Tests for the CRYPTO extension of the shadow-observation backtest fastlane.

Main's _queue_shadow_stock_fastlane_for_observation previously hard-excluded
non-stock assets (so crypto shadow observations never earned graduation
evidence). This generalizes the asset gate to {stock, crypto} and emits the
correct per-asset asset_class, while inheriting the existing reboost cooldown
(the flood guard) unchanged.
"""
from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import patch

import app.services.trading.auto_trader as at
from app.models.trading import BreakoutAlert, ScanPattern

_EMIT = "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern"
_INVALIDATE = "app.services.trading.backtest_queue.invalidate_queue_status_cache"


def _settings_stub(**overrides):
    base = dict(
        chili_autotrader_shadow_stock_fastlane_enabled=True,
        chili_autotrader_shadow_stock_fastlane_lifecycle_stages="",  # empty -> no lifecycle gate
        chili_autotrader_shadow_stock_fastlane_min_expected_net_pct=0.0,
        chili_autotrader_shadow_stock_fastlane_reboost_cooldown_minutes=0.0,
        chili_autotrader_shadow_stock_fastlane_backtest_priority=5,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _alert(asset_type, ticker="BTC-USD", pattern_id=1, alert_id=10):
    a = BreakoutAlert()
    a.id = alert_id
    a.asset_type = asset_type
    a.ticker = ticker
    a.scan_pattern_id = pattern_id
    return a


def _pattern(pattern_id=1, lifecycle="candidate", last_backtest_at=None, priority=0):
    p = ScanPattern()
    p.id = pattern_id
    p.lifecycle_stage = lifecycle
    p.last_backtest_at = last_backtest_at
    p.backtest_priority = priority
    p.promotion_status = None
    return p


def _call(db, alert, pattern):
    return at._queue_shadow_stock_fastlane_for_observation(
        db,
        alert=alert,
        pattern=pattern,
        reason="selector:shadow_observation_signal_lane",
        snap={"entry_edge": {"expected_net_pct": 0.05}},
    )


def test_crypto_observation_fastlanes_with_crypto_asset_class(db, monkeypatch):
    monkeypatch.setattr(at, "settings", _settings_stub())
    captured = {}

    def _fake_emit(db_, pid, *, source, asset_class=None, expected_evidence_value=None, payload=None, **kw):
        captured.update(asset_class=asset_class, source=source, pattern_id=pid)
        return 555

    with patch(_EMIT, side_effect=_fake_emit), patch(_INVALIDATE, lambda: None):
        res = _call(db, _alert("crypto", ticker="BTC-USD"), _pattern())
    assert res is not None and res.get("queued") is True
    assert captured["asset_class"] == "crypto"
    assert captured["pattern_id"] == 1


def test_stock_observation_unchanged(db, monkeypatch):
    monkeypatch.setattr(at, "settings", _settings_stub())
    captured = {}

    def _fake_emit(db_, pid, *, source, asset_class=None, **kw):
        captured["asset_class"] = asset_class
        return 556

    with patch(_EMIT, side_effect=_fake_emit), patch(_INVALIDATE, lambda: None):
        res = _call(db, _alert("stock", ticker="AAPL"), _pattern())
    assert res is not None and res.get("queued") is True
    assert captured["asset_class"] == "stock"


def test_other_asset_type_excluded(db, monkeypatch):
    monkeypatch.setattr(at, "settings", _settings_stub())
    called = {"n": 0}

    def _fake_emit(*a, **k):
        called["n"] += 1
        return 1

    with patch(_EMIT, side_effect=_fake_emit):
        res = _call(db, _alert("option", ticker="AAPL"), _pattern())
    assert res is None
    assert called["n"] == 0


def test_crypto_inherits_reboost_cooldown_flood_guard(db, monkeypatch):
    # Crypto must respect the same post-backtest cooldown as stock (no per-tick flood).
    monkeypatch.setattr(
        at, "settings", _settings_stub(chili_autotrader_shadow_stock_fastlane_reboost_cooldown_minutes=60.0)
    )
    called = {"n": 0}

    def _fake_emit(*a, **k):
        called["n"] += 1
        return 1

    with patch(_EMIT, side_effect=_fake_emit):
        # last backtest just now -> within the 60-min cooldown
        res = _call(db, _alert("crypto"), _pattern(last_backtest_at=datetime.utcnow()))
    assert res is not None and res.get("reason") == "recent_backtest_cooldown"
    assert called["n"] == 0
