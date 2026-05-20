from app.services.trading.bracket_reconciliation_service import _stop_tightness


def test_long_broker_higher_stop_is_tighter():
    assert _stop_tightness(
        direction="long", local_stop=18.70, broker_stop=19.00,
    ) == "broker_tighter"


def test_long_local_higher_stop_is_tighter():
    assert _stop_tightness(
        direction="long", local_stop=1.4148, broker_stop=0.8436,
    ) == "local_tighter"


def test_short_broker_lower_stop_is_tighter():
    assert _stop_tightness(
        direction="short", local_stop=105.0, broker_stop=103.0,
    ) == "broker_tighter"
