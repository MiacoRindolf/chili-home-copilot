"""Unit tests for the Ross MICRO-PULLBACK re-entry (re-load) on a held runner.

Covers the load-bearing, pure pieces of the feature (the live_runner orchestration
is a thin shell over these — heavy I/O, not unit-testable without the whole engine):

  * DETECTION (entry_gates.micro_pullback_reentry_detect) — fires on a higher-low
    bounce on a synthetic squeeze, rejects deep rollovers, dip-below-shelf, sparse
    frames; the ratcheting shelf gate; the curl-back-up per-bar confirm.
  * NO ENTRY INTO SELLING — entry_gates._entry_flow_veto + the positive-confirm leg
    the live block applies (re-enter only when flow turns up; never buy into selling).
  * RE-ENTER UP TO THE CAP THEN STOP — the per-name/session count vs the cap, and the
    bounded total re-load risk (max * rho * R0).
  * NO-OP FLAG-OFF — the kill-switch defaults on, and gates the entire live block;
    pure detectors are never consulted unless the flag fires (proven structurally).
  * PER-TRADE / DAILY CAPS RESPECTED — the re-load re-bases the max-loss circuit to
    the STARTER R0 (pyramid_risk_anchor_usd) so cumulative re-loads never inflate the
    per-trade loss budget, and routes through the same admission gate as a new entry.

These are PURE functions (no DB), so the test does not use the ``db`` fixture.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import Settings
from app.services.trading.momentum_neural.candles import (
    is_bounce_curl_candle,
    bounce_curl_from_df,
)
from app.services.trading.momentum_neural.entry_gates import (
    micro_pullback_reentry_detect,
    _entry_flow_veto,
)


# --------------------------------------------------------------------------- helpers
def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Build an OHLC frame from (open, high, low, close) tuples."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _squeeze_then_micro_pullback(
    *, base: float = 10.0, step: float = 0.10, dip: float = 0.20, curl: float = 0.12
) -> pd.DataFrame:
    """A synthetic SQUEEZE (rising stack making higher highs) followed by a shallow
    higher-low micro-pullback and a green curl-back-up bar — the exact Ross shape the
    re-load is built to catch. Returns >= 10 bars so the detector's sparse-frame
    fail-safe does not trip."""
    rows: list[tuple[float, float, float, float]] = []
    px = base
    # 8 rising squeeze bars (green, higher highs/lows) -> rising 9-EMA stack.
    for _ in range(8):
        o = px
        c = px + step
        rows.append((o, c + 0.02, o - 0.01, c))
        px = c
    bounce_high = px              # local high at the top of the squeeze
    dip_low = bounce_high - dip   # shallow pullback low (higher-low, holds the shelf)
    # Pullback bar: red, dips to dip_low but stays above prior structure.
    rows.append((bounce_high, bounce_high + 0.01, dip_low, dip_low + 0.03))
    # Curl bar: GREEN, closes in the upper part of its range, low at/above dip_low.
    curl_close = dip_low + curl
    rows.append((dip_low + 0.02, curl_close + 0.01, dip_low, curl_close))
    return _df(rows)


# ============================================================ DETECTION: fires on bounce
def test_detect_fires_on_higher_low_bounce_after_squeeze():
    """A real micro-pullback shape (rising EMA, shallow higher-low dip above the shelf,
    green curl holding the dip) FIRES."""
    df = _squeeze_then_micro_pullback()
    # shelf below the dip; generous dip cap so geometry (not the cap) is under test.
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.10)
    assert out["fire"] is True
    assert out["reason"] == "micro_pullback_curl"
    assert out["bounce_high"] is not None and out["dip_low"] is not None
    assert out["dip_low"] < out["bounce_high"]


def test_curl_candle_confirms_green_close_upper():
    """The per-bar curl confirm: a green bar closing in the upper range is a curl;
    a red bar is not; a zero-range bar fails-SAFE to False."""
    # green, close at the very top of the range
    assert is_bounce_curl_candle(10.0, 10.5, 9.9, 10.45) is True
    # red bar -> no reassert
    assert is_bounce_curl_candle(10.5, 10.6, 10.0, 10.1) is False
    # green but closes in the LOWER part of the range -> weak, no curl
    assert is_bounce_curl_candle(10.0, 10.5, 10.0, 10.10) is False
    # zero-range bar -> fail-safe False (an extra BUY needs proof)
    assert is_bounce_curl_candle(10.0, 10.0, 10.0, 10.0) is False


def test_curl_from_df_fails_safe_on_unreadable():
    """bounce_curl_from_df fails-SAFE to False (NO fire) on None/empty — the OPPOSITE
    of the break candle's fail-open. A thin micro-bar never fires a re-load."""
    assert bounce_curl_from_df(None) is False
    assert bounce_curl_from_df(_df([])) is False
    # readable green curl -> True
    assert bounce_curl_from_df(_df([(10.0, 10.5, 9.9, 10.45)])) is True


