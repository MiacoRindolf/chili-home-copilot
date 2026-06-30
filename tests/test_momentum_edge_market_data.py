"""PRINCIPAL-LEVEL adversarial edge-case hunt: MALFORMED market data into the
Ross momentum lane's entry gates + spread/L2 layer.

FOCUS (per the bug-hunt brief): construct the GNARLIEST quotes/candles/frames the
gates will plausibly meet in prod and assert the SPECIFIC fail-safe behaviour — no
fire on garbage, no crash, no negative size / negative derate / div-by-zero. These
are NOT branch-coverage smoke tests; each one would FAIL if the code silently
mishandled the malformed input (e.g. a crossed quote that produced a negative
derate, or a doji veto that fired on an unreadable bar).

Adversary classes covered:
  (1) CROSSED / LOCKED / zero / negative / non-finite quote into the spread-cost
      veto and the crypto liquidity floor.
  (2) BAD CANDLE (high<low, close outside [low,high], o=h=l=c, negative price)
      into the doji / wick / pullback / cup / flush gates.
  (3) ZERO-volume bar / 1-bar frame / all-NaN volume column into the
      volume-confirmation + resample path.
  (4) A price GAP (50% jump, no intermediate bars) into the extension veto.
  (5) NON-MONOTONIC / duplicate DatetimeIndex into the HTF resample.

PURE-LOGIC + mocks only (no DB truncate): the spread veto's DB dependency is a
fake ``execute().fetchone()``; settings are patched per-test; frames are synthetic.
The ``db`` fixture is deliberately NOT used.

TESTS-ONLY: no source file is modified. Suspected source bugs are asserted at their
CURRENT (possibly wrong) behaviour and called out in the agent's return notes; they
are marked with a ``# SUSPECTED SOURCE BUG`` comment so a fix is a single grep away.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

from app.services.trading.momentum_neural import candles as cd
from app.services.trading.momentum_neural import crypto_liquidity as cl
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import spread_cost_veto as scv


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _frame(
    n: int,
    *,
    hi_lt_lo: bool = False,
    all_nan_vol: bool = False,
    zero_vol: bool = False,
    datetime_index: bool = True,
    start_close: float = 10.0,
    end_close: float = 11.0,
) -> pd.DataFrame:
    """Synthetic OHLCV frame. ``hi_lt_lo`` swaps high/low so high<low everywhere
    (a corrupt feed)."""
    if datetime_index:
        idx = pd.date_range("2026-06-27 09:30", periods=n, freq="1min")
    else:
        idx = pd.RangeIndex(n)
    c = np.linspace(start_close, end_close, n)
    h = c + 0.05
    l = c - 0.05
    if hi_lt_lo:
        h, l = l, h
    if all_nan_vol:
        v = [np.nan] * n
    elif zero_vol:
        v = [0.0] * n
    else:
        v = [1000.0] * n
    return pd.DataFrame(
        {"Open": c, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx
    )


class _FakeDBNoRows:
    """``db.execute(...).fetchone()`` -> None (thin/no history)."""

    def execute(self, *a, **k):
        class _R:
            def fetchone(self_inner):
                return None

        return _R()


class _FakeDBRow:
    """``db.execute(...).fetchone()`` -> a fixed (p50, p75, p90, count) row."""

    def __init__(self, p50, p75, p90, n):
        self._row = (p50, p75, p90, n)

    def execute(self, *a, **k):
        row = self._row

        class _R:
            def fetchone(self_inner):
                return row

        return _R()


class _CryptoRow:
    """Minimal viability row carrying a ross_signals turnover datum."""

    def __init__(self, quote_volume_24h):
        self.execution_readiness_json = {
            "extra": {"ross_signals": {"FOO-USD": {"quote_volume_24h": quote_volume_24h}}}
        }


# ──────────────────────────────────────────────────────────────────────────────
# (1) CROSSED / LOCKED / degenerate quote -> spread-cost veto
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad_spread", [0.0, -1.0, -50.0, None, float("nan"), float("inf"), -float("inf")])
def test_spread_veto_degenerate_spread_fails_open_never_negative(bad_spread):
    """A LOCKED (spread=0), CROSSED-derived NEGATIVE, or non-finite spread must
    fail OPEN to the byte-identical pass-through (allow=True, mult=1.0) — NEVER a
    negative/zero derate and NEVER a div-by-zero. This is the contract the sizing
    path relies on: an unusable spread can't shrink size."""
    allow, mult, reason, meta = scv.adaptive_spread_cost_veto_derate(
        symbol="ABCD",
        entry_price=10.0,
        current_spread_bps=bad_spread,
        stop_distance=0.5,
        db=_FakeDBNoRows(),
        flag_enabled=True,
    )
    assert allow is True
    assert mult == 1.0  # exact pass-through, not merely >0
    assert reason == "no_spread"
    assert meta == {}


