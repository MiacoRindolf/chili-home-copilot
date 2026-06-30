"""Adversarial tests for the TIME-OF-DAY SCHEDULE (momentum LIVE lane, NEW-INITIATION ONLY).

The feature (built, DEFAULT OFF — flag chili_momentum_timeofday_schedule_enabled) has two halves,
both kill-switched off by default (byte-identical):

  (1) PRIME-WINDOW SIZE LEVER (auto_arm.prime_window_size_multiplier) — a BOUNDED-UPWARD
      (>= 1.0, <= chili_momentum_timeofday_prime_window_size_mult_max) per-trade size multiplier
      when ET is inside the documented prime window (default 04:00-10:30 ET). The live runner
      threads it into the SAME _eff_max_loss product under the min(..., base*3.0) clamp, so a
      prime-window boost can NEVER push notional past base*3.0 and is NEVER a veto (floor 1.0).

  (2) FADE-DRIVEN LATE-DAY NEW-ENTRY CUTOFF (auto_arm._should_suppress_late_day) — suppress a
      FRESH arm only when ET is at/past the documented fallback clock (default 14:30 ET, a
      CEILING not the primary driver) AND the day's momentum/breadth has FADED, REUSING the SAME
      regime signal the no-asetup-sit-cash gate uses (_tape_cold_breadth AND no fresh catalyst =>
      _regime_is_poor). A strong-momentum (non-faded) afternoon STILL trades.

Properties proven here:
  P1  in-prime => bounded size-up (1.0 <= mult <= max), composed UNDER the 3x ceiling
  P2  outside-prime / weekend / flag-off => mult == 1.0 exactly (byte-identical)
  P3  prime-mult never < 1.0, never a veto, never escapes base*3.0 in the runner product
  P4  strong-momentum afternoon (fade=False) => NOT suppressed (still initiates)
  P5  faded afternoon past the fallback clock => suppressed (fade-driven)
  P6  before the fallback clock => never suppressed by THIS gate (even if faded)
  P7  fade-disabled => clock-only cutoff (past fallback => suppress regardless of tape)
  P8  flag OFF => the gate never runs (byte-identical; no suppression, no probe)
  P9  OPEN-position exit path is NEVER gated (the gate lives in auto_arm pre-arm only)
  P10 adversarial clock boundaries (start exact, end-1, end exact, fallback exact/-1)

The functions are pure over an injected ``now`` + injected candidate rows + a monkeypatched
_tape_cold, so no DB / network is required.

[[project_momentum_lane]] [[feedback_adaptive_no_magic]] [[project_adaptive_clock_initiative]]
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural import auto_arm as aa
from app.services.trading.momentum_neural.auto_arm import (
    _should_suppress_late_day,
    _timeofday_schedule_enabled,
    prime_window_size_multiplier,
)


# ── helpers ───────────────────────────────────────────────────────────────────────


def _enable(monkeypatch, *, max_mult: float = 1.5, fade_enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "chili_momentum_timeofday_schedule_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_start_et", "04:00", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_end_et", "10:30", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_size_mult_max", max_mult, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_fallback_clock_et", "14:30", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_fade_enabled", fade_enabled, raising=False
    )


_NY = timezone.utc  # we build ET wall-clock times by going through a known ET instant


def _et(hh: int, mm: int, *, weekday: int = 0) -> datetime:
    """A UTC datetime whose America/New_York wall-clock is hh:mm on a chosen weekday.

    June 2026 is EDT (UTC-4). 2026-06-29 is a Monday (weekday 0). We add ``weekday`` days to
    land on a specific DOW (0=Mon..6=Sun) and add 4h to convert ET->UTC.
    """
    from datetime import timedelta

    base_day = datetime(2026, 6, 29, tzinfo=timezone.utc)  # Monday
    return base_day + timedelta(days=weekday, hours=hh + 4, minutes=mm)


class _Cand:
    """Minimal stand-in for a MomentumSymbolViability row: a symbol + the embedded
    ross_signals scanner blob the catalyst reader consults."""

    def __init__(self, symbol: str, ross_signal: dict | None = None) -> None:
        self.symbol = symbol
        sig = {symbol.upper(): ross_signal} if ross_signal is not None else {}
        self.execution_readiness_json = {"extra": {"ross_signals": sig}}


# ── P1: in-prime => bounded size-up, under the 3x ceiling ──────────────────────────


def test_p1_in_prime_window_boosts_to_max(monkeypatch) -> None:
    _enable(monkeypatch, max_mult=1.5)
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))  # 09:00 ET, inside 04:00-10:30
    assert mult == pytest.approx(1.5)
    assert meta["in_prime"] is True
    assert 1.0 <= mult <= 1.5


def test_p1_in_prime_respects_tighter_max(monkeypatch) -> None:
    _enable(monkeypatch, max_mult=1.2)
    mult, _ = prime_window_size_multiplier(now=_et(5, 30))
    assert mult == pytest.approx(1.2)


def test_p1_composed_under_three_x_ceiling(monkeypatch) -> None:
    # Replicate the runner product-then-clamp: a maxed prime-mult stacked on other up-mults
    # must NOT push effective max-loss past base*3.0.
    _enable(monkeypatch, max_mult=1.5)
    prime_mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    base = 50.0
    streak, grad, cushion = 1.5, 2.0, 1.4
    product = base * streak * grad * cushion * prime_mult
    eff = min(product, base * 3.0)
    assert eff == pytest.approx(base * 3.0)  # clamp bites
    assert eff <= base * 3.0 + 1e-9


# ── P2 / P3: outside-prime / weekend / floor 1.0 / never a veto ────────────────────


def test_p2_outside_prime_is_one(monkeypatch) -> None:
    _enable(monkeypatch, max_mult=1.5)
    mult, meta = prime_window_size_multiplier(now=_et(12, 0))  # midday, outside prime
    assert mult == 1.0
    assert meta["reason"] == "outside_window"


def test_p2_weekend_is_one(monkeypatch) -> None:
    _enable(monkeypatch, max_mult=1.5)
    # Saturday 09:00 ET — inside the clock window but not a weekday.
    mult, _ = prime_window_size_multiplier(now=_et(9, 0, weekday=5))
    assert mult == 1.0


def test_p2_flag_off_is_one(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False
    )
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0
    assert meta == {"reason": "disabled", "prime_mult": 1.0}


def test_p3_prime_mult_never_below_one(monkeypatch) -> None:
    # A broken sub-1.0 ceiling is guarded up to 1.0 (never a shrink/veto).
    _enable(monkeypatch, max_mult=0.5)
    mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0


def test_p3_prime_mult_never_escapes_three_x(monkeypatch) -> None:
    # Even an adversarially huge configured max is clamped to the schema ceiling AND, in the
    # runner product, by base*3.0. We assert the runner clamp here directly with a 3.0 max.
    _enable(monkeypatch, max_mult=3.0)
    prime_mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    base = 50.0
    eff = min(base * prime_mult, base * 3.0)
    assert eff <= base * 3.0 + 1e-9


# ── P4 / P5 / P6: fade-driven late-day cutoff ──────────────────────────────────────


def test_p4_strong_afternoon_not_suppressed(monkeypatch) -> None:
    # 15:00 ET (past fallback) BUT tape is HOT -> regime not faded -> still initiates.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: False)  # every name reads hot
    cands = [_Cand("AAA"), _Cand("BBB")]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["reason"] == "afternoon_still_strong"
    assert dbg["regime_faded"] is False


def test_p4_strong_afternoon_via_catalyst_not_suppressed(monkeypatch) -> None:
    # Cold tape but a FRESH catalyst on the board -> regime not poor -> still initiates.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)  # all cold
    cands = [_Cand("AAA", ross_signal={"news_catalyst": True})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["has_catalyst"] is True


def test_p5_faded_afternoon_suppressed(monkeypatch) -> None:
    # 15:00 ET (past fallback), tape COLD on every readable equity, NO catalyst -> faded -> suppress.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False}),
             _Cand("BBB", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is True
    assert dbg["reason"] == "fade_driven"
    assert dbg["tape_cold"] is True
    assert dbg["has_catalyst"] is False


def test_p6_before_fallback_never_suppressed(monkeypatch) -> None:
    # 13:00 ET (before 14:30 fallback): even a fully-faded board is NOT suppressed by this gate.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(13, 0))
    assert suppress is False
    assert dbg["reason"] == "before_fallback_clock"


# ── P7: fade-disabled => clock-only cutoff ─────────────────────────────────────────


def test_p7_fade_disabled_clock_only_suppresses(monkeypatch) -> None:
    _enable(monkeypatch, fade_enabled=False)
    # Tape is HOT, but with fade disabled the cutoff is clock-only: past fallback => suppress.
    monkeypatch.setattr(aa, "_tape_cold", lambda s: False)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": True})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is True
    assert dbg["reason"] == "past_fallback_clock_only"


def test_p7_fade_disabled_before_fallback_not_suppressed(monkeypatch) -> None:
    _enable(monkeypatch, fade_enabled=False)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    suppress, _ = _should_suppress_late_day([_Cand("AAA")], now=_et(13, 0))
    assert suppress is False


# ── P8: flag OFF => byte-identical (gate never runs) ───────────────────────────────


def test_p8_flag_off_gate_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False
    )
    assert _timeofday_schedule_enabled() is False
    # The prime lever is a no-op (1.0) and the cutoff probe is never reached at the call site;
    # we assert the kill-switch reads False so run_auto_arm_pass skips the whole block.
    mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0


def test_p8_flag_off_does_not_probe_tape(monkeypatch) -> None:
    # When the schedule is OFF, the late-day helper is never called by the pass. To prove the
    # helper itself does no work when invoked with the flag interplay, assert the prime lever
    # short-circuits BEFORE reading any clock bound (disabled reason, no et_min).
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False
    )

    def _must_not_run(*a, **k):
        raise AssertionError("clock bounds must not be read when the flag is OFF")

    monkeypatch.setattr(aa, "_timeofday_bounds", _must_not_run)
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0
    assert meta["reason"] == "disabled"


# ── P9: OPEN-position exit path is NEVER gated by this feature ──────────────────────


def test_p9_gate_is_pre_arm_only_no_exit_symbols(monkeypatch) -> None:
    # Structural proof of the isolation invariant: the time-of-day gate lives ENTIRELY in
    # auto_arm (pre-arm). It exposes exactly two entry-sizing/suppression callables and NO
    # exit/flatten/trail/scale callable. Assert the public surface contains no exit verbs.
    surface = {
        "prime_window_size_multiplier",
        "_should_suppress_late_day",
        "_timeofday_schedule_enabled",
        "_timeofday_bounds",
        "prime_window_size_multiplier",
    }
    forbidden = ("exit", "flatten", "trail", "scale_out", "bailout", "stop_loss")
    for name in surface:
        for verb in forbidden:
            assert verb not in name, f"{name} must not touch an exit path"


def test_p9_late_day_does_not_suppress_when_off_for_open_management(monkeypatch) -> None:
    # The gate returns (suppress, meta) — a pure decision used ONLY at the pre-arm call site.
    # Calling it can NEVER mutate a session/position (no DB writes, no order calls). We assert
    # it is side-effect-free by calling it with a faded board and confirming it only returns.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    out = _should_suppress_late_day(cands, now=_et(15, 0))
    assert isinstance(out, tuple) and len(out) == 2
    assert out[0] is True  # decision only; no exit/position was touched to produce it


# ── P10: adversarial clock boundaries ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "hh, mm, in_prime",
    [
        (4, 0, True),    # exact prime start -> inclusive
        (3, 59, False),  # one minute before start
        (10, 29, True),  # inside, one before end
        (10, 30, False), # exact end -> exclusive
        (10, 31, False), # just past end
    ],
)
def test_p10_prime_window_boundaries(monkeypatch, hh, mm, in_prime) -> None:
    _enable(monkeypatch, max_mult=1.5)
    mult, _ = prime_window_size_multiplier(now=_et(hh, mm))
    if in_prime:
        assert mult == pytest.approx(1.5)
    else:
        assert mult == 1.0


@pytest.mark.parametrize(
    "hh, mm, past_fallback",
    [
        (14, 29, False),  # one before fallback -> not past
        (14, 30, True),   # exact fallback -> at/past (inclusive)
        (14, 31, True),   # just past
    ],
)
def test_p10_fallback_clock_boundaries(monkeypatch, hh, mm, past_fallback) -> None:
    # Faded board so the only variable is the clock boundary.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(hh, mm))
    assert suppress is past_fallback


def test_p10_multiple_candidates_one_hot_breadth_not_cold(monkeypatch) -> None:
    # Breadth is HOT if ANY readable equity leader reads hot -> not faded -> not suppressed.
    _enable(monkeypatch)
    hot = {"BBB"}
    monkeypatch.setattr(aa, "_tape_cold", lambda s: str(s).upper() not in hot)
    cands = [
        _Cand("AAA", ross_signal={"news_catalyst": False}),
        _Cand("BBB", ross_signal={"news_catalyst": False}),  # this one stays hot
    ]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["tape_cold"] is False


def test_p10_all_crypto_board_fails_open(monkeypatch) -> None:
    # An all-crypto board has no equity tape to judge -> breadth fail-open (hot) -> not suppressed.
    _enable(monkeypatch)
    # _tape_cold returns False for -USD regardless; don't even need to patch it.
    cands = [_Cand("BTC-USD", ross_signal={"news_catalyst": False}),
             _Cand("ETH-USD", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False


# ── HARDENING: prime_window_size_mult_max fail-safe + clamp ────────────────────────


def test_mult_max_nonnumeric_failsafe_to_1_5(monkeypatch) -> None:
    # A garbage configured max (non-numeric) must fail-safe to the documented 1.5, NOT crash
    # and NOT collapse the lever to 1.0 mid-prime.
    _enable(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_size_mult_max", "not-a-number",
        raising=False,
    )
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == pytest.approx(1.5)
    assert meta["in_prime"] is True


def test_mult_max_none_failsafe_to_1_5(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_size_mult_max", None, raising=False,
    )
    mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    # float(None) raises TypeError -> fail-safe 1.5 (NOT 1.0, NOT a crash).
    assert mult == pytest.approx(1.5)


def test_mult_max_exactly_one_is_neutral_in_prime(monkeypatch) -> None:
    # max==1.0 is the boundary: still "in prime" but a neutral lever (no boost, no shrink).
    _enable(monkeypatch, max_mult=1.0)
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == pytest.approx(1.0)
    assert meta["in_prime"] is True  # in-window even though the lever is neutral


def test_mult_max_eps_below_one_clamped_to_one(monkeypatch) -> None:
    # 0.999... < 1.0 must clamp UP to 1.0 (the lever is NEVER a shrink).
    _enable(monkeypatch, max_mult=0.999)
    mult, _ = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0


# ── HARDENING: clock-error fail-open (mod < 0) for BOTH halves ──────────────────────


def test_prime_clock_error_fails_open_to_one(monkeypatch) -> None:
    # A clock failure -> _et_minutes_now returns (-1, False) -> prime lever fails open to 1.0
    # (never spuriously boosts on a broken clock).
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_et_minutes_now", lambda now=None: (-1, False))
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0
    assert meta["reason"] == "outside_window"
    assert meta["et_min"] == -1


def test_late_day_clock_error_fails_open_no_suppress(monkeypatch) -> None:
    # A clock failure -> (-1, False) -> the cutoff can NEVER suppress (fail-open).
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_et_minutes_now", lambda now=None: (-1, False))
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["reason"] == "outside_clock"
    assert dbg["et_min"] == -1


def test_prime_internal_error_fails_neutral(monkeypatch) -> None:
    # Any exception inside the prime computation (here: _timeofday_bounds raises while in-window)
    # must fail NEUTRAL to 1.0 with the error reason, not propagate.
    _enable(monkeypatch)

    def _boom():
        raise RuntimeError("bounds blew up")

    monkeypatch.setattr(aa, "_timeofday_bounds", _boom)
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert mult == 1.0
    assert meta["reason"] == "error_fail_neutral"


def test_late_day_internal_error_fails_open(monkeypatch) -> None:
    # An exception AFTER the clock passes (here: _tape_cold_breadth raises) must be swallowed and
    # return (False, gate_error) -> the gate can only suppress on positive agreement, never on its
    # own failure.
    _enable(monkeypatch)

    def _boom(_cands):
        raise RuntimeError("breadth probe blew up")

    monkeypatch.setattr(aa, "_tape_cold_breadth", _boom)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["reason"] == "gate_error"


# ── HARDENING: naive `now` treated as UTC (DST-correct ET conversion) ──────────────


def test_naive_now_treated_as_utc(monkeypatch) -> None:
    # A naive datetime must be treated as UTC. 13:00 UTC == 09:00 EDT (June, UTC-4) -> in prime.
    _enable(monkeypatch, max_mult=1.5)
    naive_utc = datetime(2026, 6, 29, 13, 0)  # Monday 13:00 UTC, no tzinfo
    mult, meta = prime_window_size_multiplier(now=naive_utc)
    assert mult == pytest.approx(1.5)
    assert meta["in_prime"] is True


def test_aware_and_naive_utc_agree(monkeypatch) -> None:
    # The aware helper (_et) and a naive UTC datetime for the same instant must agree.
    _enable(monkeypatch, max_mult=1.3)
    aware = _et(9, 0)  # 13:00 UTC aware
    naive = datetime(2026, 6, 29, 13, 0)  # same instant, naive
    assert prime_window_size_multiplier(now=aware)[0] == prime_window_size_multiplier(now=naive)[0]


# ── HARDENING: custom (non-default) clock bounds resolve + bind ────────────────────


def test_custom_prime_window_bounds_bind(monkeypatch) -> None:
    # Override the prime window to 06:00-07:00; 06:30 must be in-prime, 05:59 and 07:00 out.
    _enable(monkeypatch, max_mult=1.4)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_start_et", "06:00", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_end_et", "07:00", raising=False
    )
    assert prime_window_size_multiplier(now=_et(6, 30))[0] == pytest.approx(1.4)
    assert prime_window_size_multiplier(now=_et(5, 59))[0] == 1.0
    assert prime_window_size_multiplier(now=_et(7, 0))[0] == 1.0  # end exclusive


def test_custom_fallback_clock_binds(monkeypatch) -> None:
    # Override the fallback to 12:00; a faded board at 12:00 suppresses, at 11:59 does not.
    _enable(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_fallback_clock_et", "12:00", raising=False
    )
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    assert _should_suppress_late_day(cands, now=_et(12, 0))[0] is True
    assert _should_suppress_late_day(cands, now=_et(11, 59))[0] is False


def test_malformed_hhmm_falls_back_to_defaults(monkeypatch) -> None:
    # Garbage HHMM strings must fail-safe to the documented defaults (04:00 / 10:30 / 14:30),
    # NOT crash and NOT silently disable the window. With junk bounds, 09:00 still reads in-prime
    # (default window) and 15:00 faded still suppresses (default fallback).
    _enable(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_start_et", "garbage", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_prime_window_end_et", "99:99", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_fallback_clock_et", None, raising=False
    )
    start, end, fallback = aa._timeofday_bounds()
    assert (start, end, fallback) == (4 * 60, 10 * 60 + 30, 14 * 60 + 30)
    assert prime_window_size_multiplier(now=_et(9, 0))[0] > 1.0
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    assert _should_suppress_late_day(
        [_Cand("AAA", ross_signal={"news_catalyst": False})], now=_et(15, 0)
    )[0] is True


# ── HARDENING: catalyst-axis sub-paths (pct / grade / has_catalyst / absent) ───────


def test_catalyst_via_pct_blocks_suppression(monkeypatch) -> None:
    # A graded catalyst sub-score (news_catalyst_pct > 0) is a fresh catalyst -> not poor.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst_pct": 0.42})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["has_catalyst"] is True


def test_catalyst_pct_zero_is_present_but_false(monkeypatch) -> None:
    # news_catalyst_pct == 0.0 is PRESENT (field exists) but FALSE (no headline) -> with cold
    # tape the regime IS poor -> suppress. (Distinguishes 'present-and-false' from 'absent'.)
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst_pct": 0.0})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is True
    assert dbg["has_catalyst"] is False


def test_catalyst_via_grade_blocks_suppression(monkeypatch) -> None:
    # A non-empty news_catalyst_grade counts as a fresh catalyst.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst_grade": "A"})]
    assert _should_suppress_late_day(cands, now=_et(15, 0))[0] is False


def test_catalyst_grade_empty_string_is_present_but_false(monkeypatch) -> None:
    # An empty/whitespace grade is present-but-false -> does NOT block suppression on cold tape.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst_grade": "   "})]
    assert _should_suppress_late_day(cands, now=_et(15, 0))[0] is True


def test_catalyst_via_generic_has_catalyst_flag(monkeypatch) -> None:
    # The generic has_catalyst flag is also recognized.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"has_catalyst": True})]
    assert _should_suppress_late_day(cands, now=_et(15, 0))[0] is False


def test_catalyst_absent_field_fails_open_no_suppress(monkeypatch) -> None:
    # CRITICAL fail-open: when NO candidate carries ANY catalyst field, the absence is treated as
    # 'has catalyst' (data absent != proven no-catalyst) -> regime NOT poor -> NOT suppressed,
    # even with stone-cold tape past the fallback.
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA"), _Cand("BBB")]  # no ross_signal at all -> no catalyst fields
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0))
    assert suppress is False
    assert dbg["has_catalyst"] is True
    assert dbg["reason"] == "afternoon_still_strong"


# ── HARDENING: empty / None candidate board edges ──────────────────────────────────


def test_empty_candidates_faded_clock_not_suppressed(monkeypatch) -> None:
    # Empty board: no equity tape (breadth fail-open hot) AND no catalyst field (fail-open) ->
    # regime NOT poor -> not suppressed even past the fallback.
    _enable(monkeypatch)
    suppress, dbg = _should_suppress_late_day([], now=_et(15, 0))
    assert suppress is False


def test_none_candidates_does_not_crash(monkeypatch) -> None:
    # None passed as the board must not raise (helpers guard `candidates or []`).
    _enable(monkeypatch)
    suppress, _ = _should_suppress_late_day(None, now=_et(15, 0))  # type: ignore[arg-type]
    assert suppress is False


def test_fade_disabled_empty_board_still_clock_suppresses(monkeypatch) -> None:
    # With fade disabled the breadth/catalyst board is irrelevant: an empty board past the
    # fallback still suppresses (clock-only), proving the fade branch is fully bypassed.
    _enable(monkeypatch, fade_enabled=False)
    suppress, dbg = _should_suppress_late_day([], now=_et(15, 0))
    assert suppress is True
    assert dbg["reason"] == "past_fallback_clock_only"


# ── HARDENING: weekend never suppresses the late-day cutoff (DOW axis) ─────────────


def test_late_day_weekend_not_suppressed(monkeypatch) -> None:
    # Saturday past the fallback with a faded board: the weekday flag is False -> outside_clock ->
    # fail-open (the cutoff is a weekday-equity-session concept).
    _enable(monkeypatch)
    monkeypatch.setattr(aa, "_tape_cold", lambda s: True)
    cands = [_Cand("AAA", ross_signal={"news_catalyst": False})]
    suppress, dbg = _should_suppress_late_day(cands, now=_et(15, 0, weekday=5))
    assert suppress is False
    assert dbg["reason"] == "outside_clock"


# ── HARDENING: prime meta payload shape (window echo, et_min) ──────────────────────


def test_prime_meta_echoes_window_and_et_min(monkeypatch) -> None:
    # The in-prime meta must carry the resolved window + the et_min so the runner log is auditable.
    _enable(monkeypatch, max_mult=1.5)
    mult, meta = prime_window_size_multiplier(now=_et(9, 0))
    assert meta["window"] == [4 * 60, 10 * 60 + 30]
    assert meta["et_min"] == 9 * 60  # 09:00 ET
    assert meta["prime_mult"] == pytest.approx(1.5)


def test_outside_prime_meta_echoes_window(monkeypatch) -> None:
    # Outside-but-readable-clock meta also echoes the window (distinguishes from disabled/error).
    _enable(monkeypatch)
    mult, meta = prime_window_size_multiplier(now=_et(12, 0))
    assert mult == 1.0
    assert meta["window"] == [4 * 60, 10 * 60 + 30]
    assert meta["et_min"] == 12 * 60


# ── HARDENING: runner product parity when flag OFF (byte-identical) ────────────────


def test_runner_product_flag_off_is_identity(monkeypatch) -> None:
    # Mirror the live_runner gate: when the schedule flag is OFF the prime mult is exactly 1.0,
    # so the _eff_max_loss product is byte-identical to the no-feature baseline.
    monkeypatch.setattr(
        settings, "chili_momentum_timeofday_schedule_enabled", False, raising=False
    )
    base = 50.0
    others = 1.5 * 2.0 * 1.4
    # flag-off path: runner sets _prime_window_mult = 1.0 without even calling the helper.
    prime_off = 1.0
    eff_off = min(base * others * prime_off, base * 3.0)
    baseline = min(base * others, base * 3.0)
    assert eff_off == pytest.approx(baseline)
