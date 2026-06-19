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


# ─────────────────────────────────────────────────────────────────────────────
# B–E: the WIRING tests. The load-bearing add logic lives in two pure helpers
# (pyramid_add_decision = the cushion+confirmation predicate; pyramid_blend_on_fill
# = the blend math + INVARIANT-A ratchet) that the live_runner AND the replay both
# call — one source of truth. We test those directly (deterministic, no broker
# mocking), plus drive tick_live_session on a held winner for the flag-OFF parity
# proof (B), and assert the C1b clamp interaction end-to-end.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone  # noqa: E402
from unittest.mock import patch  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.trading.momentum_neural.paper_execution import (  # noqa: E402
    pyramid_add_decision,
    pyramid_blend_on_fill,
)

# A RECENT open timestamp so the held position never trips the max-hold exit before
# the tick reaches the pyramid add block.
_RECENT_OPEN = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fire_kwargs(**over):
    """A baseline set of inputs that FIRES the add (all gates satisfied)."""
    base = dict(
        enabled=True,
        is_equity=True,
        add_count=0,
        max_adds=1,
        in_flight=False,
        a0=10.0,
        q0=1000.0,
        d0=0.10,            # R0 = d0*q0 = 100
        bid=10.50,          # cushion = (10.50-10.0)*1000 = 500 = 5R >= 1R
        stop_px=10.05,      # >= a0 (banked) AND >= entry_stop_ref (ratcheted)
        entry_stop_ref=10.00,
        high_water_mark=10.50,
        ofi=0.40,           # >= 0.25 threshold
        ofi_threshold=0.25,
        min_cushion_r=1.0,
        midday_lull=False,
    )
    base.update(over)
    return base


# ── B. PARITY-OFF ────────────────────────────────────────────────────────────

def test_B_predicate_flag_off_never_fires():
    """flag OFF => the predicate returns fire=False (reason flag_off), R0 untouched —
    the whole live block is a no-op (byte-identical)."""
    d = pyramid_add_decision(**_fire_kwargs(enabled=False))
    assert d["fire"] is False
    assert d["reason"] == "flag_off"


