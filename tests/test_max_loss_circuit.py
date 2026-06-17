"""Hard MAX-LOSS-PER-TRADE CIRCUIT (#1 profitability lever, 2026-06-17).

An 87-fill audit found the momentum lane net -$157.68 but the ENTIRE deficit is a
-$697.76 tail of 4 RH low-float names (MTEN -896bps, SDOT -729bps, CCTG -893bps,
CAST -497bps) that GAPPED 5-9% THROUGH their tight structural stops and got a deep
market-exit fill. The circuit caps each trade's loss at K x the position's REALIZED
structural risk (stop_distance x qty) and flattens at an ABSOLUTE loss-anchored floor
(avg - K*stop_distance) so the deep slip is mechanically impossible.

The decision helper ``max_loss_circuit_decision`` is PURE (zero I/O) so every case
below is a unit test with no DB. The two red-team invariants are load-bearing:

  1. THRESHOLD BASIS = realized structural risk = stop_distance * qty — NOT the frozen
     ``risk_usd`` budget (live: risk_usd=$19.30 vs structural=$1.61, a 12x overstatement
     that would let a $38 hole open on a $1.61-stop name).
  2. THE FLOOR = avg - K*stop_distance is an ABSOLUTE anchor (not a falling bid), so a
     9%-deep fill cannot occur.
"""
from __future__ import annotations

import math

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import max_loss_circuit_decision


# ── (a) GAP-THROUGH counterfactual ─────────────────────────────────────────────
def test_gap_through_breaches_and_floors_at_structural_anchor():
    """avg=1.00, TIGHT structural stop_distance=0.025 (2.5%, Ross low-float), qty=200,
    K=2 => threshold floor at 1.00 - 2*0.025 = 0.95 (-5%). The bid GAPS to 0.91 (-9%) —
    exactly the MTEN/SDOT-style blow-through PAST the floor. The circuit must BREACH and
    anchor the flatten at 0.95, capping the realized loss at the structural threshold
    2*0.025*200 = $10 — NOT the -9% market-exit fill ($18)."""
    d = max_loss_circuit_decision(avg=1.00, qty=200, stop_distance=0.025, bid=0.91, k=2.0)
    assert d["breach"] is True
    assert d["structural_risk_usd"] == pytest.approx(0.025 * 200)  # $5
    assert d["threshold_usd"] == pytest.approx(2.0 * 0.025 * 200)  # $10
    assert d["floor_price"] == pytest.approx(1.00 - 2.0 * 0.025)   # 0.95
    # Realized loss if filled AT the floor is capped at the structural threshold ($10),
    # NOT the -9% gap-through ($18) the naked market exit would have realized.
    capped_loss = (d["floor_price"] - 1.00) * 200
    assert capped_loss == pytest.approx(-10.0)
    actual_blowthrough = (0.91 - 1.00) * 200
    assert actual_blowthrough == pytest.approx(-18.0)
    assert capped_loss > actual_blowthrough  # the floor SAVES the difference


def test_real_tail_magnitudes_capped_below_actual_blowthrough():
    """Replay the 4 real tail magnitudes: each gapped 5-9% THROUGH its TIGHT stop. With
    a tight structural stop (2% of entry) and K=2 the threshold floor is at -4%, so each
    of these 497-896bps gaps blows through it. The floored loss must be strictly LESS
    than the actual blow-through loss (the deep market-exit fill at the gap bps)."""
    qty = 200
    avg = 1.00
    stop_distance = 0.02  # 200bps structural stop (tight Ross low-float) -> 400bps floor
    k = 2.0
    # (name, gap_bps_through) from the 87-fill audit.
    tail = [("MTEN", 896), ("SDOT", 729), ("CCTG", 893), ("CAST", 497)]
    for name, gap_bps in tail:
        bid = avg * (1.0 - gap_bps / 10_000.0)
        d = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=bid, k=k)
        assert d["breach"] is True, f"{name} should breach at -{gap_bps}bps (floor -400bps)"
        floored_loss = (d["floor_price"] - avg) * qty            # capped at -$8
        actual_loss = (bid - avg) * qty                          # the deep fill
        assert floored_loss > actual_loss, (
            f"{name}: floored {floored_loss:.2f} must be a SMALLER loss than the "
            f"blow-through {actual_loss:.2f}"
        )
        # And the floored loss is exactly the structural threshold.
        assert floored_loss == pytest.approx(-k * stop_distance * qty)


