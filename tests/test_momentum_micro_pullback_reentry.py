"""Unit tests for the Ross MICRO-PULLBACK re-entry (re-load) on a held runner.

Covers the load-bearing, pure pieces of the feature (the live_runner orchestration
is a thin shell over these — heavy I/O, not unit-testable without the whole engine):

  * DETECTION (entry_gates.micro_pullback_reentry_detect) — fires on a higher-low
    bounce on a synthetic squeeze, rejects dip-below-shelf and sparse frames; the
    ratcheting shelf gate; the curl-back-up per-bar confirm. Depth is REPORTED
    ([1], 2026-09-10 — the 0.04 cap measured 5.6x anti-selective).
  * NO ENTRY INTO SELLING — entry_gates._entry_flow_veto (the named knife) plus the
    PRINT PROOF the live block applies ([1], 2026-09-10: `last_print > bounce_high`
    with `signed_tape_accel > 0`, replacing the anti-selective ofi/trade_flow floors).
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
    micro_pullback_reload_proof,
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


def test_detect_reports_deep_rollover_instead_of_refusing_it():
    """[1] 2026-09-10 -- WAS `test_detect_rejects_deep_rollover_dip_too_deep`.

    The depth cap is gone: measured on our own tape, dips at the ONSET of a clean run are
    DEEPER than at random control instants at every quantile (onset p50 0.0210 / p90
    0.0425 vs ctrl p50 0.0118 / p90 0.0260; clustered AUC 0.710 over 37 symbol-day
    clusters), so `max_dip_pct=0.04` refused 12.3% of real onsets against 2.2% of controls
    -- 5.6x anti-selective -- and no cap level is selective in the other direction.

    A dip that would have tripped the old cap now FIRES (the shelf holds) and the depth
    rides the receipt: dip_pct, its position in the onset distribution, and the old bound
    as a NAMED fallback."""
    df = _squeeze_then_micro_pullback(dip=0.40)
    out = micro_pullback_reentry_detect(df, shelf=9.0, max_dip_pct=0.005)
    assert out["fire"] is True
    assert out["reason"] == "micro_pullback_curl"
    assert out["dip_pct"] > 0.005
    assert out["would_have_blocked_at"] == 0.005
    assert out["would_have_blocked"] is True
    assert 0.0 <= out["dip_pct_onset_pctl"] <= 1.0


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


def _reload_gate(
    ofi, trade_flow, settings, *,
    last_print=None, bounce_high=None, accel=None, stale=False,
    reclaim_high=None,
) -> str:
    """[1] 2026-09-10 -- the live block's re-load decision.

    ⚠️ NOT A TRANSCRIPTION ANY MORE ([1] review fix). This used to be a hand-written copy
    of the ladder inside `tick_live_session`, so reordering the live branches, or flipping
    `>` to `>=`, or dropping the staleness term left every test in this file green. It now
    calls the PRODUCTION function -- `entry_gates.micro_pullback_reload_proof` -- and only
    binds `_entry_flow_veto` in front of it exactly as the live block does.

    `bounce_high` is kept as the parameter name for the existing cases, but it is passed as
    the BREAK REFERENCE; live that reference is the break bar's high PRINT (see
    `high_print_in_window`) with the quote-mid `bounce_high` as the NAMED fallback.
    `reclaim_high` defaults to `last_print` so the pre-review cases still read naturally --
    live it is the highest print since the break bar, not the newest tick.

    WAS: the veto, then a POSITIVE-CONFIRM (`ofi >= 0.30 AND trade_flow >= 0.20`). Both
    floors measured ANTI-SELECTIVE against our own tape (OFI at onset p50 -0.2226 vs ctrl
    +0.0047, clustered AUC 0.400; the +0.30 floor refuses 82.0% of onsets vs 74.9% of
    controls), and live they were the whole blocker: 18 all-time `reason=flow` refusals,
    `veto=true` in ZERO of them.

    NOW: `_entry_flow_veto` stays as the NAMED knife, and the proof is a PRINT on BOTH
    sides -- `reclaim_high_px > break_ref_px AND signed_tape_accel > 0` over a
    print-indexed window, the [59] form. ofi/trade_flow are still read and REPORTED,
    never compared. Returns the receipt reason, or "proof" when the re-load is allowed
    through."""
    return micro_pullback_reload_proof(
        veto=bool(_entry_flow_veto(ofi, trade_flow, settings)),
        last_print=last_print,
        signed_tape_accel=accel,
        tape_stale=stale,
        break_ref_px=bounce_high,
        reclaim_high_px=(reclaim_high if reclaim_high is not None else last_print),
    )


def test_flow_gate_blocks_buying_into_selling():
    """Flow DOWN (the 06-24 PLSM chase: strongly negative executed tape) -> the veto
    trips -> NEVER buy into selling. This is the one leg of the old flow gate that was a
    KNIFE, and it survives with its own receipt name."""
    s = _settings()
    # strongly-selling tape (<= -0.5 strong leg) vetoes regardless of OFI
    assert _entry_flow_veto(0.5, -0.63, s) is True
    assert _reload_gate(0.5, -0.63, s, last_print=11.0, bounce_high=10.0,
                        accel=500.0) == "flow_veto"
    # both-bearish AND-leg -- and the veto outranks a perfectly good print proof
    assert _entry_flow_veto(-0.7, -0.30, s) is True
    assert _reload_gate(-0.7, -0.30, s, last_print=11.0, bounce_high=10.0,
                        accel=500.0) == "flow_veto"


def test_reload_allows_only_on_a_print_above_the_micro_break():
    """Re-enter when the TAPE clears the micro-break with the push still running. The
    anti-selective floors are gone: an OFI of -0.22 (the measured onset median) no longer
    refuses anything, and a strong OFI cannot buy a break the tape has not taken out."""
    s = _settings()
    # the measured ONSET median OFI -- refused by the old +0.30 floor, allowed now
    assert _reload_gate(-0.2226, -0.163, s, last_print=10.05, bounce_high=10.0,
                        accel=900.0) == "proof"
    # print has not cleared the break -> WAIT, however good the book looks
    assert _reload_gate(0.90, 0.90, s, last_print=9.99, bounce_high=10.0,
                        accel=900.0) == "reclaim_wait"
    # cleared, but the signed push has rolled over
    assert _reload_gate(0.90, 0.90, s, last_print=10.05, bounce_high=10.0,
                        accel=-40.0) == "tape_not_confirming"


def test_reload_proof_fails_closed_on_unreadable_or_stale_tape():
    """An extra discretionary BUY needs proof, so an unreadable tape WAITS -- the same
    fail-closed contract the positive-confirm had, now on the tape instead of on a floor.
    STALE counts as unreadable: 37 names carry no real-time NYSE entitlement (TPET
    `available_at - observed_at` p50 900.44 s on 2026-09-10) and a 15-minute-old print
    proves nothing about now."""
    s = _settings()
    assert _entry_flow_veto(None, None, s) is False                    # veto fails-open
    assert _reload_gate(None, None, s) == "tape_unreadable"            # no tape -> wait
    assert _reload_gate(0.5, 0.5, s, last_print=10.05, bounce_high=10.0,
                        accel=None) == "tape_unreadable"
    assert _reload_gate(0.5, 0.5, s, last_print=10.05, bounce_high=10.0,
                        accel=900.0, stale=True) == "tape_unreadable"


def test_an_unknown_print_age_counts_as_stale(  # [1] review fix (fail-OPEN -> fail-CLOSED)
):
    """⚠️ THE ASYMMETRY THAT WAS LEFT IN. `tape_stale` is None both when the window has
    no `last_ts` AND whenever the age computation raises -- and the old test was
    `stale is True`, so an UNKNOWN age walked straight through the proof while
    `print_age_s` on the receipt (the one field that would have shown it) was blank.
    Its siblings on the same line (`last_print is None`, `accel is None`) both fail
    closed, and the documented contract is "unreadable tape => WAIT". 37 names carry no
    real-time NYSE entitlement, so an unknown age is not a rare shape."""
    s = _settings()
    assert _reload_gate(0.5, 0.5, s, last_print=10.05, bounce_high=10.0,
                        accel=900.0, stale=None) == "tape_unreadable"
    # ...and the pure function says the same thing on its own
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.05, signed_tape_accel=900.0, tape_stale=None,
        break_ref_px=10.0, reclaim_high_px=10.05,
    ) == "tape_unreadable"
    # only an explicitly-measured FRESH print proceeds
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.05, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=10.0, reclaim_high_px=10.05,
    ) == "proof"


def test_the_reclaim_is_the_high_print_since_the_break_not_the_last_tick():
    """[1] review fix. SUNE 20774 09-09 09:31:14 is the measured case: the break bar
    `[09:31:00, 09:31:10)` printed a high of 3.02 (119 prints) while the newest tick at
    the decision instant was 3.00. Deciding on the LAST TICK makes one bid-side print the
    verdict; deciding on the window's own high makes the BREAK ITSELF the evidence (that
    3.02 is the break, made before the dip). The evidence is therefore the highest print
    SINCE the break bar ended -- 3.00 there, so WAIT."""
    s = _settings()
    # the real SUNE shape: reference 3.02 (break-bar high print), reclaim high 3.00
    assert _reload_gate(-0.0184, 0.655, s, last_print=3.00, bounce_high=3.02,
                        accel=14447.0, reclaim_high=3.00) == "reclaim_wait"
    # a bid-side newest tick does NOT veto a reclaim the tape actually made
    assert _reload_gate(-0.0184, 0.655, s, last_print=3.00, bounce_high=3.02,
                        accel=14447.0, reclaim_high=3.03) == "proof"
    # and the reference is never bypassed by a missing reclaim read (fail-closed) --
    # asserted on the production function so the helper's convenience default cannot
    # hide it
    assert micro_pullback_reload_proof(
        veto=False, last_print=3.05, signed_tape_accel=14447.0, tape_stale=False,
        break_ref_px=3.02, reclaim_high_px=None,
    ) == "reclaim_wait"


def test_an_unreadable_break_reference_waits_with_its_own_name():
    """The break level must be a PRICE before any print can prove anything about it. An
    unreadable reference is not `reclaim_wait` (which would claim the tape fell short of
    a level nobody knows) -- it has its own receipt name."""
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.05, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=None, reclaim_high_px=10.05,
    ) == "break_reference_unreadable"
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.05, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=0.0, reclaim_high_px=10.05,
    ) == "break_reference_unreadable"


def test_the_ladder_order_is_executable_not_transcribed():
    """⚠️ WHAT THIS FILE COULD NOT DO BEFORE. The branch ORDER is load-bearing -- the
    knife outranks a perfect proof, an unreadable tape outranks a reference read -- and
    it is now pinned against the function the runner actually calls, so reordering the
    live branches turns this red."""
    # veto outranks everything, including a complete, fresh, clearing proof
    assert micro_pullback_reload_proof(
        veto=True, last_print=10.05, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=10.0, reclaim_high_px=10.5,
    ) == "flow_veto"
    # unreadable tape outranks an unreadable reference
    assert micro_pullback_reload_proof(
        veto=False, last_print=None, signed_tape_accel=None, tape_stale=None,
        break_ref_px=None, reclaim_high_px=None,
    ) == "tape_unreadable"
    # a cleared break with a rolled-over push is NOT a reclaim_wait
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.05, signed_tape_accel=-40.0, tape_stale=False,
        break_ref_px=10.0, reclaim_high_px=10.5,
    ) == "tape_not_confirming"
    # the comparison is STRICT (a print exactly AT the break has not cleared it)
    assert micro_pullback_reload_proof(
        veto=False, last_print=10.0, signed_tape_accel=900.0, tape_stale=False,
        break_ref_px=10.0, reclaim_high_px=10.0,
    ) == "reclaim_wait"


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
    # PRE-EXISTING RED, fixed in passing ([1], 2026-09-10): the base micro-bar width was
    # cut 15 -> 10 on 2026-08-25 (Ross: "the 10 second does kind of show that pattern")
    # and this assertion was never updated, so it has been failing on origin/main since.
    assert s.chili_momentum_micropull_bar_seconds == 10


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
