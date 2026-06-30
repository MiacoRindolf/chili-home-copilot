"""Proof tests for the STANDING fill-on-verticals fix (2026-06-29): the deep
fill-aggression budget (the 800bps vertical chase ceiling) is DECOUPLED from the
halt-gate, so a genuine NO-HALT confirmed UP-thrust vertical (a 1m new-high push that
never halted) can ALSO escalate the chase toward the hard cap — while a fade /
below-VWAP / OFI<=0 / non-new-high move stays at the shallow abs-cap (the knife guard).

Pure helpers in live_runner — NO DB. Covers (spec):
  (a) NO-HALT confirmed UP-thrust (OFI>0 + new-high + above-VWAP + RVOL) → confluence > 0
      → the chase ceiling escalates toward 800bps (the DEEP budget, NOT the abs-cap);
  (b) NO-HALT non-thrust / fade (OFI<=0 OR below-VWAP OR no new-high OR thin RVOL)
      → _nohalt_vertical_thrust_strong False → confluence None → only the abs-cap (knife guard);
  (c) a halt-resume STILL unlocks it (no regression);
  (d) the risk-first bound: the chased deeper price yields FEWER shares so dollar-risk
      stays within budget (qty * stop_distance <= the bounded R);
  (e) flag-off (nohalt_thrust_enabled=False) → byte-identical (halt-gated as today).
"""

import math

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity


@pytest.fixture(autouse=True)
def _restore_flags():
    """Snapshot + restore the flags this module mutates (settings is a singleton)."""
    keys = (
        "chili_momentum_vertical_chase_enabled",
        "chili_momentum_vertical_chase_max_bps",
        "chili_momentum_vertical_chase_min_confluence",
        "chili_momentum_vertical_chase_nohalt_thrust_enabled",
        "chili_momentum_vertical_chase_nohalt_min_confluence",
        "chili_momentum_explosive_rvol_floor",
    )
    saved = {k: getattr(settings, k) for k in keys}
    # canonical baseline for every test (each test may override)
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    settings.chili_momentum_vertical_chase_nohalt_thrust_enabled = True
    settings.chili_momentum_vertical_chase_nohalt_min_confluence = 0.6
    settings.chili_momentum_explosive_rvol_floor = 3.0
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


# expected_move_bps=2000 → _adaptive_live_max_spread_bps caps at the 300bps abs_cap.
_EM = 2000.0


def _abs_cap():
    return lr._adaptive_live_max_spread_bps(_EM)


def test_abs_cap_is_the_documented_300_default():
    assert _abs_cap() == pytest.approx(300.0)


# ── (knife guard) the no-halt strong-thrust predicate ──────────────────────────

def test_nohalt_strong_requires_all_legs():
    # the canonical PASS case: OFI>0 + new-high + above-VWAP + RVOL>floor
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=True, above_vwap=True, rvol=10.0
    ) is True


@pytest.mark.parametrize("ofi", [None, 0.0, -0.3, float("nan")])
def test_nohalt_strong_fails_closed_on_ofi(ofi):
    # OFI<=0 / missing / NaN ⇒ not buyers lifting ⇒ knife risk ⇒ no deep budget.
    assert lr._nohalt_vertical_thrust_strong(
        ofi=ofi, new_high=True, above_vwap=True, rvol=10.0
    ) is False


def test_nohalt_strong_fails_closed_without_new_high():
    # not making a new high ⇒ a fade / pullback, not an up-vertical.
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=False, above_vwap=True, rvol=10.0
    ) is False
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=None, above_vwap=True, rvol=10.0
    ) is False


def test_nohalt_strong_fails_closed_below_vwap():
    # below-VWAP / falling ⇒ knife ⇒ refuse the deep budget.
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=True, above_vwap=False, rvol=10.0
    ) is False
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=True, above_vwap=None, rvol=10.0
    ) is False


@pytest.mark.parametrize("rvol", [None, 3.0, 2.5, float("nan")])
def test_nohalt_strong_fails_closed_on_weak_rvol(rvol):
    # RVOL at/below the explosive floor (3.0) ⇒ not explosive participation ⇒ no deep budget.
    assert lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=True, above_vwap=True, rvol=rvol
    ) is False


# ── (a) NO-HALT confirmed UP-thrust unlocks the DEEP budget ────────────────────

