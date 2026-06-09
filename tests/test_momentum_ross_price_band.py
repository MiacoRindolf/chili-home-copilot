"""Ross small-cap LIVE-ARM instrument-class gate.

Large-caps (MU/MRVL on an earnings breakout) go ``live_eligible`` in
``momentum_symbol_viability`` via the broad brain momentum scoring, which is NOT
price-screened. ``_fresh_live_eligible_candidates`` ranks by viability alone, so a
$100 semi would out-rank a real $3 Ross gapper and be armed with real money. These
tests pin the price-band gate that enforces the lane's instrument CLASS at the
selection gate, reusing the profile's existing ``price_min``/``price_max`` knobs
(no new thresholds). Pure logic — no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.trading.momentum_neural.auto_arm as aa
import app.services.trading.momentum_neural.universe as uni
from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    symbols_within_profile_price_band,
)


def _snap(rows: dict[str, float]) -> list[dict]:
    """Build a Massive-style full-market snapshot from {ticker: last_price}."""
    return [{"ticker": t, "lastTrade": {"p": p}} for t, p in rows.items()]


# ── pure helper: symbols_within_profile_price_band ────────────────────────────
def test_keeps_only_in_band_small_caps():
    snap = _snap({"NPT": 3.5, "BYAH": 12.0, "MU": 105.0, "MRVL": 72.0, "TSLA": 250.0})
    kept, ok = symbols_within_profile_price_band(
        ["NPT", "BYAH", "MU", "MRVL", "TSLA"], EQUITY_ROSS_SMALLCAP, snapshot=snap
    )
    assert ok is True
    assert kept == {"NPT", "BYAH"}  # $1-$20 only; the $72-$250 large-caps dropped


def test_drops_below_floor_and_above_ceiling():
    snap = _snap({"PENNY": 0.40, "EDGE_LO": 1.0, "EDGE_HI": 20.0, "OVER": 20.01})
    kept, ok = symbols_within_profile_price_band(
        ["PENNY", "EDGE_LO", "EDGE_HI", "OVER"], EQUITY_ROSS_SMALLCAP, snapshot=snap
    )
    assert ok is True
    # inclusive bounds kept; sub-$1 manipulative tape and >$20 dropped
    assert kept == {"EDGE_LO", "EDGE_HI"}


def test_unknown_price_is_dropped_fail_safe():
    # A live-eligible name absent from the snapshot (no confirmable price) must NOT
    # survive into a live-arm pool.
    snap = _snap({"NPT": 3.5})
    kept, ok = symbols_within_profile_price_band(
        ["NPT", "GHOST"], EQUITY_ROSS_SMALLCAP, snapshot=snap
    )
    assert ok is True
    assert kept == {"NPT"}


def test_total_snapshot_outage_signals_not_ok():
    kept, ok = symbols_within_profile_price_band(
        ["NPT", "MU"], EQUITY_ROSS_SMALLCAP, snapshot=[]
    )
    assert ok is False  # caller must decide (the lane fails safe)
    assert kept == set()


def test_empty_input_is_ok_noop():
    kept, ok = symbols_within_profile_price_band([], EQUITY_ROSS_SMALLCAP, snapshot=[])
    assert ok is True
    assert kept == set()


def test_profile_without_band_is_noop():
    from app.services.trading.momentum_neural.universe import UniverseProfile

    no_band = UniverseProfile(
        profile_id="x", asset_class="equity", label="no band",
        price_min=None, price_max=None,
    )
    kept, ok = symbols_within_profile_price_band(
        ["MU", "TSLA"], no_band, snapshot=_snap({"MU": 105.0})
    )
    assert ok is True
    assert kept == {"MU", "TSLA"}  # no price class declared -> keep all


def test_uses_day_close_when_no_last_trade():
    # Premarket rows can lack lastTrade; the helper falls back to day close / vwap.
    snap = [{"ticker": "NPT", "day": {"c": 4.2}}, {"ticker": "MU", "day": {"c": 101.0}}]
    kept, ok = symbols_within_profile_price_band(["NPT", "MU"], EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert ok is True
    assert kept == {"NPT"}


# ── wrapper: _enforce_ross_price_band (auto_arm) ──────────────────────────────
def _rows(*symbols):
    return [SimpleNamespace(symbol=s) for s in symbols]


def test_wrapper_filters_rows_to_kept(monkeypatch):
    # The wrapper does `from .universe import symbols_within_profile_price_band`
    # at call time, so the patch must land on the SOURCE module (universe), not aa.
    monkeypatch.setattr(
        uni, "symbols_within_profile_price_band",
        lambda syms, profile: ({"NPT"}, True), raising=True,
    )
    out = aa._enforce_ross_price_band(_rows("NPT", "MU", "MRVL"))
    assert [r.symbol for r in out] == ["NPT"]  # large-caps dropped, order preserved


def test_wrapper_fails_safe_on_snapshot_outage(monkeypatch):
    monkeypatch.setattr(
        uni, "symbols_within_profile_price_band",
        lambda syms, profile: (set(), False), raising=True,
    )
    out = aa._enforce_ross_price_band(_rows("NPT", "MU"))
    assert out == []  # outage -> arm nothing (no large-cap leak)


def test_wrapper_fails_safe_on_helper_error(monkeypatch):
    def _boom(syms, profile):
        raise RuntimeError("snapshot client exploded")

    monkeypatch.setattr(uni, "symbols_within_profile_price_band", _boom, raising=True)
    out = aa._enforce_ross_price_band(_rows("NPT", "MU"))
    assert out == []  # helper error -> fail safe, never leak large-caps live
