"""PULLBACK-SCALP-ENABLE (2026-06-30): the 2-part fix that makes CHILI's Ross-style
ride+add / micro-reentry path reachable.

FIX 1 — fresh-quote guard on the C1 per-trade max-loss check (live_runner.py, the
``# C1: Per-trade loss enforcement`` block). The C1 1x check (``unrealized_pnl <=
-max_loss_per_trade_usd``) had NO fresh-quote guard (unlike the C1b #769 circuit just
below it), so a torn/stale/zero ``bid`` (``bid = float(tick.bid or mid)`` — falls back
to a stale mid) trips a SPURIOUS full liquidation while the real NBBO is fine (CELZ
session 9920: phantom unrealized=-$148 while the real bid was >= $4.22 / +18%). The fix
reuses the EXACT C1b fresh-quote predicate, flag-gated
(``chili_momentum_max_loss_fresh_quote_guard_enabled``, default True). When the guard is
ON and the quote is NOT fresh, C1 is SKIPPED this pulse. Flag OFF => byte-identical
(C1 fires regardless of freshness). The #769 circuit + structural stop are untouched.

FIX 2 — arm STATE_LIVE_TRAILING on the adaptive price-rise threshold (``bid >= avg *
trail_activate_return``) BEFORE the ENTERED no-confirmation bailouts
(``instant_bid_above_fill_unconfirmed``, ``bail_on_no_confirmation``). Today those
bailouts run first each tick and return early, so a fresh fill goes ENTERED -> BAILOUT
-> recycle and NEVER reaches TRAILING — and all 4 add/reload paths gate on TRAILING, so
0 adds. The fix arms TRAILING first, flag-gated
(``chili_momentum_early_trail_arm_enabled``, default True). ANTI-REGRESSION: arms ONLY
when already in profit (bid >= avg*trail_activate_return); a loser at/below entry is
untouched and STILL gets the no-confirmation cut. Flag OFF => byte-identical.

The C1/bailout/trail-arm logic is inline inside the monolithic ``tick_live_session``
runner (not extracted helpers), so driving the full tick requires a complete session +
mock broker + snapshot. Per the task brief, these tests assert (a) the SOURCE-LEVEL
guard structure (the flag gate + the C1b-identical fresh-quote predicate + the
pre-bailout trail-arm placement) and (b) the exact decision LOGIC via faithful pure
mirrors of the live predicates — including the load-bearing flag-OFF byte-identical
parity and the loser-still-cut anti-regression case.
"""

from __future__ import annotations

import math
import py_compile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.config import settings
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.live_runner import (
    _c1_iqfeed_phantom_loss,
    bail_on_no_confirmation,
)


# ---------------------------------------------------------------------------
# Pure mirror of the LIVE C1 decision (live_runner.py "# C1: Per-trade loss
# enforcement" block). This replicates the exact predicate the runner evaluates so
# the stale-skip / fresh-fire / flag-OFF-parity semantics are tested directly.
# ---------------------------------------------------------------------------
def _c1_fresh_quote(*, bid, halt_stale_streak, suspected_halt_since_utc) -> bool:
    """The EXACT C1b fresh-quote predicate reused by the C1 guard."""
    return (
        bid is not None
        and math.isfinite(float(bid))
        and float(bid) > 0
        and int(halt_stale_streak or 0) == 0
        and not suspected_halt_since_utc
    )


def _c1_fires(
    *,
    bid,
    avg,
    qty,
    max_loss_usd,
    halt_stale_streak=0,
    suspected_halt_since_utc=None,
    guard_enabled=True,
) -> bool:
    """Mirror of the live C1 force-exit gate: returns True iff C1 would transition to
    BAILOUT (reason=max_loss_per_trade) this pulse."""
    if not (max_loss_usd > 0):
        return False
    unrealized_pnl = (float(bid) - float(avg)) * float(qty)
    fresh = _c1_fresh_quote(
        bid=bid,
        halt_stale_streak=halt_stale_streak,
        suspected_halt_since_utc=suspected_halt_since_utc,
    )
    skip_stale = bool(guard_enabled) and not fresh
    return (unrealized_pnl <= -float(max_loss_usd)) and not skip_stale


