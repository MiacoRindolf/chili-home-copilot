"""Phase M.1 — smoke tests for the pattern x regime performance
diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``pattern_regime_performance_service.pattern_regime_perf_summary``.
"""
from __future__ import annotations


def test_pattern_regime_perf_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/pattern-regime-performance/diagnostics"
        "?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "pattern_regime_performance" in j
    payload = j["pattern_regime_performance"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "window_days",
        "min_trades_per_cell",
        "latest_as_of_date",
        "latest_ledger_run_id",
        "ledger_rows_total",
        "confident_cells_total",
        "by_dimension",
        "top_pattern_label_expectancy",
        "bottom_pattern_label_expectancy",
    }
    assert payload["lookback_days"] == 14
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")
    assert isinstance(payload["window_days"], int)
    assert isinstance(payload["min_trades_per_cell"], int)
    assert isinstance(payload["ledger_rows_total"], int)
    assert isinstance(payload["confident_cells_total"], int)
    assert set(payload["by_dimension"].keys()) == {
        "macro_regime",
        "breadth_label",
        "cross_asset_label",
        "ticker_regime",
        "vol_regime",
        "dispersion_label",
        "correlation_label",
        "session_label",
    }
    for dim_name, dim_payload in payload["by_dimension"].items():
        assert set(dim_payload.keys()) == {
            "total_cells",
            "confident_cells",
            "by_label",
        }, dim_name
        assert isinstance(dim_payload["total_cells"], int)
        assert isinstance(dim_payload["confident_cells"], int)
        assert isinstance(dim_payload["by_label"], dict)
    assert isinstance(payload["top_pattern_label_expectancy"], list)
    assert isinstance(payload["bottom_pattern_label_expectancy"], list)


def test_pattern_regime_perf_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/pattern-regime-performance/diagnostics"
        "?lookback_days=0"
    )
    assert r.status_code == 422
    r = client.get(
        "/api/trading/brain/pattern-regime-performance/diagnostics"
        "?lookback_days=181"
    )
    assert r.status_code == 422