def test_a_nohalt_thrust_unlocks_confluence():
    c = lr._vertical_thrust_confluence(
        halt_resume_active=False,         # NO halt
        tape_thrust_ok=True,
        squeeze_pct=None, rvol=None,
        nohalt_thrust_strong=True,        # but a CONFIRMED no-halt UP-thrust
    )
    # unlocked at the no-halt floor (0.6), NOT None.
    assert c is not None
    assert c == pytest.approx(0.6)


def test_a_nohalt_thrust_ceiling_escalates_toward_800_not_abs_cap():
    """End-to-end through _entry_repeg_price: a NO-HALT gap that exceeds the 300bps
    abs_cap is ABANDONED without the no-halt unlock, but FILLS once the confirmed
    no-halt thrust raises the ceiling toward 800bps — the deep budget, not the abs-cap."""
    L0 = 10.0
    ask = L0 * 1.05  # 500bps above the original limit: > 300 abs_cap, < 800 hard max

    # No unlock (no halt, no no-halt thrust) ⇒ abs-cap ⇒ past the ceiling ⇒ None (today's miss).
    c_none = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=None, rvol=None, nohalt_thrust_strong=False,
    )
    assert c_none is None
    assert lr._entry_repeg_price(
        original_limit_px=L0, live_ask=ask, expected_move_bps=_EM, vertical_confluence=c_none
    ) is None

    # CONFIRMED no-halt UP-thrust ⇒ deep budget ⇒ the 500bps gap is now fillable.
    c = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=1.0, rvol=50.0, nohalt_thrust_strong=True,
    )
    assert c is not None and c == pytest.approx(1.0)  # full fuel ⇒ caps at 1.0
    # the raised ceiling at confluence=1.0 is the hard max (800bps), well above 500.
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=c) == pytest.approx(800.0)
    px = lr._entry_repeg_price(
        original_limit_px=L0, live_ask=ask, expected_move_bps=_EM, vertical_confluence=c
    )
    assert px is not None and px >= ask  # the deep budget reaches the runaway offer


def test_a_nohalt_floor_is_above_halt_floor():
    """A no-halt vertical must clear a STRONGER bar: its floor (0.6) > the halt floor (0.5)."""
    c_halt = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True,
        squeeze_pct=None, rvol=None, nohalt_thrust_strong=False,
    )
    c_nohalt = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=None, rvol=None, nohalt_thrust_strong=True,
    )
    assert c_halt == pytest.approx(0.5)
    assert c_nohalt == pytest.approx(0.6)
    assert c_nohalt > c_halt


# ── (b) the knife guard: a NO-HALT non-thrust / fade stays at the abs-cap ───────

def test_b_nohalt_fade_no_deep_budget():
    """No halt + the tape ran the ask up, but the move is NOT a confirmed up-thrust
    (knife guard returned False) ⇒ confluence None ⇒ only the abs-cap (the order is
    abandoned past the abs-cap rather than chased into a falling knife)."""
    L0 = 10.0
    ask = L0 * 1.05  # 500bps gap, beyond the 300 abs_cap

    # the live signals say FADE (e.g. OFI<=0) → knife guard refuses
    nohalt_strong = lr._nohalt_vertical_thrust_strong(
        ofi=-0.2, new_high=True, above_vwap=True, rvol=10.0
    )
    assert nohalt_strong is False
    c = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=1.0, rvol=50.0, nohalt_thrust_strong=nohalt_strong,
    )
    assert c is None  # no deep budget
    # ceiling stays at the abs-cap; the 500bps gap is past it ⇒ no chase (knife guarded).
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=c) == pytest.approx(_abs_cap())
    assert lr._entry_repeg_price(
        original_limit_px=L0, live_ask=ask, expected_move_bps=_EM, vertical_confluence=c
    ) is None


def test_b_nohalt_below_vwap_no_deep_budget():
    nohalt_strong = lr._nohalt_vertical_thrust_strong(
        ofi=0.4, new_high=True, above_vwap=False, rvol=10.0
    )
    assert nohalt_strong is False
    c = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=1.0, rvol=50.0, nohalt_thrust_strong=nohalt_strong,
    )
    assert c is None


# ── (c) the halt-resume path STILL unlocks it (no regression) ──────────────────