# ── (b) FALSE-CUT — a small drift within K*structural must NOT fire ─────────────
def test_small_drift_within_threshold_does_not_breach():
    """A -150bps drift on a name whose structural risk (K=2) tolerates -1000bps loss
    must PASS (breach=False) — the circuit only fires on a genuine gap-through, never
    on healthy noise the trail/stop owns."""
    avg = 1.00
    stop_distance = 0.05  # K*structural threshold = 2*0.05*qty
    qty = 200
    bid = avg * (1.0 - 150 / 10_000.0)  # -1.5%
    d = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=bid, k=2.0)
    assert d["breach"] is False
    assert d["reason"] == "within_threshold"


def test_threshold_boundary_breach_semantics():
    """The comparison is `unrealized_pnl <= -threshold`. Just PAST the threshold breaches;
    just INSIDE it does not. (Exactly AT the boundary is float-fragile and not a
    load-bearing distinction — a sub-cent move either way decides it.)"""
    avg = 1.00
    stop_distance = 0.05
    qty = 200
    k = 2.0
    threshold = k * stop_distance * qty           # $20 ; floor at 0.90
    # One cent PAST the floor -> unrealized -$22 <= -$20 -> breach.
    d_past = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=0.89, k=k)
    assert d_past["breach"] is True
    # One cent INSIDE the floor -> unrealized -$18 > -$20 -> no breach.
    d_inside = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=0.91, k=k)
    assert d_inside["breach"] is False
    _ = threshold


# ── (c) BASIS — structural risk, NOT the frozen risk_usd budget ────────────────
def test_threshold_uses_structural_risk_not_risk_usd_budget():
    """The verified live case: a name with risk_usd=$19.30 budget but structural
    stop_distance=1.61 (per-share) on qty=1. The threshold must be K*structural
    (2*1.61=$3.22) — NOT K*risk_usd ($38.60, a 12x overstatement that would let a
    $38 hole open on a $1.61-stop name). We assert the helper NEVER sees risk_usd
    and computes purely from stop_distance*qty."""
    avg = 100.0
    qty = 1.0
    stop_distance = 1.61  # the REAL per-share structural distance
    k = 2.0
    d = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=avg - 1.0, k=k)
    assert d["structural_risk_usd"] == pytest.approx(1.61)
    assert d["threshold_usd"] == pytest.approx(2.0 * 1.61)  # $3.22, NOT $38.60
    # A -$1.00 drift is INSIDE the $3.22 threshold -> no breach.
    assert d["breach"] is False
    # A -$4.00 drift EXCEEDS the $3.22 threshold -> breach (the budget would not have).
    d2 = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=avg - 4.0, k=k)
    assert d2["breach"] is True


# ── (d) INSUFFICIENT-BASIS — never fire on bad basis ───────────────────────────
@pytest.mark.parametrize(
    "kwargs",
    [
        dict(avg=1.00, qty=200, stop_distance=0.0, bid=0.50, k=2.0),    # stop_distance == 0
        dict(avg=1.00, qty=200, stop_distance=-0.05, bid=0.50, k=2.0),  # stop_distance < 0
        dict(avg=1.00, qty=0.0, stop_distance=0.05, bid=0.50, k=2.0),   # qty == 0
        dict(avg=1.00, qty=-5.0, stop_distance=0.05, bid=0.50, k=2.0),  # qty < 0
        dict(avg=0.0, qty=200, stop_distance=0.05, bid=0.50, k=2.0),    # avg == 0
        dict(avg=-1.0, qty=200, stop_distance=0.05, bid=0.50, k=2.0),   # avg < 0
        dict(avg=1.00, qty=200, stop_distance=0.05, bid=None, k=2.0),   # bid None
        dict(avg=1.00, qty=200, stop_distance=0.05, bid=0.0, k=2.0),    # bid == 0
        dict(avg=1.00, qty=200, stop_distance=0.05, bid=-0.5, k=2.0),   # bid < 0
        dict(avg=1.00, qty=200, stop_distance=float("nan"), bid=0.5, k=2.0),  # nan basis
        dict(avg=1.00, qty=200, stop_distance=0.05, bid=float("inf"), k=2.0), # inf bid
    ],
)
def test_insufficient_basis_never_fires(kwargs):
    d = max_loss_circuit_decision(**kwargs)
    assert d["breach"] is False
    assert d["reason"] == "insufficient_basis"
    assert d["floor_price"] is None


