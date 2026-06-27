"""MESO-tier: the momentum sizing-COMPOSITION pipeline contract.

This test pins the end-to-end SIZING pipeline that lives in
``live_runner._maybe_enter_live`` (the ~6660-6815 region):

    17 per-factor size multipliers
        -> _safe_mult(each)           # NaN/inf/negative -> 1.0 (fail-neutral)
        -> prod * base_max_loss
        -> _eff_max_loss = min(prod, base * 3.0)   # hard combined-mult ceiling
        -> compute_risk_first_quantity(_eff_max_loss, ...)  # risk-budget -> qty
        -> notional ceiling cap (max_notional)
        -> liquidity ($-volume) cap   (liquidity_capped_notional, applied to
                                       max_notional BEFORE the qty compute)

The contract under test is multiplier -> budget -> qty -> caps, END TO END.

We drive the REAL helpers:
  * ``_safe_mult``                     (live_runner)
  * ``compute_risk_first_quantity``    (risk_policy)
  * ``liquidity_capped_notional``      (risk_policy)
  * ``catalyst_conviction_size_multiplier`` / ``green_day_graduation_multiplier``
    / ``streak_risk_multiplier`` (risk_policy) — flag/db monkeypatched so the
    composed product is deterministic.

``_compose_eff_max_loss`` below is a VERBATIM mirror of the live ``_eff_max_loss``
product expression (live_runner.py ~6747-6750). If the source product order or
the 3x clamp ever changes, this mirror — and the asserted composed qty — should
be re-derived; the explicit numeric assertions are what catch a subtly-wrong
change.

Pure-logic + mocks; no DB fixture, no network. py_compile-clean.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_runner import _safe_mult
from app.services.trading.momentum_neural.risk_policy import (
    catalyst_conviction_size_multiplier,
    compute_risk_first_quantity,
    green_day_graduation_multiplier,
    liquidity_capped_notional,
    streak_risk_multiplier,
)


# --------------------------------------------------------------------------- #
# VERBATIM mirror of the live _eff_max_loss composition (live_runner ~6747).
# Order MUST match the source product; the 3x clamp is the hard ceiling.
# --------------------------------------------------------------------------- #
def _compose_eff_max_loss(base_max_loss: float, **factors: float) -> float:
    """Reproduce ``_eff_max_loss = min(base * prod(_safe_mult(f)), base * 3.0)``.

    All 17 factors default to 1.0 (the byte-identical OFF path). Pass only the
    factors a test wants to perturb. Each is passed through the REAL
    ``_safe_mult`` exactly as the runner does.
    """
    names = (
        "streak", "graduation", "cushion", "l2", "sched", "liq", "meta",
        "prior_day", "overnight", "fatigue", "sym_fatigue", "hot_cold",
        "time_fatigue", "halt", "dip_velocity", "catalyst", "prime_window",
    )
    prod = float(base_max_loss)
    for n in names:
        prod *= _safe_mult(factors.get(n, 1.0))
    return min(prod, float(base_max_loss) * 3.0)


# ======================================================================== #
# 1. THE 3x CLAMP: a strong-catalyst + green-day + prime stack clamps to
#    EXACTLY base * 3.0 (the combined multiplier saturates the ceiling).
# ======================================================================== #
def test_strong_stack_clamps_to_exactly_base_times_three() -> None:
    base = 50.0
    # streak hot (1.5) * graduation (2.0) * catalyst (1.45) * prime (1.5) ...
    # raw product = 1.5 * 2.0 * 1.45 * 1.5 = 6.525 >> 3.0  -> must clamp.
    eff = _compose_eff_max_loss(
        base,
        streak=1.5,
        graduation=2.0,
        catalyst=1.45,
        prime_window=1.5,
    )
    assert eff == pytest.approx(base * 3.0)  # exactly the hard ceiling
    # and NOT the raw product
    assert eff < base * 6.525


def test_stack_just_below_ceiling_is_not_clamped() -> None:
    base = 50.0
    # 1.5 * 1.9 = 2.85 < 3.0  -> the product passes through UNCLAMPED.
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=1.9)
    assert eff == pytest.approx(base * 2.85)
    assert eff < base * 3.0


def test_stack_exactly_at_ceiling_resolves_to_ceiling() -> None:
    base = 40.0
    # 1.5 * 2.0 = 3.0 exactly: min(base*3.0, base*3.0) == base*3.0.
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=2.0)
    assert eff == pytest.approx(base * 3.0)


# ======================================================================== #
# 2. CLAMPED BUDGET -> qty -> NOTIONAL ceiling binds.
#    With a fat budget the risk-first qty is large enough that the notional
#    ceiling (not the risk budget) is what bounds the final qty.
# ======================================================================== #
def test_clamped_budget_qty_binds_on_notional_ceiling() -> None:
    base = 50.0
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=2.0, catalyst=1.45)
    assert eff == pytest.approx(150.0)  # base*3.0

    entry = 10.0
    atr_pct = 0.02
    stop_mult = 0.60
    # stop_distance = entry * max(0.003, atr_pct*stop_mult) = 10 * 0.012 = 0.12
    # risk-first qty (uncapped) = 150 / 0.12 = 1250 shares -> notional 12,500
    # notional ceiling 1,000 -> qty = 1000 / 10 = 100 shares (floored to 1).
    qty, meta = compute_risk_first_quantity(
        entry_price=entry,
        atr_pct=atr_pct,
        max_loss_usd=eff,
        max_notional_ceiling_usd=1_000.0,
        base_increment=1.0,
        base_min_size=1.0,
        stop_atr_mult=stop_mult,
    )
    assert qty == pytest.approx(100.0)
    assert meta["capped_by"] == "notional_ceiling"
    assert meta["notional_usd"] == pytest.approx(1_000.0)


def test_clamped_budget_qty_binds_on_risk_when_ceiling_is_loose() -> None:
    base = 50.0
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=2.0)  # 150.0
    entry = 10.0
    # stop_distance = 0.12; risk-qty = 150/0.12 = 1250 -> notional 12,500 < 20,000 ceiling
    qty, meta = compute_risk_first_quantity(
        entry_price=entry,
        atr_pct=0.02,
        max_loss_usd=eff,
        max_notional_ceiling_usd=20_000.0,
        base_increment=1.0,
        base_min_size=1.0,
        stop_atr_mult=0.60,
    )
    assert qty == pytest.approx(1_250.0)
    assert meta["capped_by"] is None  # risk budget bound, not the ceiling
    assert meta["model"] == "risk_first"


# ======================================================================== #
# 3. LIQUIDITY ($-volume) CAP binds BEFORE the notional ceiling.
#    In the runner, max_notional is first reshaped by liquidity_capped_notional,
#    THEN handed to compute_risk_first_quantity. Whichever of {equity-notional,
#    liquidity} is smaller is the binding ceiling.
# ======================================================================== #
def test_liquidity_cap_binds_before_notional_ceiling() -> None:
    equity_cap = 10_000.0
    dollar_volume = 200_000.0  # 1% participation -> liq cap = 2,000
    capped = liquidity_capped_notional(equity_cap, dollar_volume, fraction=0.01)
    assert capped == pytest.approx(2_000.0)  # liquidity binds (< equity_cap)

    base = 50.0
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=2.0)  # 150 budget (fat)
    entry = 5.0
    # risk-qty uncapped = 150 / (5*0.012=0.06) = 2500 -> notional 12,500.
    # liquidity-capped ceiling 2,000 -> qty = 2000/5 = 400.
    qty, meta = compute_risk_first_quantity(
        entry_price=entry,
        atr_pct=0.02,
        max_loss_usd=eff,
        max_notional_ceiling_usd=capped,
        base_increment=1.0,
        base_min_size=1.0,
        stop_atr_mult=0.60,
    )
    assert qty == pytest.approx(400.0)
    assert meta["capped_by"] == "notional_ceiling"  # the (liquidity-shrunk) ceiling
    assert meta["notional_usd"] == pytest.approx(2_000.0)


def test_equity_cap_binds_when_liquidity_is_deep() -> None:
    # Deep name: 1% of $50M = $500k >> $10k equity cap -> equity cap binds (unchanged).
    capped = liquidity_capped_notional(10_000.0, 50_000_000.0, fraction=0.01)
    assert capped == pytest.approx(10_000.0)


def test_liquidity_cap_fails_open_on_missing_dollar_volume() -> None:
    # Crypto / no dvol -> fail OPEN: ceiling unchanged (the equity cap survives).
    assert liquidity_capped_notional(10_000.0, None) == pytest.approx(10_000.0)
    assert liquidity_capped_notional(10_000.0, 0.0) == pytest.approx(10_000.0)


def test_liquidity_cap_disabled_when_fraction_nonpositive() -> None:
    # fraction<=0 disables the cap (fail-open) even with real dvol.
    assert liquidity_capped_notional(10_000.0, 200_000.0, fraction=0.0) == pytest.approx(10_000.0)


# ======================================================================== #
# 4. DERATE STACK shrinks size correctly (size-down levers compose, qty drops
#    proportionally because the risk budget — not a ceiling — binds).
# ======================================================================== #
def test_derate_stack_shrinks_qty_proportionally() -> None:
    base = 100.0
    # liquidity-risk 0.5 * meta-label 0.5 * prior-day 0.8 = 0.2 product.
    eff_full = _compose_eff_max_loss(base)  # all 1.0 -> base
    eff_derated = _compose_eff_max_loss(base, liq=0.5, meta=0.5, prior_day=0.8)
    assert eff_full == pytest.approx(100.0)
    assert eff_derated == pytest.approx(20.0)  # 100 * 0.2

    entry = 8.0
    # stop_distance = 8 * 0.012 = 0.096; loose ceiling so RISK binds in both.
    qty_full, _ = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.02, max_loss_usd=eff_full,
        max_notional_ceiling_usd=1_000_000.0, base_increment=1.0,
        base_min_size=1.0, stop_atr_mult=0.60,
    )
    qty_der, _ = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.02, max_loss_usd=eff_derated,
        max_notional_ceiling_usd=1_000_000.0, base_increment=1.0,
        base_min_size=1.0, stop_atr_mult=0.60,
    )
    # full: 100/0.096 = 1041.66 -> floor 1041 ; derated: 20/0.096 = 208.33 -> 208
    assert qty_full == pytest.approx(1_041.0)
    assert qty_der == pytest.approx(208.0)
    # the derate is ~5x smaller (the 0.2 product) modulo whole-share flooring.
    assert qty_der < qty_full
    assert qty_der / qty_full == pytest.approx(0.2, abs=0.01)


def test_derate_below_min_size_rejects_with_below_min_reason() -> None:
    base = 50.0
    # crush the budget so the risk-first qty rounds below base_min_size -> 0 qty.
    eff = _compose_eff_max_loss(base, liq=0.5, meta=0.4)  # 50 * 0.2 = 10.0
    entry = 100.0
    # stop_distance = 100 * 0.012 = 1.2 ; qty = 10/1.2 = 8.33 -> floor(8.33/100)*100 = 0
    qty, meta = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=1_000_000.0, base_increment=100.0,
        base_min_size=100.0, stop_atr_mult=0.60,
    )
    assert qty == 0.0
    assert meta["reason"] == "below_min_size"


# ======================================================================== #
# 5. _safe_mult sanitization: a poisoned factor (NaN / inf / negative) must
#    fail NEUTRAL (-> 1.0), so it cannot NaN-out or sign-flip the whole budget.
# ======================================================================== #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0, -0.0001])
def test_safe_mult_poisoned_factor_fails_neutral_in_product(bad: float) -> None:
    base = 50.0
    # A poisoned catalyst factor must NOT poison the product: it -> 1.0 neutral.
    eff = _compose_eff_max_loss(base, streak=1.2, catalyst=bad)
    assert math.isfinite(eff)
    assert eff > 0.0
    assert eff == pytest.approx(base * 1.2)  # poisoned factor dropped to 1.0


def test_safe_mult_unit_neutralization() -> None:
    assert _safe_mult(float("nan")) == 1.0
    assert _safe_mult(float("inf")) == 1.0
    assert _safe_mult(-2.0) == 1.0
    assert _safe_mult(None) == 1.0
    assert _safe_mult("not a number") == 1.0
    # a valid factor passes through UNCHANGED (happy path untouched)
    assert _safe_mult(0.5) == 0.5
    assert _safe_mult(1.5) == 1.5
    assert _safe_mult(0.0) == 0.0  # a legit zero (e.g. late-window sched) survives


def test_nan_factor_would_have_killed_the_fill_without_safe_mult() -> None:
    # Demonstrate the failure mode _safe_mult prevents: a raw NaN factor makes the
    # whole product NaN, which would silently zero the fill. _safe_mult neutralizes it.
    base = 50.0
    raw_product = base * 1.2 * float("nan")
    assert math.isnan(raw_product)  # the bug _safe_mult guards against
    sanitized = _compose_eff_max_loss(base, streak=1.2, catalyst=float("nan"))
    assert not math.isnan(sanitized)  # guarded


# ======================================================================== #
# 6. REAL multiplier helpers drive the composition (flag/db monkeypatched).
#    catalyst (STRONG=rank3) + green-day graduation feed the SAME product.
# ======================================================================== #
def test_real_catalyst_strong_feeds_clamped_stack() -> None:
    with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", True), \
         patch.object(settings, "chili_momentum_catalyst_conviction_step", 0.15), \
         patch.object(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5), \
         patch(
            "app.services.trading.momentum_neural.catalyst.catalyst_grade_rank",
            return_value=3,
         ):
        cat_mult, cat_meta = catalyst_conviction_size_multiplier("STRONGCO")
    # 1.0 + 0.15*3 = 1.45 (<= max 1.5)
    assert cat_mult == pytest.approx(1.45)
    assert cat_meta["grade_rank"] == 3

    base = 50.0
    # streak 1.5 * graduation 2.0 * catalyst 1.45 = 4.35 -> clamps to 3.0.
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=2.0, catalyst=cat_mult)
    assert eff == pytest.approx(base * 3.0)


def test_real_catalyst_max_multiplier_caps_the_factor() -> None:
    # step*rank overshoots max -> the helper clamps the FACTOR at max_multiplier.
    with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", True), \
         patch.object(settings, "chili_momentum_catalyst_conviction_step", 0.5), \
         patch.object(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5), \
         patch(
            "app.services.trading.momentum_neural.catalyst.catalyst_grade_rank",
            return_value=3,
         ):
        cat_mult, _ = catalyst_conviction_size_multiplier("STRONGCO")
    # 1.0 + 0.5*3 = 2.5 -> clamped to max 1.5
    assert cat_mult == pytest.approx(1.5)


def test_real_catalyst_disabled_is_neutral() -> None:
    with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", False):
        cat_mult, meta = catalyst_conviction_size_multiplier("ANYCO")
    assert cat_mult == 1.0
    assert meta["reason"] == "disabled"


def test_real_green_day_graduation_multiplier_from_streak() -> None:
    # 4 consecutive green days, step 0.1 -> 1.0 + 0.1*(4-1) = 1.3.
    with patch.object(settings, "chili_momentum_green_day_graduation_enabled", True), \
         patch.object(settings, "chili_momentum_green_day_step_per_day", 0.1), \
         patch.object(settings, "chili_momentum_green_day_max_multiplier", 2.0), \
         patch(
            "app.services.trading.momentum_neural.risk_policy.consecutive_green_days",
            return_value=(4, {"green_usd": 400.0, "days_seen": 4}),
         ):
        grad_mult, grad_meta = green_day_graduation_multiplier(None, execution_family="robinhood_spot")
    assert grad_mult == pytest.approx(1.3)
    assert grad_meta["consecutive_green_days"] == 4

    base = 50.0
    # streak 1.5 * graduation 1.3 = 1.95 < 3.0 -> UNCLAMPED.
    eff = _compose_eff_max_loss(base, streak=1.5, graduation=grad_mult)
    assert eff == pytest.approx(base * 1.95)


def test_real_green_day_single_day_does_not_graduate() -> None:
    # streak<=1 -> 1.0 (no graduation off a single green day).
    with patch.object(settings, "chili_momentum_green_day_graduation_enabled", True), \
         patch.object(settings, "chili_momentum_green_day_step_per_day", 0.1), \
         patch.object(settings, "chili_momentum_green_day_max_multiplier", 2.0), \
         patch(
            "app.services.trading.momentum_neural.risk_policy.consecutive_green_days",
            return_value=(1, {"green_usd": 100.0, "days_seen": 1}),
         ):
        grad_mult, _ = green_day_graduation_multiplier(None, execution_family="robinhood_spot")
    assert grad_mult == pytest.approx(1.0)


# ======================================================================== #
# 7. REAL streak_risk_multiplier: cold/loss streak DERATES the budget; the
#    composed qty shrinks. Drives the helper with a fake db query result.
# ======================================================================== #
class _FakeStreakQuery:
    """Minimal SQLAlchemy-query stand-in for streak_risk_multiplier."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeStreakDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeStreakQuery(self._rows)


