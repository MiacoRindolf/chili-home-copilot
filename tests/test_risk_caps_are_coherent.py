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
crossover is 0.03 / 4.0 = 0.75%, BELOW the p05 stop (0.82%). Counted against the same 88
submits: the loss budget would decide 85 of them (only the 3 tightest, min stop 0.56%, still
hit the buying-power ceiling) where today 50 of 88 are `capped_by='notional_ceiling'`. The
ceiling becomes buying power / liquidity, not a size opinion. An explicit fraction remains a
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

import re
from pathlib import Path

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import (
    MEASURED_STOP_P75_PCT,
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
    crossover must sit inside the stop distribution the lane trades.

    REVIEW NOTE (2026-09-11): on the DERIVED path the crossover is loss_frac / multiplier,
    which is <= loss_frac for any multiplier >= 1, so this leg can only go red if the
    operator sets the per-trade loss fraction above the p75 stop (5.59%). That is a real
    configuration the operator could reach — 3% today, and the canon has moved before — but
    it is a WEAK guard on its own. The strong ones are
    ``test_the_account_headroom_bounds_the_second_name`` (the exposure the derived ceiling
    newly creates) and ``test_the_derived_path_still_binds_on_a_cash_account`` below."""
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


def test_the_derived_path_still_binds_on_a_cash_account():
    """The tripwire that CAN go red on the derived path. On a cash account (multiplier 1.0 —
    RH without Gold, Coinbase, the replay seam) the ceiling is the whole account, so the loss
    budget only binds from a `loss_frac` stop up. If the operator's loss fraction ever rises
    above the p75 traded stop, the ceiling decides every trade on that venue and the budget is
    decorative again — the 2026-09-09 failure, on a different venue."""
    loss, _ = _fracs()
    x_cash, meta = crossover_derived(loss, MULTIPLIERS["cash"])
    assert x_cash == pytest.approx(loss, abs=1e-6)
    assert x_cash <= STOP_PCT["p75"] + 1e-9, (
        f"cash account: the budget only binds at a {100*x_cash:.2f}% stop, above the p75 "
        f"traded stop of {100*STOP_PCT['p75']:.2f}% ({meta})"
    )
    # And it names the arithmetic no setting can fix, from the product.
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(1.0)


def test_a_named_ceiling_change_on_an_unmeasured_venue_is_stated():
    """RH / Coinbase MOVED under [27] and NOTHING was measured there. The stop distribution in
    this file is alpaca_spot only (n=88). Rather than pretend otherwise, the test states the
    size of the move and pins the derivation so a silent further change trips.

    RH Gold (2.0x) on a $12,500 account at the operator canon: derived ceiling $25,000 where
    the retired 0.15 fraction gave $3,750 — 6.67x. Coinbase (cash 1.0x) on $2,000: $2,000 vs
    $300. OPEN QUESTION in the PR body: no stop distribution has been measured on either
    venue, so the crossover there is asserted against the ALPACA p75 as the only measurement
    we have."""
    for name, mult, equity, old_frac in (
        ("robinhood_gold", 2.0, 12_500.0, 0.15),
        ("coinbase_cash", 1.0, 2_000.0, 0.15),
    ):
        loss_usd = equity * 0.03
        ceiling, meta = coherent_notional_ceiling_usd(
            equity_usd=equity, multiplier=mult, loss_usd=loss_usd,
        )
        assert ceiling == pytest.approx(equity * mult, abs=0.01), name
        assert meta["binding"] == "buying_power", name
        # The measured size of the step this PR takes on an unmeasured venue.
        assert ceiling / (equity * old_frac) == pytest.approx(mult / old_frac, abs=1e-6)
        # The crossover still sits inside the only stop distribution we have measured.
        assert meta["crossover_stop_pct"] <= STOP_PCT["p75"] + 1e-9, (name, meta)


def test_the_derived_crossover_at_the_operator_canon_sits_below_the_p05_stop():
    """3% loss on the 4.0x paper account: the budget binds from a 0.75% stop up — below the p05
    stop of 0.82%, so the ceiling decides 3 of 88 measured entries instead of 50. NOT zero: the
    tightest measured stop is 0.56% and those three still hit buying power. The receipt says
    which bound decided, on every one of them."""
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


def realized_risk_frac_at(exposure_frac: float, loss_frac: float, stop_pct: float) -> float:
    """What a position sized under a ceiling of ``exposure_frac x equity`` actually risks at
    a ``stop_pct`` stop: the stop distance times the shares the ceiling permits, capped by
    the budget. This is the whole 2026-09-09 arithmetic, in one place."""
    return min(loss_frac, exposure_frac * stop_pct)


def test_the_realized_risk_at_the_median_stop_is_stated_not_assumed():
    """What the configuration ACTUALLY risks on a typical trade, computed rather than hoped.

    REVIEW FIX (2026-09-11): the bound is unchanged, but the test now PROVES it can fire.
    On the derived path ``halt_to_zero_exposure_frac >= 1.0`` by construction (the ceiling
    is at least the account), so the assertion below can only go red at a per-trade loss
    fraction above 12% — outside anything the operator runs. A guard nobody can trip is not
    safety ([[feedback_machinery_that_cannot_fire_is_not_safety]]), so the POSITIVE CONTROL
    is asserted first: the 2026-09-09 pair this test was written for must fail the same
    bound, through the same function. If the bound is ever loosened into uselessness, that
    control goes red."""
    # POSITIVE CONTROL — the pair that caused the incident. 0.15 ceiling against the 3%
    # canon realized $50.68 of a $331.61 budget (15.3%); at the p50 stop the arithmetic is
    # 0.15 x 2.49% = 0.37% against a 3% budget = 12.5% of it.
    incident = realized_risk_frac_at(0.15, 0.03, STOP_PCT["p50"])
    assert incident < 0.2 * 0.03, "the bound no longer catches the 2026-09-09 pair"
    # ... and the lane's interim override is caught by the same bound at the p05 stop, which
    # is where the tightest-stop entries the ceiling actually decided live.
    assert realized_risk_frac_at(0.512, 0.03, STOP_PCT["p05"]) < 0.2 * 0.03

    loss, notional = _fracs()
    exposures = (
        {"override": notional}
        if notional > 0
        else {name: crossover_derived(loss, m)[1]["halt_to_zero_exposure_frac"]
              for name, m in MULTIPLIERS.items()}
    )
    for name, exposure_frac in exposures.items():
        realized = realized_risk_frac_at(exposure_frac, loss, STOP_PCT["p50"])
        assert realized > 0
        # not a threshold on the value — a guard that it is not a rounding artefact of a stale pair
        assert realized >= 0.2 * loss, (
            f"{name}: at the median {100*STOP_PCT['p50']:.2f}% stop this risks "
            f"{100*realized:.2f}% of equity against a {100*loss:.2f}% budget — under a fifth of "
            f"it. That is the 2026-09-09 failure returning."
        )


def test_a_stale_override_is_flagged_in_the_product_not_only_in_this_file(monkeypatch):
    """THE GUARD THIS FILE CANNOT BE ([27] review, 2026-09-11).

    ``_fracs()`` reads the PYTEST process's settings, and app/config.py refuses to read the
    lane `.env` when ``CHILI_PYTEST`` is set ("dapat sumukat ang suite sa code defaults") —
    deliberately. So this file sees 0.01 / 0.0 and can NEVER see the pair the lane runs. The
    lane .env right now still carries the interim 0.512 override beside the 3% canon, whose
    crossover is 5.86% — above the p75 traded stop of 5.59%. The tripwire therefore also
    lives in the product, on the value actually loaded, and REPORTS rather than refusing
    (an override is the operator's call, and a gate here would block every entry)."""
    import app.services.trading.momentum_neural.risk_policy as rp

    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 10_320.34)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)

    # The lane's own stale pair: flagged, with both numbers in the receipt.
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.512)
    _, stale = rp.equity_relative_notional_cap_with_meta(500.0, "robinhood_spot")
    assert stale["source"] == "operator_fraction_override"
    assert stale["override_crossover_above_measured_p75"] is True
    assert stale["measured_stop_p75_pct"] == pytest.approx(MEASURED_STOP_P75_PCT)
    assert stale["crossover_stop_pct"] > MEASURED_STOP_P75_PCT

    # A pair set from the CURRENT distribution is not flagged.
    monkeypatch.setattr(
        settings, "chili_momentum_risk_notional_fraction_of_equity", 0.03 / STOP_PCT["p75"],
    )
    _, fresh = rp.equity_relative_notional_cap_with_meta(500.0, "robinhood_spot")
    assert fresh["override_crossover_above_measured_p75"] is False

    # And the constant the product checks against is the p75 this file measured.
    assert MEASURED_STOP_P75_PCT == pytest.approx(STOP_PCT["p75"])


