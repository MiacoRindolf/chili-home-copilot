"""Cold-start tick-stream volume confirmation (2026-07-18, VIVS 2026-07-15).

VERIFIED DEFECT: after the spread fix (633feeb) removed all wide_bbo_spread
blocks, the VIVS 2026-07-15 replay (+90% in a minute, $1.1M+/min on tape, a
Ross-class winner) STILL got zero entries — 4,257 ``live_entry_trigger_wait``
events with ``reason=insufficient_bars`` (sink chili_weekend_test, session 2).
``momentum_volume_confirmation`` demands >= 25 OHLCV bars (20-bar volume
average + EMA-9 seed) on the 15m frame the live runner feeds it, and a freshly
ignited cold symbol has NO bar history by construction — the fastest winners
(the ones the ignition detector nominates seconds after first frames) were
unenterable for hours.

THE FIX (additive-only): below 25 bars the SAME confirmation intent is computed
from the tick tape (``iqfeed_trade_ticks``): trailing-60s $-volume and
trailing-10s prints vs adaptive floors (max(base, surge_mult x the symbol's own
(t-300s, t-60s] baseline)) + last price above the 60s tick-VWAP and the 60s
low. Declines fall through to the exact legacy ``(False, "insufficient_bars")``;
frames with >= 25 bars never reach the fallback (byte-identical).

Fixture numbers are REAL, pulled read-only from the prod tape
(``iqfeed_trade_ticks``, prod chili):
  * VIVS first tape minute 12:07 UTC: 6,766 prints, $3,594,838, price 3.00→3.64
  * VIVS 12:07:50–12:08:00 burst: 5,252 prints, $2,867,542 in TEN seconds
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from app.services.trading.momentum_neural import entry_gates as eg


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _frame(n: int, *, spike_last_vol: float | None = None) -> pd.DataFrame:
    """Synthetic rising OHLCV frame (mirrors test_momentum_edge_market_data)."""
    idx = pd.date_range("2026-07-15 09:30", periods=n, freq="15min")
    c = np.linspace(10.0, 11.0, n)
    v = [1000.0] * n
    if spike_last_vol is not None:
        v[-1] = float(spike_last_vol)
    return pd.DataFrame(
        {"Open": c, "High": c + 0.05, "Low": c - 0.05, "Close": c, "Volume": v},
        index=idx,
    )


# VIVS-shaped aggregate row in the wrapper's SQL column order:
# (last_price, dollar_60, shares_60, min_60, prints_10, base_dollar, base_prints, span_s)
# 12:07:47 subscribe → ~45s of tape; $3.59M/60s; 5,252 prints in the 10s burst;
# vwap_60 = 3_594_838 / 1_090_000 ≈ 3.298 < last 3.53 (buyers in control).
_VIVS_ROW = (3.53, 3_594_838.0, 1_090_000.0, 3.00, 5252, 0.0, 0, 45.0)

_VIVS_STATS = {
    "last_price": 3.53,
    "vwap_60": 3_594_838.0 / 1_090_000.0,
    "min_60": 3.00,
    "dollar_60": 3_594_838.0,
    "prints_10": 5252,
    "base_dollar": 0.0,
    "base_prints": 0,
    "span_s": 45.0,
}


class _FakeAggDB:
    """``db.execute(...).fetchone()`` -> one canned aggregate row (the SQL seam)."""

    def __init__(self, row):
        self._row = row
        self.calls = 0

    def execute(self, *_a, **_k):
        self.calls += 1
        row = self._row

        class _R:
            def fetchone(self_inner):
                return row

        return _R()


class _RaisingDB:
    def execute(self, *_a, **_k):
        raise RuntimeError("tape read failed")


_AS_OF = datetime(2026, 7, 15, 12, 8, 0)


def _decide(stats, **over):
    kw = {"dollar_vol_base": 150_000.0, "prints_base": 20.0, "surge_mult": 4.0}
    kw.update(over)
    return eg._tick_stream_volume_decision(dict(stats), **kw)


# ──────────────────────────────────────────────────────────────────────────────
# (1) Pure decision — the VIVS-shaped cold frame ADMITS, thin tape WAITS
# ──────────────────────────────────────────────────────────────────────────────

def test_vivs_shaped_cold_frame_admits():
    """The exact defect case: a 45s-old tape printing $3.59M/60s with a 5,252-print
    10s burst, last 3.53 above the 3.298 tick-VWAP — must ADMIT. This is the frame
    that collected 4,257 insufficient_bars waits with zero entries."""
    ok, reason, dbg = _decide(_VIVS_STATS)
    assert ok is True
    assert reason == "momentum_ok_tick_stream"
    assert dbg["dollar_vol_threshold"] == 150_000.0  # cold tape keeps the base floor
    assert dbg["prints_threshold"] == 20.0


def test_thin_cold_frame_dollar_vol_waits():
    """A thin cold frame (low $-vol) must still WAIT: $8k/60s is flat small-cap
    tape (<$50k/min per the ignition-floor derivation), not an ignition."""
    ok, reason, _ = _decide({
        "last_price": 2.05, "vwap_60": 2.02, "min_60": 2.00,
        "dollar_60": 8_000.0, "prints_10": 25,
        "base_dollar": 0.0, "base_prints": 0, "span_s": 45.0,
    })
    assert ok is False
    assert reason == "tick_fallback_dollar_vol_below_floor"


def test_prints_floor_blocks_few_big_prints():
    """A handful of huge block prints can clear the $-vol floor without genuine
    breadth — the 10s print-rate floor must still block (few prints = no crowd)."""
    ok, reason, _ = _decide({
        "last_price": 5.10, "vwap_60": 5.00, "min_60": 4.90,
        "dollar_60": 500_000.0, "prints_10": 4,
        "base_dollar": 0.0, "base_prints": 0, "span_s": 45.0,
    })
    assert ok is False
    assert reason == "tick_fallback_prints_below_floor"


def test_below_vwap_blocks_backside_distribution():
    """Heavy tape with last price BELOW the 60s tick-VWAP is distribution/backside
    (sellers in control) — the EMA-9-analog trend check must block. Numbers are
    VIVS's own 12:09 backside minute shape (heavy $-vol, price under VWAP)."""
    ok, reason, _ = _decide({
        "last_price": 2.63, "vwap_60": 2.75, "min_60": 2.63,
        "dollar_60": 3_830_769.0, "prints_10": 1500,
        "base_dollar": 0.0, "base_prints": 0, "span_s": 45.0,
    })
    assert ok is False
    assert reason == "tick_fallback_below_vwap"