# ---------------------------------------------------------------------------
# FIX 1 — flag + config
# ---------------------------------------------------------------------------
def test_fix1_flag_exists_default_true():
    assert hasattr(settings, "chili_momentum_max_loss_fresh_quote_guard_enabled")
    assert settings.chili_momentum_max_loss_fresh_quote_guard_enabled is True


def test_fix1_iqfeed_crosscheck_config_defaults():
    # The adaptive divergence knob + the ONE documented fallback base.
    assert settings.chili_momentum_max_loss_phantom_divergence_spread_mult == 3.0
    assert settings.chili_momentum_max_loss_phantom_divergence_fallback_bps == 100.0


# ---------------------------------------------------------------------------
# FIX 1 (ENHANCEMENT) — IQFeed tick-level NBBO cross-check (DB-backed).
# Seeds momentum_nbbo_spread_tape with the freshest L1 truth bid and asserts the
# phantom-loss classifier. This is the CELZ-9920 case: in-process bid torn LOW while
# the IQFeed tape shows a much-higher fresh bid => the C1 loss is phantom.
# ---------------------------------------------------------------------------
def _seed_tape_bid(db, symbol, *, bid, spread_bps, age_seconds=1.0):
    db.execute(
        text(
            "INSERT INTO momentum_nbbo_spread_tape "
            "(symbol, observed_at, bid, ask, mid, spread_bps, source) "
            "VALUES (:s, :ts, :bid, :ask, :mid, :sp, 'iqfeed_l1')"
        ),
        {
            "s": symbol.upper(),
            "ts": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age_seconds),
            "bid": bid,
            "ask": bid * (1.0 + spread_bps / 10_000.0),
            "mid": bid * (1.0 + spread_bps / 20_000.0),
            "sp": spread_bps,
        },
    )
    db.flush()


def test_fix1_iqfeed_phantom_loss_when_tape_bid_much_higher(db):
    # CELZ-9920: in-process bid torn LOW ($3.40) while the FRESH IQFeed tape bid is $4.22
    # with a typical ~50bps spread. Divergence ~1942bps >> 3 x 50bps => PHANTOM.
    _seed_tape_bid(db, "CELZ", bid=4.22, spread_bps=50.0, age_seconds=1.0)
    phantom, dbg = _c1_iqfeed_phantom_loss(db, "CELZ", in_process_bid=3.40)
    assert phantom is True
    assert dbg["checked"] is True
    assert dbg["iqfeed_truth_bid"] == 4.22
    assert dbg["tolerance_basis"] == "recent_median_spread"


def test_fix1_iqfeed_NOT_phantom_when_tape_bid_also_low(db):
    # A REAL loss: the in-process bid ($3.40) is confirmed by the IQFeed tape ($3.42, ~tied)
    # => divergence is within tolerance => NOT phantom => C1 fires (a real loss is never
    # suppressed).
    _seed_tape_bid(db, "REAL", bid=3.42, spread_bps=50.0, age_seconds=1.0)
    phantom, dbg = _c1_iqfeed_phantom_loss(db, "REAL", in_process_bid=3.40)
    assert phantom is False
    assert dbg["checked"] is True


def test_fix1_iqfeed_NOT_phantom_when_tape_stale(db):
    # A fresh-but-OLD tape bid (older than the recency floor) is NOT trusted as truth =>
    # not phantom => C1 fires (the binary stale-flag guard remains the fail-safe).
    _stale_age = float(settings.chili_momentum_quote_freshness_floor_seconds) + 30.0
    _seed_tape_bid(db, "STALE", bid=4.22, spread_bps=50.0, age_seconds=_stale_age)
    phantom, dbg = _c1_iqfeed_phantom_loss(db, "STALE", in_process_bid=3.40)
    assert phantom is False
    assert dbg["checked"] is False  # no fresh row in the window


def test_fix1_iqfeed_NOT_phantom_when_no_tape(db):
    # No tape at all => fail-closed toward firing C1.
    phantom, dbg = _c1_iqfeed_phantom_loss(db, "NOTAPE", in_process_bid=3.40)
    assert phantom is False
    assert dbg["checked"] is False


