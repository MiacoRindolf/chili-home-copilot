"""Phase I - smoke tests for the risk-dial and capital-reweight
diagnostics endpoints. Confirms the JSON payload matches the frozen
shape returned by the service layer.
"""
from __future__ import annotations


def test_risk_dial_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/risk-dial/diagnostics?lookback_hours=24"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "risk_dial" in j
    payload = j["risk_dial"]
    assert set(payload.keys()) == {
        "mode", "lookback_hours", "dial_events_total",
        "by_regime", "by_source", "by_dial_bucket",
        "mean_dial_value", "latest_dial",
        "override_rejected_count", "capped_at_ceiling_count",
    }
    assert payload["lookback_hours"] == 24
    assert set(payload["by_dial_bucket"].keys()) == {
        "under_0_5", "0_5_to_0_8", "0_8_to_1_0", "1_0_to_1_2", "over_1_2",
    }


def test_capital_reweight_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/capital-reweight/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "capital_reweight" in j
    payload = j["capital_reweight"]
    assert set(payload.keys()) == {
        "mode", "lookback_days", "sweeps_total",
        "mean_mean_drift_bps", "p90_p90_drift_bps",
        "single_bucket_cap_trigger_count",
        "concentration_cap_trigger_count", "latest_sweep",
    }
    assert payload["lookback_days"] == 14