def test_real_streak_three_losses_floors_to_half_and_derates_qty() -> None:
    # 5 outcomes, newest-first all losses -> >=3 consec losses -> hard floor 0.5.
    rows = [(-5.0, "stop_loss")] * 5
    with patch(
        "app.services.trading.momentum_neural.outcome_labels.is_real_entry_outcome",
        return_value=True,
    ):
        streak_mult, meta = streak_risk_multiplier(_FakeStreakDb(rows), execution_family="robinhood_spot")
    assert streak_mult == pytest.approx(0.5)
    assert meta["consecutive_losses"] >= 3

    base = 100.0
    eff = _compose_eff_max_loss(base, streak=streak_mult)
    assert eff == pytest.approx(50.0)  # halved budget

    entry = 10.0
    qty, _ = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=1_000_000.0, base_increment=1.0,
        base_min_size=1.0, stop_atr_mult=0.60,
    )
    # stop_distance = 0.12; qty = 50/0.12 = 416.66 -> 416
    assert qty == pytest.approx(416.0)


def test_real_streak_hot_hand_boosts_to_one_point_five() -> None:
    # 10 outcomes all wins -> win_rate 1.0 -> clamp(0.5+1.0, 0.5, 1.5) = 1.5.
    rows = [(7.0, "target")] * 10
    with patch(
        "app.services.trading.momentum_neural.outcome_labels.is_real_entry_outcome",
        return_value=True,
    ):
        streak_mult, meta = streak_risk_multiplier(_FakeStreakDb(rows), execution_family="robinhood_spot")
    assert streak_mult == pytest.approx(1.5)
    assert meta["win_rate"] == pytest.approx(1.0)