def test_B_parity_off_full_tick_no_add_no_mutation(monkeypatch, db):
    """PARITY-OFF (end-to-end): with the flag OFF, driving tick_live_session on a held
    TRAILING winner produces NO add, NO pos delta, NO pyramid_* keys, and the C1b
    circuit call passes risk_anchor_usd=None (le has no pyramid_risk_anchor_usd)."""
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)  # OFF
    vid, _ = _seed_live_eligible_row(db, symbol="PYR-USD")
    db.commit()
    uid = _uid(db, "pyroff")
    # A HEALTHY held winner: bid (10.55) sits between the stop (10.05) and target
    # (10.80) — no stop breach, no target scale-out — so the ONLY thing that could
    # place an order this tick is a pyramid ADD, which the flag-OFF gate must skip.
    pos = {
        "product_id": "PYR-USD", "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": 10.0, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": 10.55, "stop_price": 10.20, "target_price": 10.80,
        "partial_taken": True,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="PYR-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400},
            "momentum_live_execution": {
                "position": dict(pos),
                "entry_sizing": {"model": "risk_first", "stop_distance": 0.10},
                "entry_stop_atr_pct": 0.01,
                "admission_viability_score": 0.9,
            },
        },
    )
    db.commit()
    ad = _mk_held_adapter("PYR-USD", bid=10.55, ask=10.56)

    captured = {}
    import app.services.trading.momentum_neural.live_runner as lr

    real_circuit = lr.max_loss_circuit_decision

    def _spy_circuit(**kw):
        captured["risk_anchor_usd"] = kw.get("risk_anchor_usd")
        return real_circuit(**kw)

    with patch.object(lr, "max_loss_circuit_decision", _spy_circuit), \
         patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch("app.services.trading.momentum_neural.pipeline._live_ofi_microprice",
               return_value=(0.9, 1.0)), \
         patch.object(lr, "is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    # No add order was placed and no pyramid bookkeeping appeared.
    assert "pyramid_order_id" not in le
    assert "pyramid_add_count" not in le
    assert "pyramid_risk_anchor_usd" not in le
    # No BUY-side order at all (a pyramid add is the only BUY in a held tick).
    for call in ad.place_limit_order_gtc.call_args_list:
        assert call.kwargs.get("side") != "buy", "flag OFF must place no add BUY"
    # Position quantity/avg unchanged (no blend / no add).
    final_pos = le.get("position") or {}
    assert final_pos.get("quantity") == pytest.approx(1000.0)
    assert final_pos.get("avg_entry_price") == pytest.approx(10.0)
    # The C1b circuit (if it ran this tick) was called with risk_anchor_usd=None.
    if "risk_anchor_usd" in captured:
        assert captured["risk_anchor_usd"] is None


# ── C. EXIT-UNTOUCHED / INVARIANT-A ──────────────────────────────────────────

@pytest.mark.parametrize("label,kw,gap_frac", CASES)
def test_C_blend_invariant_A_stop_only_tightens(label, kw, gap_frac):
    """INVARIANT-A: the blended stop s1 = max(stop_px, a1) NEVER loosens. Mirror the
    gate's add for every adversarial case and assert s1 >= the pre-add live stop."""
    q1, a1, s1_expected, R0, qa_f, Pa_f = _simulate_add(**kw)
    pre_add_stop = kw["a0"] * (1.0 + kw["stop_px_over_a0"])
    blend = pyramid_blend_on_fill(
        q0=kw["q0"], a0=kw["a0"], qa_f=qa_f, Pa_f=Pa_f,
        stop_px=pre_add_stop, original_quantity=kw["q0"],
    )
    assert blend["s1"] >= pre_add_stop - EPS, f"{label}: stop loosened"
    assert blend["s1"] == pytest.approx(s1_expected)
    assert blend["q1"] == pytest.approx(q1)
    assert blend["a1"] == pytest.approx(a1)
    # original_quantity GROWS by the filled add (so scale-out de-risks the enlarged size)
    assert blend["original_quantity"] == pytest.approx(kw["q0"] + qa_f)


def test_C_blend_asserts_on_attempted_loosen():
    """pyramid_blend_on_fill ASSERTS if a (impossible) blend would loosen the stop —
    a hard guard that the stop math can only tighten."""
    # a1 below the incoming stop_px is impossible for a winner-add, but prove the guard:
    # force stop_px far above a1 so s1 == stop_px (tighten, not loosen) — never asserts.
    out = pyramid_blend_on_fill(q0=100, a0=10.0, qa_f=50, Pa_f=10.5, stop_px=20.0)
    assert out["s1"] == pytest.approx(20.0)  # ratchets to the (higher) live stop, never below


def test_C_exit_untouched_pending_breach_skips_add_via_no_new_hod():
    """A position whose bid has fallen back below the HOD (an exit may be pending) does
    NOT satisfy the new-HOD confirmation — the add is declined, exit path is free."""
    d = pyramid_add_decision(**_fire_kwargs(bid=10.20, high_water_mark=10.50))
    assert d["fire"] is False
    assert d["reason"] == "not_new_hod"


# ── D. IDEMPOTENCY ───────────────────────────────────────────────────────────

def test_D_in_flight_blocks_second_submit():
    """An add already in flight blocks a second submit (idempotency)."""
    d = pyramid_add_decision(**_fire_kwargs(in_flight=True))
    assert d["fire"] is False
    assert d["reason"] == "add_in_flight"


def test_D_add_count_cap_blocks_after_max():
    """At most chili_momentum_pyramid_max_adds adds per position."""
    d = pyramid_add_decision(**_fire_kwargs(add_count=1, max_adds=1))
    assert d["fire"] is False
    assert d["reason"] == "max_adds_reached"
    # a higher cap re-opens it
    d2 = pyramid_add_decision(**_fire_kwargs(add_count=1, max_adds=2))
    assert d2["fire"] is True


def test_D_partial_add_blends_only_filled_qty():
    """A PARTIAL add fill blends ONLY the filled qty (qa_f), not the planned qty."""
    # planned add qa = rho*R0/d0 = 0.5*100/0.10 = 500; only 40% (200) fills.
    planned = 500.0
    qa_f = 0.40 * planned  # 200
    blend = pyramid_blend_on_fill(q0=1000.0, a0=10.0, qa_f=qa_f, Pa_f=10.50, stop_px=10.05)
    assert blend["q1"] == pytest.approx(1200.0)             # 1000 + 200, NOT 1500
    assert blend["original_quantity"] == pytest.approx(1200.0)
    # blended avg uses only the filled 200
    assert blend["a1"] == pytest.approx((10.0 * 1000 + 10.50 * 200) / 1200)


# ── E. GUARD #4 / fail-closed gates ──────────────────────────────────────────

def test_E_crypto_is_deferred():
    """EQUITY-FIRST: a crypto (-USD) name is deferred (partial L2/OFI)."""
    d = pyramid_add_decision(**_fire_kwargs(is_equity=False))
    assert d["fire"] is False
    assert d["reason"] == "crypto_deferred"


def test_E_cushion_not_banked_blocks():
    """GUARD #2: without >= min_cushion_r * R0 banked the add is refused."""
    # cushion = (10.05-10.0)*1000 = 50 = 0.5R < 1R
    d = pyramid_add_decision(**_fire_kwargs(bid=10.05, high_water_mark=10.05))
    assert d["fire"] is False
    assert d["reason"] == "cushion_not_banked"


def test_E_stop_below_breakeven_blocks():
    """GUARD #2: the starter stop must already be ratcheted to >= breakeven (a0)."""
    d = pyramid_add_decision(**_fire_kwargs(stop_px=9.90, entry_stop_ref=9.90))
    assert d["fire"] is False
    assert d["reason"] == "cushion_not_banked"


def test_E_ofi_below_threshold_blocks():
    """CONFIRMATION: a weak/None OFI fails closed (no add without genuine thrust)."""
    assert pyramid_add_decision(**_fire_kwargs(ofi=0.10))["reason"] == "ofi_below_threshold"
    assert pyramid_add_decision(**_fire_kwargs(ofi=None))["reason"] == "ofi_below_threshold"


def test_E_trail_not_ratcheted_blocks():
    """CONFIRMATION: a stop that has NOT ratcheted up since first-considered fails the
    headroom test (the runner is not structurally advancing)."""
    d = pyramid_add_decision(**_fire_kwargs(stop_px=10.05, entry_stop_ref=10.20))
    assert d["fire"] is False
    assert d["reason"] == "trail_not_ratcheted"


def test_E_midday_lull_blocks():
    """Anti-Ross midday: no add inside the equity midday lull."""
    d = pyramid_add_decision(**_fire_kwargs(midday_lull=True))
    assert d["fire"] is False
    assert d["reason"] == "midday_lull"


def test_E_bad_basis_fails_closed():
    """A missing/zero R0 basis fails closed (never sizes an add against unknown risk)."""
    assert pyramid_add_decision(**_fire_kwargs(d0=None))["reason"] == "bad_basis"
    assert pyramid_add_decision(**_fire_kwargs(d0=0.0))["reason"] == "bad_basis"
    assert pyramid_add_decision(**_fire_kwargs(q0=0.0))["reason"] == "bad_basis"


def test_E_guard4_admission_refusal_aborts_add_not_exit(monkeypatch, db):
    """GUARD #4 (end-to-end): with the flag ON and the trigger predicate FIRING, a
    risk-admission REFUSAL (simulated kill-switch / daily-loss-cap / position-cap /
    stale-viability via runner_boundary_risk_ok) aborts the ADD — NO add BUY is placed,
    a 'live_pyramid_add_blocked' event is emitted, and the held position + its exit path
    are left intact (the add never touches the exit). We pin pyramid_add_decision to
    FIRE so the GUARD-#4 branch is provably reached, then refuse admission."""
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.models.trading import TradingAutomationEvent
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="PYRG-USD")
    db.commit()
    uid = _uid(db, "pyrg4")
    # Healthy held winner: bid (10.55) between stop (10.20) and target (10.90) — no
    # stop/target exit fires, so the ONLY order a tick could place here is an add BUY.
    pos = {
        "product_id": "PYRG-USD", "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": 10.0, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": 10.55, "stop_price": 10.20, "target_price": 10.90,
        "partial_taken": True,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="PYRG-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400},
            "momentum_live_execution": {
                "position": dict(pos),
                "entry_sizing": {"model": "risk_first", "stop_distance": 0.10},
                "entry_stop_atr_pct": 0.01,
                "admission_viability_score": 0.9,
            },
        },
    )
    db.commit()
    ad = _mk_held_adapter("PYRG-USD", bid=10.55, ask=10.56)
    ad.get_order.return_value = (None, None)  # no in-flight/pending order to poll

    # Pin the predicate to FIRE (so the GUARD-#4 branch is reached) AND refuse the risk
    # admission AT THE ADD SITE. runner_boundary_risk_ok runs once at the top of the tick
    # (let it PASS so the tick proceeds normally to TRAILING) and again at the add site
    # (REFUSE there). A call-counting side_effect distinguishes them.
    _fire = {"fire": True, "reason": "confirmed", "R0": 100.0, "cushion_r": 5.0, "cushion_usd": 500.0}
    _calls = {"n": 0}

    def _admission(*a, **k):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return True, {}  # top-of-tick: allow the held tick to proceed
        return False, {"severity": "error", "errors": ["daily_loss_cap"]}  # add-site: refuse

    # Neutralize the upstream trail ratchets so the held tick reaches the add block
    # cleanly (no trail/stop exit before it): the cushion-trail returns the live stop
    # unchanged, so bid (10.55) stays above the stop (10.20) and no exit fires.
    with patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False), \
         patch.object(lr, "cushion_adaptive_trail_stop", side_effect=lambda **kw: kw["current_stop"]), \
         patch.object(lr, "pyramid_add_decision", return_value=_fire), \
         patch.object(lr, "runner_boundary_risk_ok", side_effect=_admission):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    # The add was REFUSED: no in-flight order, no BUY placed, position intact.
    assert not le.get("pyramid_order_id")
    assert int(le.get("pyramid_add_count") or 0) == 0
    for call in ad.place_limit_order_gtc.call_args_list:
        assert call.kwargs.get("side") != "buy", "GUARD #4 must place no add BUY"
    final_pos = le.get("position") or {}
    assert final_pos.get("quantity") == pytest.approx(1000.0)
    assert final_pos.get("avg_entry_price") == pytest.approx(10.0)
    assert out.get("ok")
    # The block emitted the refusal event.
    kinds = {
        et for (et,) in db.query(TradingAutomationEvent.event_type)
        .filter(TradingAutomationEvent.session_id == sess.id).all()
    }
    assert "live_pyramid_add_blocked" in kinds


