"""Risk-neutral confirmation-pyramid — THE INVARIANT GATE (the load-bearing proof).

The pyramid adds shares to an already-winning position. It is risk-neutral BY
CONSTRUCTION only if, after the add and after ratcheting the stop to blended
breakeven, the ENLARGED position's worst-case realized loss stays <= the starter's
ORIGINAL structural risk R0. Two failure surfaces:

  (i)  structural stop-out at s1 = max(stop_px, a1): WCL = (a1 - s1)*q1 <= 0   (ratchet).
  (ii) GAP-THROUGH below s1: caught by the #769 max-loss circuit. The circuit re-bases
       its threshold on the LIVE qty (k*stop_distance*q1) — which for the ENLARGED q1 is
       ~3-4.5x R0. GUARD #1 (the risk_anchor_usd clamp) caps threshold_usd to R0 so the
       enlarged worst-case stays <= R0. WITHOUT the clamp, (ii) loses multiples of R0 —
       this test FAILS without GUARD #1 and PASSES with it.

These are pure assertions on ``max_loss_circuit_decision`` + the blend math — no DB.
"""

import math

import pytest

from app.services.trading.momentum_neural.risk_policy import max_loss_circuit_decision

K = 2.0  # chili_momentum_max_loss_risk_multiple default
EPS = 1e-6


def _simulate_add(*, q0, a0, d0, rho, fill_frac, slip_bps, stop_px_over_a0, win_px):
    """Mirror the spec's add math. Returns (q1, a1, s1, R0, qa_f, Pa_f)."""
    R0 = d0 * q0                          # original structural risk
    add_budget = rho * R0
    qa_planned = add_budget / d0          # == compute_risk_first_quantity(max_loss=rho*R0, atr→d0)
    qa_f = qa_planned * fill_frac         # partial add fill
    Pa_f = win_px * (1.0 + slip_bps / 10_000.0)   # add fill price (slipped)
    q1 = q0 + qa_f
    a1 = (a0 * q0 + Pa_f * qa_f) / q1     # blended avg
    stop_px = a0 * (1.0 + stop_px_over_a0)  # starter stop already ratcheted to >= a0 (GUARD #2)
    s1 = max(stop_px, a1)                 # INVARIANT-A: ratchet to blended breakeven, tighten-only
    return q1, a1, s1, R0, qa_f, Pa_f


# Lens-1 adversarial attack cases: (label, kwargs, gap_through_bid_frac_of_a1)
CASES = [
    ("ideal",                dict(q0=1000, a0=10.0, d0=0.10, rho=0.5, fill_frac=1.0, slip_bps=0,   stop_px_over_a0=0.02, win_px=10.50), 0.95),
    ("add_slips_50bps",      dict(q0=1000, a0=10.0, d0=0.10, rho=0.5, fill_frac=1.0, slip_bps=50,  stop_px_over_a0=0.02, win_px=10.50), 0.95),
    ("gap_through_3pct",     dict(q0=1000, a0=10.0, d0=0.10, rho=0.5, fill_frac=1.0, slip_bps=0,   stop_px_over_a0=0.02, win_px=10.50), 0.97),
    ("cushion_barely_1R",    dict(q0=1000, a0=10.0, d0=0.10, rho=0.5, fill_frac=1.0, slip_bps=0,   stop_px_over_a0=0.00, win_px=10.10), 0.96),
    ("partial_40pct_add",    dict(q0=1000, a0=10.0, d0=0.10, rho=0.5, fill_frac=0.4, slip_bps=20,  stop_px_over_a0=0.02, win_px=10.50), 0.95),
    ("partial_taken_starter",dict(q0=500,  a0=10.0, d0=0.10, rho=0.5, fill_frac=1.0, slip_bps=0,   stop_px_over_a0=0.03, win_px=10.60), 0.94),
    ("low_float_tight_stop", dict(q0=2000, a0=3.00, d0=0.03, rho=0.5, fill_frac=1.0, slip_bps=30,  stop_px_over_a0=0.01, win_px=3.20),  0.90),
]