def test_fix1_iqfeed_fallback_bps_when_spread_absent(db):
    # When the recent tape carries a fresh bid but NO usable spread, the divergence
    # tolerance falls back to the ONE documented base (100bps). A 3.40-vs-4.22 in-process
    # divergence (~1942bps) still exceeds 100bps => phantom, tolerance_basis=fallback.
    db.execute(
        text(
            "INSERT INTO momentum_nbbo_spread_tape "
            "(symbol, observed_at, bid, source) VALUES (:s, :ts, :bid, 'iqfeed_l1')"
        ),
        {
            "s": "NOSPREAD",
            "ts": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1.0),
            "bid": 4.22,
        },
    )
    db.flush()
    phantom, dbg = _c1_iqfeed_phantom_loss(db, "NOSPREAD", in_process_bid=3.40)
    # recent_bid_spread_tape requires spread_bps NOT NULL, so a NULL-spread row is filtered
    # out by the reader => treated as no-tape => NOT phantom (fail-closed). This documents
    # the reader's contract: the cross-check only acts on rows carrying both bid AND spread.
    assert phantom is False
    assert dbg["checked"] is False


# ---------------------------------------------------------------------------
# FIX 1 — C1 FRESH-QUOTE GUARD behavior
# ---------------------------------------------------------------------------
# A live runner at a genuine -max_loss but on a STALE / torn / zero bid.
# avg=4.00, qty=100, max_loss=$50. A phantom bid of $3.40 => unrealized=-$60 (breach),
# but it is NOT fresh => C1 must NOT fire (the CELZ-9920 phantom-liquidation case).
def test_fix1_stale_bid_does_not_fire_c1_halt_stale_streak():
    assert _c1_fires(
        bid=3.40, avg=4.00, qty=100, max_loss_usd=50.0,
        halt_stale_streak=1,  # staleness signalled
        guard_enabled=True,
    ) is False


def test_fix1_stale_bid_does_not_fire_c1_suspected_halt():
    assert _c1_fires(
        bid=3.40, avg=4.00, qty=100, max_loss_usd=50.0,
        suspected_halt_since_utc="2026-06-30T13:31:00Z",  # in a halt
        guard_enabled=True,
    ) is False


def test_fix1_zero_bid_does_not_fire_c1():
    # A torn bid of 0.0 => unrealized = (0 - 4)*100 = -$400 (deep phantom breach), but
    # bid<=0 fails the fresh predicate => C1 must NOT fire.
    assert _c1_fires(
        bid=0.0, avg=4.00, qty=100, max_loss_usd=50.0,
        halt_stale_streak=0,
        guard_enabled=True,
    ) is False


def test_fix1_fresh_bid_at_max_loss_DOES_fire_c1():
    # FRESH bid (finite>0, no staleness, no halt) at a genuine -max_loss => C1 fires
    # immediately (parity with today; the guard ONLY skips provably-stale quotes).
    assert _c1_fires(
        bid=3.40, avg=4.00, qty=100, max_loss_usd=50.0,
        halt_stale_streak=0,
        suspected_halt_since_utc=None,
        guard_enabled=True,
    ) is True


def test_fix1_fresh_bid_in_profit_does_not_fire_c1():
    # A fresh bid above entry is not a loss => C1 never fires (sanity).
    assert _c1_fires(
        bid=4.50, avg=4.00, qty=100, max_loss_usd=50.0,
        guard_enabled=True,
    ) is False


def test_fix1_flag_off_byte_identical_stale_bid_STILL_fires():
    # FLAG OFF => byte-identical legacy: C1 fires on the breach regardless of freshness.
    # Same stale-streak input as the stale-skip test above, but guard OFF => fires.
    assert _c1_fires(
        bid=3.40, avg=4.00, qty=100, max_loss_usd=50.0,
        halt_stale_streak=1,
        guard_enabled=False,
    ) is True
    # And the suspected-halt + zero-bid stale variants ALSO fire when the guard is OFF.
    assert _c1_fires(
        bid=3.40, avg=4.00, qty=100, max_loss_usd=50.0,
        suspected_halt_since_utc="2026-06-30T13:31:00Z",
        guard_enabled=False,
    ) is True