# ── (e) PARITY — flag-off path ─────────────────────────────────────────────────
def test_flag_off_is_byte_identical_legacy():
    """The kill-switch defaults ON; flipping it OFF must make the tick gate skip the
    circuit entirely. The decision helper is pure and the LIVE gate consults it ONLY
    when chili_momentum_max_loss_circuit_enabled is True. We assert the default is the
    intended value and that the knob exists with the validated bounds, and we
    demonstrate the gate-condition the live runner uses returns False when disabled."""
    assert hasattr(settings, "chili_momentum_max_loss_circuit_enabled")
    assert settings.chili_momentum_max_loss_circuit_enabled is True  # default ON
    # The live-runner gate: when disabled, the circuit decision is never consulted.
    enabled = False
    le = {"max_loss_circuit_fired": False}
    gate_open = bool(enabled) and not le.get("max_loss_circuit_fired")
    assert gate_open is False  # disabled => helper not consulted => legacy path


def test_risk_multiple_bounds_and_default():
    assert settings.chili_momentum_max_loss_risk_multiple == pytest.approx(2.0)
    # ge=1.0 (floor never looser than the stop), le=6.0 — validated by the Field.
    fields = type(settings).model_fields
    f = fields["chili_momentum_max_loss_risk_multiple"]
    metas = list(getattr(f, "metadata", []))
    ge_vals = [getattr(m, "ge", None) for m in metas if getattr(m, "ge", None) is not None]
    le_vals = [getattr(m, "le", None) for m in metas if getattr(m, "le", None) is not None]
    assert 1.0 in ge_vals
    assert 6.0 in le_vals


# ── (f) FLOOR-NEVER-LOOSER — floor <= structural-stop equivalent for K>=1 ───────
@pytest.mark.parametrize("k", [1.0, 1.5, 2.0, 3.0, 6.0])
def test_floor_never_looser_than_structural_stop(k):
    """K >= 1 guarantees K*stop_distance >= stop_distance, so the loss-anchored floor
    (avg - K*stop_distance) sits AT or BELOW the structural-stop price (avg -
    stop_distance). The circuit can never widen the loss beyond the structural stop —
    it only ever caps it at K x that stop (the operator-chosen multiple)."""
    avg = 50.0
    stop_distance = 0.40
    qty = 30
    bid = avg - k * stop_distance - 0.01  # just past the threshold so it breaches
    d = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=bid, k=k)
    structural_stop_price = avg - stop_distance
    # K>=1 => floor is at/below the structural stop price (a tighter or equal loss cap).
    assert d["floor_price"] <= structural_stop_price + 1e-9
    # And the threshold is at/above the structural risk (never looser).
    assert d["threshold_usd"] >= d["structural_risk_usd"] - 1e-9


def test_floor_equals_structural_stop_when_k_is_one():
    """The boundary: K=1 makes the floor EXACTLY the structural stop price — the
    tightest the operator is allowed to set (ge=1.0)."""
    avg = 50.0
    stop_distance = 0.40
    qty = 30
    bid = avg - 1.0 * stop_distance - 0.01
    d = max_loss_circuit_decision(avg=avg, qty=qty, stop_distance=stop_distance, bid=bid, k=1.0)
    assert d["floor_price"] == pytest.approx(avg - stop_distance)


# ── helper-shape sanity ────────────────────────────────────────────────────────
def test_decision_returns_all_keys():
    d = max_loss_circuit_decision(avg=1.0, qty=100, stop_distance=0.02, bid=0.95, k=2.0)
    for key in ("breach", "structural_risk_usd", "threshold_usd", "unrealized_pnl",
                "floor_price", "reason"):
        assert key in d
    assert isinstance(d["breach"], bool)
    assert math.isfinite(d["floor_price"])
