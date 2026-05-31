from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5o-zero-adapter-closeout-summary.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_closeout_summary", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_closeout_summary_pins_zero_adapter_candidate_state() -> None:
    module = _load_module()

    summary = module.build_closeout_summary()

    assert summary["status"] == "COMPLETE_POSITIVE"
    assert summary["adapter_candidate_count"] == 0
    assert summary["future_blocker_count"] == 48
    assert summary["leave_alone_count"] == 16
    assert all(summary["invariants"].values())


def test_closeout_summary_groups_remaining_behavior_surfaces() -> None:
    module = _load_module()

    summary = module.build_closeout_summary()

    assert summary["future_blockers_by_group"] == {
        "learning_research_reporting": 5,
        "live_action_broker_reconcile": 21,
        "risk_capital_gate": 22,
    }
    assert summary["leave_alone_by_group"] == {
        "private_helper_type_only": 2,
        "public_ui_schema_contract": 14,
    }
    assert summary["blocked_until_runtime_source_clean"] == [
        "live_action_broker_reconcile",
        "risk_capital_gate",
        "public_ui_schema_contract",
    ]


def test_closeout_summary_fails_closed_if_adapter_candidate_reappears() -> None:
    module = _load_module()
    payload = module._load_map()
    payload["phase5o_bucket_counts"]["adapter_candidate"] = 1
    payload["phase5o_bucket_counts"]["leave_alone"] -= 1
    payload["items"][0]["phase5o_bucket"] = "adapter_candidate"

    summary = module.build_closeout_summary(payload)

    assert summary["status"] == "ALERT"
    assert summary["adapter_candidate_count"] == 1
    assert not summary["invariants"]["adapter_candidates_zero"]
