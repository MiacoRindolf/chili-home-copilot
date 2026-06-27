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


# ══════════════════════════════════════════════════════════════════════════════════
# HARDENING (adversarial branch coverage) — appended.
#
# Targets the remaining UNTESTED branches in catalyst_conviction_size_multiplier and
# catalyst_grade_rank:
#   H1  step  falsy-zero / None  guard  (risk_policy.py:1062 — `_step_raw if not None`)
#   H2  max_mult None  guard            (risk_policy.py:1064)
#   H3  max(0, rank) negative-rank floor (risk_policy.py:1078)
#   H4  meta payload completeness on the STRONG path
#   H5  int() coercion of a float grade_rank
#   H6  fetch-fresh path (sets omitted => grade_rank called with None kwargs)
#   H7  catalyst_grade_rank edge inputs: None / "" symbol, fail-open try/except,
#       empty/None strong set, crypto, weak-AND-fake stacking, guard-off + weak
# ══════════════════════════════════════════════════════════════════════════════════


# ── H1: the falsy-zero / None step guard (the explicit `is not None` check) ────────


def test_h1_step_zero_is_not_overwritten_by_default(monkeypatch) -> None:
    """REGRESSION GUARD: a legitimate step=0.0 must STAY 0.0, NOT fall back to the 0.15
    default. With `... or 0.15` (the bug this guard prevents) a strong catalyst would
    wrongly multiply by 1.45 instead of 1.0. Assert the SPECIFIC neutral value."""
    _enable(monkeypatch, step=0.0, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == 1.0  # 1.0 + 0.0*3 == 1.0 — NOT 1.45 (the `or 0.15` fallback bug)
    assert meta["step"] == 0.0
    assert meta["grade_rank"] == 3  # the grade is still strong; only the step is 0


def test_h1_step_none_falls_back_to_default(monkeypatch) -> None:
    # An explicitly-None step (config absent) DOES fall back to the 0.15 default.
    _enable(monkeypatch, max_mult=1.5)
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_step", None, raising=False
    )
    strong, weak, fake = _strong()
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(1.45)  # default 0.15 * rank 3
    assert meta["step"] == pytest.approx(0.15)


# ── H2: the None-aware max_multiplier guard ───────────────────────────────────────


def test_h2_max_mult_none_falls_back_to_default(monkeypatch) -> None:
    # max_multiplier=None (config absent) must fall back to 1.5, not crash / fail-neutral.
    _enable(monkeypatch, step=0.15)
    monkeypatch.setattr(
        settings, "chili_momentum_catalyst_conviction_max_multiplier", None, raising=False
    )
    strong, weak, fake = _strong()
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    assert mult == pytest.approx(1.45)
    assert meta["max_multiplier"] == pytest.approx(1.5)
    assert meta.get("reason") != "error_fail_neutral"  # None is handled, not an error


# ── H3: the max(0, rank) negative-rank floor ──────────────────────────────────────


def test_h3_negative_rank_floored_to_no_boost(monkeypatch) -> None:
    """If the grade accessor ever returned a NEGATIVE rank, max(0, rank) must floor the
    boost to 0 (multiplier 1.0) — a catalyst can only ADD size, never shrink it."""
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    monkeypatch.setattr(cat, "catalyst_grade_rank", lambda *a, **k: -5)
    mult, meta = catalyst_conviction_size_multiplier("ABCD")
    assert mult == 1.0  # 1.0 + 0.15*max(0,-5) == 1.0, NOT 1.0 - 0.75
    assert meta["grade_rank"] == -5  # the raw rank is echoed, but the BOOST is floored


def test_h3_rank_one_partial_boost(monkeypatch) -> None:
    # A hypothetical intermediate rank (1) yields the proportional step boost — proves the
    # formula uses the rank, not a hard 3-or-0 branch.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    monkeypatch.setattr(cat, "catalyst_grade_rank", lambda *a, **k: 1)
    mult, _ = catalyst_conviction_size_multiplier("ABCD")
    assert mult == pytest.approx(1.15)  # 1.0 + 0.15*1


# ── H4: meta payload completeness on the STRONG (enabled) path ─────────────────────


def test_h4_meta_payload_complete_on_strong(monkeypatch) -> None:
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    strong, weak, fake = _strong()
    mult, meta = catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=strong, weak_symbols=weak, fake_symbols=fake
    )
    # The enabled path returns the full diagnostic payload (NOT the disabled/error shape).
    assert set(meta.keys()) == {"conviction_mult", "grade_rank", "step", "max_multiplier"}
    assert meta["conviction_mult"] == pytest.approx(round(mult, 4))
    assert meta["step"] == pytest.approx(0.15)
    assert meta["max_multiplier"] == pytest.approx(1.5)
    assert "reason" not in meta  # reason only on disabled / error


# ── H5: int() coercion of a float grade_rank ──────────────────────────────────────


def test_h5_float_rank_coerced_to_int(monkeypatch) -> None:
    # catalyst_grade_rank is wrapped in int(); a float rank (e.g. 3.0) coerces cleanly.
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    monkeypatch.setattr(cat, "catalyst_grade_rank", lambda *a, **k: 3.0)
    mult, meta = catalyst_conviction_size_multiplier("ABCD")
    assert mult == pytest.approx(1.45)
    assert meta["grade_rank"] == 3
    assert isinstance(meta["grade_rank"], int)


