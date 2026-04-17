"""Phase L.20 - smoke test for the ticker-regime diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``ticker_regime_service.ticker_regime_summary``.
"""
from __future__ import annotations


def test_ticker_regime_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/ticker-regime/diagnostics"
        "?lookback_days=7&latest_tickers_limit=20"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "ticker_regime" in j
    payload = j["ticker_regime"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "distinct_tickers",
        "by_ticker_regime_label",
        "by_asset_class",
        "mean_coverage_score",
        "mean_trend_score",
        "mean_mean_revert_score",
        "latest_tickers",
    }
    assert payload["lookback_days"] == 7
    assert set(payload["by_ticker_regime_label"].keys()) == {
        "trend_up",
        "trend_down",
        "mean_revert",
        "choppy",
        "neutral",
    }
    assert isinstance(payload["by_asset_class"], dict)
    assert isinstance(payload["latest_tickers"], list)
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_ticker_regime_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    # ge=1: zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/ticker-regime/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    # le=30: out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/ticker-regime/diagnostics?lookback_days=31"
    )
    assert r.status_code == 422


def test_ticker_regime_diagnostics_latest_limit_clamped(paired_client):
    client, _user = paired_client
    # ge=1: zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/ticker-regime/diagnostics?latest_tickers_limit=0"
    )
    assert r.status_code == 422
    # le=200: out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/ticker-regime/diagnostics"
        "?latest_tickers_limit=500"
    )
    assert r.status_code == 422
