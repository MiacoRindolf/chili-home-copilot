"""Phase J - smoke tests for the drift-monitor and recert-queue
diagnostics endpoints. Confirms the JSON payload matches the frozen
shape returned by the service layer.
"""
from __future__ import annotations


def test_drift_monitor_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/drift-monitor/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "drift_monitor" in j
    payload = j["drift_monitor"]
    assert set(payload.keys()) == {
        "mode", "lookback_days", "drift_events_total",
        "by_severity", "patterns_red", "patterns_yellow",
        "mean_brier_delta", "mean_cusum_statistic", "latest_drift",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_severity"].keys()) == {"green", "yellow", "red"}


def test_recert_queue_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/recert-queue/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "recert_queue" in j
    payload = j["recert_queue"]
    assert set(payload.keys()) == {
        "mode", "lookback_days", "recert_events_total",
        "by_source", "by_severity", "by_status",
        "patterns_queued_distinct", "latest_recert",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_source"].keys()) == {
        "drift_monitor", "manual", "scheduler", "other",
    }
    assert set(payload["by_severity"].keys()) == {
        "red", "yellow", "green", "null",
    }
    assert set(payload["by_status"].keys()) == {
        "proposed", "dispatched", "completed", "cancelled", "other",
    }
