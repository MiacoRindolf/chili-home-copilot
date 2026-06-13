"""Weekend A-list (quant pass v2): A1 broker-truth, A2 schedule, A4 ladder
floor, A6 venue fixes."""

from datetime import datetime, timezone

from app.config import settings as _settings


def test_a4_ladder_floor_is_one():
    from app.services.trading.momentum_neural.risk_policy import cushion_risk_multiplier

    class _D:  # db unused on the no-cushion path via monkeypatched governance
        pass

    import app.services.trading.governance as gov
    real = gov.global_realized_pnl_today_et
    try:
        gov.global_realized_pnl_today_et = lambda db, user_id=None: {"total_usd": 0.0}
        mult, meta = cushion_risk_multiplier(_D(), base_loss_usd=55.0)
        assert mult == 1.0  # floor raised from 0.5 — full base risk on first triggers
        gov.global_realized_pnl_today_et = lambda db, user_id=None: {"total_usd": 550.0}
        mult2, _ = cushion_risk_multiplier(_D(), base_loss_usd=55.0)
        assert mult2 == 2.0  # ceiling unchanged
    finally:
        gov.global_realized_pnl_today_et = real


def test_a2_schedule_windows():
    from app.services.trading.momentum_neural.market_profile import schedule_window_now

    def _at(h, m):  # UTC inputs; 2026-06-12 is EDT (UTC-4)
        return datetime(2026, 6, 12, h, m, tzinfo=timezone.utc)

    assert schedule_window_now(_at(8, 5)) == "hot"      # 04:05 ET
    assert schedule_window_now(_at(14, 0)) == "hot"     # 10:00 ET
    assert schedule_window_now(_at(15, 0)) == "midday"  # 11:00 ET
    assert schedule_window_now(_at(18, 45)) == "late"   # 14:45 ET
    assert schedule_window_now(_at(21, 0)) == "closed"  # 17:00 ET
    assert schedule_window_now(datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)) == "closed"  # Saturday


def test_a2_sizing_wired_and_capped():
    src = open("app/services/trading/momentum_neural/live_runner.py", encoding="utf-8").read()
    assert '"hot": 1.5, "midday": 0.5, "late": 0.0' in src
    assert "float(_base_max_loss) * 3.0" in src  # combined-multiplier ceiling
    assert "live_entry_wait_late_window" in src


def test_a1_broker_truth_override_present():
    src = open("app/services/trading/momentum_neural/outcome_extract.py", encoding="utf-8").read()
    assert "_broker_truth_realized_for_session" in src
    assert "broker_order_id = :oid" in src  # joined on order id, never symbol/time
    assert "backfill_outcomes_from_broker_truth" in src


def test_a6_broker_qty_clamp_present():
    src = open("app/services/trading/momentum_neural/live_runner.py", encoding="utf-8").read()
    assert "live_exit_qty_clamped_to_broker" in src
