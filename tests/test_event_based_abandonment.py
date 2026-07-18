"""Event/structure-based abandonment of pre-entry watchers (Ross stays on a strong
stock all day). Proves the keep-vs-reap building blocks the reaper uses:

  * conviction: ross_score>=floor OR rvol>=coiling-exempt extreme floor OR daily_breaking
    (SAME source the arm-queue / continuation gate read) — no per-session fetch;
  * front-side: fail-OPEN on absent snapshot, but AFFIRMATIVE backside (cached is_backside,
    retrace>veto, or last_mid<session_vwap) demotes a watcher to reap (no slot leak);
  * hard ceiling: derived adaptively from the extend window (one documented multiple);
  * kill-switch OFF => the helper is inert (the reaper never calls it).

These are pure-function proofs (no DB) so they run anywhere without TEST_DATABASE_URL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _event_based_abandonment_enabled,
    _event_based_max_extend_seconds,
    _session_still_front_side,
    _session_still_high_conviction,
    _watch_extend_seconds,
)


def _row(symbol: str, *, ross: float | None = None, rvol: float | None = None,
         daily_breaking: bool = False):
    """A fake MomentumSymbolViability carrying the SAME persisted shape the helpers read
    (execution_readiness_json.extra.ross_scores[SYM] + ross_signals[SYM])."""
    sig: dict = {"ticker": symbol}
    if rvol is not None:
        sig["vol_ratio"] = rvol
    if daily_breaking:
        sig["daily_breaking_major"] = True
    extra: dict = {"ross_signals": {symbol.upper(): sig}}
    if ross is not None:
        extra["ross_scores"] = {symbol.upper(): ross}
    return SimpleNamespace(symbol=symbol, execution_readiness_json={"extra": extra})


# ── kill-switch parity ────────────────────────────────────────────────────────────────
def test_flag_off_by_default():
    # default OFF => reaper never runs the event check => byte-identical fixed clock.
    assert _event_based_abandonment_enabled() is False


def test_flag_on_when_set(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_event_based_abandonment_enabled", True)
    assert _event_based_abandonment_enabled() is True


# ── conviction (same source as the arm-queue) ─────────────────────────────────────────
def test_high_conviction_by_ross_score(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    assert _session_still_high_conviction(_row("IVF", ross=0.85)) is True
    assert _session_still_high_conviction(_row("IVF", ross=0.50)) is False


def test_high_conviction_by_extreme_rvol(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_explosive_rvol_floor", 3.0)
    monkeypatch.setattr(settings, "chili_momentum_coiling_exempt_rvol_mult", 3.0)
    # floor = 3.0 * 3.0 = 9.0x
    assert _session_still_high_conviction(_row("IVF", rvol=9.5)) is True
    assert _session_still_high_conviction(_row("IVF", rvol=5.0)) is False


def test_high_conviction_by_daily_breaking():
    assert _session_still_high_conviction(_row("IVF", daily_breaking=True)) is True


def test_no_row_is_not_high_conviction():
    # a watcher with NO fresh candidate row => not high-conviction => falls through to reap.
    assert _session_still_high_conviction(None) is False


# ── front-side (fail-open; affirmative backside reaps) ────────────────────────────────
def test_front_side_fail_open_on_missing_snapshot():
    assert _session_still_front_side(None) is True
    assert _session_still_front_side({}) is True  # no backside evidence => keep-eligible


def test_cached_is_backside_flag_authoritative():
    assert _session_still_front_side({"is_backside": True}) is False
    assert _session_still_front_side({"is_backside": False}) is True


def test_faded_retrace_demotes(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_event_based_retrace_veto", 0.66)
    assert _session_still_front_side({"retrace_from_hod": 0.80}) is False  # faded
    assert _session_still_front_side({"retrace_from_hod": 0.40}) is True   # still near highs


def test_below_vwap_demotes():
    assert _session_still_front_side({"last_mid": 9.0, "session_vwap": 10.0}) is False
    assert _session_still_front_side({"last_mid": 11.0, "session_vwap": 10.0}) is True


def test_garbage_axis_is_ignored_fail_open():
    # unparseable values must not cut a keep candidate short.
    assert _session_still_front_side({"retrace_from_hod": "nan?", "last_mid": "x"}) is True


# ── hard ceiling (adaptive, one documented multiple) ──────────────────────────────────
def test_ceiling_is_multiple_of_extend(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 300)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600)
    monkeypatch.setattr(settings, "chili_momentum_event_based_max_extend_mult", 3.0)
    assert _watch_extend_seconds() == 600
    assert _event_based_max_extend_seconds() == 1800  # 3 * 600


def test_ceiling_never_tighter_than_extend(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600)
    monkeypatch.setattr(settings, "chili_momentum_event_based_max_extend_mult", 1.0)
    # mult 1.0 => ceiling == extend window (clamp floor), never below it.
    assert _event_based_max_extend_seconds() == _watch_extend_seconds()


# ── the composite keep rule the loop applies ──────────────────────────────────────────
def test_keep_requires_both_conviction_and_front_side(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    hot = _row("IVF", ross=0.9)
    cold = _row("DUD", ross=0.3)
    # high-conviction + front-side => KEEP eligible
    assert _session_still_high_conviction(hot) and _session_still_front_side({}) is True
    # high-conviction but FADED => reap (front-side False)
    assert _session_still_high_conviction(hot) and _session_still_front_side(
        {"is_backside": True}
    ) is False
    # front-side but COOLED out of conviction => reap (conviction False)
    assert _session_still_high_conviction(cold) is False


# ── VRAX 2026-07-09 winner-kill: the daily_breaking_major keep-leg ────────────────────
# EVIDENCE (prod, adversarially verified): 13:52:36Z the reaper cancelled the VRAX
# watcher (session 12067, watching_live) because the conviction row read ross 0.62-0.66
# < the 0.7 continuation floor and scanner vol_ratio ~1.61x < the ~9x coiling-exempt
# floor, and the persisted ross_signals entry did NOT carry daily_breaking_major=True —
# even though VRAX had broken its prior daily high hours earlier and sat +230% over the
# prior close (~10.10 vs prev close ~3.32). It then ran 10 -> 13.19 with no watcher on
# it (212 re-entry blocks). The stamp is refresh-batch-scoped and the extra blob is
# wholesale-replaced per upsert, so one un-stamped write erases the keep-leg.
#
# No daily OHLCV table exists in prod (checked 2026-07-17: only 10s tenbeat candles +
# ticks), so per the task contract the fixture daily bars are shaped from the recorded
# evidence: prev close ~3.32 (change_pct history +233-296%), prior-day high 3.60,
# session bid ~10.10 at 13:48-13:52Z (live_entry_bid_prop events), HOD 13.19.

VRAX_REAP_PRICE = 10.10       # session last_mid at the 13:52:36Z reap window
VRAX_PRIOR_DAY_HIGH = 3.60    # prior-day (07-08) high — broken that morning
VRAX_ROSS_AT_REAP = 0.62      # decayed composite the reaper read
VRAX_RVOL_AT_REAP = 1.61      # scanner vol_ratio the reaper read


def _vrax_daily_df():
    """VRAX-2026-07-09-shaped daily OHLCV: ~28 quiet sub-$4 bars, prior day high 3.60
    close 3.32, then today's partial bar (the runner day: high 13.19). Same columns
    ``daily_levels.compute_daily_context`` reads (Open/High/Low/Close/Volume)."""
    import pandas as pd

    rows = []
    for i in range(28):  # quiet base, all real volume (clears the half-window gate)
        rows.append({"Open": 3.20, "High": 3.40, "Low": 3.00, "Close": 3.20, "Volume": 250_000})
    # prior day (iloc[-2]): the level VRAX broke that morning
    rows.append({"Open": 3.20, "High": VRAX_PRIOR_DAY_HIGH, "Low": 3.10, "Close": 3.32, "Volume": 400_000})
    # today's partial bar (intraday fetch reality: last bar = the in-progress day)
    rows.append({"Open": 4.00, "High": 13.19, "Low": 3.90, "Close": VRAX_REAP_PRICE, "Volume": 60_000_000})
    idx = pd.date_range("2026-05-28", periods=len(rows), freq="B")
    return pd.DataFrame(rows, index=idx)


def _flat_daily_df():
    """A flat non-runner: same shape, but today's bar never leaves the base range."""
    import pandas as pd

    rows = []
    for i in range(29):
        rows.append({"Open": 3.20, "High": 3.60, "Low": 3.00, "Close": 3.20, "Volume": 250_000})
    rows.append({"Open": 3.20, "High": 3.25, "Low": 2.95, "Close": 3.05, "Volume": 200_000})
    idx = pd.date_range("2026-05-28", periods=len(rows), freq="B")
    return pd.DataFrame(rows, index=idx)


