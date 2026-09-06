"""The first-target partial is a STRATEGY decision, not a venue decision (2026-09-06).

Operator rule: no per-broker strategy. Anything Robinhood can do must be doable on Alpaca;
the venue layer may differ in transport, never in what the strategy decides.

MEASURED (clean gate-15 baseline, 69 symbol-days replayed in both families,
scratchpad/broker_shape_cost.py): the Alpaca lane took 2 partial fills against Robinhood's
341, because `scaling` was gated on `normalize_execution_family(...) not in
ALPACA_EXECUTION_FAMILIES` — the same first-target touch flattened the WHOLE position on
Alpaca while Robinhood sold the tranche, banked it, moved the balance to breakeven and held
the runner. `alpaca_scale_out_suppressed_for_deadman` fired 228 times and
`tranche_oco_skipped_extended_hours` 223 times on the winners; the eight worst names cost
about $1,380 against the same strategy with partials available.

The gate is now FEASIBILITY, not family: can the tranche be sold this instant with a stop
still covering the runner? That is a real venue question (a resting full-qty deadman consumes
Alpaca's qty_available), and when the answer is no it is recorded with the numbers instead of
silently becoming a different trade.

Runnable: pytest tests/test_broker_agnostic_partial_gate.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr

Q, F = 355.0, 106.0


def test_no_resting_deadman_means_the_tranche_is_sellable():
    ok, dbg = lr.alpaca_partial_tranche_sellable({}, partial_qty=F, position_qty=Q)
    assert ok is True and dbg["reason"] == "no_resting_deadman"


def test_a_full_quantity_deadman_holds_the_tranche():
    le = {"deadman_stop": {"qty": Q, "order_id": "oid-1"}}
    ok, dbg = lr.alpaca_partial_tranche_sellable(le, partial_qty=F, position_qty=Q)
    assert ok is False and dbg["reason"] == "deadman_holds_tranche"
    assert dbg["deadman_qty"] == Q and dbg["partial_qty"] == F and dbg["position_qty"] == Q


def test_a_shrunk_deadman_leaves_the_tranche_free():
    """This is what PATH B buys: the stop rests for R = Q - f, so f is sellable."""
    le = {"deadman_stop": {"qty": Q - F, "order_id": "oid-2"}}
    ok, dbg = lr.alpaca_partial_tranche_sellable(le, partial_qty=F, position_qty=Q)
    assert ok is True and dbg["reason"] == "deadman_leaves_tranche_free"


def test_an_already_reserved_oco_tranche_is_sellable(monkeypatch):
    le = {"deadman_stop": {"qty": Q, "order_id": "oid-3"},
          "scale_limit_order_id": "oco-1", "scale_limit_is_oco": True, "scale_limit_qty": F}
    ok, dbg = lr.alpaca_partial_tranche_sellable(le, partial_qty=F, position_qty=Q)
    assert ok is True and dbg["reason"] == "tranche_already_reserved" and dbg["reserved_qty"] == F


def test_unreadable_quantities_fail_closed():
    for kwargs in ({"partial_qty": None, "position_qty": Q}, {"partial_qty": F, "position_qty": 0.0},
                   {"partial_qty": float("nan"), "position_qty": Q}, {"partial_qty": "x", "position_qty": Q}):
        ok, dbg = lr.alpaca_partial_tranche_sellable({"deadman_stop": {"qty": Q}}, **kwargs)
        assert ok is False and dbg["reason"] == "quantities_unreadable", kwargs


def test_the_scaling_decision_no_longer_branches_on_the_execution_family():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("scale_qty, runner_qty, can_split = scale_out_quantity(")
    j = src.find("exit_reason = \"scale_out_target\" if scaling else \"target\"", i)
    assert 0 < i < j
    block = src[i:j]
    # the family term is gone from the decision itself ...
    assert "scaling = bool(_partial_wanted and _tranche_ok)" in block
    assert "not in ALPACA_EXECUTION_FAMILIES\n            )" not in block
    # ... the family only selects WHICH feasibility question is asked ...
    assert "alpaca_partial_tranche_sellable(" in block
    # ... and an unsellable tranche is a receipt with the numbers, never a silent flatten
    assert '"alpaca_partial_tranche_unsellable"' in block
    assert '"fallback": "whole_position_at_target"' in block


def test_the_parity_contract_the_gate_used_to_break_is_still_stated():
    """The split is named a parity contract shared by the paper and live runners, so a
    family gate on it was a per-broker strategy by definition."""
    import pathlib

    from app.services.trading.momentum_neural import paper_execution as pe

    text = pathlib.Path(pe.__file__).read_text(encoding="utf-8")
    i = text.find("parity contract")
    assert i > 0
    context = text[max(0, i - 400):i + 400]
    assert "scale_out_fraction" in context and "BREAKEVEN" in context.upper()
