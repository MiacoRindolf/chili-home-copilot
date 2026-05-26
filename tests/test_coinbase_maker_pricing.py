from app.services.trading.coinbase_maker_pricing import plan_post_only_buy_limit


def test_post_only_buy_limit_improves_inside_spread_by_named_ticks():
    plan = plan_post_only_buy_limit(
        bid=100.0,
        ask=100.05,
        price_increment=0.01,
        improve_ticks=2,
    )

    assert plan is not None
    assert plan.limit_price_text == "100.02"
    assert plan.improved_ticks == 2


def test_post_only_buy_limit_does_not_cross_one_tick_spread():
    plan = plan_post_only_buy_limit(
        bid=100.0,
        ask=100.01,
        price_increment=0.01,
        improve_ticks=3,
    )

    assert plan is not None
    assert plan.limit_price_text == "100.0"
    assert plan.improved_ticks == 0


def test_post_only_buy_limit_uses_bid_when_increment_unknown():
    plan = plan_post_only_buy_limit(
        bid="2.04",
        ask="2.05",
        price_increment=None,
        improve_ticks=5,
    )

    assert plan is not None
    assert plan.limit_price_text == "2.04"
    assert plan.improved_ticks == 0