def test_spread_veto_zero_stop_distance_no_div_by_zero():
    """A LOCKED candle yields a zero stop distance (entry==stop). cost_of_r would
    divide by zero — the gate must short-circuit to fail-open, not raise or emit inf."""
    allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
        symbol="ABCD",
        entry_price=10.0,
        current_spread_bps=300.0,
        stop_distance=0.0,
        db=_FakeDBNoRows(),
        flag_enabled=True,
    )
    assert (allow, mult, reason) == (True, 1.0, "no_stop_distance")


@pytest.mark.parametrize("bad_entry", [0.0, -10.0, None, float("nan"), float("inf")])
def test_spread_veto_bad_entry_price_fails_open(bad_entry):
    """A non-positive / non-finite entry price (a corrupt last-trade print) must
    fail open — never derate, never crash."""
    allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
        symbol="ABCD",
        entry_price=bad_entry,
        current_spread_bps=300.0,
        stop_distance=0.5,
        db=_FakeDBNoRows(),
        flag_enabled=True,
    )
    assert (allow, mult, reason) == (True, 1.0, "no_entry_price")


def test_spread_veto_extreme_spread_floors_never_below_floor_or_negative():
    """An EXTREME anomaly vs the name's own p90 AND a cost that eats most of R must
    DERATE TO THE FLOOR (mult==floor), allow=True — never below the floor, never
    negative, never a hard block. Pins the derate-only contract + the floor clamp."""
    db = _FakeDBRow(100.0, 150.0, 200.0, 50)  # healthy own distribution in bps
    allow, mult, reason, meta = scv.adaptive_spread_cost_veto_derate(
        symbol="X",
        entry_price=10.0,
        current_spread_bps=5000.0,  # ~50% spread; >> p90*extreme_mult
        stop_distance=0.10,  # tight stop -> cost_of_r huge
        db=db,
        flag_enabled=True,
    )
    floor = float(getattr(scv.settings, "chili_momentum_spread_cost_derate_floor", 0.5) or 0.5)
    assert allow is True
    assert mult == pytest.approx(floor)  # floored, not below
    assert 0.0 < mult <= 1.0  # never negative / never zero size
    assert "extreme_spread_floored" in reason
    assert meta.get("extreme_floor") is True


def test_spread_veto_locked_name_distribution_p50_zero_fails_open():
    """If the name's OWN recent spreads are all LOCKED (p50<=0), the percentile
    helper must reject the distribution (return None) so the anomaly axis can't
    divide by a zero p50; the gate then judges on cost-of-R only with
    name_dist=insufficient_history. Asserts no ZeroDivision on anomaly_ratio."""
    db = _FakeDBRow(0.0, 0.0, 0.0, 50)
    allow, mult, reason, meta = scv.adaptive_spread_cost_veto_derate(
        symbol="X",
        entry_price=10.0,
        current_spread_bps=300.0,
        stop_distance=0.5,
        db=db,
        flag_enabled=True,
    )
    assert allow is True
    assert meta.get("name_dist") == "insufficient_history"
    assert meta.get("anomaly_ratio") is None  # never computed against a 0 p50
    assert 0.0 < mult <= 1.0


def test_name_spread_percentiles_locked_p50_returns_none():
    """Direct unit on the percentile helper: a p50 of 0 (all-locked history) must
    return None (the p50<=0 guard) so callers never use a degenerate baseline."""
    db = _FakeDBRow(0.0, 0.0, 0.0, 99)
    assert scv.name_spread_percentiles(db, "X", lookback_days=20.0) is None


# ──────────────────────────────────────────────────────────────────────────────
# (1b) CROSSED / LOCKED / degenerate turnover -> crypto liquidity floor
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "qv,expect_reason",
    [
        (-100.0, "liquidity_data_missing"),   # negative turnover -> _f rejects -> missing
        (float("nan"), "liquidity_data_missing"),
        (float("inf"), "liquidity_data_missing"),
        (0.0, "quote_volume_below_floor"),     # zero turnover -> finite but below floor
    ],
)
def test_crypto_liquidity_degenerate_turnover_fails_closed_no_cap(qv, expect_reason):
    """Crypto fails CLOSED: a negative / NaN / inf / zero 24h turnover must BLOCK
    (ok=False) with NO notional cap — never a negative or NaN size cap, never an
    accidental pass."""
    ok, detail, cap = cl.crypto_liquidity_ok("FOO-USD", _CryptoRow(qv))
    assert ok is False
    assert cap is None
    assert detail.get("reason") == expect_reason


