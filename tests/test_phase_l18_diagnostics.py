"""Phase L.18 - smoke test for the breadth + RS diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``breadth_relstr_service.breadth_relstr_summary``.
"""
from __future__ import annotations


def test_breadth_relstr_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/breadth-relstr/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "breadth_relstr" in j
    payload = j["breadth_relstr"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_breadth_label",
        "by_leader_sector",
        "by_laggard_sector",
        "mean_advance_ratio",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_breadth_label"].keys()) == {
        "broad_risk_on", "mixed", "broad_risk_off",
    }
    assert isinstance(payload["by_leader_sector"], dict)
    assert isinstance(payload["by_laggard_sector"], dict)
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_breadth_relstr_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    # ge=1 - zero should fail validation with 422.
    r = client.get(
        "/api/trading/brain/breadth-relstr/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    # le=180 - out-of-range should also 422.
    r = client.get(
        "/api/trading/brain/breadth-relstr/diagnostics?lookback_days=181"
    )
    assert r.status_code == 422
