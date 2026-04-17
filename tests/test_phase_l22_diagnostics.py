"""Phase L.22 — smoke tests for the intraday-session diagnostics endpoint.

Confirms the JSON payload matches the frozen shape returned by
``intraday_session_service.intraday_session_summary``.
"""
from __future__ import annotations


def test_intraday_session_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/intraday-session/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "intraday_session" in j
    payload = j["intraday_session"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_session_label",
        "mean_or_range_pct",
        "mean_midday_compression_ratio",
        "mean_ph_range_pct",
        "mean_intraday_rv",
        "mean_session_range_pct",
        "mean_gap_open_pct_abs",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_session_label"].keys()) == {
        "session_trending_up",
        "session_trending_down",
        "session_range_bound",
        "session_reversal",
        "session_gap_and_go",
        "session_gap_fade",
        "session_compressed",
        "session_neutral",
    }
    assert payload["mode"] in ("off", "shadow", "compare", "authoritative")


def test_intraday_session_diagnostics_lookback_clamped(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/intraday-session/diagnostics?lookback_days=0"
    )
    assert r.status_code == 422
    r = client.get(
        "/api/trading/brain/intraday-session/diagnostics?lookback_days=181"
    )
    assert r.status_code == 422
