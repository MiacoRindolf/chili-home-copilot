from app.services.trading.pattern_position_monitor import _persistable_decision_source


def test_persistable_decision_source_maps_mechanical_critical_exit() -> None:
    assert _persistable_decision_source("mechanical_critical_exit") == "mechanical"


def test_persistable_decision_source_caps_unknown_values() -> None:
    value = _persistable_decision_source("very_long_monitor_source_name")
    assert value == "very_long_monitor_so"
    assert len(value) == 20
