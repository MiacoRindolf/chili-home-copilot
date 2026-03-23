"""OOS promotion gate: optional aggregate trade floor (short-TF / crypto / family)."""

from app.services.trading.learning import brain_apply_oos_promotion_gate


def test_oos_gate_pending_when_aggregate_trades_below_min():
    prom, allow = brain_apply_oos_promotion_gate(
        origin="brain_discovered",
        mean_is_win_rate=55.0,
        mean_oos_win_rate=50.0,
        oos_tickers_with_result=3,
        min_oos_aggregate_trades=100,
        oos_aggregate_trade_count=5,
    )
    assert prom == "pending_oos"
    assert allow is True


def test_oos_gate_promoted_when_aggregate_trades_sufficient():
    prom, allow = brain_apply_oos_promotion_gate(
        origin="brain_discovered",
        mean_is_win_rate=55.0,
        mean_oos_win_rate=50.0,
        oos_tickers_with_result=3,
        min_oos_aggregate_trades=10,
        oos_aggregate_trade_count=50,
    )
    assert prom == "promoted"
    assert allow is True
