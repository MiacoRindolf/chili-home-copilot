"""Phase K unit tests for the pure ops health aggregator."""
from __future__ import annotations

from app.services.trading.ops_health_model import (
    OpsHealthSnapshotInput,
    PHASE_KEYS,
    compute_snapshot,
)


def _empty_input(
    *,
    phases: dict | None = None,
    scheduler: dict | None = None,
    governance: dict | None = None,
    lookback_days: int = 14,
) -> OpsHealthSnapshotInput:
    return OpsHealthSnapshotInput(
        phase_summaries=phases or {},
        scheduler=scheduler,
        governance=governance,
        lookback_days=lookback_days,
    )


# ---------------------------------------------------------------------------
# baseline / empty
# ---------------------------------------------------------------------------


def test_empty_summaries_every_phase_absent_yellow():
    """All phases missing summary is a yellow signal, not red."""
    snap = compute_snapshot(_empty_input())
    d = snap.to_dict()
    assert d["overall_severity"] == "yellow"
    phase_keys_out = [p["key"] for p in d["phases"]]
    assert phase_keys_out == list(PHASE_KEYS)
    for p in d["phases"]:
        assert p["present"] is False
        assert p["mode"] is None
        assert p["red_count"] == 0
        assert p["yellow_count"] == 0


def test_all_phases_present_green_overall_green():
    phases = {
        key: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0, "green": 10}}
        for key in PHASE_KEYS
    }
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    assert d["overall_severity"] == "green"
    for p in d["phases"]:
        assert p["present"] is True
        assert p["mode"] == "shadow"
        assert p["red_count"] == 0
        assert p["yellow_count"] == 0


# ---------------------------------------------------------------------------
# severity extraction variants
# ---------------------------------------------------------------------------


def test_severity_breakdown_variant_supported():
    phases = {
        "ledger": {
            "mode": "shadow",
            "severity_breakdown": {"red": 2, "yellow": 1},
        },
    }
    # Fill remaining phases so overall is driven by ledger
    for k in PHASE_KEYS:
        phases.setdefault(k, {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}})
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    ledger = [p for p in d["phases"] if p["key"] == "ledger"][0]
    assert ledger["red_count"] == 2
    assert ledger["yellow_count"] == 1
    assert d["overall_severity"] == "red"


def test_red_count_flat_field():
    phases = {k: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}} for k in PHASE_KEYS}
    phases["exit_engine"] = {"mode": "shadow", "red_count": 3, "yellow_count": 1}
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    exit_row = [p for p in d["phases"] if p["key"] == "exit_engine"][0]
    assert exit_row["red_count"] == 3
    assert d["overall_severity"] == "red"


def test_unknown_shape_notes_it():
    phases = {k: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}} for k in PHASE_KEYS}
    phases["drift_monitor"] = {"mode": "shadow", "something": "weird"}
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    row = [p for p in d["phases"] if p["key"] == "drift_monitor"][0]
    assert "no_severity_breakdown" in row["notes"]


# ---------------------------------------------------------------------------
# overall severity rules
# ---------------------------------------------------------------------------


def test_yellow_elsewhere_bubbles_up():
    phases = {k: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}} for k in PHASE_KEYS}
    phases["risk_dial"] = {"mode": "shadow", "by_severity": {"red": 0, "yellow": 2}}
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    assert d["overall_severity"] == "yellow"


def test_red_trumps_yellow():
    phases = {k: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}} for k in PHASE_KEYS}
    phases["risk_dial"] = {"mode": "shadow", "by_severity": {"red": 0, "yellow": 2}}
    phases["venue_truth"] = {"mode": "shadow", "by_severity": {"red": 1, "yellow": 0}}
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    assert d["overall_severity"] == "red"


def test_authoritative_mode_tighter_guard():
    """Authoritative + any yellow = red, per the tighter guard."""
    phases = {k: {"mode": "shadow", "by_severity": {"red": 0, "yellow": 0}} for k in PHASE_KEYS}
    phases["ledger"] = {"mode": "authoritative", "by_severity": {"red": 0, "yellow": 1}}
    snap = compute_snapshot(_empty_input(phases=phases))
    d = snap.to_dict()
    assert d["overall_severity"] == "red"


# ---------------------------------------------------------------------------
# scheduler / governance
# ---------------------------------------------------------------------------


def test_scheduler_running_and_jobs_list():
    scheduler = {"running": True, "jobs": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    snap = compute_snapshot(_empty_input(scheduler=scheduler))
    d = snap.to_dict()
    assert d["scheduler"]["running"] is True
    assert d["scheduler"]["job_count"] == 3


def test_scheduler_missing_defaults():
    snap = compute_snapshot(_empty_input())
    d = snap.to_dict()
    assert d["scheduler"] == {"running": False, "job_count": 0}


def test_governance_kill_switch_dict_form():
    gov = {"kill_switch": {"engaged": True}, "pending_approvals": [{"id": 1}]}
    snap = compute_snapshot(_empty_input(governance=gov))
    d = snap.to_dict()
    assert d["governance"] == {
        "kill_switch_engaged": True,
        "pending_approvals": 1,
    }


def test_governance_kill_switch_bool_form():
    gov = {"kill_switch": False, "pending_approvals": 0}
    snap = compute_snapshot(_empty_input(governance=gov))
    d = snap.to_dict()
    assert d["governance"] == {
        "kill_switch_engaged": False,
        "pending_approvals": 0,
    }


def test_governance_missing_defaults():
    snap = compute_snapshot(_empty_input())
    d = snap.to_dict()
    assert d["governance"] == {
        "kill_switch_engaged": False,
        "pending_approvals": 0,
    }


# ---------------------------------------------------------------------------
# wire contract
# ---------------------------------------------------------------------------


def test_wire_shape_top_level_keys():
    snap = compute_snapshot(_empty_input(lookback_days=7))
    d = snap.to_dict()
    assert set(d.keys()) == {
        "overall_severity",
        "lookback_days",
        "scheduler",
        "governance",
        "phases",
    }
    assert d["lookback_days"] == 7


def test_phase_keys_ordered():
    snap = compute_snapshot(_empty_input())
    d = snap.to_dict()
    keys = [p["key"] for p in d["phases"]]
    assert keys == list(PHASE_KEYS)
