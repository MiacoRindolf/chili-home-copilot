"""``/api/trading/scan/status`` — brain_runtime primary aggregate + compatibility mirrors."""

from __future__ import annotations


def test_scan_status_brain_runtime_first_after_ok(client):
    r = client.get("/api/trading/scan/status")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    keys = list(data.keys())
    assert keys[0] == "ok"
    assert keys[1] == "brain_runtime"


def test_scan_status_brain_runtime_shape_and_mirrors(client):
    r = client.get("/api/trading/scan/status")
    assert r.status_code == 200
    data = r.json()
    br = data.get("brain_runtime") or {}
    assert isinstance(br, dict)
    assert "work_ledger" in br
    assert "release" in br
    assert "scheduler" in br
    assert "scan" in br
    assert br.get("compatibility_mirror_keys") == ["work_ledger", "release", "scheduler", "scan"]
    assert isinstance(br.get("compatibility_mirror_note"), str)
    ls = br.get("learning_summary")
    assert isinstance(ls, dict)
    assert "running" in ls

    assert data["work_ledger"] == br["work_ledger"]
    assert data["release"] == br["release"]
    assert data["scheduler"] == br["scheduler"]
    assert data["scan"] == br["scan"]

    learn = data.get("learning") or {}
    assert learn.get("status_role") == "reconcile_compatibility"
