"""v2 proactive sell-into-strength exit: state machine, the SELL-EARLY GUARD
(the merge gate), INVARIANT A, and the fast_orderbook ladder reader.

The #1 risk this layer must bound is SELLING A WINNER EARLY in a continuation. The
firewall is two-fold: (a) the decision is gated behind a distribution confluence-AND
+ a continuation VETO, and (b) the order is a RESTING LIMIT at/above the bid — an
unfilled rung is a free option, so even a wrong signal in a real continuation cannot
give back profit. Tests (a) directly; (b) is asserted via ``limit_px >= bid``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.services.trading.momentum_neural.paper_execution import sell_into_strength_ladder
from app.services.trading.momentum_neural.pipeline import (
    LadderRead,
    read_ladder_distribution,
)

# A deep-run winner: entry 1.0, atr 2% * stop_mult 0.6 => risk_dist 0.012 (=120bps);
# hwm 1.024 => peak_r = 2.0 (>= arm_r 1.0 + harvest_gap 0.5). rr=2.0.
_POS = dict(
    high_water_mark=1.024, entry_price=1.0, bid=1.020, atr_pct=0.02, stop_atr_mult=0.6,
    reward_risk=2.0, current_stop=1.010, breakeven_floor=1.0, remaining_qty=100.0,
)


def _ladder(**kw) -> LadderRead:
    base = dict(
        depth_imbal=-0.30, depth_imbal_pctile=0.10, ofi=-0.80, micro_edge=-20.0,
        bid_refill=-0.50, ask_build=0.20, spread_bps=40.0, snapshot_age_s=2.0, n_snaps=6,
    )
    base.update(kw)
    return LadderRead(**base)


# ── TRUE DISTRIBUTION fires ───────────────────────────────────────────────────

def test_true_distribution_fires_resting_limit():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder())
    assert r["state"] == "sell_into_strength"
    assert r["fired"] is True
    assert r["action"] == "sell_limit"
    # Resting limit is AT/ABOVE the bid — never a hidden market dump (the core safety).
    assert r["limit_px"] >= _POS["bid"]
    # Small first increment (10% at the arm, capped 25%).
    assert 0.10 <= r["first_increment_frac"] <= 0.25
    assert abs(r["sell_qty"] - r["first_increment_frac"] * _POS["remaining_qty"]) < 1e-3


# ── 🔴 SELL-EARLY GUARD (the merge gate) ──────────────────────────────────────

def test_sell_early_guard_bid_refill_holds():
    """JASMY/EIGEN continuation-pullback: deep run, momentarily ask-heavy + OFI dipped
    negative — BUT buyers are RESTACKING the bid. Must HOLD (do not sell the winner)."""
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(bid_refill=3.5))
    assert r["state"] == "hold"
    assert r["fired"] is False
    assert r["vetoed_by"] == "bid_refill"
    assert r["reason"] == "continuation_veto"


def test_sell_early_guard_weak_ofi_holds():
    # Flow not decisively negative (ofi above -thr/2) => continuation, HOLD.
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(bid_refill=-0.10, ofi=-0.20))
    assert r["state"] == "hold"
    assert r["vetoed_by"] == "ofi_weak"


def test_sell_early_guard_micro_nonneg_holds():
    # Price still bid-favored (micro >= 0) => continuation, HOLD.
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(bid_refill=-0.10, ofi=-0.80, micro_edge=5.0))
    assert r["state"] == "hold"
    assert r["vetoed_by"] == "micro_nonneg"


def test_continuation_full_strength_holds():
    # REZ-like live continuation: bids stacking hard, OFI positive, micro positive.
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(
        depth_imbal_pctile=0.83, ofi=0.59, micro_edge=10.75, bid_refill=5.86))
    assert r["state"] == "hold"
    assert r["vetoed_by"] == "bid_refill"


# ── INVARIANT A (ratchet-only) ────────────────────────────────────────────────

def test_invariant_a_never_below_floor_when_firing():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder())
    floor = max(_POS["current_stop"], _POS["breakeven_floor"])
    assert r["new_stop_floor"] >= floor
    assert r["new_stop_floor"] >= _POS["current_stop"]  # never loosens
    # On FILL the remainder ratchets to the fill floor, never below the structural floor.
    assert r["fill_ratchet_floor"] >= floor


def test_invariant_a_holds_across_all_branches_and_garbage():
    import itertools
    stops = [0.5, 1.010, 1.05, float("nan")]
    bes = [0.0, 1.0, 1.10]
    refills = [-0.5, 3.0]
    micros = [-20.0, 5.0]
    for cs, be, rf, mi in itertools.product(stops, bes, refills, micros):
        pos = dict(_POS, current_stop=cs, breakeven_floor=be)
        r = sell_into_strength_ladder(**pos, ladder=_ladder(bid_refill=rf, micro_edge=mi))
        nsf = r["new_stop_floor"]
        # never None; never below a finite current_stop
        assert nsf is not None
        if math.isfinite(cs):
            assert nsf >= cs - 1e-9


# ── gates: profit-arm / deep-run / staleness / thinness / liquidity ───────────

def test_below_profit_arm_holds():
    pos = dict(_POS, high_water_mark=1.005)  # peak_r ~0.42 < arm_r 1.0
    r = sell_into_strength_ladder(**pos, ladder=_ladder())
    assert r["state"] == "hold" and r["armed"] is False and r["reason"] == "below_profit_arm"


def test_armed_but_not_deep_run_holds():
    pos = dict(_POS, high_water_mark=1.014)  # peak_r ~1.17: armed but < 1.5 deep-run
    r = sell_into_strength_ladder(**pos, ladder=_ladder())
    assert r["armed"] is True and r["state"] == "hold" and r["reason"] == "not_deep_run"


def test_stale_book_holds():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(snapshot_age_s=99.0))
    assert r["state"] == "hold" and r["reason"] == "stale_or_thin"


def test_thin_window_holds():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(n_snaps=2))
    assert r["state"] == "hold" and r["reason"] == "stale_or_thin"


def test_illiquid_spread_holds():
    # spread_cap = 3 * risk_dist_bps = 360bps; 500 > 360 => illiquid, HOLD.
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(spread_bps=500.0))
    assert r["state"] == "hold" and r["reason"] == "illiquid"


def test_missing_signal_holds():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(ofi=None))
    assert r["state"] == "hold" and r["reason"] == "missing_signal"


def test_no_distribution_holds():
    # pctile high (NOT ask-heavy) but vetoes pass: D1 fails => no_distribution.
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(depth_imbal_pctile=0.90))
    assert r["state"] == "hold" and r["reason"] == "no_distribution"


def test_cooldown_holds_even_on_true_distribution():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder(), cooldown_active=True)
    assert r["state"] == "hold" and r["reason"] == "cooldown"


def test_none_ladder_holds():
    r = sell_into_strength_ladder(**_POS, ladder=None)
    assert r["state"] == "hold"


def test_counterfactual_emitted_on_armed_tick():
    r = sell_into_strength_ladder(**_POS, ladder=_ladder())
    # the pure-hold baseline the live A/B measures capture against
    assert r["counterfactual_hold_stop"] == max(_POS["current_stop"], _POS["breakeven_floor"])


# ── reader: fast_orderbook 2-tuple parse + fail-open + equity no-op ───────────

def _ins(db, ticker, snap_at, bids, asks, spread=30.0):
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO fast_orderbook (ticker, snapshot_at, bid_levels, ask_levels, "
        "bid_total_size, ask_total_size, imbalance, spread_bps, source) VALUES "
        "(:t, :s, CAST(:b AS jsonb), CAST(:a AS jsonb), :bt, :at, :im, :sp, 'test')"
    ), {
        "t": ticker, "s": snap_at,
        "b": __import__("json").dumps(bids), "a": __import__("json").dumps(asks),
        "bt": sum(x[1] for x in bids), "at": sum(x[1] for x in asks),
        "im": 0.0, "sp": spread,
    })


def test_reader_parses_two_tuples_and_computes(db):
    now = datetime.utcnow()
    # 4 snaps, ask-heavy newest + bid thinning across the window
    for i, (bb, aa) in enumerate([
        ([[1.0, 1000.0], [0.99, 500.0]], [[1.01, 400.0], [1.02, 300.0]]),   # oldest, bid-heavy
        ([[1.0, 800.0], [0.99, 500.0]], [[1.01, 600.0], [1.02, 400.0]]),
        ([[1.0, 400.0], [0.99, 500.0]], [[1.01, 900.0], [1.02, 700.0]]),
        ([[1.0, 200.0], [0.99, 500.0]], [[1.01, 1200.0], [1.02, 800.0]]),   # newest, ask-heavy
    ]):
        _ins(db, "TEST-USD", now - timedelta(seconds=(3 - i) * 2), bb, aa)
    db.commit()
    lr = read_ladder_distribution("TEST-USD", db, k=6)
    assert lr.n_snaps == 4
    assert lr.depth_imbal is not None and lr.depth_imbal < 0      # newest is ask-heavy
    assert lr.bid_refill is not None and lr.bid_refill < 0        # best bid 1000 -> 200
    assert lr.ask_build is not None and lr.ask_build > 0          # ask side grew
    assert lr.snapshot_age_s is not None and lr.snapshot_age_s < 30


def test_reader_failopen_on_empty(db):
    lr = read_ladder_distribution("NOPE-USD", db, k=6)
    assert lr.n_snaps == 0 and lr.depth_imbal is None and lr.depth_imbal_pctile is None


def test_reader_equity_noop(db):
    lr = read_ladder_distribution("NVDA", db, k=6)
    assert lr.n_snaps == 0 and lr.depth_imbal is None and lr.ofi is None