def test_crypto_liquidity_healthy_turnover_positive_finite_cap():
    """A healthy turnover yields a STRICTLY POSITIVE, FINITE notional cap
    (= fraction * per-minute $-vol) — sanity that the math never inverts/NaNs."""
    ok, detail, cap = cl.crypto_liquidity_ok("FOO-USD", _CryptoRow(1_000_000_000.0))
    assert ok is True
    assert cap is not None and cap > 0.0 and math.isfinite(cap)


# ──────────────────────────────────────────────────────────────────────────────
# (2) BAD CANDLE -> doji / wick / curl / bottoming-tail gates
# ──────────────────────────────────────────────────────────────────────────────
def test_doji_veto_high_lt_low_never_blocks():
    """A corrupt candle with high<low yields range<=0 -> the doji veto MUST fail
    SAFE to veto=False (never block on an unreadable bar). A True here would silently
    kill valid entries on a single bad print."""
    veto, dbg = eg._doji_trigger_veto(10.0, 9.0, 11.0, 10.0, atr_pct=0.02, base_body_frac=0.25)
    assert veto is False


def test_doji_veto_flat_bar_o_h_l_c_equal_never_blocks():
    """o==h==l==c (a zero-range print, common on a halt or a thin tick) -> range 0
    -> fail-safe veto=False, no div-by-zero on body/range."""
    veto, _ = eg._doji_trigger_veto(10.0, 10.0, 10.0, 10.0, atr_pct=None, base_body_frac=0.25)
    assert veto is False


def test_doji_veto_negative_price_does_not_crash_and_does_not_block():
    """A negative-price candle (corrupt feed) must not raise and must not block.
    Current behaviour: the strong-full-body override fires (body_frac 0 but a green
    full body) so veto=False — documents that negative prices fail OPEN, not into a
    spurious veto."""
    veto, dbg = eg._doji_trigger_veto(-5.0, -4.0, -6.0, -5.0, atr_pct=0.02, base_body_frac=0.25)
    assert veto is False


def test_strong_bull_break_candle_zero_range_is_false():
    """A zero-range bar must NOT register as a conviction break (fail-safe False)."""
    assert cd.is_strong_bull_break_candle(10.0, 10.0, 10.0, 10.0) is False


def test_topping_tail_high_lt_low_is_false_no_negative_wick_fire():
    """high<low -> range<=0 -> topping-tail must be False (never fire an EXIT signal
    on a corrupt bar where upper_wick/range would be a negative-over-negative ratio)."""
    assert cd.is_topping_tail(10.0, 9.0, 11.0, 10.0) is False


def test_bottoming_tail_high_lt_low_is_false():
    """The flush V-bounce bottoming-tail must fail-safe False on high<low."""
    assert eg._bottoming_tail(10.0, 9.0, 11.0, 10.0) is False


def test_bounce_curl_zero_range_is_false_fail_safe_no_fire():
    """The re-load curl candle fails SAFE (False = NO fire) on a zero-range bar —
    an extra discretionary BUY must never be triggered by an unreadable micro-bar."""
    assert cd.is_bounce_curl_candle(10.0, 10.0, 10.0, 10.0) is False


def test_break_candle_from_df_crossed_frame_does_not_crash():
    """``break_candle_ok_from_df`` on a high<low last bar: range<=0 -> the underlying
    predicate is False, so the fail-open df wrapper returns False (no conviction),
    and crucially does not raise."""
    out = cd.break_candle_ok_from_df(_frame(5, hi_lt_lo=True))
    assert out is False


# ──────────────────────────────────────────────────────────────────────────────
# (3) ZERO-volume / 1-bar / all-NaN-volume -> volume confirmation + resample
# ──────────────────────────────────────────────────────────────────────────────
def test_volume_confirmation_one_bar_frame_insufficient():
    """A 1-bar frame must short-circuit to insufficient_bars (no IndexError on the
    25-bar minimum), never a fire."""
    ok, reason = eg.momentum_volume_confirmation(_frame(1))
    assert ok is False
    assert reason == "insufficient_bars"


def test_volume_confirmation_flat_volume_below_1p5x_does_not_fire():
    """A frame with a VALID, finite average volume but a current bar BELOW the 1.5x
    threshold (constant 1000-share volume: cur_v == avg_v < 1.5*avg_v) must NOT confirm.
    This exercises the SECOND guard (``cur_v < 1.5*avg_v`` -> 'volume_below_1p5x_avg'),
    distinct from the all-zero/all-NaN cases that trip the FIRST avg-invalid guard first."""
    ok, reason = eg.momentum_volume_confirmation(_frame(30))
    assert ok is False
    assert reason == "volume_below_1p5x_avg"