def _clear_daily_ctx_cache():
    from app.services.trading.momentum_neural.live_runner import (
        _current_decision_runtime_state,
    )

    _current_decision_runtime_state().daily_ctx_cache.clear()


def test_vrax_0709_step2_keep_fires_when_daily_leg_is_populated(monkeypatch):
    """STEP-2 PROOF: with CORRECT inputs the keep already fires TODAY. The recorded
    daily OHLCV derives breaking_major_level=True at the reap-window price, and a row
    carrying that stamp KEEPS despite the decayed ross 0.62 / rvol 1.61 composite —
    so the 07-09 reap was an INPUT-POPULATION failure (branch 3a), not a logic gap."""
    from app.services.trading.momentum_neural.daily_levels import compute_daily_context

    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    ctx = compute_daily_context(_vrax_daily_df(), lookback=20, price=VRAX_REAP_PRICE)
    # the leg IS derivable from recorded daily OHLCV (px 10.10 > prior-day high 3.60)...
    assert ctx.breaking_major_level is True
    # ...and the pipeline's stamp precondition holds (daily_structure_pct computed).
    assert ctx.daily_structure_pct is not None
    row = _row("VRAX", ross=VRAX_ROSS_AT_REAP, rvol=VRAX_RVOL_AT_REAP,
               daily_breaking=bool(ctx.breaking_major_level))
    assert _session_still_high_conviction(row) is True


