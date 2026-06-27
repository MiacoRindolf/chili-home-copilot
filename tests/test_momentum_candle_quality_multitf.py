"""Doji veto + HTF-against (multi-TF alignment) entry-quality vetoes for the Ross
momentum lane (flag ``chili_momentum_candle_quality_multitf_veto_enabled``, default OFF).

Adversarial coverage of the two additive gates that slot into
``pullback_break_confirmation`` AFTER the trigger fires and BEFORE the downstream
VWAP/MACD/volume confirmations:

  (1) DOJI VETO     — a true doji trigger candle (weak body relative to range =
                      indecision) is BLOCKED; a strong full-body commitment candle PASSES.
                      ATR-adaptive band (ONE documented base, widened by atr_pct).
  (2) HTF-AGAINST   — the higher TF (5m, resampled from the 1m df, no new feed) being
                      CLEARLY bearish (5m EMA-9 rolling DOWN / MACD peaked) BLOCKS; a
                      NEUTRAL/LAGGING HTF MUST still PASS (Ross 1m-FAST geometry preserved);
                      an aligned-UP HTF passes.

THE TRAP this guards against: requiring full multi-TF alignment breaks Ross's 1m-fast
geometry (the 1m leads, the HTF lags). So the HTF veto fires ONLY when the HTF is clearly
AGAINST, never when it is merely neutral/lagging.

Flag default OFF -> byte-identical (both gates skipped).
"""
from __future__ import annotations

import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    _doji_trigger_veto,
    _htf_against_veto,
    _resample_htf,
    pullback_break_confirmation,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _df(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """rows = (open, high, low, close, volume)."""
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v} for (o, h, lo, c, v) in rows]
    )


def _base(close: float, vol: float = 1000.0) -> tuple[float, float, float, float, float]:
    return (close, close + 0.3, close - 0.3, close, vol)