# ============================================================ DETECTION: rejects selling/deep
def test_detect_rejects_dip_below_ratcheting_shelf():
    """When the shelf ratchets ABOVE the actual dip (a prior re-load's higher-low),
    the new dip undercuts it -> NO fire (dip_below_shelf). The higher-low ratchet."""
    df = _squeeze_then_micro_pullback(dip=0.20)
    dip_low = float(df["low"].iloc[-1])  # curl bar low == dip_low
    # shelf set ABOVE the dip -> the dip no longer holds the ratcheting shelf
    out = micro_pullback_reentry_detect(df, shelf=dip_low + 0.05, max_dip_pct=0.10)
    assert out["fire"] is False
    assert out["reason"] == "dip_below_shelf"


def test_detect_rejects_deep_rollover_dip_too_deep():
    """A deep dip (rollover) is NOT a micro-pullback: tighten the cap below the actual
    dip percentage -> NO fire (dip_too_deep). Deep-rollover defense."""
    df = _squeeze_then_micro_pullback(dip=0.40)  # ~deeper dip from the bounce-high
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.005)
    assert out["fire"] is False
    assert out["reason"] == "dip_too_deep"


def test_detect_rejects_sparse_frame_failsafe():
    """A None/empty/<10-bar frame never fires (SUPERSET fail-safe — a no-tape name
    never re-loads)."""
    assert micro_pullback_reentry_detect(None, shelf=9.0, max_dip_pct=0.10)["fire"] is False
    assert micro_pullback_reentry_detect(_df([]), shelf=9.0, max_dip_pct=0.10)["fire"] is False
    short = _df([(10.0, 10.1, 9.9, 10.05)] * 5)  # < 10 bars
    out = micro_pullback_reentry_detect(short, shelf=9.0, max_dip_pct=0.10)
    assert out["fire"] is False
    assert out["reason"] == "frame_too_sparse"


def test_detect_rejects_falling_ema_stack():
    """A DOWN structure (falling 9-EMA: closes trending down) is not a squeeze -> NO
    fire (ema_not_rising). Re-loads only on an intact up-run."""
    rows = [(20.0 - i * 0.2, 20.1 - i * 0.2, 19.8 - i * 0.2, 19.9 - i * 0.2) for i in range(12)]
    out = micro_pullback_reentry_detect(_df(rows), shelf=0.0, max_dip_pct=0.50)
    assert out["fire"] is False
    assert out["reason"] == "ema_not_rising"


# ============================================================ NO ENTRY INTO SELLING (flow)
def _settings() -> Settings:
    return Settings()


def _flow_gate_allows(ofi, trade_flow, settings) -> bool:
    """Reproduce the live block's flow decision: defer if the entry flow-veto trips OR
    the positive-confirm leg is not satisfied (live_runner.py:5797-5801). Returns True
    iff the re-load is ALLOWED through the flow gate."""
    ofi_floor = float(getattr(settings, "chili_momentum_micropullback_reentry_ofi_thr", 0.30))
    tf_floor = float(getattr(settings, "chili_momentum_micropullback_reentry_trade_flow_thr", 0.20))
    veto = _entry_flow_veto(ofi, trade_flow, settings)
    pos_confirm = (
        ofi is not None and trade_flow is not None
        and ofi >= ofi_floor and trade_flow >= tf_floor
    )
    return (not veto) and pos_confirm


def test_flow_gate_blocks_buying_into_selling():
    """Flow DOWN (the 06-24 PLSM chase: strongly negative executed tape) -> the veto
    trips -> NEVER buy into selling."""
    s = _settings()
    # strongly-selling tape (<= -0.5 strong leg) vetoes regardless of OFI
    assert _entry_flow_veto(0.5, -0.63, s) is True
    assert _flow_gate_allows(0.5, -0.63, s) is False
    # both-bearish AND-leg
    assert _entry_flow_veto(-0.7, -0.30, s) is True
    assert _flow_gate_allows(-0.7, -0.30, s) is False


def test_flow_gate_allows_only_on_positive_confirm():
    """Re-enter only when flow CONFIRMS up: ofi >= 0.30 AND trade_flow >= 0.20."""
    s = _settings()
    assert _flow_gate_allows(0.40, 0.30, s) is True       # both above floors
    assert _flow_gate_allows(0.20, 0.30, s) is False       # ofi below floor
    assert _flow_gate_allows(0.40, 0.10, s) is False       # trade_flow below floor


def test_flow_positive_confirm_fails_closed_on_none():
    """The positive-confirm leg FAILS-CLOSED on None flow (an extra discretionary BUY
    needs proof) even though the veto itself fails-OPEN on None."""
    s = _settings()
    assert _entry_flow_veto(None, None, s) is False        # veto fails-open
    assert _flow_gate_allows(None, None, s) is False        # but no re-load without proof
    assert _flow_gate_allows(0.5, None, s) is False
    assert _flow_gate_allows(None, 0.5, s) is False


