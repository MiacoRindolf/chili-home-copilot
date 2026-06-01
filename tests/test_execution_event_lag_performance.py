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


def test_single_venue_summary_reuses_global_percentiles(monkeypatch) -> None:
    calls = 0

    def counting_percentiles(values, *qs):
        nonlocal calls
        calls += 1
        rows = sorted(float(v) for v in values)
        last = len(rows) - 1
        return tuple(rows[max(0, min(last, int(round(last * q))))] for q in qs)

    class _Rows:
        def fetchall(self):
            return [
                ("coinbase", 10.0),
                ("coinbase", 20.0),
                ("coinbase", 30.0),
                ("coinbase", 40.0),
                ("coinbase", 50.0),
            ]

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    monkeypatch.setattr(eel, "_percentiles", counting_percentiles)

    summary = eel.measure_execution_event_lag(_Db(), lookback_seconds=300)

    assert calls == 1
    assert summary.per_venue["coinbase"]["p50_ms"] == summary.p50_ms
    assert summary.per_venue["coinbase"]["p95_ms"] == summary.p95_ms
    assert summary.per_venue["coinbase"]["max_ms"] == summary.max_ms