# The SAME formula, copied. Each entry is (path, line, the literal it hardcodes). Refresh
# with: git grep -n 'max(0\.003' app/services/trading/momentum_neural/
STOP_FLOOR_COPY_SITES = (
    "app/services/trading/momentum_neural/entry_gates.py",
    "app/services/trading/momentum_neural/live_runner.py",
    "app/services/trading/momentum_neural/paper_execution.py",
    "app/services/trading/momentum_neural/paper_runner.py",
    "app/services/trading/momentum_neural/replay_v2.py",
)
_STOP_FLOOR_RE = re.compile(r"max\(\s*(0\.\d+)\s*,")
# The formula is ``max(<floor>, atr_pct * stop_atr_mult)``. Only lines that carry the
# STOP-multiplier half of it are stop floors; every other `max(<float>, ... atr ...)` in
# these files is a different measurement (a vol floor, a tolerance band, a clamp).
_STOP_MULT_RE = re.compile(r"(stop_atr_mult|STOP_ATR_MULT|noise_floor|\b_?sm\b)")


def test_every_stop_floor_site_uses_this_one_value():
    """[27] review, 2026-09-11. ``RISK_FIRST_STOP_FLOOR_PCT`` named the SIZER's floor and its
    comment claimed the literal had been at "three sites" and was now "one name". It was not:
    the identical ``max(0.003, atr_pct * stop_atr_mult)`` formula — the stop actually WRITTEN
    into orders and exits — survives at ~20 further sites in paper_execution, live_runner,
    entry_gates, paper_runner and replay_v2, none of which imports this module (adding a
    settings + SQLAlchemy dependency to them for one float is worse than a pin).

    THE FAILURE THIS DEFENDS, concretely: retune this constant to 0.001 to reflect a measured
    distribution. ``compute_risk_first_quantity`` follows and the ceiling's ``loss / floor``
    bound TRIPLES; the stop the order carries does not move, because it is still reading its
    own 0.003. Two numbers set independently — the exact failure [27] exists to end. This test
    goes red at that moment and names every file that has to move with it."""
    root = Path(__file__).resolve().parents[1]
    found = 0
    offenders: list[str] = []
    for rel in STOP_FLOOR_COPY_SITES:
        src = (root / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            # ``noise_floor = max(0.003, 0.5 * (a or 0.01))`` aliases the ATR to ``a``, so the
            # name of the variable is the only tell on that line.
            if "atr" not in line.lower() and "noise_floor" not in line:
                continue
            if not _STOP_MULT_RE.search(line):
                continue
            for m in _STOP_FLOOR_RE.finditer(line):
                value = float(m.group(1))
                if value <= 0.0:            # a "never negative" clamp, not a stop floor
                    continue
                found += 1
                if value != RISK_FIRST_STOP_FLOOR_PCT:
                    offenders.append(f"{rel}:{lineno} hardcodes {value} -> {line.strip()}")
    assert found >= 15, (
        f"expected the known copies of the stop-floor formula, found {found} — the scan "
        f"stopped matching, which means this guard stopped guarding"
    )
    assert not offenders, (
        "RISK_FIRST_STOP_FLOOR_PCT = "
        f"{RISK_FIRST_STOP_FLOOR_PCT} but these order-writing sites still use their own "
        "literal. Move them together or the sizer and the stop disagree:\n  "
        + "\n  ".join(offenders)
    )


def test_no_setting_can_reach_the_budget_below_a_matching_stop():
    """The arithmetic ceiling: one position cannot risk more than `multiplier x stop_pct`,
    whatever any fraction is set to. Cash (1.0) is the old statement; margin raises the
    reachable stop by exactly the multiplier.

    REVIEW FIX (2026-09-11): this used to recompute `min(loss, mult*sp)` locally and assert
    it equalled itself — the product was never called. It now derives the ceiling from
    risk_policy for each multiplier, computes the risk the SIZER would actually take
    (`min(budget, ceiling x stop_pct)`), and asserts THAT against the loss budget."""
    loss, _ = _fracs()
    budget_usd = EQUITY_USD * loss
    for name, mult in MULTIPLIERS.items():
        ceiling, meta = coherent_notional_ceiling_usd(
            equity_usd=EQUITY_USD, multiplier=mult, loss_usd=budget_usd,
        )
        assert ceiling > 0, meta
        for sp_name, sp in STOP_PCT.items():
            # Risk actually taken = stop distance x the shares the ceiling permits.
            realized_usd = min(budget_usd, ceiling * sp)
            realized_frac = realized_usd / EQUITY_USD
            assert realized_frac <= loss + 1e-9
            if mult * sp < loss:
                assert realized_frac == pytest.approx(mult * sp, abs=1e-6), (
                    f"{name} x{mult} @ {sp_name}: a {100*sp:.2f}% stop cannot risk "
                    f"{100*loss:.1f}% — the reachable maximum is {100*mult*sp:.2f}%"
                )
            else:
                assert realized_frac == pytest.approx(loss, abs=1e-6)
    # and the derived ceiling agrees: on a cash account the exposure is 1.0x equity, so the
    # risk at a stop is the stop itself.
    _, meta = crossover_derived(loss, 1.0)
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(1.0)


def test_the_halt_to_zero_tail_is_reported_not_hidden():
    """The tail the operator owns: at 3% / 4.0x, the notional the budget asks for at the p50 stop
    is 1.20x equity in one name, at p05 3.66x — inside the 4.0x ceiling, which the receipt
    carries as halt_to_zero_exposure_frac. Report only; no gate.

    REVIEW FIX (2026-09-11): the first version of this test asserted
    ``0.03 / STOP_PCT["p50"] == approx(1.2048)`` — both sides literals defined in this file,
    with the product untouched. It would have passed forever whatever risk_policy did. It now
    asks the PRODUCT what notional the budget buys at each measured stop, and checks that the
    reported tail bounds it."""
    ceiling, meta = coherent_notional_ceiling_usd(
        equity_usd=EQUITY_USD, multiplier=4.0, loss_usd=EQUITY_USD * 0.03,
    )
    reported_tail = meta["halt_to_zero_exposure_frac"]
    for name, sp in STOP_PCT.items():
        # What risk-first sizing ASKS for at this stop, from the product's own loss budget.
        wanted_notional = float(meta["loss_usd"]) / sp
        taken = min(wanted_notional, ceiling)          # the ceiling is the only thing that cuts
        assert taken / EQUITY_USD <= reported_tail + 1e-9, (
            f"{name}: a {100*sp:.2f}% stop takes {taken/EQUITY_USD:.2f}x equity in one name "
            f"but the receipt reports a {reported_tail:.2f}x tail"
        )
    # The two the PR body quotes, computed from the product rather than restated.
    assert float(meta["loss_usd"]) / STOP_PCT["p50"] / EQUITY_USD == pytest.approx(1.2048, abs=1e-3)
    assert float(meta["loss_usd"]) / STOP_PCT["p05"] / EQUITY_USD == pytest.approx(3.6585, abs=1e-3)
    assert reported_tail == pytest.approx(4.0)


def test_the_account_headroom_bounds_the_second_name(monkeypatch):
    """THE BLOCKING REVIEW FINDING, as a tripwire. The derived ceiling is the account's WHOLE
    buying power; frozen once and enforced per-trade, it let a second name pass the same
    ceiling seconds later. The measurement this defends: the notional a name may take, PLUS
    what the account already carries, never exceeds buying power."""
    bp = EQUITY_USD * 4.0
    committed = 0.0
    taken = []
    for _ in range(4):
        ceiling, meta = coherent_notional_ceiling_usd(
            equity_usd=EQUITY_USD, multiplier=4.0, loss_usd=EQUITY_USD * 0.03,
            committed_notional_usd=committed,
        )
        # Each name sizes risk-first at the measured p05 stop (the tightest we trade) and is
        # cut by whatever the ceiling allows.
        want = (EQUITY_USD * 0.03) / STOP_PCT["p05"]
        got = min(want, ceiling)
        taken.append(got)
        committed += got
        assert committed <= bp + 0.01, f"{committed:.2f} committed against {bp:.2f} buying power"
    # And it is a MECHANISM, not a gate: the later names are sized down, not refused, until
    # the account is genuinely full.
    assert taken[0] > 0 and taken[1] > 0
    assert taken[0] >= taken[1] >= taken[2] >= taken[3] >= 0.0


def test_the_daily_cap_is_not_smaller_than_one_full_size_loss():
    """A per-trade cap above the daily cap means the FIRST full-size loss trips the day. Under
    the derived ceiling the budget is reachable on any stop above the crossover, so the worst
    single loss IS the budget.

    REVIEW FIX (2026-09-11): this degraded to ``pytest.skip`` on exactly the condition it
    exists to catch — a guard that cannot fire is not safety
    ([[feedback_machinery_that_cannot_fire_is_not_safety]]). It now FAILS, and names the two
    numbers plus the two ways out."""
    loss, notional = _fracs()
    daily = float(settings.chili_momentum_risk_daily_loss_fraction_of_equity)
    worst_single = min(loss, notional * STOP_PCT["p75"]) if notional > 0 else loss
    assert not (daily > 0 and worst_single > daily), (
        f"one full-size loss risks {100*worst_single:.2f}% of equity while the daily breaker "
        f"is {100*daily:.2f}% — the FIRST such loss ends the day. Raise "
        f"chili_momentum_risk_daily_loss_fraction_of_equity above {100*worst_single:.2f}%, or "
        f"lower chili_momentum_risk_loss_fraction_of_equity below {100*daily:.2f}%."
    )


def test_both_fractions_are_readable_and_bounded():
    """Fail loudly rather than sizing against a missing or absurd value. 0 for the notional
    fraction is the DERIVED default, not an absence."""
    loss, notional = _fracs()
    assert 0.0 < loss <= 1.0
    assert 0.0 <= notional <= 1.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
