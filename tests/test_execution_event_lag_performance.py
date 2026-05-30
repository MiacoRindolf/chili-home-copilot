from __future__ import annotations

from app.services.trading import execution_event_lag as eel


def test_percentiles_matches_individual_percentile_values() -> None:
    values = [80.0, 10.0, 40.0, 20.0, 70.0, 30.0, 60.0, 50.0]

    p50, p95, p99 = eel._percentiles(values, 0.50, 0.95, 0.99)

    assert p50 == eel._percentile(values, 0.50)
    assert p95 == eel._percentile(values, 0.95)
    assert p99 == eel._percentile(values, 0.99)


def test_percentiles_preserves_empty_input_shape() -> None:
    assert eel._percentiles([], 0.50, 0.95, 0.99) == (None, None, None)


def test_percentiles_clamps_out_of_range_quantiles() -> None:
    values = [3.0, 1.0, 2.0]

    assert eel._percentiles(values, -1.0, 2.0) == (1.0, 3.0)
