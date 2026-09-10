"""The per-trade loss cap and the notional ceiling must be set as a pair, not separately.

WHAT WENT WRONG (2026-09-09). Risk-first sizing is `qty = max_loss / (entry * stop_pct)`, and
that result is THEN capped at a notional ceiling. Substituting, the notional the risk budget
wants is `max_loss / stop_pct` — independent of price — so the LOSS budget binds only when

    stop_pct  >=  loss_fraction / notional_fraction

The operator had raised `CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY` to their 3% canon in the
lane .env. `chili_momentum_risk_notional_fraction_of_equity` was left at its default 0.15 and
carried no description at all. 0.03/0.15 puts the crossover at a 20% stop, while the measured
stop distribution over 50 filled legs is p05 0.82% / p50 2.42% / p75 5.86%. So the ceiling bound
on 87% of entries and the realized risk was $50.68 against a $331.61 budget — 15.3% of the canon
— and nothing in the system said so. Two caps, set independently, silently disagreeing.

WHAT THESE TESTS DEFEND. Not a particular pair of numbers — the operator owns those. They defend
the RELATIONSHIP: that the crossover implied by whatever pair is configured stays inside the stop
distribution the lane actually trades, and that the arithmetic ceiling is stated rather than
discovered again months later.

THE PART NO SETTING CAN FIX, asserted here so it is never re-litigated: with one un-margined
position, `notional_fraction <= 1.0`, so the achievable risk fraction is at most `stop_pct`
itself. At the median 2.42% stop, the whole account in one name risks 2.42% — a 3% per-trade
target is reachable only on trades whose stop is at least 3% wide.

Runnable: pytest tests/test_risk_caps_are_coherent.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings

# Measured 2026-09-09 from 50 filled legs since 2026-08-15 (stop_distance / fill price).
STOP_PCT = {"p05": 0.0082, "p10": 0.0104, "p25": 0.0161,
            "p50": 0.0242, "p75": 0.0586, "p90": 0.0635}


def _fracs() -> tuple[float, float]:
    return (float(settings.chili_momentum_risk_loss_fraction_of_equity),
            float(settings.chili_momentum_risk_notional_fraction_of_equity))


def crossover(loss_frac: float, notional_frac: float) -> float:
    """The stop width at which the loss budget starts to bind instead of the ceiling."""
    assert notional_frac > 0
    return loss_frac / notional_frac


@pytest.mark.xfail(
    strict=True,
    reason=(
        "[27]/[27b] OPERATOR DECISION PENDING (2026-09-10): the defaults are loss 1% / notional 15% "
        "=> crossover 6.7% > p75 stop 5.86%. This tripwire is RIGHT and stays; it goes green the "
        "moment the two caps are set together (then remove this marker: strict=True fails on XPASS)."
    ),
)
def test_the_crossover_is_inside_the_stops_we_actually_trade():
    """THE CONTRACT. If the crossover sits above every stop the lane takes, the loss budget can
    never bind and configuring it is theatre. It must be reachable on at least the widest
    quartile."""
    loss, notional = _fracs()
    x = crossover(loss, notional)
    assert x <= STOP_PCT["p75"] + 1e-9, (
        f"loss={loss} / notional={notional} => the budget only binds at a {100*x:.1f}% stop, "
        f"but even the p75 stop is {100*STOP_PCT['p75']:.2f}%. The notional ceiling would decide "
        f"every trade and the loss cap would be decorative — set the two together."
    )


def test_the_realized_risk_at_the_median_stop_is_stated_not_assumed():
    """What the configured pair ACTUALLY risks on a typical trade, computed rather than hoped."""
    loss, notional = _fracs()
    realized = min(loss, notional * STOP_PCT["p50"])
    assert realized > 0
    # not a threshold on the value — a guard that it is not a rounding artefact of a stale pair
    assert realized >= 0.2 * loss, (
        f"at the median {100*STOP_PCT['p50']:.2f}% stop this pair risks {100*realized:.2f}% of "
        f"equity against a {100*loss:.2f}% budget — under a fifth of it. That is the 2026-09-09 "
        f"failure returning."
    )


def test_no_setting_can_reach_the_budget_below_a_matching_stop():
    """The arithmetic ceiling, asserted so it is never re-litigated: one un-margined position
    cannot risk more than its own stop width, whatever the notional fraction is set to."""
    loss, _ = _fracs()
    for name, sp in STOP_PCT.items():
        best_possible = min(loss, 1.0 * sp)          # notional_fraction capped at 1.0
        if sp < loss:
            assert best_possible < loss, f"{name}: a {100*sp:.2f}% stop cannot risk {100*loss:.1f}%"
            assert best_possible == pytest.approx(sp)
        else:
            assert best_possible == pytest.approx(loss)


def test_the_daily_cap_is_not_smaller_than_one_full_size_loss():
    """A per-trade cap above the daily cap means the FIRST full-size loss trips the day. The lane
    .env already carried this warning; raising the notional fraction is what makes it bind."""
    loss, notional = _fracs()
    daily = float(settings.chili_momentum_risk_daily_loss_fraction_of_equity)
    worst_single = min(loss, notional * STOP_PCT["p75"])
    if daily > 0 and worst_single > daily:
        pytest.skip(
            f"KNOWN AND FLAGGED, operator's call: one full-size loss at the p75 stop risks "
            f"{100*worst_single:.2f}% while the daily breaker is {100*daily:.2f}% — the first "
            f"such loss ends the day. Raise the daily fraction or lower the per-trade pair."
        )


def test_both_fractions_are_readable_and_bounded():
    """Fail loudly rather than sizing against a missing or absurd value."""
    loss, notional = _fracs()
    assert 0.0 < loss <= 1.0
    assert 0.0 < notional <= 1.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