def test_real_streak_insufficient_history_is_neutral() -> None:
    rows = [(-1.0, "stop_loss")] * 3  # < 5 real outcomes
    with patch(
        "app.services.trading.momentum_neural.outcome_labels.is_real_entry_outcome",
        return_value=True,
    ):
        streak_mult, meta = streak_risk_multiplier(_FakeStreakDb(rows), execution_family="robinhood_spot")
    assert streak_mult == pytest.approx(1.0)
    assert meta["reason"] == "insufficient_history"


# ======================================================================== #
# 8. END-TO-END: real helpers -> composed budget -> qty under BOTH caps,
#    with the liquidity cap binding first. The single integrated assertion.
# ======================================================================== #
def test_end_to_end_real_helpers_clamped_budget_liquidity_then_qty() -> None:
    # Build a STRONG stack from the real helpers.
    with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", True), \
         patch.object(settings, "chili_momentum_catalyst_conviction_step", 0.15), \
         patch.object(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5), \
         patch(
            "app.services.trading.momentum_neural.catalyst.catalyst_grade_rank",
            return_value=3,
         ):
        cat_mult, _ = catalyst_conviction_size_multiplier("RUNNER")
    with patch.object(settings, "chili_momentum_green_day_graduation_enabled", True), \
         patch.object(settings, "chili_momentum_green_day_step_per_day", 0.1), \
         patch.object(settings, "chili_momentum_green_day_max_multiplier", 2.0), \
         patch(
            "app.services.trading.momentum_neural.risk_policy.consecutive_green_days",
            return_value=(20, {"green_usd": 9_999.0, "days_seen": 20}),
         ):
        grad_mult, _ = green_day_graduation_multiplier(None, execution_family="robinhood_spot")
    assert cat_mult == pytest.approx(1.45)
    assert grad_mult == pytest.approx(2.0)  # 1.0 + 0.1*19 = 2.9 -> clamped to max 2.0

    base = 50.0
    # 1.45 * 2.0 = 2.9 < 3.0 -> NOT clamped -> budget = 145.0.
    eff = _compose_eff_max_loss(base, catalyst=cat_mult, graduation=grad_mult)
    assert eff == pytest.approx(145.0)

    # Liquidity ceiling binds: equity cap 50k, dvol 1M @ 1% -> 10k liq cap.
    capped = liquidity_capped_notional(50_000.0, 1_000_000.0, fraction=0.01)
    assert capped == pytest.approx(10_000.0)

    entry = 4.0
    # stop_distance = 4 * max(0.003, 0.02*0.6=0.012) = 4*0.012 = 0.048.
    # risk-qty uncapped = 145 / 0.048 = 3020.8 -> notional 12,083 > 10,000 cap.
    # -> qty = 10,000 / 4 = 2500 (whole shares).
    qty, meta = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=capped, base_increment=1.0,
        base_min_size=1.0, stop_atr_mult=0.60,
    )
    assert qty == pytest.approx(2_500.0)
    assert meta["capped_by"] == "notional_ceiling"  # the liquidity-shrunk ceiling
    assert meta["notional_usd"] == pytest.approx(10_000.0)