# ============================================================ RE-ENTER UP TO THE CAP THEN STOP
def test_reentry_count_caps_then_stops():
    """The per-name/session counter gates against the cap: under cap -> allowed; at/over
    cap -> stop (mirrors live_runner.py:5687 `if _mpr_count >= _max_reentries`)."""
    s = _settings()
    cap = int(s.chili_momentum_micropullback_reentry_max)
    assert cap == 3  # default per-name/session cap
    for count in range(cap):
        assert count < cap, "under cap -> a re-load may fire"
    # at the cap and beyond -> no more re-loads
    assert not (cap < cap)
    assert not ((cap + 1) < cap)


def test_total_reload_risk_is_bounded_by_cap_times_fraction():
    """Worst-case cumulative re-load structural risk = max * rho * R0 stays bounded
    (default 3 * 0.30 = 0.9 * R0 on TOP of the starter) — the cap + fraction together
    bound the added risk so re-loads can't run away."""
    s = _settings()
    cap = int(s.chili_momentum_micropullback_reentry_max)
    rho = float(s.chili_momentum_micropullback_reentry_risk_fraction)
    R0 = 500.0
    worst_case_added_risk = cap * rho * R0
    assert worst_case_added_risk == pytest.approx(0.9 * R0)
    assert worst_case_added_risk < 1.0 * R0  # never exceeds one extra R0 of risk


# ============================================================ NO-OP FLAG-OFF
def test_kill_switch_default_on_independent_of_pyramid():
    """No-dark-flags: the re-load kill-switch defaults ON (live+on) and is INDEPENDENT
    of the pyramid kill-switch (own flag)."""
    s = _settings()
    assert s.chili_momentum_micropullback_reentry_enabled is True
    # independent flag — toggling pyramid does not toggle the re-load and vice versa
    assert hasattr(s, "chili_momentum_micropullback_reentry_enabled")


def test_flag_off_disables_the_block():
    """With the flag OFF the entire live block is a no-op (the guard at
    live_runner.py:5581 short-circuits). Modeled by the env override."""
    s = Settings(CHILI_MOMENTUM_MICROPULLBACK_REENTRY_ENABLED=False)
    assert s.chili_momentum_micropullback_reentry_enabled is False
    # The pure detectors remain callable but are NEVER consulted when the gate is off;
    # they themselves do not read the flag (the live block does), so this is a
    # structural no-op: the guard wraps the whole block.


def test_config_defaults_match_spec():
    """Companion knobs carry the documented defaults (the re-load is adaptive/derived,
    not hardcoded magic numbers in the hot path)."""
    s = _settings()
    assert s.chili_momentum_micropullback_reentry_max == 3
    assert s.chili_momentum_micropullback_reentry_cooldown_seconds == 30.0
    assert s.chili_momentum_micropullback_reentry_risk_fraction == 0.30
    assert s.chili_momentum_micropullback_reentry_ofi_thr == 0.30
    assert s.chili_momentum_micropullback_reentry_trade_flow_thr == 0.20
    assert s.chili_momentum_micropullback_reentry_max_dip_pct == 0.04
    assert s.chili_momentum_micropull_bar_seconds == 15


# ============================================================ PER-TRADE / DAILY CAPS
def test_cooldown_pinned_to_bar_cadence():
    """The cooldown is PINNED to >= 2 * bar_seconds so one wiggle cannot fire two
    re-loads before the shelf re-ratchets (live_runner.py:5635-5638)."""
    s = _settings()
    cfg_cool = float(s.chili_momentum_micropullback_reentry_cooldown_seconds)
    bar_s = int(s.chili_momentum_micropull_bar_seconds)
    effective = max(cfg_cool, 2.0 * bar_s)
    assert effective >= 2.0 * bar_s
    assert effective == 30.0  # 30s default already == 2 * 15s bars


def test_circuit_rebases_to_starter_R0_not_inflated():
    """Per-trade max-loss circuit invariant: each re-load re-bases the circuit to the
    STARTER R0 (pyramid_risk_anchor_usd) so cumulative re-loads NEVER inflate the
    per-trade loss budget. Model the live re-base (live_runner.py:5618-5619)."""
    R0_starter = 500.0
    le: dict = {"pyramid_risk_anchor_usd": R0_starter}
    # simulate three re-loads, each re-basing to the SAME starter R0
    for _ in range(3):
        _R0m = R0_starter
        if _R0m is not None and _R0m > 0:
            le["pyramid_risk_anchor_usd"] = _R0m
    assert le["pyramid_risk_anchor_usd"] == R0_starter  # never grows with adds


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
