"""Phase L.19 - smoke test for the cross-asset diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``cross_asset_service.cross_asset_summary``.
"""
from __future__ import annotations


def test_cross_asset_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/cross-asset/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "cross_asset" in j
    payload = j["cross_asset"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_cross_asset_label",
        "by_bond_equity_label",
        "by_credit_equity_label",
        "by_usd_crypto_label",
        "by_vix_breadth_label",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_cross_asset_label"].keys()) == {
        "risk_on_crosscheck",
        "risk_off_crosscheck",
        "divergence",
        "neutral",
    }
    assert isinstance(payload["by_bond_equity_label"], dict)
    assert isinstance(payload["by_credit_equity_label"], dict)
    assert isinstance(payload["by_usd_crypto_label"], dict)
    assert isinstance(payload["by_vix_breadth_label"], dict)
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_cross_asset_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    # ge=1 - zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/cross-asset/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    # le=180 - out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/cross-asset/diagnostics?lookback_days=181"
    )
    assert r.status_code == 422