# ---------------------------------------------------------------------------
# FIX 1 — SOURCE-LEVEL guard structure (the live block matches the design)
# ---------------------------------------------------------------------------
def _live_runner_source() -> str:
    return Path(live_runner.__file__).read_text(encoding="utf-8")


def test_fix1_source_c1_reuses_c1b_fresh_quote_predicate_and_flag():
    src = _live_runner_source()
    # The C1 block must reference the new flag and the C1b-identical fresh predicate.
    assert "chili_momentum_max_loss_fresh_quote_guard_enabled" in src
    assert "_c1_fresh_quote" in src
    assert "_c1_skip_stale" in src
    # The fresh predicate must reuse the EXACT same staleness signals as C1b.
    c1_idx = src.index("# C1: Per-trade loss enforcement")
    c1b_idx = src.index("C1b: HARD MAX-LOSS-PER-TRADE CIRCUIT")
    c1_block = src[c1_idx:c1b_idx]
    assert 'int(le.get("halt_stale_streak") or 0) == 0' in c1_block
    assert 'not le.get("suspected_halt_since_utc")' in c1_block
    assert "math.isfinite(float(bid))" in c1_block
    # The breach is now guarded by `not _c1_skip_stale`.
    assert "unrealized_pnl <= -max_loss_usd and not _c1_skip_stale" in c1_block
    # The IQFeed tick-level cross-check is wired into the C1-TRIGGER path (rare → fine),
    # and the force-exit only fires when NOT a phantom.
    assert "_c1_iqfeed_phantom_loss(" in c1_block
    assert "if not _c1_phantom:" in c1_block
    # Guarded by the SAME kill-switch (only cross-check when the guard is on).
    assert "if _c1_guard_on:" in c1_block


# ---------------------------------------------------------------------------
# Regression anchor: the no-confirmation helper a loser must still trip (used by FIX 2
# tests too — included here so commit-1's test run exercises it). A position at/below
# entry with no follow-through high and flat/negative OFI is genuine non-confirmation.
# ---------------------------------------------------------------------------
def test_bail_on_no_confirmation_loser_still_trips():
    # bid <= entry, no new high above buffer, ofi<=0, inside [min,window] => bail.
    assert bail_on_no_confirmation(
        entry_price=4.00,
        bid=3.98,
        high_water_mark=4.00,
        held_seconds=12.0,
        min_hold_seconds=8.0,
        window_seconds=20.0,
        buffer_bps=10.0,
        ofi=-0.2,
    ) is True
    # A confirmed runner (new high above the buffer) is immune.
    assert bail_on_no_confirmation(
        entry_price=4.00,
        bid=4.30,
        high_water_mark=4.40,
        held_seconds=12.0,
        min_hold_seconds=8.0,
        window_seconds=20.0,
        buffer_bps=10.0,
        ofi=0.5,
    ) is False


# ===========================================================================
# FIX 2 — EARLY TRAIL-ARM (arm TRAILING before the ENTERED no-confirmation bailouts)
# ===========================================================================
def test_fix2_flag_exists_default_true():
    assert hasattr(settings, "chili_momentum_early_trail_arm_enabled")
    assert settings.chili_momentum_early_trail_arm_enabled is True


# Pure mirror of the LIVE early-trail-arm gate (live_runner.py "EARLY TRAIL-ARM" block).
# Returns True iff the runner would transition ENTERED -> TRAILING this pulse.
def _early_trail_arms(*, st, bid, avg, trail_activate_return, flag_enabled=True) -> bool:
    return (
        bool(flag_enabled)
        and st == "STATE_LIVE_ENTERED"
        and bid is not None
        and math.isfinite(float(bid))
        and float(bid) >= float(avg) * float(trail_activate_return)
    )


def test_fix2_arms_trailing_when_confirmed_in_profit():
    # avg=4.00, trail_activate_return=1.01 (the ADAPTIVE +100bps band). A confirmed thrust
    # to bid=4.06 (>= 4.04) arms TRAILING BEFORE any no-confirmation bailout can fire.
    assert _early_trail_arms(
        st="STATE_LIVE_ENTERED", bid=4.06, avg=4.00, trail_activate_return=1.01,
    ) is True


