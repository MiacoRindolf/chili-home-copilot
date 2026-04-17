"""Phase K - smoke tests for the divergence panel and /ops/health
diagnostics endpoints. Confirms the JSON payload matches the frozen
shape returned by the service layer.
"""
from __future__ import annotations


def test_divergence_diagnostics_endpoint(paired_client):
    client, _user = paired_client
    r = client.get(
        "/api/trading/brain/divergence/diagnostics?lookback_days=14"
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "divergence" in j
    payload = j["divergence"]
    assert set(payload.keys()) == {
        "mode",
        "lookback_days",
        "divergence_events_total",
        "by_severity",
        "patterns_red",
        "patterns_yellow",
        "mean_score",
        "layers_tracked",
        "latest_divergence",
    }
    assert payload["lookback_days"] == 14
    assert set(payload["by_severity"].keys()) == {"green", "yellow", "red"}
    assert set(payload["layers_tracked"]) == {
        "ledger", "exit", "venue", "bracket", "sizer",
    }


def test_ops_health_endpoint(paired_client):
    client, _user = paired_client
    r = client.get("/api/trading/brain/ops/health?lookback_days=14")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert "ops_health" in j
    payload = j["ops_health"]
    # Top-level keys - frozen shape.
    expected = {
        "overall_severity",
        "lookback_days",
        "scheduler",
        "governance",
        "phases",
        "enabled",
    }
    assert expected.issubset(set(payload.keys()))
    assert payload["lookback_days"] == 14
    assert payload["overall_severity"] in {"green", "yellow", "red"}
    # scheduler / governance sub-shapes.
    assert set(payload["scheduler"].keys()) == {"running", "job_count"}
    assert set(payload["governance"].keys()) == {
        "kill_switch_engaged", "pending_approvals",
    }
    # phases list contains the expected 15 keys in order.
    assert isinstance(payload["phases"], list)
    phase_keys = [p["key"] for p in payload["phases"]]
    assert phase_keys == [
        "ledger", "exit_engine", "net_edge", "pit", "triple_barrier",
        "execution_cost", "venue_truth", "bracket_intent",
        "bracket_reconciliation", "position_sizer", "risk_dial",
        "capital_reweight", "drift_monitor", "recert_queue", "divergence",
    ]
    for p in payload["phases"]:
        assert set(p.keys()) == {
            "key", "present", "mode", "red_count", "yellow_count", "notes",
        }


def test_ops_health_endpoint_lookback_clamping(paired_client):
    """lookback_days is clamped within the model-defined range."""
    client, _user = paired_client
    r = client.get("/api/trading/brain/ops/health?lookback_days=30")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ops_health"]["lookback_days"] == 30