def _firing_rows() -> list[tuple[float, float, float, float, float]]:
    """Long flat base (so EMA-9 lags), impulse, shallow pullback, then a STRONG full-body
    green break bar + volume spike -> pullback_break_ok. The break bar is a conviction
    candle (close at the high, tiny wicks) so it passes the doji gate when the flag is ON."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]               # shallow pullback
    # current: STRONG full-body green break (open 109.7, close 111.1 at the high, tiny wicks)
    rows.append((109.7, 111.2, 109.65, 111.1, 3200.0))
    return rows


def _dt_index(df: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """Attach a 1-minute DatetimeIndex so the HTF resampler can read it."""
    df = df.copy()
    df.index = pd.date_range("2026-06-27 09:30", periods=len(df), freq=freq)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# DOJI VETO — pure-function adversarial cases
# ──────────────────────────────────────────────────────────────────────────────
def test_doji_candle_is_vetoed() -> None:
    """A classic doji: open ~= close, closing in the LOWER half of a long range (so it is
    NOT a strong full-body candle) => body/range tiny => VETO."""
    # range 2.0, body 0.05 -> body_frac 0.025 << base 0.25; close_pos = (99.95-99)/2 = 0.475 < 0.50
    veto, dbg = _doji_trigger_veto(100.0, 101.0, 99.0, 99.95, atr_pct=None, base_body_frac=0.25)
    assert veto is True, dbg
    assert dbg["doji_body_frac"] < dbg["doji_threshold"]


def test_full_body_commitment_candle_passes() -> None:
    """A strong full-body green candle (close at the high, tiny wicks) => NOT a doji => PASS."""
    # open 100.0, close 102.9, high 103.0, low 99.95 -> body 2.9 of range ~3.05 -> ~0.95
    veto, dbg = _doji_trigger_veto(100.0, 103.0, 99.95, 102.9, atr_pct=None, base_body_frac=0.25)
    assert veto is False, dbg


def test_doji_threshold_is_atr_adaptive() -> None:
    """The doji band WIDENS with volatility. A bar whose body/range = 0.28 is NOT a doji on a
    CALM name (threshold = base 0.25) but IS indecision for a high-ATR name (threshold
    0.25 + 0.10 = 0.35). Proves ONE documented base widened by atr_pct (no fixed magic)."""
    # range 1.0, body 0.28; upper wick dominant + close in lower half -> NOT a strong candle
    # (so the full-body override never rescues it): open 99.86, close 100.14, low 99.70, high 100.70.
    o, h, lo, c = 99.86, 100.70, 99.70, 100.14   # body 0.28, range 1.00 -> frac 0.28
    # calm name (atr 0.0, base 0.25): 0.28 >= 0.25 -> NOT a doji -> PASS.
    veto_calm, dbg_calm = _doji_trigger_veto(o, h, lo, c, atr_pct=0.0, base_body_frac=0.25)
    assert veto_calm is False, dbg_calm
    # volatile name (atr 0.10): threshold 0.35 -> 0.28 < 0.35 -> now a doji -> VETO.
    veto_vol, dbg_vol = _doji_trigger_veto(o, h, lo, c, atr_pct=0.10, base_body_frac=0.25)
    assert veto_vol is True, dbg_vol
    assert dbg_vol["doji_threshold"] > dbg_calm["doji_threshold"]


def test_doji_zero_range_bar_fails_safe() -> None:
    """A zero-range (unreadable) bar must NEVER block (fail-safe), not veto."""
    veto, _ = _doji_trigger_veto(100.0, 100.0, 100.0, 100.0, atr_pct=None, base_body_frac=0.25)
    assert veto is False


def test_doji_bad_inputs_fail_open() -> None:
    veto, _ = _doji_trigger_veto("x", None, 1.0, 2.0, atr_pct=None, base_body_frac=0.25)  # type: ignore[arg-type]
    assert veto is False


# ──────────────────────────────────────────────────────────────────────────────
# HTF-AGAINST VETO — pure-function adversarial cases
# ──────────────────────────────────────────────────────────────────────────────
def _htf_uptrend_1m() -> pd.DataFrame:
    """A 1m frame whose 5m resample is a clean rising EMA-9 (HTF aligned UP)."""
    rows = []
    px = 100.0
    for _ in range(60):
        rows.append((px, px + 0.2, px - 0.1, px + 0.15, 1000.0))
        px += 0.15
    return _dt_index(_df(rows))


def _htf_downtrend_1m() -> pd.DataFrame:
    """A 1m frame whose 5m resample is a clearly DECLINING EMA-9 (HTF rolling down)."""
    rows = []
    px = 120.0
    for _ in range(60):
        rows.append((px, px + 0.1, px - 0.2, px - 0.15, 1000.0))
        px -= 0.15
    return _dt_index(_df(rows))


def _htf_neutral_lagging_1m() -> pd.DataFrame:
    """A 1m frame whose 5m EMA-9 is still RISING (lagging the 1m) while the very last 1m bars
    chop sideways: the HTF is NEITHER rolling down NOR peaked -> neutral/lagging -> MUST PASS.
    Built as a long steady climb (EMA-9 rising) then a tiny flat tail that does NOT drag the
    slow 5m EMA negative."""
    rows = []
    px = 100.0
    for _ in range(55):
        rows.append((px, px + 0.2, px - 0.1, px + 0.15, 1000.0))
        px += 0.15
    # flat tail (no new highs, no breakdown) — the 5m EMA-9 still leans up from the climb
    last = px
    for _ in range(5):
        rows.append((last, last + 0.05, last - 0.05, last, 1000.0))
    return _dt_index(_df(rows))


def test_htf_clearly_bearish_is_vetoed() -> None:
    """5m EMA-9 rolling DOWN (clearly against the long) => VETO."""
    veto, dbg = _htf_against_veto(_htf_downtrend_1m())
    assert veto is True, dbg
    assert dbg.get("htf_against") in ("ema9_sustained_rolldown", "macd_peaked")


def test_htf_aligned_up_passes() -> None:
    """5m EMA-9 rising (HTF aligned with the long) => PASS."""
    veto, dbg = _htf_against_veto(_htf_uptrend_1m())
    assert veto is False, dbg


def test_htf_neutral_lagging_passes() -> None:
    """⭐ THE LOAD-BEARING CASE: a NEUTRAL/LAGGING HTF (5m not yet rolling down, EMA still
    leaning up from the prior climb, MACD not peaked) MUST PASS — a merely-lagging HTF must
    NOT block a valid 1m-fast entry (Ross geometry: the 1m leads, the HTF lags)."""
    veto, dbg = _htf_against_veto(_htf_neutral_lagging_1m())
    assert veto is False, dbg
    assert "htf_against" not in dbg


# ── FIX 2: SUSTAINED roll-down, not a single lagging down-tick ──────────────────
def _htf_single_downtick_1m() -> pd.DataFrame:
    """A 1m frame whose 5m EMA-9 RISES across the whole climb, then dips for ONLY THE LAST
    5m sample (a single lagging down-tick — a slow EMA dipping for one bar off a flush while
    the 1m has already turned up). The prior 5m steps are still UP, so the slope is NOT a
    sustained multi-bar roll-down. FIX 2: this must NOT register as clearly-against -> PASS.
    This is exactly the dip-rip / VWAP-reclaim geometry the lane wants to catch.

    Built as exactly 50 climb bars (10 clean 5m bars, EMA-9 rising at every step) then ONE more
    5m bar (5 x 1m) that flushes — pulling only the NEWEST 5m EMA-9 sample below the prior one
    while the prior->prior step is still UP (verified: ema9 tail = ...103.40, 103.92, 103.24 ->
    last step down, prior step up -> sustained-over-3 is False)."""
    rows = []
    px = 100.0
    for _ in range(50):
        rows.append((px, px + 0.2, px - 0.1, px + 0.12, 1000.0))
        px += 0.12
    top = px
    for _ in range(5):
        rows.append((top, top + 0.02, top - 1.2, top - 1.1, 1000.0))
        top -= 1.1
    return _dt_index(_df(rows))


def _htf_sustained_rolldown_1m() -> pd.DataFrame:
    """A 1m frame whose 5m EMA-9 declines across MULTIPLE consecutive HTF samples — a CLEAR,
    SUSTAINED bearish roll-down (not a single down-tick). FIX 2: this must still VETO."""
    rows = []
    px = 120.0
    # Brief rise so the EMA has somewhere to roll over FROM.
    for _ in range(20):
        rows.append((px, px + 0.2, px - 0.1, px + 0.15, 1000.0))
        px += 0.15
    # Then a long, steady decline so the 5m EMA-9 is strictly lower across each of the last
    # several samples (sustained multi-bar negative slope).
    for _ in range(40):
        rows.append((px, px + 0.1, px - 0.25, px - 0.20, 1000.0))
        px -= 0.20
    return _dt_index(_df(rows))


def test_htf_single_downtick_passes_fix2() -> None:
    """⭐ FIX 2 CORE: a SINGLE lagging 5m EMA-9 down-tick (one sample lower, prior steps up) is
    NOT a sustained roll-down -> it must NOT flag clearly-against -> PASS. (The old single-bar
    ``ema9[cur] < ema9[cur-1]`` would have vetoed this and killed the dip-rip.)"""
    veto, dbg = _htf_against_veto(_htf_single_downtick_1m())
    assert veto is False, dbg
    assert dbg.get("htf_against") != "ema9_sustained_rolldown", dbg
    # the helper still RECORDS the single-step slope as negative — proving it SAW the down-tick
    # and deliberately declined to veto on it (not a thin-frame no-read).
    assert dbg.get("htf_ema9_slope", 0.0) < 0.0, dbg


def test_htf_sustained_rolldown_still_vetoes_fix2() -> None:
    """⭐ FIX 2 GUARD: a genuinely-bearish SUSTAINED 5m roll-down (EMA-9 strictly lower across
    each of the last N samples) must STILL veto — the loosening only raised the bar on a
    single down-tick; it must not let a real downtrend through."""
    veto, dbg = _htf_against_veto(_htf_sustained_rolldown_1m())
    assert veto is True, dbg
    assert dbg.get("htf_against") in ("ema9_sustained_rolldown", "macd_peaked"), dbg


def test_htf_rolldown_bars_threshold_is_configurable() -> None:
    """The sustained-roll-down length is ONE documented base (default 3 samples), adaptive via
    the param. A single down-tick passes at the default but a rolldown_bars=2 (require only ONE
    down step) would catch it — proving the threshold drives the strictness (no fixed magic)."""
    df = _htf_single_downtick_1m()
    # default (3 samples / 2 consecutive down steps): a single down-tick PASSES.
    assert _htf_against_veto(df, rolldown_bars=3)[0] is False
    # require only a single down STEP (2 samples): now the lone down-tick DOES veto.
    veto2, dbg2 = _htf_against_veto(df, rolldown_bars=2)
    assert veto2 is True, dbg2
    assert dbg2.get("htf_against") == "ema9_sustained_rolldown", dbg2


def test_htf_non_datetime_index_fails_open() -> None:
    """No DatetimeIndex => cannot resample the HTF => fail-OPEN (never block)."""
    rows = [_base(100.0 + i * 0.1) for i in range(40)]
    veto, _ = _htf_against_veto(_df(rows))  # plain RangeIndex
    assert veto is False


def test_htf_thin_frame_fails_open() -> None:
    veto, _ = _htf_against_veto(_dt_index(_df([_base(100.0)])))
    assert veto is False


def test_resample_htf_none_on_non_datetime() -> None:
    assert _resample_htf(_df([_base(100.0 + i) for i in range(10)])) is None


def test_resample_htf_builds_5m_bars() -> None:
    htf = _resample_htf(_dt_index(_df([_base(100.0 + i * 0.1) for i in range(20)])))
    assert htf is not None
    assert len(htf) >= 2
    assert {"Open", "High", "Low", "Close"}.issubset(set(htf.columns))


# ──────────────────────────────────────────────────────────────────────────────
# INTEGRATION through pullback_break_confirmation — flag OFF byte-identical + ON behavior
# ──────────────────────────────────────────────────────────────────────────────
def _doji_break_rows() -> list[tuple[float, float, float, float, float]]:
    """Same firing structure but the break bar is a DOJI (opens and closes near mid-range
    with long wicks) that still pokes a new high over the pullback high."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]               # shallow pullback
    # break bar: high 111.2 pokes a NEW HIGH over the pullback high. The body is tiny and the
    # close sits in the LOWER half of the range (open 110.18, close 110.22, low 109.60) -> a
    # true doji that is NOT a strong full-body candle. GREEN (close >= open) so it does NOT
    # trip the always-on red-volume-exhaustion veto (which keys off a RED max-volume new-high
    # bar) — isolating the NEW doji gate as the only differing veto between flag OFF and ON.
    rows.append((110.18, 111.20, 109.60, 110.22, 3200.0))
    return rows


