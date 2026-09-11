"""[27] REVIEW FIXES (2026-09-11) — the account's notional headroom, and the two measurement
harnesses the derived ceiling silently changed.

The per-trade notional ceiling is now DERIVED from broker truth, and on the paper account
that is the account's ENTIRE buying power ($41,281 on $10,320 equity). Frozen once at
admission and enforced independently at the primary entry and at each of the four add sites,
nothing subtracted what the account already carried — two names admitted 30 s apart each
passed the same full-buying-power ceiling. ``aggregate_open_notional_usd`` is the NOTIONAL
twin of ``aggregate_open_risk_usd`` that makes the headroom measurable; the pure arithmetic
lives in ``risk_policy.coherent_notional_ceiling_usd`` / ``account_headroom_capped_ceiling``
and is tested in tests/test_equity_relative_notional.py.

The two harnesses:
  * ``replay_v2`` capped each trade at ``basis x (getattr(..., 0.15) or 0.15)``. [27] flipped
    that setting's default to 0.0 and ``0.0 or 0.15`` evaluates to 0.15, so the ``or`` idiom
    PINNED replay to the retired fraction while live moved to equity x broker multiplier —
    a 26.7x divergence in the instrument used to justify sizing changes, with the file's own
    "REPLAY->LIVE SIZING PARITY" comment still asserting the opposite.
  * ``counterfactual_replay``'s A-grade cash-fraction model read the same setting for its
    default fraction, and its branch requires ``fraction > 0`` — so 0.0 silently DISABLED the
    model while ``confidence_reasons`` still printed
    ``counterfactual_a_grade_cash_fraction_sizing:0.0``.

Runnable: pytest tests/test_notional_ceiling_account_headroom.py -v
"""
from __future__ import annotations

import pytest

from app import models
from app.config import settings
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import counterfactual_replay as cf
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.risk_evaluator import aggregate_open_notional_usd

EQUITY = 10_320.34
BUYING_POWER = 41_281.36


def _user_and_variant(db, name):
    u = models.User(name=name)
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="hr", variant_key=f"hr_{name}", label="hr", params_json={})
    db.add(v)
    db.flush()
    return u, v


def _sess(db, u, v, symbol, *, state, le, caps=None, execution_family="alpaca_spot"):
    snap = {"momentum_live_execution": le}
    if caps is not None:
        snap["momentum_policy_caps"] = caps
    s = TradingAutomationSession(
        user_id=u.id, symbol=symbol, mode="live", variant_id=v.id, state=state,
        execution_family=execution_family, risk_snapshot_json=snap,
    )
    db.add(s)
    db.flush()
    return s


# ── the account ledger ───────────────────────────────────────────────────────


def test_open_notional_counts_held_positions_at_entry_basis(db) -> None:
    u, v = _user_and_variant(db, "held")
    _sess(db, u, v, "AAA", state="live_entered",
          le={"position": {"quantity": 1000.0, "avg_entry_price": 4.0, "stop_price": 3.9}})
    _sess(db, u, v, "BBB", state="live_trailing",
          le={"position": {"quantity": 500.0, "avg_entry_price": 12.0, "stop_price": 11.0}})
    total, meta = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert total == pytest.approx(4_000.0 + 6_000.0)
    assert meta["held"] == 2 and meta["inflight"] == 0 and meta["unreadable"] == []


def test_open_notional_counts_in_flight_entries_the_held_sum_cannot_see(db) -> None:
    """The burst case: a sibling submitted seconds ago holds no position yet, but its order
    can fill at any instant. Held-only would let the next name pass the same ceiling."""
    u, v = _user_and_variant(db, "inflight")
    _sess(db, u, v, "CCC", state="live_pending_entry",
          le={"entry_submitted": True, "entry_inflight_notional_usd": 7_500.0})
    total, meta = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert total == pytest.approx(7_500.0)
    assert meta["inflight"] == 1


def test_open_notional_over_charges_rather_than_reporting_zero(db) -> None:
    """A sibling with no persisted notional (pre-submit race / older image) is charged its OWN
    frozen per-trade ceiling — the most it could possibly have submitted. Over-charging sizes
    the next entry DOWN, which is the safe side; under-charging is the defect."""
    u, v = _user_and_variant(db, "unreadable")
    s = _sess(db, u, v, "DDD", state="live_pending_entry",
              le={"entry_submitted": True},
              caps={"max_notional_per_trade_usd": 2_500.0})
    total, meta = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert total == pytest.approx(2_500.0)
    assert meta["unreadable"] == [s.id]


def test_open_notional_ignores_pending_rows_that_never_reached_the_broker(db) -> None:
    u, v = _user_and_variant(db, "pretransport")
    _sess(db, u, v, "EEE", state="live_pending_entry", le={})
    total, meta = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert total == 0.0 and meta["inflight"] == 0


def test_open_notional_excludes_the_submitter_and_never_raises(db) -> None:
    u, v = _user_and_variant(db, "exclude")
    mine = _sess(db, u, v, "FFF", state="live_entered",
                 le={"position": {"quantity": 100.0, "avg_entry_price": 5.0}})
    total, _ = aggregate_open_notional_usd(
        db, user_id=u.id, execution_family="alpaca_spot", exclude_session_id=mine.id,
    )
    assert total == 0.0
    # A malformed snapshot is a sizing input, not a gate: it must not raise.
    _sess(db, u, v, "GGG", state="live_entered", le={"position": {"quantity": "x"}})
    total2, meta2 = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert total2 >= 0.0 and isinstance(meta2["unreadable"], list)