def test_c_halt_resume_still_unlocks():
    # halt-resume + confirmed tape, no no-halt thrust supplied ⇒ unchanged 0.5 floor.
    c = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True,
        squeeze_pct=None, rvol=None,
    )
    assert c == pytest.approx(0.5)
    # full fuel still caps at 1.0 (the original behavior).
    c_full = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True,
        squeeze_pct=1.0, rvol=50.0,
    )
    assert c_full == pytest.approx(1.0)


def test_c_halt_resume_still_fails_closed_without_tape():
    # tape None/False ⇒ None even on a halt-resume (unchanged fail-closed).
    assert lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=None, squeeze_pct=0.9, rvol=10.0
    ) is None
    assert lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=False, squeeze_pct=0.9, rvol=10.0
    ) is None


# ── (d) the RISK-FIRST bound: the deeper chase buys FEWER shares, same risk ─────

def test_d_risk_first_bound_holds_on_deep_chase():
    """The chase loop re-sizes risk-first at the chased price (live_runner ~7732). A
    DEEPER fill ⇒ a wider stop distance ⇒ FEWER shares so dollar-risk stays pinned at
    max_loss_usd. Prove qty*stop_distance <= the bounded R at BOTH the original limit
    and the chased (deep-budget) price, and that the deep chase never increases risk."""
    atr_pct = 0.04
    max_loss = 100.0
    stop_atr_mult = 0.60
    ceiling = 1_000_000.0  # huge so the notional cap never binds (risk is the binder)

    L0 = 10.0          # original limit
    chased = L0 * 1.05  # the deep-budget chased price (+500bps)

    qty0, meta0 = compute_risk_first_quantity(
        entry_price=L0, atr_pct=atr_pct, max_loss_usd=max_loss,
        max_notional_ceiling_usd=ceiling, base_increment=0.0, base_min_size=0.0,
        stop_atr_mult=stop_atr_mult,
    )
    qty1, meta1 = compute_risk_first_quantity(
        entry_price=chased, atr_pct=atr_pct, max_loss_usd=max_loss,
        max_notional_ceiling_usd=ceiling, base_increment=0.0, base_min_size=0.0,
        stop_atr_mult=stop_atr_mult,
    )

    # dollar-risk == qty * stop_distance is pinned at max_loss at BOTH prices (<= bound).
    r0 = qty0 * float(meta0["stop_distance"])
    r1 = qty1 * float(meta1["stop_distance"])
    assert r0 == pytest.approx(max_loss, rel=1e-9)
    assert r1 == pytest.approx(max_loss, rel=1e-9)
    assert r1 <= max_loss + 1e-6
    assert r0 <= max_loss + 1e-6

    # the chase buys FEWER shares at the worse price (same risk, not more).
    assert qty1 < qty0
    # risk did NOT increase from the deeper chase.
    assert r1 <= r0 + 1e-6


# ── (e) flag-off ⇒ byte-identical (halt-gated as today) ────────────────────────

def test_e_flag_off_nohalt_is_inert():
    settings.chili_momentum_vertical_chase_nohalt_thrust_enabled = False
    # a CONFIRMED no-halt thrust now does NOTHING (no halt ⇒ None, exactly today's behavior).
    c = lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True,
        squeeze_pct=1.0, rvol=50.0, nohalt_thrust_strong=True,
    )
    assert c is None
    # the halt-resume path is unaffected by the flag.
    c_halt = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True,
        squeeze_pct=None, rvol=None, nohalt_thrust_strong=True,
    )
    assert c_halt == pytest.approx(0.5)


def test_e_flag_off_matches_legacy_signature_call():
    """Flag off + the OLD 4-arg call shape (no nohalt kwarg) ⇒ identical to the
    pre-change halt-gated builder for every input combination."""
    settings.chili_momentum_vertical_chase_nohalt_thrust_enabled = False
    for halt in (True, False):
        for tape in (True, False, None):
            for sq in (None, 0.2, 0.7, 1.0):
                for rv in (None, 2.0, 5.0, 50.0):
                    c = lr._vertical_thrust_confluence(
                        halt_resume_active=halt, tape_thrust_ok=tape,
                        squeeze_pct=sq, rvol=rv,
                    )
                    if not halt or tape is not True:
                        assert c is None
                    else:
                        assert c is not None and 0.5 <= c <= 1.0