def test_vrax_0709_reap_reproduced_when_stamp_missing(monkeypatch):
    """DEFECT REPRODUCTION: the exact row the reaper read at 13:52:36Z — decayed ross,
    flat scanner vol_ratio, NO daily_breaking_major key — fails the keep and the
    day's biggest runner is reaped mid-run (before the fix, with no current_price)."""
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    row = _row("VRAX", ross=VRAX_ROSS_AT_REAP, rvol=VRAX_RVOL_AT_REAP)  # stamp absent
    assert _session_still_high_conviction(row) is False


def test_vrax_0709_fix_derives_daily_breaking_end_to_end(monkeypatch):
    """THE FIX, END-TO-END: stamp absent (as observed live), but the reap loop passes
    the session's cached last_mid — the keep-leg re-derives breaking-major from the
    recorded daily levels (real _daily_ctx_cached -> real compute_daily_context on the
    fixture bars) and the VRAX watcher is KEPT. Front-side (still above VWAP at 13:52)
    completes the composite keep."""
    from app.services.trading.momentum_neural import live_runner

    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    monkeypatch.setattr(
        live_runner, "_replay_aware_fetch_ohlcv_df",
        lambda ticker, interval="1d", period="6mo": _vrax_daily_df(),
    )
    _clear_daily_ctx_cache()
    try:
        row = _row("VRAX", ross=VRAX_ROSS_AT_REAP, rvol=VRAX_RVOL_AT_REAP)  # stamp absent
        assert _session_still_high_conviction(row, current_price=VRAX_REAP_PRICE) is True
        # composite: still front-side at the reap window (10.10 above session VWAP)
        assert _session_still_front_side({"last_mid": 10.10, "session_vwap": 8.9}) is True
    finally:
        _clear_daily_ctx_cache()


def test_decayed_nonrunner_still_reaps(monkeypatch):
    """AVOID-BEHAVIOUR GUARD: a decayed-score NON-runner (flat price, never broke its
    daily levels) derives NO daily-breaking keep and still reaps; and even a name that
    DID break levels reaps once it fades below VWAP (the front-side leg — entry gates,
    not the reaper, remain the ZDAI-class avoid mechanism)."""
    from app.services.trading.momentum_neural import live_runner

    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    monkeypatch.setattr(
        live_runner, "_replay_aware_fetch_ohlcv_df",
        lambda ticker, interval="1d", period="6mo": _flat_daily_df(),
    )
    _clear_daily_ctx_cache()
    try:
        row = _row("DUD", ross=0.30, rvol=1.0)  # cooled composite, no stamp
        # flat price 3.05 < prior-day high 3.60 and < swing high => no derived keep
        assert _session_still_high_conviction(row, current_price=3.05) is False
        # faded runner: below VWAP fails the composite's front-side leg => reap
        assert _session_still_front_side({"last_mid": 9.0, "session_vwap": 10.0}) is False
    finally:
        _clear_daily_ctx_cache()


def test_derivation_is_fail_closed_and_monotonic(monkeypatch):
    """FAIL-CLOSED: a broken daily fetch derives nothing (keep unchanged = reap).
    MONOTONIC: a persisted True stamp is NEVER falsified by the derivation path."""
    from app.services.trading.momentum_neural import live_runner

    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)

    def _boom(ticker, interval="1d", period="6mo"):
        raise RuntimeError("provider down")

    monkeypatch.setattr(live_runner, "_replay_aware_fetch_ohlcv_df", _boom)
    _clear_daily_ctx_cache()
    try:
        row = _row("VRAX", ross=VRAX_ROSS_AT_REAP, rvol=VRAX_RVOL_AT_REAP)
        # fetch fails -> derivation False -> same reap as today (never raises)
        assert _session_still_high_conviction(row, current_price=VRAX_REAP_PRICE) is False
        # persisted stamp True stays True even when the derivation path is broken
        stamped = _row("VRAX", ross=VRAX_ROSS_AT_REAP, rvol=VRAX_RVOL_AT_REAP,
                       daily_breaking=True)
        assert _session_still_high_conviction(stamped, current_price=VRAX_REAP_PRICE) is True
    finally:
        _clear_daily_ctx_cache()