def test_volume_confirmation_all_zero_volume_does_not_fire():
    """An all-ZERO-volume frame (a dead/halted name) must NOT confirm volume:
    avg_v computes to 0 -> the FIRST guard (``avg_v <= 0``) fires -> 'volume_avg_zero'.
    A fire here would size a trade on a name with literally no trading."""
    ok, reason = eg.momentum_volume_confirmation(_frame(30, zero_vol=True))
    assert ok is False
    assert reason == "volume_avg_zero"


def test_volume_confirmation_all_nan_volume_fails_closed_no_fire():
    """FIX HIGH-1 (entry_gates.py momentum_volume_confirmation, ~L142-152).

    With an ALL-NaN Volume column (a feed outage / missing-vol vendor row) and a
    valid rising close above EMA-9, ``volume_ratio`` is all-None, so the fallback
    window math runs with NaN ``avg_v``/``cur_v``. Pre-fix, the guards ``avg_v <= 0``
    and ``cur_v < 1.5*avg_v`` were BOTH False under NaN comparison, so the function
    returned a BOGUS ``(True, 'momentum_ok_abs_vol')`` confirm on ZERO real volume.

    The ``not math.isfinite(avg_v) or avg_v <= 0`` guard now FAILS CLOSED: an all-NaN
    volume yields NO confirm (``volume_avg_zero``). Adversarial pin: a feed outage must
    never manufacture a volume confirmation."""
    ok, reason = eg.momentum_volume_confirmation(_frame(30, all_nan_vol=True))
    # CORRECTED behaviour: all-NaN volume => fail-closed, no fire.
    assert ok is False
    assert reason == "volume_avg_zero"


def test_resample_htf_one_bar_returns_none():
    """A 1-bar frame is too thin to resolve a higher timeframe -> None (caller fails
    open), never a single-row HTF that downstream trend reads would misinterpret."""
    assert eg._resample_htf(_frame(1)) is None


def test_resample_htf_all_nan_volume_does_not_drop_all_rows():
    """An all-NaN Volume column must not nuke the resampled HTF: ``sum`` of NaN ->
    0.0 (not NaN), so price OHLC rows survive ``dropna``. Asserts the resample still
    yields a usable frame (a regression here would silently disable the HTF veto)."""
    htf = eg._resample_htf(_frame(20, all_nan_vol=True))
    assert htf is not None
    assert len(htf) >= 2


# ──────────────────────────────────────────────────────────────────────────────
# (4) PRICE GAP (50% jump) -> extension veto + measured-move semantics
# ──────────────────────────────────────────────────────────────────────────────
def test_extension_veto_50pct_gap_above_level_vetoes_chase():
    """A 50% jump above the breakout level (no intermediate bars — a gap) is a chase
    into a top: the extension veto MUST return True (defer the buy). The single most
    important market-data edge for not buying a blow-off gap."""
    assert eg._entry_extension_veto(15.0, 10.0, 0.015, scv.settings) is True


def test_extension_veto_entry_at_level_does_not_veto():
    """Entry exactly AT the breakout level (0% extension) must NOT veto — the canonical
    in-spec entry. Guards against an off-by-one '>=' that would block clean breaks."""
    assert eg._entry_extension_veto(10.0, 10.0, 0.015, scv.settings) is False


@pytest.mark.parametrize("bad_level", [0.0, -10.0])
def test_extension_veto_nonpositive_level_fails_open(bad_level):
    """A non-positive breakout level (corrupt) must fail open to no-veto (parity),
    never block on a garbage level."""
    assert eg._entry_extension_veto(15.0, bad_level, 0.015, scv.settings) is False


def test_extension_veto_none_atr_uses_floor_cap_and_vetoes():
    """FIX HIGH-2: atr_pct=None no longer DISARMS the chase-guard. A missing/non-finite
    ATR (a thin low-float runner with no computable volatility) now falls back to the FLAT
    extension floor (~8%), so a +50% extension (15 vs 10) still VETOES instead of letting
    an unguarded chase through. (A valid breakout_level/entry_price is still required.)"""
    assert eg._entry_extension_veto(15.0, 10.0, None, scv.settings) is True


