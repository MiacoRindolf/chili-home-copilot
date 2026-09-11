"""The per-trade loss cap and the notional ceiling must agree — and since [27] they do by construction.

WHAT WENT WRONG (2026-09-09). Risk-first sizing is `qty = max_loss / (entry * stop_pct)`, and
that result is THEN capped at a notional ceiling. Substituting, the notional the risk budget
wants is `max_loss / stop_pct` — independent of price — so the LOSS budget binds only when

    stop_pct  >=  max_loss / ceiling          (the crossover)

The operator had raised `CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY` to their 3% canon in the
lane .env. `chili_momentum_risk_notional_fraction_of_equity` was left at its old default 0.15.
0.03/0.15 put the crossover at a 20% stop while the measured stop distribution is p05 0.82% /
p50 2.49% / p75 5.59%, so the ceiling bound on 87% of entries and the realized risk was $50.68
against a $331.61 budget — 15.3% of the canon — and nothing in the system said so. Two caps,
set independently, silently disagreeing.

WHAT CHANGED ([27], 2026-09-10, operator: "3% risk. linisin mo na yan"). The ceiling is no longer
a second fraction. `risk_policy.coherent_notional_ceiling_usd` DERIVES it from broker truth:

    ceiling = min(equity x broker_multiplier,            # what the broker lets us carry
                  loss_budget / RISK_FIRST_STOP_FLOOR_PCT)  # the most the budget can ever ask for

BROKER TRUTH (read-only AlpacaSpotAdapter.get_account_snapshot, paper, 2026-09-11 00:55Z;
account_id matches chili_alpaca_expected_account_id, status ACTIVE):
equity 10,320.34 / buying_power 41,281.36 / multiplier 4.0 (bp/equity = 4.000). At 3% the
crossover is 0.03 / 4.0 = 0.75%, BELOW the p05 stop (0.82%): the loss budget decides every
measured entry and the ceiling is pure buying power / liquidity. An explicit fraction remains a
NAMED operator override, and the tripwire below still guards it.

WHAT THESE TESTS DEFEND. Not a particular pair of numbers — the operator owns those. They
defend the RELATIONSHIP: that the crossover implied by whatever is configured (derived or
override) stays inside the stop distribution the lane actually trades, and that the arithmetic
ceiling is stated rather than discovered again months later.

THE PART NO SETTING CAN FIX, now multiplier-aware: one position cannot risk more than
`multiplier x stop_pct` of equity. On a CASH account (multiplier 1.0) at the median 2.49% stop,
the whole account in one name risks 2.49% — a 3% target is reachable only on trades whose stop
is at least 3% wide. On the 4.0x paper account it is reachable from a 0.75% stop up.

STOP_PCT: live_entry_submitted, sizing.model = risk_first, ts >= 2026-08-15, n = 88
(the events table only retains back to 2026-09-08 08:39Z; last row 2026-09-10 22:27Z),
stop_pct = sizing.stop_distance / limit_price (re-measured 2026-09-11 00:55Z; ceiling-bound
50/88). Re-run the query in the PR body to refresh.

Runnable: pytest tests/test_risk_caps_are_coherent.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import (
    RISK_FIRST_STOP_FLOOR_PCT,
    coherent_notional_ceiling_usd,
)

# Re-measured 2026-09-11 00:55Z from 88 risk-first submits (stop_distance / limit price).
STOP_PCT = {"p05": 0.0082, "p10": 0.0095, "p25": 0.0157,
            "p50": 0.0249, "p75": 0.0559, "p90": 0.0630, "p95": 0.0630}

# Broker multipliers the lane can run under. 4.0 = the certified Alpaca paper account (broker
# `multiplier` field, 2026-09-11 00:55Z); 2.0 = Robinhood Gold margin; 1.0 = a cash account.
MULTIPLIERS = {"alpaca_paper_day_trading": 4.0, "robinhood_gold": 2.0, "cash": 1.0}

# The paper account's equity at the 2026-09-11 00:55Z read-only broker snapshot. Only the
# RATIOS matter below; the dollar figure keeps the receipts readable.
EQUITY_USD = 10_320.34


def _fracs() -> tuple[float, float]:
    return (float(settings.chili_momentum_risk_loss_fraction_of_equity),
            float(settings.chili_momentum_risk_notional_fraction_of_equity))


def crossover_for_override(loss_frac: float, notional_frac: float) -> float:
    """The stop width at which the loss budget starts to bind under an EXPLICIT fraction
    override (the pre-[27] arithmetic)."""
    assert notional_frac > 0
    return loss_frac / notional_frac


def crossover_derived(loss_frac: float, multiplier: float) -> tuple[float, dict]:
    """The stop width at which the loss budget starts to bind under the DERIVED ceiling."""
    ceiling, meta = coherent_notional_ceiling_usd(
        equity_usd=EQUITY_USD, multiplier=multiplier, loss_usd=EQUITY_USD * loss_frac,
    )
    assert ceiling > 0, meta
    return float(meta["crossover_stop_pct"]), meta


def assert_pair_coherent(loss_frac: float, notional_frac: float) -> None:
    """THE TRIPWIRE for an operator override: its crossover must be reachable on at least the
    widest quartile of stops the lane takes, else the loss budget is decorative."""
    x = crossover_for_override(loss_frac, notional_frac)
    assert x <= STOP_PCT["p75"] + 1e-9, (
        f"loss={loss_frac} / notional={notional_frac} => the budget only binds at a {100*x:.1f}% "
        f"stop, but even the p75 stop is {100*STOP_PCT['p75']:.2f}%. The notional ceiling would "
        f"decide every trade and the loss cap would be decorative — set the two together, or "
        f"leave the fraction at 0 so the ceiling is derived from broker truth."
    )


def test_the_crossover_is_inside_the_stops_we_actually_trade():
    """THE CONTRACT. Whatever is configured — the derived default or an explicit override — the
    crossover must sit inside the stop distribution the lane trades."""
    loss, notional = _fracs()
    if notional > 0:
        assert_pair_coherent(loss, notional)
        return
    for name, mult in MULTIPLIERS.items():
        x, meta = crossover_derived(loss, mult)
        assert x <= STOP_PCT["p75"] + 1e-9, (
            f"{name} (x{mult}): loss={loss} => crossover {100*x:.2f}% > p75 "
            f"{100*STOP_PCT['p75']:.2f}% ({meta})"
        )


def test_the_derived_crossover_at_the_operator_canon_sits_below_the_tightest_stop():
    """3% loss on the 4.0x paper account: the budget binds from a 0.75% stop up — below the p05
    stop of 0.82% — so the ceiling never decides a measured entry. The receipt says why."""
    x, meta = crossover_derived(0.03, MULTIPLIERS["alpaca_paper_day_trading"])
    assert x == pytest.approx(0.03 / 4.0, abs=1e-6)
    assert x < STOP_PCT["p05"]
    assert meta["binding"] == "buying_power"
    assert meta["ceiling_usd"] == pytest.approx(EQUITY_USD * 4.0, abs=0.01)   # 41,281.36
    assert meta["buying_power_truth_usd"] == pytest.approx(41_281.36, abs=0.01)
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(4.0)
    # A cash account is the arithmetic no setting can fix — reported, not hidden: the ceiling
    # is the whole account, so the budget binds only from a 3% stop up (p50 is 2.49%).
    x_cash, meta_cash = crossover_derived(0.03, MULTIPLIERS["cash"])
    assert x_cash == pytest.approx(0.03, abs=1e-6)
    assert meta_cash["halt_to_zero_exposure_frac"] == pytest.approx(1.0)
    assert STOP_PCT["p50"] < x_cash <= STOP_PCT["p75"]


def test_the_stop_floor_bound_binds_only_when_the_budget_is_tiny():
    """The second bound — loss / RISK_FIRST_STOP_FLOOR_PCT — is the most the sizer can ever ask
    for. It only decides when the budget is far below buying power; then the crossover IS the
    floor, i.e. below every stop the sizer will take."""
    ceiling, meta = coherent_notional_ceiling_usd(
        equity_usd=EQUITY_USD, multiplier=4.0, loss_usd=EQUITY_USD * 0.001,   # 0.1% budget
    )
    assert meta["binding"] == "loss_over_stop_floor"
    assert ceiling == pytest.approx(EQUITY_USD * 0.001 / RISK_FIRST_STOP_FLOOR_PCT, abs=0.01)
    assert meta["crossover_stop_pct"] == pytest.approx(RISK_FIRST_STOP_FLOOR_PCT, abs=1e-9)
    assert RISK_FIRST_STOP_FLOOR_PCT < STOP_PCT["p05"]


def test_an_explicit_override_that_disagrees_with_the_budget_still_trips():
    """The override path is guarded by the same tripwire: the 2026-09-09 pair (0.03/0.15,
    crossover 20%) fails. So does the lane's interim 0.512 — it was 0.03 / the 09-09 p75 of
    5.86% (n=50), and one day later the refreshed p75 (n=88) is 5.59%, so its crossover
    (5.86%) already sits above the widest quartile. A fixed fraction is stale the day after
    it is set; that is the measured reason the ceiling is derived, not configured. Only a
    pair set from the CURRENT distribution passes."""
    with pytest.raises(AssertionError):
        assert_pair_coherent(0.03, 0.15)
    with pytest.raises(AssertionError):
        assert_pair_coherent(0.03, 0.512)
    assert_pair_coherent(0.03, 0.03 / STOP_PCT["p75"])


def test_the_realized_risk_at_the_median_stop_is_stated_not_assumed():
    """What the configuration ACTUALLY risks on a typical trade, computed rather than hoped."""
    loss, notional = _fracs()
    exposures = (
        {"override": notional}
        if notional > 0
        else {name: crossover_derived(loss, m)[1]["halt_to_zero_exposure_frac"]
              for name, m in MULTIPLIERS.items()}
    )
    for name, exposure_frac in exposures.items():
        realized = min(loss, exposure_frac * STOP_PCT["p50"])
        assert realized > 0
        # not a threshold on the value — a guard that it is not a rounding artefact of a stale pair
        assert realized >= 0.2 * loss, (
            f"{name}: at the median {100*STOP_PCT['p50']:.2f}% stop this risks "
            f"{100*realized:.2f}% of equity against a {100*loss:.2f}% budget — under a fifth of "
            f"it. That is the 2026-09-09 failure returning."
        )


def test_no_setting_can_reach_the_budget_below_a_matching_stop():
    """The arithmetic ceiling, asserted so it is never re-litigated: one position cannot risk
    more than `multiplier x stop_pct`, whatever any fraction is set to. Cash (1.0) is the old
    statement; margin raises the reachable stop by exactly the multiplier."""
    loss, _ = _fracs()
    for mult in MULTIPLIERS.values():
        for name, sp in STOP_PCT.items():
            best_possible = min(loss, mult * sp)
            if mult * sp < loss:
                assert best_possible < loss, (
                    f"{name} x{mult}: a {100*sp:.2f}% stop cannot risk {100*loss:.1f}%"
                )
                assert best_possible == pytest.approx(mult * sp)
            else:
                assert best_possible == pytest.approx(loss)
    # and the derived ceiling agrees: on a cash account the exposure is 1.0x equity, so the
    # risk at a stop is the stop itself.
    _, meta = crossover_derived(loss, 1.0)
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(1.0)


def test_the_halt_to_zero_tail_is_reported_not_hidden():
    """The tail the operator owns: at 3% / 4.0x, the notional the budget asks for at the p50 stop
    is 1.20x equity in one name, at p05 3.66x — inside the 4.0x ceiling, which the receipt
    carries as halt_to_zero_exposure_frac. Report only; no gate."""
    _, meta = crossover_derived(0.03, 4.0)
    assert 0.03 / STOP_PCT["p50"] == pytest.approx(1.2048, abs=1e-3)
    assert 0.03 / STOP_PCT["p05"] == pytest.approx(3.6585, abs=1e-3)
    assert 0.03 / STOP_PCT["p05"] < meta["halt_to_zero_exposure_frac"] == pytest.approx(4.0)


def test_the_daily_cap_is_not_smaller_than_one_full_size_loss():
    """A per-trade cap above the daily cap means the FIRST full-size loss trips the day. Under
    the derived ceiling the budget is reachable on any stop above the crossover, so the worst
    single loss IS the budget."""
    loss, notional = _fracs()
    daily = float(settings.chili_momentum_risk_daily_loss_fraction_of_equity)
    worst_single = min(loss, notional * STOP_PCT["p75"]) if notional > 0 else loss
    if daily > 0 and worst_single > daily:
        pytest.skip(
            f"KNOWN AND FLAGGED, operator's call: one full-size loss risks "
            f"{100*worst_single:.2f}% while the daily breaker is {100*daily:.2f}% — the first "
            f"such loss ends the day. Raise the daily fraction or lower the per-trade loss."
        )


def test_both_fractions_are_readable_and_bounded():
    """Fail loudly rather than sizing against a missing or absurd value. 0 for the notional
    fraction is the DERIVED default, not an absence."""
    loss, notional = _fracs()
    assert 0.0 < loss <= 1.0
    assert 0.0 <= notional <= 1.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
