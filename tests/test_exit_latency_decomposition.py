"""DB-free guards on the phase-1 exit-latency loss decomposition.

These lock the arithmetic, not a policy. If someone edits a measured input in
`scripts/forensic_exit_latency_decomposition.py` the reconciliation breaks here
first, which is the whole point: the corpus is small enough that a single typo
would silently change the conclusion.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SRC = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "forensic_exit_latency_decomposition.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "forensic_exit_latency_decomposition", _SRC
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves annotations through sys.modules, so register first.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_corpus_is_the_four_priced_alpaca_live_losses():
    keys = [t.key for t in MOD.TRADES]
    assert keys == ["CDTG", "UPC", "CANF_c1", "JLHL"]
    assert all(t.realized < 0 for t in MOD.TRADES)


@pytest.mark.parametrize("trade", MOD.TRADES, ids=lambda t: t.key)
def test_buckets_reconcile_to_realized(trade):
    total = trade.A + trade.C + trade.B + trade.D
    assert total == pytest.approx(trade.realized, abs=1e-6)


@pytest.mark.parametrize("trade", MOD.TRADES, ids=lambda t: t.key)
def test_b_splits_into_b1_b0_b2(trade):
    assert trade.B1 + trade.B0 + trade.B2 == pytest.approx(trade.B, abs=1e-9)


@pytest.mark.parametrize("trade", MOD.TRADES, ids=lambda t: t.key)
def test_no_unexplained_residual(trade):
    # Alpaca is commission-free gross; every cent must land in A, C or B.
    assert trade.D == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("trade", MOD.TRADES, ids=lambda t: t.key)
def test_realized_equals_price_difference_times_qty(trade):
    assert (trade.fill_px - trade.entry_px) * trade.qty == pytest.approx(
        trade.realized, abs=1e-6
    )


def test_aggregate_reconciles():
    total = sum(t.A + t.C + t.B + t.D for t in MOD.TRADES)
    assert total == pytest.approx(-186.030137, abs=1e-5)


def test_breach_to_fill_dominates_the_ledger_losses():
    """B is the majority of the aggregate loss; A alone is a minority."""
    b = sum(t.B for t in MOD.TRADES)
    a = sum(t.A for t in MOD.TRADES)
    realized = sum(t.realized for t in MOD.TRADES)
    assert b / realized > 0.60
    assert a / realized < 0.50


def test_jlhl_is_the_case_where_waiting_helped():
    jlhl = next(t for t in MOD.TRADES if t.key == "JLHL")
    assert jlhl.B > 0.0
    assert jlhl.B == pytest.approx(7.7378, abs=1e-3)


def test_canf_tighten_wrote_a_stop_above_the_last_known_bid():
    canf = next(t for t in MOD.TRADES if t.key == "CANF_c1")
    assert canf.s_breach > canf.bid_cross
    assert canf.C > 0.0  # the tighten itself reduced the arithmetic loss ...
    assert canf.B < canf.C  # ... but the gap it opened cost far more


def test_timing_series_is_monotonic_per_trade():
    by_trade = {}
    for row in MOD.timing_rows():
        by_trade.setdefault(row["trade"], []).append(row["t_plus_s_from_cross"])
    assert by_trade
    for key, series in by_trade.items():
        assert series == sorted(series), key


def test_undecomposable_leg_is_declared_with_a_reason():
    assert len(MOD.UNDECOMPOSABLE) == 1
    leg = MOD.UNDECOMPOSABLE[0]
    assert leg["key"] == "CANF_c2"
    assert leg["broker_pnl"] == pytest.approx(-108.850005, abs=1e-6)
    assert (leg["fill_px"] - leg["entry_px"]) * leg["qty"] == pytest.approx(
        leg["broker_pnl"], abs=1e-5
    )
    assert leg["why"].strip()


def test_day_total_matches_broker_truth():
    """2026-09-02 broker truth = the three priced legs plus the unpriced one."""
    priced = sum(t.realized for t in MOD.TRADES if t.day == "2026-09-02")
    unpriced = sum(u["broker_pnl"] for u in MOD.UNDECOMPOSABLE)
    assert priced + unpriced == pytest.approx(-254.78007, abs=1e-4)


def test_assert_reconciles_helper_passes():
    MOD.assert_reconciles()
