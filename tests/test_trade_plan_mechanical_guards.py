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
