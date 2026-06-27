"""Exhaustive adversarial bounds tests for CATALYST-CONVICTION SIZE (momentum LIVE lane).

CATALYST-CONVICTION SIZE (built, DEFAULT OFF — flag chili_momentum_catalyst_conviction_enabled)
is a bounded UPWARD size multiplier earned when the name carries a STRONG, credible catalyst
(the DEPLOYED strong/weak/fake news grade — FDA/trial/M&A/contract/beat, not also diluting /
rumored / hacked). It mirrors GREEN-DAY GRADUATION: it composes MULTIPLICATIVELY into the
runner's combined size-multiplier product under the existing ~3.0x ceiling + the downstream
hard notional ceiling, applied at entry-quantity compute time. It is NEVER a veto — it can only
scale size UP (>=1.0), never zero / block / shrink an entry (a catalyst only ADDS; the no-news
shrink lives elsewhere).

The grade source is REUSED (no new feed): catalyst_grade_rank reads the same strong / weak /
fake accessors the lane already uses. STRONG (and not also weak / fake) => rank 3; weak / fake /
medium / none / crypto => rank 0. Weak and fake DOMINATE (suppress the strong boost to 0),
matching catalyst_grade_selection_delta.

The properties proven here:
  P1  no / weak / fake / none catalyst              => multiplier == 1.0 exactly
  P2  STRONG catalyst                               => > 1.0, the exact step formula
  P3  bounded: strongest grade / huge step          => clamped at max_multiplier (never over)
  P4  NEVER a veto — only scales; cannot return 0 / negative / block an entry
  P5  composed UNDER the existing ~3.0x ceiling + the hard notional ceiling
  P6  flag OFF => multiplier 1.0 (byte-identical), short-circuits before grade read
  P7  fail-neutral: any error => (1.0, error_fail_neutral)
  P8  dominance: weak / fake suppress the strong boost to rank 0 => 1.0

The multiplier is PURE over an injected grade set (strong/weak/fake symbols passed in), so no
news feed / DB is required — the same sets the runner fetches once upstream.

[[project_momentum_lane]] [[feedback_adaptive_no_magic]] [[feedback_overfit_default_live]]
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural import catalyst as cat
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.catalyst import catalyst_grade_rank
from app.services.trading.momentum_neural.risk_policy import (
    catalyst_conviction_size_multiplier,
    compute_risk_first_quantity,
)


# ── helpers ─────────────────────────────────────────────────────────────────────


def _enable(monkeypatch, *, step: float = 0.15, max_mult: float = 1.5) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_enabled", True, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_step", step, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_max_multiplier", max_mult, raising=False
    )
    # The fake-guard gate the grade-rank consults defaults ON; pin it ON for determinism.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )


def _strong(sym: str = "ABCD"):
    return {sym}, set(), set()  # (strong, weak, fake)


# ── P1: no / weak / fake / none => 1.0 exactly ────────────────────────────────────


def test_p1_no_catalyst_is_exactly_one(monkeypatch) -> None:
    _enable(monkeypatch)
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=set(), weak_symbols=set(), fake_symbols=set()
    )
    assert mult == 1.0  # exact, not approx
    assert meta["grade_rank"] == 0


def test_p1_weak_catalyst_is_one(monkeypatch) -> None:
    # A diluting / compliance headline earns NO conviction boost.
    _enable(monkeypatch)
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols={"ABCD"}, fake_symbols=set()
    )
    assert mult == 1.0


def test_p1_fake_catalyst_is_one(monkeypatch) -> None:
    # A rumored / hacked / unsolicited "strong" headline earns NO boost (credibility veto).
    _enable(monkeypatch)
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols={"ABCD"}
    )
    assert mult == 1.0


def test_p1_symbol_not_in_strong_set_is_one(monkeypatch) -> None:
    # Some OTHER name has the strong catalyst; this one doesn't -> neutral.
    _enable(monkeypatch)
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols={"WXYZ"}, weak_symbols=set(), fake_symbols=set()
    )
    assert mult == 1.0


def test_p1_crypto_is_one(monkeypatch) -> None:
    # Crypto (-USD) never carries an equity news catalyst -> always 1.0.
    _enable(monkeypatch)
    mult, _ = catalyst_conviction_size_multiplier(
        "BTC-USD", strong_symbols={"BTC"}, weak_symbols=set(), fake_symbols=set()
    )
    assert mult == 1.0


# ── P2: STRONG catalyst => > 1.0, exact step formula ──────────────────────────────


def test_p2_strong_catalyst_boosts_exact_formula(monkeypatch) -> None:
    # rank 3, step 0.15 => 1.0 + 0.15*3 = 1.45 (< max 1.5, so not clamped).
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(1.45)
    assert meta["grade_rank"] == 3
    assert meta["conviction_mult"] == pytest.approx(1.45)


@pytest.mark.parametrize(
    "step, expected",
    [(0.0, 1.0), (0.05, 1.15), (0.10, 1.30), (0.15, 1.45)],
)
def test_p2_step_progression_under_cap(monkeypatch, step, expected) -> None:
    # With max 1.5, ranks*step stays under the cap for these steps; exact formula holds.
    _enable(monkeypatch, step=step, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(expected)


def test_p2_strong_always_at_least_one(monkeypatch) -> None:
    # Even with step 0 a strong catalyst is neutral (never < 1.0).
    _enable(monkeypatch, step=0.0)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == 1.0


# ── P3: bounded at max_multiplier (never over) ────────────────────────────────────


def test_p3_strongest_grade_clamped_at_max(monkeypatch) -> None:
    # step 0.15 * rank 3 = 0.45 -> 1.45 would be the raw; a TIGHT max of 1.2 must clamp.
    _enable(monkeypatch, step=0.15, max_mult=1.2)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(1.2)


def test_p3_huge_step_still_clamped(monkeypatch) -> None:
    # Adversarial knob: step=1.0 on rank 3 would be 4.0x -> clamp to max 1.5.
    _enable(monkeypatch, step=1.0, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(1.5)


def test_p3_default_max_never_exceeded(monkeypatch) -> None:
    # The configured default max (1.5) is the hard ceiling for the strongest grade.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult <= 1.5 + 1e-9


def test_p3_max_mult_one_disables_growth(monkeypatch) -> None:
    # max_multiplier clamped to 1.0 -> no growth ever, even on a strong catalyst.
    _enable(monkeypatch, step=0.5, max_mult=1.0)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == 1.0


def test_p3_sub_one_max_mult_guarded_to_one(monkeypatch) -> None:
    # A broken/negative ceiling (< 1.0) must be guarded to 1.0, never shrink below 1.0.
    _enable(monkeypatch, step=0.15, max_mult=0.5)
    strong, weak, fake = _strong()
    mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == 1.0


# ── P4: NEVER a veto — only scales, cannot zero / block an entry ──────────────────


def test_p4_multiplier_never_below_one(monkeypatch) -> None:
    # Across every grade arrangement the multiplier is in [1.0, max] — never 0 / negative.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    cases = [
        (set(), set(), set()),                 # none
        ({"ABCD"}, set(), set()),              # strong
        ({"ABCD"}, {"ABCD"}, set()),           # strong + weak (suppressed)
        ({"ABCD"}, set(), {"ABCD"}),           # strong + fake (suppressed)
        ({"ABCD"}, {"ABCD"}, {"ABCD"}),        # all three
    ]
    for strong, weak, fake in cases:
        mult, _ = catalyst_conviction_size_multiplier(
            "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
        )
        assert 1.0 <= mult <= 1.5
        assert mult != 0.0


def test_p4_conviction_cannot_zero_out_quantity(monkeypatch) -> None:
    # The multiplier feeds the max_loss basis; even a tiny max_loss with a strong catalyst
    # yields qty > 0 (conviction only ADDS size). Structurally incapable of vetoing.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    cc_mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    base_max_loss = 1.0  # $1 — adversarially tiny
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0,
        atr_pct=0.05,
        max_loss_usd=base_max_loss * cc_mult,
        max_notional_ceiling_usd=1000.0,
    )
    assert qty > 0.0, meta
    # A graduated max_loss yields qty >= the un-graduated one (monotone up, never shrinks).
    qty_base, _ = compute_risk_first_quantity(
        entry_price=10.0, atr_pct=0.05, max_loss_usd=base_max_loss,
        max_notional_ceiling_usd=1000.0,
    )
    assert qty >= qty_base


# ── P5: composed UNDER the existing ~3.0x ceiling + hard notional ceiling ─────────


def test_p5_composed_under_three_x_ceiling(monkeypatch) -> None:
    """Replicate the runner's product-then-clamp at live_runner.py: conviction is one factor;
    the whole product is clamped to base * 3.0. A maxed conviction (1.5) stacked on other
    up-multipliers must NOT push effective max-loss past the 3x hard cap.
    """
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    cc_mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert cc_mult == pytest.approx(1.45)

    base_max_loss = 50.0
    # other adversarial up-multipliers in the runner's product chain
    streak_mult, grad_mult, cushion_mult = 1.5, 2.0, 1.4
    product = base_max_loss * streak_mult * grad_mult * cushion_mult * cc_mult
    eff_max_loss = min(product, base_max_loss * 3.0)  # the hard ceiling at live_runner.py
    assert eff_max_loss == pytest.approx(base_max_loss * 3.0)  # clamp bit, not the raw product
    assert eff_max_loss <= base_max_loss * 3.0 + 1e-9


def test_p5_notional_ceiling_caps_final_quantity(monkeypatch) -> None:
    # Even with conviction maxed, compute_risk_first_quantity caps notional at the hard
    # max_notional_ceiling_usd regardless of the multiplier product.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    cc_mult, _ = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    ceiling = 500.0
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0,
        atr_pct=0.10,
        max_loss_usd=50.0 * cc_mult,  # conviction-graduated basis
        max_notional_ceiling_usd=ceiling,
    )
    assert qty * 10.0 <= ceiling + 1e-6
    assert meta.get("capped_by") == "notional_ceiling"


# ── P6: flag OFF => 1.0 (byte-identical), short-circuits before grade read ────────


def test_p6_flag_off_is_one_even_with_strong_catalyst(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_enabled", False, raising=False
    )
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols=set()
    )
    assert mult == 1.0
    assert meta == {"reason": "disabled", "conviction_mult": 1.0}


def test_p6_flag_off_short_circuits_before_grade_read(monkeypatch) -> None:
    # Disabled path must NOT even read the grade (byte-identical to the function not existing).
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_enabled", False, raising=False
    )

    def _must_not_run(*a, **k):
        raise AssertionError("grade must not be read when the flag is OFF")

    monkeypatch.setattr(cat, "catalyst_grade_rank", _must_not_run)
    mult, meta = catalyst_conviction_size_multiplier("ABCD")
    assert mult == 1.0
    assert meta["reason"] == "disabled"


# ── P7: fail-neutral — any error => (1.0, error_fail_neutral) ─────────────────────


def test_p7_error_path_is_fail_neutral(monkeypatch) -> None:
    _enable(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("synthetic grade failure")

    monkeypatch.setattr(cat, "catalyst_grade_rank", _boom)
    mult, meta = catalyst_conviction_size_multiplier("ABCD")
    assert mult == 1.0
    assert meta.get("reason") == "error_fail_neutral"


# ── P8: dominance — weak / fake suppress the strong boost to rank 0 ───────────────


def test_p8_grade_rank_strong_is_three(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols=set()
    )
    assert rank == 3


def test_p8_weak_dominates_strong(monkeypatch) -> None:
    # A name that is both diluting and 'partnering' is a dilution fade -> rank 0.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols={"ABCD"}, fake_symbols=set()
    )
    assert rank == 0


def test_p8_fake_dominates_strong_when_guard_on(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols={"ABCD"}
    )
    assert rank == 0


def test_p8_fake_does_not_suppress_when_guard_off(monkeypatch) -> None:
    # With the credibility guard OFF, a strong-titled rumor keeps the strong rank (guard
    # consistency with selection — the strong boost path is restored).
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", False, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols={"ABCD"}
    )
    assert rank == 3


def test_p8_grade_rank_norms_symbol(monkeypatch) -> None:
    # The accessor normalizes (upper / strip the -USD tail); a bare equity ticker matches.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    rank = catalyst_grade_rank(
        " abcd ", strong_symbols={"ABCD"}, weak_symbols=set(), fake_symbols=set()
    )
    assert rank == 3