def test_inconsistent_aggregates_no_uptick_fail_closed():
    """Defensive: aggregates where last == min_60 while vwap reads lower (an
    inconsistent/degenerate read) must fail CLOSED on the uptick check — the
    fallback can never admit on unreadable aggregates."""
    ok, reason, _ = _decide({
        "last_price": 2.00, "vwap_60": 1.90, "min_60": 2.00,
        "dollar_60": 500_000.0, "prints_10": 100,
        "base_dollar": 0.0, "base_prints": 0, "span_s": 45.0,
    })
    assert ok is False
    assert reason == "tick_fallback_no_uptick"


def test_missing_tape_fail_closed():
    """No last price / no vwap (empty window) -> no_tape, never an admit."""
    ok, reason, _ = _decide({
        "last_price": None, "vwap_60": None, "min_60": None,
        "dollar_60": 0.0, "prints_10": 0,
        "base_dollar": 0.0, "base_prints": 0, "span_s": 0.0,
    })
    assert ok is False
    assert reason == "tick_fallback_no_tape"


# ──────────────────────────────────────────────────────────────────────────────
# (2) Adaptive floors — the base is a FLOOR, the symbol's own baseline raises it
# ──────────────────────────────────────────────────────────────────────────────

def test_established_baseline_raises_floor_adaptively():
    """A name that already traded $1M over its 240s pre-surge baseline needs a
    genuine 4x surge (threshold $1M/60s), so a $300k/60s window — above the base
    floor but NOT a surge for THIS name — must wait. Floors, not ceilings."""
    ok, reason, dbg = _decide({
        "last_price": 5.20, "vwap_60": 5.05, "min_60": 4.95,
        "dollar_60": 300_000.0, "prints_10": 200,
        "base_dollar": 1_000_000.0, "base_prints": 100, "span_s": 300.0,
    })
    assert ok is False
    assert reason == "tick_fallback_dollar_vol_below_floor"
    assert dbg["dollar_vol_threshold"] == pytest.approx(4.0 * 1_000_000.0 / 240.0 * 60.0)


def test_short_span_keeps_documented_base_floor():
    """The same baseline numbers but only 90s of tape (< the 120s baseline minimum)
    must NOT raise the bar — a cold name falls back to the documented floors and
    the $300k/60s window admits."""
    ok, reason, dbg = _decide({
        "last_price": 5.20, "vwap_60": 5.05, "min_60": 4.95,
        "dollar_60": 300_000.0, "prints_10": 200,
        "base_dollar": 1_000_000.0, "base_prints": 100, "span_s": 90.0,
    })
    assert ok is True
    assert reason == "momentum_ok_tick_stream"
    assert dbg["dollar_vol_threshold"] == 150_000.0


