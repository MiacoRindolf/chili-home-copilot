"""Selection-bias ledger: ``param_hash`` matches ``BacktestParamSet`` (distinct_param_hash_count)."""

from __future__ import annotations

from app.models.trading import BacktestParamSet, ScanPattern
from app.services.trading.backtest_param_sets import get_or_create_backtest_param_set
from app.services.trading.selection_bias import (
    build_outcome_fingerprint,
    build_research_run_key,
    build_validation_slice_key,
    record_validation_slice_use,
    summarize_slice_usage,
)

RULES_FINGERPRINT = "rf_ledgertest"


def _eval_row(*, is_win_rate: float, oos_win_rate: float) -> dict:
    return {
        "ticker": "SPY",
        "chart_time_from": 1700000000,
        "chart_time_to": 1701000000,
        "ohlc_bars": 100,
        "in_sample_bars": 80,
        "out_of_sample_bars": 20,
        "oos_holdout_fraction": 0.2,
        "period": "1y",
        "interval": "1d",
        "spread_used": 0.01,
        "commission_used": 0.0,
        "is_win_rate": is_win_rate,
        "oos_win_rate": oos_win_rate,
        "trade_count": 5,
    }


def test_summarize_distinct_param_hash_counts_real_backtest_param_sets(db):
    scan_pattern = ScanPattern(
        name="Ledger param hash regression",
        rules_json={},
        origin="test",
    )
    db.add(scan_pattern)
    db.flush()
    scan_pattern_id = int(scan_pattern.id)

    pid_a = get_or_create_backtest_param_set(
        db, {"period": "1y", "ledger_param_hash_test": "alpha"}
    )
    pid_b = get_or_create_backtest_param_set(
        db, {"period": "1y", "ledger_param_hash_test": "beta"}
    )
    assert pid_a is not None and pid_b is not None
    db.flush()
    row_a = db.get(BacktestParamSet, int(pid_a))
    row_b = db.get(BacktestParamSet, int(pid_b))
    assert row_a is not None and row_b is not None
    ph_a = row_a.param_hash
    ph_b = row_b.param_hash
    assert ph_a != ph_b

    eval_a = [_eval_row(is_win_rate=50.0, oos_win_rate=55.0)]
    eval_b = [_eval_row(is_win_rate=51.0, oos_win_rate=55.0)]
    sk = build_validation_slice_key(
        origin="brain_discovered",
        asset_class="stocks",
        timeframe="1d",
        hypothesis_family=None,
        eval_rows=eval_a,
    )
    assert sk == build_validation_slice_key(
        origin="brain_discovered",
        asset_class="stocks",
        timeframe="1d",
        hypothesis_family=None,
        eval_rows=eval_b,
    )

    ofp_a = build_outcome_fingerprint(eval_a)
    ofp_b = build_outcome_fingerprint(eval_b)
    assert ofp_a != ofp_b

    rrk_a = build_research_run_key(
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        outcome_fingerprint=ofp_a,
    )
    rrk_b = build_research_run_key(
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        outcome_fingerprint=ofp_b,
    )

    record_validation_slice_use(
        db,
        research_run_key=rrk_a,
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        param_hash=ph_a,
    )
    record_validation_slice_use(
        db,
        research_run_key=rrk_b,
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        param_hash=ph_b,
    )
    db.commit()

    summ = summarize_slice_usage(db, slice_key=sk)
    assert summ["usage_count"] == 2
    assert summ["distinct_param_hash_count"] == 2

    eval_c = [_eval_row(is_win_rate=52.0, oos_win_rate=55.0)]
    ofp_c = build_outcome_fingerprint(eval_c)
    rrk_c = build_research_run_key(
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        outcome_fingerprint=ofp_c,
    )
    record_validation_slice_use(
        db,
        research_run_key=rrk_c,
        slice_key=sk,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=RULES_FINGERPRINT,
        param_hash=ph_a,
    )
    db.commit()

    summ2 = summarize_slice_usage(db, slice_key=sk)
    assert summ2["usage_count"] == 3
    assert summ2["distinct_param_hash_count"] == 2