def test_the_two_name_burst_fits_the_account(db) -> None:
    """END TO END on the reviewer's numbers. Symbol A takes $37,757 at the p05 stop. Symbol B
    admits 30 s later: with the ledger wired in, its ceiling is the REMAINDER, not the whole
    $41,281 again."""
    u, v = _user_and_variant(db, "burst")
    _sess(db, u, v, "AAA", state="live_entered",
          le={"position": {"quantity": 10_000.0, "avg_entry_price": 3.7757}})
    committed, _ = aggregate_open_notional_usd(db, user_id=u.id, execution_family="alpaca_spot")
    assert committed == pytest.approx(37_757.0, abs=0.01)
    ceiling, meta = rp.coherent_notional_ceiling_usd(
        equity_usd=EQUITY, multiplier=4.0, loss_usd=EQUITY * 0.03,
        committed_notional_usd=committed,
    )
    assert meta["binding"] == "buying_power_headroom"
    assert committed + ceiling <= BUYING_POWER + 0.01


# ── replay_v2: the `or 0.15` idiom ───────────────────────────────────────────


def test_replay_v2_derives_its_ceiling_with_the_same_function_live_uses(monkeypatch) -> None:
    """PARITY (CLAUDE.md: dual code paths). The retired expression and the derivation, fed the
    SAME basis, must no longer be able to disagree by 27x without anyone noticing."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    basis = EQUITY
    loss = basis * 0.03

    # What the retired line computed, verbatim — `0.0 or 0.15` is 0.15.
    retired = basis * float(
        getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15) or 0.15
    )
    assert retired == pytest.approx(basis * 0.15)          # the bug, reproduced

    derived, meta = rp.coherent_notional_ceiling_usd(
        equity_usd=basis, multiplier=1.0, loss_usd=loss,
    )
    assert derived == pytest.approx(basis)                 # cash assumption, the whole account
    assert meta["binding"] == "buying_power"
    assert derived / retired == pytest.approx(1 / 0.15, rel=1e-6)   # 6.67x here, 26.7x live

    # And the source line no longer contains the `or <literal>` idiom at all.
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "trading" / "momentum_neural" / "replay_v2.py"
    ).read_text(encoding="utf-8")
    assert "chili_momentum_risk_notional_fraction_of_equity" not in src, (
        "replay_v2 must derive the ceiling, not read the retired fraction"
    )
    assert "coherent_notional_ceiling_usd" in src


# ── counterfactual_replay: the silently disabled A-grade model ───────────────


def test_counterfactual_cash_fraction_is_never_the_disabling_zero(monkeypatch) -> None:
    """``_simulate_candidate_trade`` requires ``fraction > 0`` for the
    ``a_grade_cash_fraction_notional`` model. With the live default now 0.0, reading the
    setting silently dropped every ``--cash-usd`` run back to risk-first sizing while the
    receipt still claimed the model had run."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    assert cf.A_GRADE_CASH_FRACTION_DEFAULT > 0.0
    # the branch condition the model is gated on, applied to the value the module would pick
    chosen = (
        float(settings.chili_momentum_risk_notional_fraction_of_equity or 0.0)
        or cf.A_GRADE_CASH_FRACTION_DEFAULT
    )
    assert chosen > 0.0

    # An operator override (> 0) still wins over the named default.
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.512)
    chosen2 = (
        float(settings.chili_momentum_risk_notional_fraction_of_equity or 0.0)
        or cf.A_GRADE_CASH_FRACTION_DEFAULT
    )
    assert chosen2 == pytest.approx(0.512)


def test_counterfactual_cash_fraction_default_is_named_in_source() -> None:
    """No hidden fallback: the value the counterfactual falls back to is a NAMED module
    constant with the reason beside it, not a literal inside a getattr default."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "trading" / "momentum_neural" / "counterfactual_replay.py"
    ).read_text(encoding="utf-8")
    assert "A_GRADE_CASH_FRACTION_DEFAULT = 0.15" in src
    assert 'getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)' not in src


def test_counterfactual_ceiling_uses_the_runs_own_risk_budget(monkeypatch) -> None:
    """D4's guard, restated against the derived ceiling. Under the ``replay_account_equity``
    seam the cap is ``min(injected equity x 1.0, run risk / RISK_FIRST_STOP_FLOOR_PCT)``, so
    a $13k mission account can never size a $15.4k leg (D4's measured failure) — and the
    loss bound is the run's OWN ``--risk-usd``, not the settings per-trade fixed cap, which
    has nothing to do with the run."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)
    with rp.replay_account_equity(lambda *_a, **_k: 13_000.0):
        usd, meta = rp.equity_relative_notional_cap_with_meta(
            130_000.0, loss_fixed_fallback_usd=25.0,
        )
    assert usd == pytest.approx(13_000.0)          # the account, never $15.4k
    assert meta["source"] == "replay_equity_seam"
    assert meta["binding"] == "buying_power"
    assert meta["multiplier"] == pytest.approx(1.0)

    # A tiny run risk makes the LOSS bound the binding leg, and it is reported as such.
    with rp.replay_account_equity(lambda *_a, **_k: 13_000.0):
        monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.0)
        usd2, meta2 = rp.equity_relative_notional_cap_with_meta(
            130_000.0, loss_fixed_fallback_usd=25.0,
        )
    assert meta2["binding"] == "loss_over_stop_floor"
    assert usd2 == pytest.approx(25.0 / rp.RISK_FIRST_STOP_FLOOR_PCT, abs=0.01)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