# ──────────────────────────────────────────────────────────────────────────────
# (3) DB wrapper — availability guards + the SQL seam
# ──────────────────────────────────────────────────────────────────────────────

def test_wrapper_vivs_aggregate_row_admits():
    db = _FakeAggDB(_VIVS_ROW)
    ok, reason, dbg = eg.tick_stream_volume_confirmation("VIVS", db=db, as_of=_AS_OF)
    assert ok is True
    assert reason == "momentum_ok_tick_stream"
    assert db.calls == 1
    assert dbg["prints_10"] == 5252


def test_wrapper_requires_db_and_equity_symbol():
    ok, reason, _ = eg.tick_stream_volume_confirmation("VIVS", db=None, as_of=_AS_OF)
    assert (ok, reason) == (False, "tick_fallback_unavailable")
    ok, reason, _ = eg.tick_stream_volume_confirmation("", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF)
    assert (ok, reason) == (False, "tick_fallback_unavailable")
    # Crypto has no equity tick tape — it rides the existing OFI/flow path.
    ok, reason, _ = eg.tick_stream_volume_confirmation(
        "BTC-USD", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF
    )
    assert (ok, reason) == (False, "tick_fallback_unavailable")


def test_wrapper_kill_switch_off_declines():
    stub = SimpleNamespace(
        chili_momentum_tick_vol_fallback_enabled=False,
    )
    ok, reason, _ = eg.tick_stream_volume_confirmation(
        "VIVS", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF, settings_obj=stub
    )
    assert (ok, reason) == (False, "tick_fallback_disabled")


def test_wrapper_read_error_fail_closed():
    ok, reason, _ = eg.tick_stream_volume_confirmation("VIVS", db=_RaisingDB(), as_of=_AS_OF)
    assert (ok, reason) == (False, "tick_fallback_read_error")


def test_wrapper_empty_tape_row_fail_closed():
    ok, reason, _ = eg.tick_stream_volume_confirmation(
        "VIVS", db=_FakeAggDB((None,) * 8), as_of=_AS_OF
    )
    assert ok is False
    assert reason == "tick_fallback_no_tape"


# ──────────────────────────────────────────────────────────────────────────────
# (4) Gate integration — the fallback only ADMITS what the bar gate starves
# ──────────────────────────────────────────────────────────────────────────────

def test_cold_frame_vivs_tape_admits_via_fallback():
    """A 5-bar cold frame + the VIVS tape -> the gate ADMITS via the tick stream."""
    ok, reason = eg.momentum_volume_confirmation(
        _frame(5), symbol="VIVS", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF
    )
    assert ok is True
    assert reason == "momentum_ok_tick_stream"


def test_cold_frame_without_db_keeps_legacy_insufficient_bars():
    """No db/symbol threaded (legacy caller shape) -> the exact legacy decline."""
    ok, reason = eg.momentum_volume_confirmation(_frame(5))
    assert (ok, reason) == (False, "insufficient_bars")
    ok, reason = eg.momentum_volume_confirmation(None)
    assert (ok, reason) == (False, "insufficient_bars")


def test_cold_frame_thin_tape_declines_with_legacy_reason():
    """A thin cold tape must decline with the LEGACY reason string — consumers
    that match on insufficient_bars (telemetry, replay regression) see the exact
    pre-fix surface; the fallback's internal decline reason never leaks."""
    thin_row = (2.05, 8_000.0, 4_000.0, 2.00, 3, 0.0, 0, 45.0)
    ok, reason = eg.momentum_volume_confirmation(
        _frame(5), symbol="THIN", db=_FakeAggDB(thin_row), as_of=_AS_OF
    )
    assert (ok, reason) == (False, "insufficient_bars")


def test_warm_frame_path_byte_identical_and_fallback_never_invoked(monkeypatch):
    """>= 25-bar frames must take the exact legacy path: same (ok, reason) with or
    without symbol/db threaded, and the tick fallback is NEVER invoked (spy raises
    if touched)."""
    def _boom(*_a, **_k):
        raise AssertionError("tick fallback must not run on a >=25-bar frame")

    monkeypatch.setattr(eg, "tick_stream_volume_confirmation", _boom)

    # (a) a no-fire warm frame (constant volume): legacy volume_below_1p5x_avg
    flat = _frame(30)
    legacy = eg.momentum_volume_confirmation(flat)
    wired = eg.momentum_volume_confirmation(
        flat, symbol="VIVS", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF
    )
    assert legacy == wired == (False, "volume_below_1p5x_avg")

    # (b) a firing warm frame (last-bar volume spike on a rising close)
    hot = _frame(30, spike_last_vol=5000.0)
    legacy = eg.momentum_volume_confirmation(hot)
    wired = eg.momentum_volume_confirmation(
        hot, symbol="VIVS", db=_FakeAggDB(_VIVS_ROW), as_of=_AS_OF
    )
    assert legacy == wired
    assert legacy[0] is True
    assert legacy[1] in ("momentum_ok_rel_vol", "momentum_ok_abs_vol")