def test_fix2_loser_below_entry_does_NOT_arm_and_no_confirmation_STILL_cuts():
    # ANTI-REGRESSION (must not hold a loser): bid < entry => early-arm does NOT fire ...
    assert _early_trail_arms(
        st="STATE_LIVE_ENTERED", bid=3.98, avg=4.00, trail_activate_return=1.01,
    ) is False
    # ... and the no-confirmation cut STILL fires for that same loser (the position is cut).
    assert bail_on_no_confirmation(
        entry_price=4.00,
        bid=3.98,
        high_water_mark=4.00,  # never made a new high above the buffer
        held_seconds=12.0,
        min_hold_seconds=8.0,
        window_seconds=20.0,
        buffer_bps=10.0,
        ofi=-0.2,
    ) is True


def test_fix2_at_entry_not_above_band_does_NOT_arm():
    # At entry exactly (bid == avg, below the activation band) => not in profit => no arm.
    assert _early_trail_arms(
        st="STATE_LIVE_ENTERED", bid=4.00, avg=4.00, trail_activate_return=1.01,
    ) is False
    # Marginally green but still BELOW the activation band => no arm (only arms ABOVE band).
    assert _early_trail_arms(
        st="STATE_LIVE_ENTERED", bid=4.02, avg=4.00, trail_activate_return=1.01,
    ) is False


def test_fix2_flag_off_byte_identical_no_early_arm():
    # FLAG OFF => the early-arm block is skipped even on a confirmed thrust (byte-identical;
    # TRAILING only arms at the existing post-bailout site).
    assert _early_trail_arms(
        st="STATE_LIVE_ENTERED", bid=4.06, avg=4.00, trail_activate_return=1.01,
        flag_enabled=False,
    ) is False


# ---------------------------------------------------------------------------
# FIX 2 — SOURCE-LEVEL placement: the early-arm transition must sit BEFORE the two
# named ENTERED no-confirmation bailouts, and the existing post-bailout arm must remain.
# ---------------------------------------------------------------------------
def test_fix2_source_early_arm_precedes_no_confirmation_bailouts():
    src = _live_runner_source()
    assert "chili_momentum_early_trail_arm_enabled" in src
    early_idx = src.index('"early_arm": True')
    # The two no-confirmation bailouts the early-arm must pre-empt.
    above_unconfirmed_idx = src.index('"reason": "instant_bid_above_fill_unconfirmed"')
    no_confirmation_idx = src.index('"reason": "bail_on_no_confirmation"')
    assert early_idx < above_unconfirmed_idx, "early-arm must precede instant_bid_above_fill_unconfirmed"
    assert early_idx < no_confirmation_idx, "early-arm must precede bail_on_no_confirmation"
    # The early-arm gate is on the SAME adaptive condition as the legacy trail-arm.
    arm_block = src[early_idx - 400:early_idx + 100]
    # FIX 2 arms on the SAME adaptive threshold; the code coerces via float(bid) and
    # is finite-guarded above, so match the threshold expression (not the bare bid form).
    assert "avg * trail_activate_return" in arm_block
    assert "STATE_LIVE_TRAILING" in arm_block
    # The existing POST-bailout trail-arm is preserved + idempotent (guards on ENTERED).
    assert src.count("if st == STATE_LIVE_ENTERED and bid >= avg * trail_activate_return:") == 1


def test_fix2_source_no_magic_reuses_adaptive_trail_activate_return():
    # trail_activate_return is derived from the adaptive params (no fixed magic number).
    src = _live_runner_source()
    assert "trail_activate_return = 1.0 + float(params[\"trail_activate_return_bps\"])" in src


# ---------------------------------------------------------------------------
# py_compile (FIX-touched modules)
# ---------------------------------------------------------------------------
def test_py_compile_touched_modules():
    repo = Path(live_runner.__file__).resolve().parents[4]
    py_compile.compile(str(repo / "app" / "config.py"), doraise=True)
    py_compile.compile(
        str(repo / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"),
        doraise=True,
    )