@pytest.mark.parametrize("label,kw,gap_frac", CASES)
def test_pyramid_enlarged_worst_case_le_R0_WITH_guard1(label, kw, gap_frac):
    """THE GATE: with GUARD #1 (risk_anchor_usd=R0) the enlarged worst-case <= R0."""
    q1, a1, s1, R0, qa_f, _ = _simulate_add(**kw)
    assert qa_f > 0 and q1 > kw["q0"], f"{label}: add must enlarge the position"

    # (i) structural stop-out at the blended-breakeven ratchet — never worse than 0.
    structural_wcl = (a1 - s1) * q1
    assert structural_wcl <= EPS, f"{label}: structural WCL {structural_wcl:.4f} > 0"

    # (ii) gap-through BELOW s1, caught by the CLAMPED #769 circuit (anchor=R0).
    gap_bid = a1 * gap_frac
    clamped = max_loss_circuit_decision(
        avg=a1, qty=q1, stop_distance=kw["d0"], bid=gap_bid, k=K, risk_anchor_usd=R0,
    )
    assert clamped["breach"] is True, f"{label}: deep gap-through must breach the circuit"
    # the realized loss when flattening at the clamped floor == threshold_usd
    capped_loss = (a1 - clamped["floor_price"]) * q1
    assert clamped["threshold_usd"] <= R0 + EPS, f"{label}: threshold {clamped['threshold_usd']:.2f} > R0 {R0:.2f}"
    assert capped_loss <= R0 + EPS, f"{label}: capped loss {capped_loss:.2f} > R0 {R0:.2f}"


@pytest.mark.parametrize("label,kw,gap_frac", CASES)
def test_guard1_is_necessary_unclamped_exceeds_R0(label, kw, gap_frac):
    """NECESSITY: WITHOUT GUARD #1 the enlarged circuit threshold blows past R0 — proving
    the clamp is load-bearing (this is the bug the red-team caught)."""
    q1, a1, s1, R0, _, _ = _simulate_add(**kw)
    gap_bid = a1 * gap_frac
    unclamped = max_loss_circuit_decision(
        avg=a1, qty=q1, stop_distance=kw["d0"], bid=gap_bid, k=K,  # NO risk_anchor_usd
    )
    # k*stop_distance*q1 for the ENLARGED qty is multiples of R0 (k * (q1/q0) * R0).
    assert unclamped["threshold_usd"] > R0 + EPS, (
        f"{label}: unclamped threshold {unclamped['threshold_usd']:.2f} should exceed R0 {R0:.2f}"
    )


def test_max_loss_circuit_anchor_none_byte_identical():
    """PARITY: risk_anchor_usd=None (and unset) is byte-identical to the legacy circuit."""
    common = dict(avg=10.0, qty=1500.0, stop_distance=0.10, bid=9.60, k=2.0)
    legacy = max_loss_circuit_decision(**common)
    explicit_none = max_loss_circuit_decision(**common, risk_anchor_usd=None)
    assert legacy == explicit_none
    # legacy floor == avg - k*stop_distance (the documented absolute floor)
    assert legacy["floor_price"] == pytest.approx(10.0 - 2.0 * 0.10)
    assert legacy["threshold_usd"] == pytest.approx(2.0 * 0.10 * 1500.0)


def test_anchor_only_tightens_never_loosens():
    """The clamp is a TIGHTEN of #769, never a weaken — Hard-Rule (never loosen the floor)."""
    common = dict(avg=10.0, qty=1500.0, stop_distance=0.10, bid=9.60, k=2.0)
    base = max_loss_circuit_decision(**common)                                  # threshold = 300
    tight = max_loss_circuit_decision(**common, risk_anchor_usd=100.0)          # clamp to 100
    assert tight["threshold_usd"] <= base["threshold_usd"]
    assert tight["floor_price"] >= base["floor_price"]   # tighter floor sits HIGHER (less loss)
    # a HUGE anchor must NOT loosen below the structural circuit
    loose_attempt = max_loss_circuit_decision(**common, risk_anchor_usd=10_000.0)
    assert loose_attempt["threshold_usd"] == pytest.approx(base["threshold_usd"])
    assert math.isclose(loose_attempt["floor_price"], base["floor_price"], abs_tol=1e-9)