def test_extension_veto_nan_atr_uses_floor_cap_and_vetoes_50pct_gap():
    """⚠️ EDGE: a NaN atr_pct is NOT None, so it slips past the 'missing -> no veto'
    guard. ``max(0.0, nan)`` returns 0.0, so the cap collapses to the floor (~8%) and a
    +50% gap still vetoes. This pins that a NaN ATR is treated as a CALM name (floor
    cap), not as 'missing'. Direction is SAFE (a chase is still blocked), but the
    behaviour is subtle and load-bearing — a future refactor that changed the
    ``max(0.0, a)`` idiom could flip it to fail-open and let a blow-off gap through."""
    assert eg._entry_extension_veto(15.0, 10.0, float("nan"), scv.settings) is True


def test_extension_veto_nan_entry_price_fails_open():
    """A NaN entry price: ``float('nan') <= 0`` is False, so the non-positive guard
    does NOT catch it, but the final comparison ``nan >= lvl*(1+cap)`` is False ->
    no veto. Pins that a corrupt entry print fails OPEN here (no spurious veto)."""
    assert eg._entry_extension_veto(float("nan"), 10.0, 0.015, scv.settings) is False


# ──────────────────────────────────────────────────────────────────────────────
# (5) NON-MONOTONIC / DUPLICATE DatetimeIndex -> HTF resample
# ──────────────────────────────────────────────────────────────────────────────
def test_resample_htf_non_monotonic_index_fails_open_none():
    """A non-monotonic / out-of-order DatetimeIndex (e.g. a backfill that interleaved
    stale bars) makes pandas ``resample`` raise; the gate MUST catch it and return
    None (fail-open), never propagate the ValueError up the entry path."""
    idx = pd.to_datetime(
        [
            "2026-06-27 09:30",
            "2026-06-27 09:31",
            "2026-06-27 09:30",  # back in time
            "2026-06-27 09:32",
            "2026-06-27 09:31",  # back in time again
            "2026-06-27 09:33",
        ]
    )
    c = np.array([10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    df = pd.DataFrame(
        {"Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c, "Volume": [100.0] * 6},
        index=idx,
    )
    assert eg._resample_htf(df) is None


def test_resample_htf_duplicate_timestamps_does_not_crash():
    """A DUPLICATE-timestamp index (two bars stamped the same minute — a common
    dual-feed merge artefact) must not raise. It is monotonic-nondecreasing so
    resample is allowed; assert we get either None or a valid (>=2 row) frame, never
    an exception."""
    idx = pd.to_datetime(
        [
            "2026-06-27 09:30",
            "2026-06-27 09:30",  # duplicate
            "2026-06-27 09:35",
            "2026-06-27 09:40",
            "2026-06-27 09:45",
            "2026-06-27 09:50",
        ]
    )
    c = np.linspace(10.0, 10.5, 6)
    df = pd.DataFrame(
        {"Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c, "Volume": [100.0] * 6},
        index=idx,
    )
    out = eg._resample_htf(df)  # must not raise
    assert out is None or len(out) >= 2


def test_resample_htf_non_datetime_index_returns_none():
    """A RangeIndex (no datetime) must return None — the resample needs a clock."""
    assert eg._resample_htf(_frame(20, datetime_index=False)) is None


def test_htf_against_veto_crossed_frame_fails_open_no_veto():
    """The multi-TF alignment veto on a fully corrupt (high<low) frame must fail OPEN
    (veto=False) — a malformed HTF feed can NEVER block a valid 1m-fast entry."""
    veto, _ = eg._htf_against_veto(_frame(40, hi_lt_lo=True))
    assert veto is False


# ──────────────────────────────────────────────────────────────────────────────
# (2b) BAD CANDLE -> full breakout gates (flush / cup) must not bogus-fire
# ──────────────────────────────────────────────────────────────────────────────
def test_flush_dip_buy_crossed_frame_no_fire():
    """A 30-bar frame with high<low everywhere must NOT fire the flush-dip-buy (no
    bottoming-tail can form on inverted bars), and must not raise."""
    ok, reason, _ = eg.flush_dip_buy_confirmation(_frame(30, hi_lt_lo=True), entry_interval="1m")
    assert ok is False


def test_cup_and_handle_crossed_frame_no_fire_flag_on():
    """With the cup flag ENABLED and a fully corrupt (high<low) frame, the gate must
    not fire and must not raise (swing pivots over inverted bars find no real
    double-top rim). Patches the flag so the candle path is actually exercised rather
    than short-circuited by the default-off flag."""
    with patch.object(eg.settings, "chili_momentum_cup_and_handle_entry_enabled", True):
        ok, reason, _ = eg.cup_and_handle_confirmation(
            _frame(40, hi_lt_lo=True), entry_interval="1m"
        )
    assert ok is False
