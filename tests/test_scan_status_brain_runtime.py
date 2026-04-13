"""``/api/trading/scan/status`` — brain_runtime primary aggregate + compatibility mirrors.

Post-``31ca070`` deploy validation contract (no SHA / ``release`` fingerprint):

- ``brain_runtime.release`` and top-level ``release`` are always ``{}`` — expected; do not
  assert ``git_commit`` or compare JSON to ``git rev-parse HEAD``.
- Validate payload shape, ``learning`` last, mirror equality, ``learning.status_role``,
  ``brain_runtime.learning_summary`` (incl. ``status_role``, ``tickers_processed``),
  ``activity_signals`` (four minimal keys), and ``work_ledger`` via ``brain_runtime``.

Top-level mirror equality assertions are **regression** until mirrors are removed from
``api_scan_status`` (see ``.cursor/plans/scan_status_mirror_removal_readiness.plan.md``).

See ``.cursor/plans/lc_shrink_validation_reset.plan.md`` and
``.cursor/rules/chili-scan-status-deploy-validation.mdc``.
"""

from __future__ import annotations


def test_scan_status_brain_runtime_first_after_ok(client):
    r = client.get("/api/trading/scan/status")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    keys = list(data.keys())
    assert keys[0] == "ok"
    assert keys[1] == "brain_runtime"
    assert keys == [
        "ok",
        "brain_runtime",
        "prescreen",
        "work_ledger",
        "release",
        "scheduler",
        "scan",
        "learning",
    ]


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

    assert data["work_ledger"] == br["work_ledger"]
    assert data["release"] == br["release"]
    assert data["scheduler"] == br["scheduler"]
    assert data["scan"] == br["scan"]

    learn = data.get("learning") or {}
    assert learn.get("status_role") == "reconcile_compatibility"

    assert br.get("release") == {}
    assert data.get("release") == {}


def test_scan_status_compat_mirrors_zero_omits_top_level_duplicates(client):
    """compat_mirrors=0 omits legacy top-level mirrors; brain_runtime and learning unchanged."""
    r_full = client.get("/api/trading/scan/status")
    r0 = client.get("/api/trading/scan/status?compat_mirrors=0")
    assert r_full.status_code == 200 and r0.status_code == 200
    d0 = r0.json()
    assert d0.get("ok") is True
    assert list(d0.keys()) == ["ok", "brain_runtime", "prescreen", "learning"]
    assert "work_ledger" not in d0
    assert "release" not in d0
    assert "scheduler" not in d0
    assert "scan" not in d0
    br0 = d0.get("brain_runtime") or {}
    brf = r_full.json().get("brain_runtime") or {}
    assert br0.get("work_ledger") == brf.get("work_ledger")
    assert (d0.get("learning") or {}).get("status_role") == "reconcile_compatibility"


def test_scan_status_compat_mirrors_one_explicit_matches_default(client):
    r_def = client.get("/api/trading/scan/status")
    r1 = client.get("/api/trading/scan/status?compat_mirrors=1")
    assert r_def.json() == r1.json()