# ── H6: fetch-fresh path — sets omitted => grade_rank called with None kwargs ──────


def test_h6_sets_omitted_forwards_none_to_grade_rank(monkeypatch) -> None:
    """When the strong/weak/fake sets are NOT passed, the multiplier forwards None to
    catalyst_grade_rank (which fetches fresh upstream). Assert the None forwarding so the
    'fetch once upstream' contract can't silently regress to always re-fetching."""
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    captured: dict = {}

    def _spy(symbol, *, strong_symbols=None, weak_symbols=None, fake_symbols=None):
        captured["strong"] = strong_symbols
        captured["weak"] = weak_symbols
        captured["fake"] = fake_symbols
        return 3

    monkeypatch.setattr(cat, "catalyst_grade_rank", _spy)
    mult, _ = catalyst_conviction_size_multiplier("ABCD")
    assert mult == pytest.approx(1.45)
    assert captured == {"strong": None, "weak": None, "fake": None}


def test_h6_passed_sets_forwarded_verbatim(monkeypatch) -> None:
    # Conversely, explicit sets are forwarded verbatim (no fresh fetch).
    _enable(monkeypatch, step=0.15, max_mult=1.5)
    captured: dict = {}

    def _spy(symbol, *, strong_symbols=None, weak_symbols=None, fake_symbols=None):
        captured["strong"] = strong_symbols
        captured["weak"] = weak_symbols
        captured["fake"] = fake_symbols
        return 3

    monkeypatch.setattr(cat, "catalyst_grade_rank", _spy)
    s, w, f = {"ABCD"}, {"WEAK"}, {"FAKE"}
    catalyst_conviction_size_multiplier(
        "ABCD", strong_symbols=s, weak_symbols=w, fake_symbols=f
    )
    assert captured == {"strong": s, "weak": w, "fake": f}


# ── H7: catalyst_grade_rank direct edge / failure coverage ─────────────────────────


def test_h7_grade_rank_none_symbol_is_zero(monkeypatch) -> None:
    # A None symbol must not crash — _norm("") => "" not in strong => 0.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    assert catalyst_grade_rank(None, strong_symbols={"ABCD"}) == 0


def test_h7_grade_rank_empty_symbol_is_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    assert catalyst_grade_rank("", strong_symbols={"ABCD"}) == 0


def test_h7_grade_rank_empty_strong_set_is_zero(monkeypatch) -> None:
    # `not strong or sym not in strong` — an empty strong set short-circuits to 0.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    assert catalyst_grade_rank("ABCD", strong_symbols=set()) == 0


def test_h7_grade_rank_crypto_short_circuits_zero(monkeypatch) -> None:
    # The -USD guard returns 0 BEFORE consulting the strong set (even if -USD root is strong).
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    assert catalyst_grade_rank("ETH-USD", strong_symbols={"ETH"}) == 0


def test_h7_grade_rank_weak_and_fake_both_present_is_zero(monkeypatch) -> None:
    # Both suppressors set: weak is checked first and dominates -> 0 (guard ON).
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols={"ABCD"}, fake_symbols={"ABCD"}
    )
    assert rank == 0


def test_h7_grade_rank_weak_still_dominates_when_guard_off(monkeypatch) -> None:
    # Guard OFF lifts the FAKE suppression but NOT the WEAK one — weak always dominates.
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", False, raising=False
    )
    rank = catalyst_grade_rank(
        "ABCD", strong_symbols={"ABCD"}, weak_symbols={"ABCD"}, fake_symbols={"ABCD"}
    )
    assert rank == 0  # weak still suppresses even with the fake guard OFF


def test_h7_grade_rank_fail_open_on_internal_error(monkeypatch) -> None:
    """The try/except in catalyst_grade_rank fails OPEN to 0 (no conviction invented). Force
    an error inside the body by handing a strong_symbols whose membership test raises."""
    monkeypatch.setattr(
        settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False
    )

    class _Boom:
        def __contains__(self, item):
            raise RuntimeError("synthetic membership failure")

        def __bool__(self):
            return True

    rank = catalyst_grade_rank("ABCD", strong_symbols=_Boom())
    assert rank == 0  # fail-open, not a propagated exception


def test_h7_conviction_mult_fail_neutral_when_grade_rank_fails_open(monkeypatch) -> None:
    # End-to-end: a grade_rank that fails-open to 0 yields a neutral 1.0 multiplier (NOT
    # the error_fail_neutral branch — grade_rank swallows its own error and returns 0).
    _enable(monkeypatch, step=0.15, max_mult=1.5)

    class _Boom:
        def __contains__(self, item):
            raise RuntimeError("boom")

        def __bool__(self):
            return True

    mult, meta = catalyst_conviction_size_multiplier("ABCD", strong_symbols=_Boom())
    assert mult == 1.0
    assert meta["grade_rank"] == 0
    assert meta.get("reason") != "error_fail_neutral"  # handled inside grade_rank
