from app.services.trading.trade_plan_extractor import extract_trade_plan_mechanical


def test_mechanical_trade_plan_adds_price_guards_for_sparse_complex_pattern():
    plan = extract_trade_plan_mechanical(
        pattern_conditions=[
            {"indicator": f"custom_signal_{i}", "op": ">", "value": 1}
            for i in range(6)
        ],
        entry_price=100.0,
        stop_loss=92.0,
        target_price=120.0,
        current_price=101.0,
        indicators={"vwap": 99.5},
    )

    invalidations = plan["invalidation_conditions"]

    assert len(invalidations) >= 2
    assert invalidations[0] == {
        "desc": "Price breaks the hard stop level",
        "indicator": "price",
        "op": "<=",
        "value": 92.0,
        "severity": "critical",
    }
    warning = next(
        item for item in invalidations
        if item["desc"] == "Price loses the early-warning cushion before the hard stop"
    )
    assert warning["indicator"] == "price"
    assert warning["op"] == "<="
    assert warning["severity"] == "warning"
    assert warning["value"] == plan["key_levels"]["early_warning"]


def test_mechanical_trade_plan_does_not_duplicate_existing_price_stop_guard():
    plan = extract_trade_plan_mechanical(
        pattern_conditions=[
            {"indicator": "price", "op": ">", "value": 92.0},
            *[
                {"indicator": f"custom_signal_{i}", "op": ">", "value": 1}
                for i in range(5)
            ],
        ],
        entry_price=100.0,
        stop_loss=92.0,
        target_price=120.0,
        current_price=101.0,
        indicators={},
    )

    stop_guards = [
        item for item in plan["invalidation_conditions"]
        if item.get("indicator") == "price"
        and item.get("op") == "<="
        and item.get("value") == 92.0
    ]

    assert len(stop_guards) == 1


def test_mechanical_trade_plan_normalizes_parser_indicator_aliases():
    plan = extract_trade_plan_mechanical(
        pattern_conditions=[
            {"indicator": "price", "op": ">", "ref": "ema_20"},
            {"indicator": "rel_vol", "op": ">=", "value": 1.5},
            {"indicator": "vwap_reclaim", "op": "==", "value": True},
        ],
        entry_price=100.0,
        stop_loss=94.0,
        target_price=115.0,
        current_price=101.0,
        indicators={"rel_vol": 1.8, "vwap": 100.5, "ema_20": 99.0},
    )

    invalidations = plan["invalidation_conditions"]
    monitoring = plan["monitoring_signals"]

    assert {
        "desc": "volume_ratio no longer meets pattern condition (volume_ratio >= 1.5)",
        "indicator": "volume_ratio",
        "op": "<",
        "severity": "warning",
        "value": 1.5,
    } in invalidations
    assert any(item["indicator"] == "vwap" for item in invalidations)
    assert any(item["indicator"] == "volume_ratio" for item in monitoring)
