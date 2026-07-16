from __future__ import annotations

import importlib


def test_live_runner_dynamic_pipeline_import_contracts_exist() -> None:
    pipeline = importlib.import_module("app.services.trading.momentum_neural.pipeline")
    for name in (
        "_live_book_imbalance",
        "_live_flow_slope",
        "_live_ofi_microprice",
        "_live_realized_vol",
        "_live_trade_flow",
        "read_ladder_distribution",
        "read_target_level_trade_prints",
    ):
        assert callable(getattr(pipeline, name, None)), name


def test_risk_policy_dynamic_helper_import_contracts_exist() -> None:
    outcome_labels = importlib.import_module("app.services.trading.momentum_neural.outcome_labels")
    catalyst = importlib.import_module("app.services.trading.momentum_neural.catalyst")

    assert callable(getattr(outcome_labels, "is_real_entry_outcome", None))
    assert callable(getattr(catalyst, "catalyst_grade_rank", None))


def test_live_runner_restored_helper_compatibility_exports_exist() -> None:
    live_runner = importlib.import_module("app.services.trading.momentum_neural.live_runner")

    for name in (
        "_live_ofi_microprice",
        "read_ladder_distribution",
        "read_target_level_trade_prints",
        "is_real_entry_outcome",
        "catalyst_grade_rank",
    ):
        assert callable(getattr(live_runner, name, None)), name
