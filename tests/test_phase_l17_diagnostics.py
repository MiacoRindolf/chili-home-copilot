"""Phase L.17 - smoke test for the macro regime diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``macro_regime_service.macro_regime_summary``.
"""
from __future__ import annotations


def test_macro_regime_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/macro-regime/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "macro_regime" in j
    payload = j["macro_regime"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_label",
        "by_rates_regime",
        "by_credit_regime",
        "by_usd_regime",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_label"].keys()) == {
        "risk_on", "cautious", "risk_off",
    }
    assert isinstance(payload["by_rates_regime"], dict)
    assert isinstance(payload["by_credit_regime"], dict)
    assert isinstance(payload["by_usd_regime"], dict)
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_macro_regime_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    # ge=1 - zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/macro-regime/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    # le=180 - out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/macro-regime/diagnostics?lookback_days=181"
    )
    assert r.status_code == 422