def test_flag_off_is_byte_identical(monkeypatch) -> None:
    """Flag OFF: a doji break that WOULD be vetoed when ON instead fires exactly as the legacy
    path (the new block is entirely skipped). Proves default-OFF == byte-identical entry."""
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_candle_quality_multitf_veto_enabled", False, raising=False
    )
    df = _df(_doji_break_rows())
    ok, reason, _ = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True, reason
    # the legacy path fires (a fire reason, never the new doji veto) -> byte-identical
    assert reason in ("pullback_break_ok", "first_pullback_ok")


def test_flag_on_vetoes_doji_break(monkeypatch) -> None:
    """Flag ON: the SAME doji break bar is now blocked by the doji veto."""
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_candle_quality_multitf_veto_enabled", True, raising=False
    )
    df = _df(_doji_break_rows())
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is False, (reason, dbg)
    assert reason == "doji_trigger_veto"


def test_flag_on_full_body_break_still_fires(monkeypatch) -> None:
    """Flag ON but a STRONG full-body break bar passes the doji gate (and a RangeIndex df means
    the HTF read fails open) -> the valid 1m-fast entry still fires. Proves the gates are
    SURGICAL (they block doji/HTF-against, not normal breaks)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_candle_quality_multitf_veto_enabled", True, raising=False
    )
    df = _df(_firing_rows())  # RangeIndex -> HTF fails open; strong body -> doji passes
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True, (reason, dbg)
    assert reason in ("pullback_break_ok", "first_pullback_ok")


# ──────────────────────────────────────────────────────────────────────────────
# FIX 1: _deep_reclaim EXEMPTION — the dip-buy reversal path is carved out of BOTH
# the HTF-against veto AND the doji veto (same `if not _deep_reclaim` guard the
# backside gates already use). A dip-rip catches the turn off a flush, so it EXPECTS
# a lagging/rolling-down HTF and an indecision bar at the bottom — those must NOT veto it.
# ──────────────────────────────────────────────────────────────────────────────
def _bearish_htf_df() -> pd.DataFrame:
    """A 1m frame with a DatetimeIndex whose 5m EMA-9 is in a SUSTAINED roll-down (clearly
    bearish HTF) — and enough bars (>=10) to enter pullback_break_confirmation."""
    return _htf_sustained_rolldown_1m()


def _stub_trigger(pattern: str, df: pd.DataFrame):
    """Build a fake _evaluate_break_retest that fires a completed-bar break with the given
    ``pattern`` in debug, levels just under the last close so the tick-break path is irrelevant.
    Returns (ok=True, reason, pb_high, pb_low, debug)."""
    last_close = float(df["Close"].iloc[-1])

    def _fake(high, low, close, ema9, cur, **kw):  # noqa: ANN001
        debug = {
            "entry_interval": kw.get("entry_interval", "5m"),
            "pattern": pattern,
            "pullback_high": last_close - 0.5,
            "pullback_low": last_close - 1.5,
        }
        return True, "pullback_break_ok", last_close - 0.5, last_close - 1.5, debug

    return _fake


def _neutralize_1m_backside(monkeypatch) -> None:
    """Make the EARLIER 1m backside gates (_detect_back_side + front_side_state lifecycle veto)
    inert so the NEW 5m HTF / doji gates are the only ones under test. Those 1m gates fire on a
    bearish 1m frame and would short-circuit BEFORE the HTF block — they have their own
    _deep_reclaim carve-out and their own tests; here we isolate the new block."""
    from app.services.trading.momentum_neural import ross_momentum as _rm

    monkeypatch.setattr(entry_gates, "_detect_back_side", lambda *a, **k: (False, ""), raising=True)
    monkeypatch.setattr(
        _rm, "front_side_state",
        lambda *a, **k: type("_FS", (), {"is_backside": False})(), raising=True,
    )


def _flag_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_candle_quality_multitf_veto_enabled", True, raising=False
    )
    # Isolate the new block: keep first-pullback from overriding our stubbed trigger, and
    # neutralize the earlier 1m backside gates (tested elsewhere) so the HTF/doji gate is the
    # one exercised.
    monkeypatch.setattr(
        settings, "chili_momentum_entry_first_pullback_enabled", False, raising=False
    )
    _neutralize_1m_backside(monkeypatch)


def test_deep_reclaim_dip_rip_exempt_from_htf_against(monkeypatch) -> None:
    """⭐ FIX 1 CORE: a deep_reclaim dip-rip (1m turned up, 5m EMA still rolling down off the
    flush, pattern='deep_reclaim') with the flag ON must NOT be killed by the HTF-against veto.
    The clearly-bearish HTF is EXACTLY what a dip-buy reversal expects — the exemption lets it
    through. We assert the reason is NEVER htf_against_veto (the gate it used to die on)."""
    _flag_on(monkeypatch)
    df = _bearish_htf_df()
    monkeypatch.setattr(
        entry_gates, "_evaluate_break_retest", _stub_trigger("deep_reclaim", df), raising=True
    )
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", require_retest=True)
    # the deep_reclaim path is EXEMPT -> it must never be vetoed by the HTF gate.
    assert reason != "htf_against_veto", (reason, dbg)
    assert dbg.get("htf") is None or "htf_against" not in dbg.get("htf", {}), dbg


def test_non_deep_reclaim_still_vetoed_by_bearish_htf(monkeypatch) -> None:
    """⭐ FIX 1 GUARD: the SAME clearly-bearish HTF, but a NON-deep-reclaim pattern, must STILL
    be vetoed by htf_against_veto — the exemption is surgical to the dip-buy path; a genuinely
    bearish HTF on an ordinary continuation entry is not let through."""
    _flag_on(monkeypatch)
    df = _bearish_htf_df()
    monkeypatch.setattr(
        entry_gates, "_evaluate_break_retest", _stub_trigger("bull_flag", df), raising=True
    )
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", require_retest=True)
    assert ok is False, (reason, dbg)
    assert reason == "htf_against_veto", (reason, dbg)


def test_deep_reclaim_dip_rip_exempt_from_doji(monkeypatch) -> None:
    """⭐ FIX 1 (doji symmetry): a deep_reclaim whose TRIGGER bar is a doji (an indecision
    candle at the very bottom of the flush — typical of the reversal) must NOT be vetoed by the
    doji gate when the flag is ON. RangeIndex df -> the HTF read fails open, so the ONLY gate in
    play is the doji one, which the deep_reclaim exemption carves out."""
    _flag_on(monkeypatch)
    # Doji break bar (tiny body, lower-half close) on a plain RangeIndex frame (HTF fails open).
    df = _df(_doji_break_rows())
    monkeypatch.setattr(
        entry_gates, "_evaluate_break_retest", _stub_trigger("deep_reclaim", df), raising=True
    )
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", require_retest=True)
    assert reason != "doji_trigger_veto", (reason, dbg)


def test_non_deep_reclaim_doji_still_vetoed(monkeypatch) -> None:
    """⭐ FIX 1 GUARD (doji): a NON-deep-reclaim doji break on a RangeIndex frame (HTF fails
    open) is STILL vetoed by the doji gate — the doji veto is only loosened for the dip-buy
    path, never weakened for ordinary entries."""
    _flag_on(monkeypatch)
    df = _df(_doji_break_rows())
    monkeypatch.setattr(
        entry_gates, "_evaluate_break_retest", _stub_trigger("bull_flag", df), raising=True
    )
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", require_retest=True)
    assert ok is False, (reason, dbg)
    assert reason == "doji_trigger_veto", (reason, dbg)


def test_flag_off_still_byte_identical_with_deep_reclaim(monkeypatch) -> None:
    """Flag OFF: even on the bearish-HTF / deep_reclaim path the whole new block is skipped ->
    the stubbed trigger's fire is returned unchanged (no htf/doji keys added). Re-proves
    default-OFF == byte-identical regardless of the exemption."""
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_candle_quality_multitf_veto_enabled", False, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_entry_first_pullback_enabled", False, raising=False
    )
    df = _bearish_htf_df()
    monkeypatch.setattr(
        entry_gates, "_evaluate_break_retest", _stub_trigger("bull_flag", df), raising=True
    )
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", require_retest=True)
    assert reason not in ("htf_against_veto", "doji_trigger_veto"), (reason, dbg)
    assert "htf" not in dbg and "doji" not in dbg, dbg
