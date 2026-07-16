"""TapeWsRecorder — throttle/change detection + day_volume accounting (no DB)."""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.services.trading.momentum_neural.tape_ws_recorder import TapeWsRecorder


def _quote(bid, ask, **extra):
    return SimpleNamespace(
        bid=bid,
        ask=ask,
        price=(bid + ask) / 2,
        timestamp=time.time(),
        **extra,
    )


def _trade(size):
    # TradeSnapshot shape: has size, no bid attr
    return SimpleNamespace(price=2.0, size=size, timestamp=time.time())


def _rec():
    r = TapeWsRecorder()
    r._running = True
    return r


def test_quote_change_buffers_row_with_volume_accounting():
    r = _rec()
    r._vol_base["DSY"] = 1_000_000.0
    r._on_tick("DSY", _trade(5_000))
    r._on_tick("DSY", _trade(2_500))
    r._on_tick("DSY", _quote(2.40, 2.43))
    assert len(r._buffer) == 1
    row = r._buffer[0]
    assert row["symbol"] == "DSY" and row["bid"] == 2.40 and row["ask"] == 2.43
    assert row["day_volume"] == 1_007_500.0  # baseline + ws trades since anchor
    assert 0 < row["spread_bps"] < 200


def test_unchanged_quote_not_rebuffered():
    r = _rec()
    r._on_tick("DSY", _quote(2.40, 2.43))
    r._last_row_t["DSY"] = 0.0  # bypass time throttle; change-detection must hold
    r._on_tick("DSY", _quote(2.40, 2.43))
    assert len(r._buffer) == 1


def test_time_throttle_blocks_rapid_rows():
    r = _rec()
    r._on_tick("DSY", _quote(2.40, 2.43))
    r._on_tick("DSY", _quote(2.41, 2.44))  # changed, but within 1s spacing
    assert len(r._buffer) == 1


def test_crossed_or_empty_quotes_rejected():
    r = _rec()
    r._on_tick("DSY", _quote(2.50, 2.40))  # crossed
    r._on_tick("DSY", SimpleNamespace(bid=None, ask=2.40, price=2.4, timestamp=0))
    assert r._buffer == []


def test_baseline_anchor_resets_ws_accumulation():
    r = _rec()
    r._vol_base["DSY"] = 100.0
    r._vol_ws["DSY"] = 50.0
    # simulate a newer sampler anchor arriving (what _anchor_volume_baselines does)
    from datetime import datetime
    r._vol_base["DSY"] = 9_000.0
    r._vol_base_at["DSY"] = datetime(2026, 6, 11, 12, 0, 0)
    r._vol_ws["DSY"] = 0.0
    r._on_tick("DSY", _quote(2.40, 2.43))
    assert r._buffer[0]["day_volume"] == 9_000.0


def test_not_running_ignores():
    r = _rec()
    r._running = False
    r._on_tick("DSY", _quote(2.40, 2.43))
    assert r._buffer == []


def test_massive_quote_row_preserves_three_clocks_and_connection_identity():
    r = _rec()
    snap = _quote(
        2.40,
        2.43,
        provider_event_at=1_700_000_000.123,
        received_at=1_700_000_000.200,
        available_at=1_700_000_000.250,
        bridge_run_id="10000000-0000-0000-0000-000000000001",
        connection_generation=7,
    )
    r._on_tick("DSY", snap)
    row = r._buffer[0]
    assert row["provider_event_at"].timestamp() == snap.provider_event_at
    assert row["received_at"].timestamp() == snap.received_at
    assert row["available_at"].timestamp() == snap.available_at
    assert row["timestamp_basis"] == "massive_sip_unix_ms"
    assert row["message_type"] == "Q"
    assert row["bridge_run_id"] == snap.bridge_run_id
    assert row["connection_generation"] == 7