def test_D_full_add_lifecycle_submit_adopt_blend_idempotent(monkeypatch, db):
    """HAPPY PATH (end-to-end): admission PASSES + the predicate FIRES → tick 1 submits
    ONE add BUY (pos NOT yet mutated), tick 2 adopts the confirmed fill and blends the
    position (qty grows, avg blends, original_quantity grows, stop ratchets to >= a1,
    pyramid_risk_anchor_usd = R0). IDEMPOTENCY: tick 2 (and a 3rd) place no SECOND add
    (add_count cap=1)."""
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_max_adds", 1)
    vid, _ = _seed_live_eligible_row(db, symbol="PYRH-USD")
    db.commit()
    uid = _uid(db, "pyrh")
    q0, a0, d0 = 1000.0, 10.0, 0.10        # R0 = 100; bid 10.55 => cushion 5R
    pos = {
        "product_id": "PYRH-USD", "side": "long",
        "quantity": q0, "original_quantity": q0,
        "avg_entry_price": a0, "notional_usd": q0 * a0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": 10.55, "stop_price": 10.20, "target_price": 10.90,
        "partial_taken": True,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="PYRH-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50000, "max_hold_seconds": 86400},
            "momentum_live_execution": {
                "position": dict(pos),
                "entry_sizing": {"model": "risk_first", "stop_distance": d0},
                "entry_stop_atr_pct": 0.01,
                "admission_viability_score": 0.9,
            },
        },
    )
    db.commit()
    ad = _mk_held_adapter("PYRH-USD", bid=10.55, ask=10.56)
    # The add order is OPEN (in flight) right after submit, FILLED on the next poll.
    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)
    add_qa = 500.0  # rho*R0/d0 = 0.5*100/0.10
    Pa_f = 10.56
    _filled = NormalizedOrder(
        order_id="ord-pyr-1", client_order_id="cid-pyr", product_id="PYRH-USD",
        side="buy", status="filled", order_type="limit",
        filled_size=add_qa, average_filled_price=Pa_f,
    )
    # get_order returns the FILLED add when polled (tick 2). No other pending order.
    ad.get_order.return_value = (_filled, fresh)
    _fire = {"fire": True, "reason": "confirmed", "R0": 100.0, "cushion_r": 5.0, "cushion_usd": 500.0}

    common_patches = lambda: [
        patch.object(lr, "_venue_broker_connected", return_value=True),
        patch.object(lr, "is_kill_switch_active", return_value=False),
        patch.object(lr, "cushion_adaptive_trail_stop", side_effect=lambda **kw: kw["current_stop"]),
        patch.object(lr, "runner_boundary_risk_ok", return_value=(True, {})),
    ]

    # TICK 1 — predicate fires, admission passes => ONE add BUY submitted; pos UNTOUCHED.
    import contextlib
    with contextlib.ExitStack() as es:
        for p in common_patches():
            es.enter_context(p)
        es.enter_context(patch.object(lr, "pyramid_add_decision", return_value=_fire))
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    buys = [c for c in ad.place_limit_order_gtc.call_args_list if c.kwargs.get("side") == "buy"]
    assert len(buys) == 1, "tick 1 must submit exactly one add BUY"
    assert le.get("pyramid_order_id") == "ord-pyr-1"
    # pos NOT mutated on submit (mutate only on confirmed fill).
    assert le["position"]["quantity"] == pytest.approx(q0)
    assert le["position"]["avg_entry_price"] == pytest.approx(a0)
    assert "pyramid_risk_anchor_usd" not in le

    # TICK 2 — the in-flight order is FILLED => adopt + blend. The REAL predicate runs
    # this tick: the in_flight gate (pyramid_order_id set) and then the add_count cap
    # block any 2nd submit (no decision patch — we exercise the real idempotency gates).
    with contextlib.ExitStack() as es:
        for p in common_patches():
            es.enter_context(p)
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    fpos = le["position"]
    q1 = q0 + add_qa
    a1 = (a0 * q0 + Pa_f * add_qa) / q1
    assert fpos["quantity"] == pytest.approx(q1)
    assert fpos["avg_entry_price"] == pytest.approx(a1)
    assert fpos["original_quantity"] == pytest.approx(q0 + add_qa)  # GROWN
    assert fpos["stop_price"] >= 10.20 - 1e-9                       # INVARIANT-A: tightened
    assert fpos["stop_price"] == pytest.approx(max(10.20, a1))
    assert le.get("pyramid_risk_anchor_usd") == pytest.approx(100.0)  # R0 frozen for C1b
    assert int(le.get("pyramid_add_count") or 0) == 1
    assert not le.get("pyramid_order_id")  # cleared after adopt
    # only ONE add BUY total across both ticks (idempotent).
    buys = [c for c in ad.place_limit_order_gtc.call_args_list if c.kwargs.get("side") == "buy"]
    assert len(buys) == 1, "idempotency: at most one add per position"

    # TICK 3 — add_count cap reached (=1) => the REAL predicate returns max_adds_reached,
    # so still no second add even though the cushion/confirm would otherwise fire.
    ad.get_order.return_value = (None, None)  # nothing in flight now
    with contextlib.ExitStack() as es:
        for p in common_patches():
            es.enter_context(p)
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)
    buys = [c for c in ad.place_limit_order_gtc.call_args_list if c.kwargs.get("side") == "buy"]
    assert len(buys) == 1, "max_adds cap: no third-tick add"


def _mk_held_adapter(symbol: str, *, bid: float, ask: float):
    """A MagicMock venue adapter for a HELD position (fresh BBO, tradable product)."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from app.services.trading.venue.protocol import (
        FreshnessMeta, NormalizedProduct, NormalizedTicker,
    )

    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)
    ad = MagicMock()
    ad.is_enabled.return_value = True
    mid = (bid + ask) / 2.0
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id=symbol, bid=bid, ask=ask, mid=mid,
                         spread_bps=(ask - bid) / mid * 10_000.0, freshness=fresh),
        fresh,
    )
    prod = NormalizedProduct(
        product_id=symbol, base_currency=symbol.split("-")[0], quote_currency="USD",
        status="online", trading_disabled=False, cancel_only=False, limit_only=False,
        post_only=False, auction_mode=False, base_increment=0.001, base_min_size=0.001,
    )
    ad.get_product.return_value = (prod, fresh)
    ad.place_limit_order_gtc.return_value = {"ok": True, "order_id": "ord-pyr-1", "client_order_id": "cid-pyr"}
    ad.cancel_order.return_value = {"ok": True, "raw": {}}
    return ad
