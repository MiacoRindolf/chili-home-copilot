"""``/api/trading/scan/status`` — brain_runtime primary aggregate (no top-level mirrors).

Post-``31ca070`` deploy validation contract (no SHA / ``release`` fingerprint):

- ``brain_runtime.release`` is always ``{}`` — expected; do not assert ``git_commit`` or compare
  JSON to ``git rev-parse HEAD``.
- Happy path: ``ok``, ``brain_runtime``, ``prescreen``, ``learning`` — ``learning`` last.
  Legacy root keys ``work_ledger`` / ``release`` / ``scheduler`` / ``scan`` are **not** present;
  read them only under ``brain_runtime``.
- ``encode_error`` path (not exercised here) still returns flat mirror **keys** (empty) per frozen
  contract — see ``docs/TRADING_BRAIN_WORK_LEDGER.md``.

See ``.cursor/plans/lc_shrink_validation_reset.plan.md`` and
``.cursor/rules/chili-scan-status-deploy-validation.mdc``.
"""

from __future__ import annotations


def test_scan_status_brain_runtime_key_order_no_top_level_mirrors(client):
    r = client.get("/api/trading/scan/status")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    keys = list(data.keys())
    assert keys[0] == "ok"
    assert keys[1] == "brain_runtime"
    assert keys == ["ok", "brain_runtime", "prescreen", "learning"]
    for k in ("work_ledger", "release", "scheduler", "scan"):
        assert k not in data


def test_scan_status_brain_runtime_shape_under_aggregate(client):
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
    assert ls.get("status_role") == "reconcile_compatibility"
    assert isinstance(ls.get("tickers_processed"), int)
    asig = br.get("activity_signals")
    assert isinstance(asig, dict)
    assert set(asig.keys()) == {
        "reconcile_active",
        "ledger_busy",
        "retry_or_dead_attention",
        "outcome_head_id",
    }
    assert isinstance(asig.get("reconcile_active"), bool)
    assert isinstance(asig.get("ledger_busy"), bool)
    assert isinstance(asig.get("retry_or_dead_attention"), bool)
    assert asig.get("outcome_head_id") is None or isinstance(asig.get("outcome_head_id"), int)

    learn = data.get("learning") or {}
    assert learn.get("status_role") == "reconcile_compatibility"

    assert br.get("release") == {}
