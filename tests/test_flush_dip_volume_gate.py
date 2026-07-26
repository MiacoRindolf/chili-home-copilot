"""Ross-parity L1b (2026-07-25): flush_dip relative-volume gate.

The audit found flush_dip_buy was the only dip/breakout-family fire with NO volume
confirm on its trigger bar (ORB/ABCD/vwap_reclaim all require a spike). The gate reuses
``chili_momentum_pullback_volume_spike_multiple`` (no new number), FAIL-OPEN on
uncomputable data (thin data never blocks — the ORB convention), reject reason
``flush_dip_low_volume``, kill-switch ``chili_momentum_flush_dip_volume_gate_enabled``.

Fixtures are imported from tests/test_momentum_mock_fire_reversal.py (the canonical
flush-dip geometry + the frozen 10:00-ET morning clock) so both suites stay in lockstep.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from app.services.trading.momentum_neural.entry_gates import flush_dip_buy_confirmation
from tests.test_momentum_mock_fire_reversal import (
    _CANDLES,
    _FLUSH_NOW,
    _GATES,
    _flush_arrays,
    _flush_dip_df,
    _flush_settings,
)


def _fire(ms, df, *, gate_on=True):
    _flush_settings(ms)
    ms.chili_momentum_flush_dip_volume_gate_enabled = gate_on
    with patch(f"{_GATES}.compute_all_from_df", return_value=_flush_arrays(len(df))), \
            patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
        return flush_dip_buy_confirmation(
            df, entry_interval="1m", symbol="TEST", db=MagicMock(), now=_FLUSH_NOW,
        )


def test_surging_curl_still_fires():
    # canonical fixture (curl bar 3M vs 1M lead-in = ratio 3.0) -> fires
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, _flush_dip_df())
    assert ok is True, f"surging curl must fire, got {reason} dbg={dbg}"
    assert reason == "flush_dip_buy"
    assert dbg["vol_ratio"] >= 1.5


def test_flat_volume_curl_blocked():
    df = _flush_dip_df()
    df["Volume"] = 1_000_000  # flat -> ratio ~1.0 < 1.5
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, df)
    assert ok is False
    assert reason == "flush_dip_low_volume"
    assert dbg["vol_ratio"] < 1.5


def test_uncomputable_volume_fails_open():
    df = _flush_dip_df()
    df["Volume"] = np.nan  # ratio uncomputable -> fail-OPEN -> fires
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, df)
    assert ok is True, f"uncomputable volume must fail-open, got {reason} dbg={dbg}"
    assert reason == "flush_dip_buy"


def test_flag_off_is_byte_identical():
    df = _flush_dip_df()
    df["Volume"] = 1_000_000  # would be blocked with the gate ON
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, df, gate_on=False)
    assert ok is True, f"flag OFF must skip the gate, got {reason}"
    assert reason == "flush_dip_buy"


def test_missing_flag_skips_added_volume_read():
    ms = SimpleNamespace()
    _flush_settings(ms)
    delattr(ms, "chili_momentum_flush_dip_volume_gate_enabled")
    df = _flush_dip_df()
    df["Volume"] = 1_000_000  # would be rejected if missing silently meant ON
    compute = MagicMock(return_value=_flush_arrays(len(df)))
    with patch(f"{_GATES}.settings", new=ms), \
            patch(f"{_GATES}.compute_all_from_df", compute), \
            patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
        ok, reason, _ = flush_dip_buy_confirmation(
            df,
            entry_interval="1m",
            symbol="TEST",
            db=MagicMock(),
            now=_FLUSH_NOW,
        )
    assert ok is True
    assert reason == "flush_dip_buy"
    assert compute.call_count == 1
