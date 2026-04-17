"""Phase L.21 - smoke tests for the vol-dispersion diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``vol_dispersion_service.vol_dispersion_summary``.
"""
from __future__ import annotations


def test_vol_dispersion_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/vol-dispersion/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "vol_dispersion" in j
    payload = j["vol_dispersion"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_vol_regime_label",
        "by_dispersion_label",
        "by_correlation_label",
        "mean_vixy_close",
        "mean_vix_slope_4m_1m",
        "mean_cross_section_return_std_20d",
        "mean_abs_corr_20d",
        "mean_sector_leadership_churn_20d",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_vol_regime_label"].keys()) == {
        "vol_compressed",
        "vol_normal",
        "vol_expanded",
        "vol_spike",
    }
    assert set(payload["by_dispersion_label"].keys()) == {
        "dispersion_low",
        "dispersion_normal",
        "dispersion_high",
    }
    assert set(payload["by_correlation_label"].keys()) == {
        "correlation_low",
        "correlation_normal",
        "correlation_spike",
    }
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_vol_dispersion_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    # ge=1: zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/vol-dispersion/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    # le=180: out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/vol-dispersion/diagnostics?lookback_days=181"
    )
    assert r.status_code == 422