# ──────────────────────────────────────────────────────────────────────────────
# (5) Real-Postgres SQL validation — the aggregate query against the real table
# ──────────────────────────────────────────────────────────────────────────────

def _insert_ticks(db, symbol: str, ticks) -> None:
    for at, price, size in ticks:
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size) "
                "VALUES (:s, :at, :p, :z)"
            ),
            {"s": symbol, "at": at, "p": price, "z": size},
        )
    db.commit()


def _cleanup_ticks(db, symbol: str) -> None:
    try:
        db.rollback()
        db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE symbol = :s"), {"s": symbol})
        db.commit()
    except Exception:
        db.rollback()


def test_real_db_vivs_shaped_tape_admits(db):
    """End-to-end against real Postgres: a VIVS-shaped cold tape (60 prints,
    ~$157k/60s, 25-print final-10s burst, rising 3.00→3.53) inserted into
    iqfeed_trade_ticks ADMITS through the actual SQL aggregate — this pins the
    CTE/FILTER/make_interval/CAST query itself, not a fake row."""
    sym = "TVFVIVS"
    try:
        as_of = datetime(2026, 7, 15, 12, 8, 0)
        prices = np.linspace(3.00, 3.53, 60)
        ticks = []
        # 35 prints across 12:07:05–12:07:49 (subscribe-cold: 55s of tape)
        for i in range(35):
            at = as_of - timedelta(seconds=55) + timedelta(seconds=i * 44.0 / 35.0)
            ticks.append((at, float(prices[i]), 800.0))
        # 25-print burst inside the final 10s (12:07:50–12:08:00)
        for j in range(25):
            at = as_of - timedelta(seconds=10) + timedelta(seconds=j * 9.5 / 25.0)
            ticks.append((at, float(prices[35 + j]), 800.0))
        _insert_ticks(db, sym, ticks)

        ok, reason, dbg = eg.tick_stream_volume_confirmation(sym, db=db, as_of=as_of)
        assert ok is True, dbg
        assert reason == "momentum_ok_tick_stream"
        assert dbg["prints_10"] >= 20
        assert dbg["dollar_60"] >= 150_000.0
        # And through the gate itself with a 5-bar cold frame:
        ok, reason = eg.momentum_volume_confirmation(_frame(5), symbol=sym, db=db, as_of=as_of)
        assert (ok, reason) == (True, "momentum_ok_tick_stream")
    finally:
        _cleanup_ticks(db, sym)


def test_real_db_thin_tape_waits(db):
    """A genuinely thin cold tape (8 prints, ~$160 traded) must keep WAITING with
    the legacy decline through the gate."""
    sym = "TVFTHIN"
    try:
        as_of = datetime(2026, 7, 15, 12, 8, 0)
        ticks = [
            (as_of - timedelta(seconds=50) + timedelta(seconds=i * 6), 2.0 + i * 0.001, 10.0)
            for i in range(8)
        ]
        _insert_ticks(db, sym, ticks)

        ok, reason, _ = eg.tick_stream_volume_confirmation(sym, db=db, as_of=as_of)
        assert ok is False
        assert reason == "tick_fallback_dollar_vol_below_floor"
        ok, reason = eg.momentum_volume_confirmation(_frame(5), symbol=sym, db=db, as_of=as_of)
        assert (ok, reason) == (False, "insufficient_bars")
    finally:
        _cleanup_ticks(db, sym)


# ──────────────────────────────────────────────────────────────────────────────
# (6) Config surface — ONE documented base per knob, default ON
# ──────────────────────────────────────────────────────────────────────────────

def test_knob_defaults_documented():
    from app.config import Settings

    f = Settings.model_fields
    assert f["chili_momentum_tick_vol_fallback_enabled"].default is True
    assert f["chili_momentum_tick_vol_fallback_dollar_vol_base"].default == 150_000.0
    assert f["chili_momentum_tick_vol_fallback_prints_base"].default == 20
    assert f["chili_momentum_tick_vol_fallback_surge_mult"].default == 4.0
