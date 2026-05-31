#!/usr/bin/env python
"""Read-only Phase 5O zero-adapter-candidates closeout summary.

This script intentionally reads only the checked-in Phase 5O compatibility map.
It does not touch runtime, Docker, Postgres, broker APIs, flags, or shared-root
state. Its purpose is to make the closeout claim machine-checkable:

- there are no remaining adapter candidates;
- the remaining behavior-bearing files are explicitly future rename blockers;
- public/schema and model-export compatibility surfaces are intentionally left
  alone for a later public-contract phase.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = REPO_ROOT / "docs" / "STRATEGY" / "phase5o_remaining_runtime_compat_map.json"

EXPECTED_BUCKET_COUNTS = {
    "adapter_candidate": 0,
    "future_rename_blocker": 48,
    "leave_alone": 16,
}

EXPECTED_FUTURE_BLOCKERS_BY_GROUP = {
    "learning_research_reporting": 5,
    "live_action_broker_reconcile": 21,
    "risk_capital_gate": 22,
}

EXPECTED_LEAVE_ALONE_BY_GROUP = {
    "private_helper_type_only": 2,
    "public_ui_schema_contract": 14,
}


def _load_map() -> dict[str, Any]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _paths_by(items: list[dict[str, Any]], key: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        grouped[str(item[key])].append(str(item["path"]))
    return {group: sorted(paths) for group, paths in sorted(grouped.items())}


def build_closeout_summary(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or _load_map()
    items = list(payload["items"])

    bucket_counter = Counter(str(item["phase5o_bucket"]) for item in items)
    bucket_counts = {
        bucket: int(bucket_counter.get(bucket, 0))
        for bucket in payload["phase5o_bucket_counts"]
    }
    future_items = [
        item for item in items if item["phase5o_bucket"] == "future_rename_blocker"
    ]
    leave_alone_items = [item for item in items if item["phase5o_bucket"] == "leave_alone"]
    adapter_items = [item for item in items if item["phase5o_bucket"] == "adapter_candidate"]

    future_by_group = Counter(str(item["contract_group"]) for item in future_items)
    leave_by_group = Counter(str(item["contract_group"]) for item in leave_alone_items)
    future_by_subtype = Counter(str(item["phase5o_subtype"]) for item in future_items)

    invariants = {
        "bucket_counts_match_map": bucket_counts == payload["phase5o_bucket_counts"],
        "adapter_candidates_zero": not adapter_items
        and payload["phase5o_bucket_counts"].get("adapter_candidate") == 0,
        "expected_bucket_counts": payload["phase5o_bucket_counts"]
        == EXPECTED_BUCKET_COUNTS,
        "future_blocker_groups_match": dict(future_by_group)
        == EXPECTED_FUTURE_BLOCKERS_BY_GROUP,
        "leave_alone_groups_match": dict(leave_by_group)
        == EXPECTED_LEAVE_ALONE_BY_GROUP,
        "all_items_have_rationales": all(
            item.get("path")
            and item.get("contract_group")
            and item.get("phase5o_bucket")
            and item.get("phase5o_subtype")
            and item.get("rationale")
            for item in items
        ),
    }

    status = "COMPLETE_POSITIVE" if all(invariants.values()) else "ALERT"
    return {
        "status": status,
        "reason": (
            "Phase 5O adapter candidates closed; remaining surfaces sequenced"
            if status == "COMPLETE_POSITIVE"
            else "Phase 5O closeout invariants failed"
        ),
        "orm_trade_symbol_compat_count": payload["orm_trade_symbol_compat_count"],
        "bucket_counts": dict(bucket_counts),
        "future_blocker_count": len(future_items),
        "leave_alone_count": len(leave_alone_items),
        "adapter_candidate_count": len(adapter_items),
        "future_blockers_by_group": dict(future_by_group),
        "leave_alone_by_group": dict(leave_by_group),
        "future_blockers_by_subtype": dict(sorted(future_by_subtype.items())),
        "future_blocker_paths_by_group": _paths_by(future_items, "contract_group"),
        "leave_alone_paths_by_group": _paths_by(leave_alone_items, "contract_group"),
        "adapter_candidate_paths": sorted(item["path"] for item in adapter_items),
        "invariants": invariants,
        "recommended_next_lane": "phase5p-learning-reporting-lifecycle-decay-adapter-plan",
        "blocked_until_runtime_source_clean": [
            "live_action_broker_reconcile",
            "risk_capital_gate",
            "public_ui_schema_contract",
        ],
    }


def main() -> int:
    summary = build_closeout_summary()
    print(f"VERDICT_STATUS={summary['status']}")
    print(f"VERDICT_REASON={summary['reason']}")
    print(f"ORM_TRADE_SYMBOL_COMPAT={summary['orm_trade_symbol_compat_count']}")
    print(f"ADAPTER_CANDIDATES={summary['adapter_candidate_count']}")
    print(f"FUTURE_RENAME_BLOCKERS={summary['future_blocker_count']}")
    print(f"LEAVE_ALONE={summary['leave_alone_count']}")
    print(
        "FUTURE_BLOCKERS_BY_GROUP="
        + json.dumps(summary["future_blockers_by_group"], sort_keys=True)
    )
    print(
        "LEAVE_ALONE_BY_GROUP="
        + json.dumps(summary["leave_alone_by_group"], sort_keys=True)
    )
    print(
        "FUTURE_BLOCKERS_BY_SUBTYPE="
        + json.dumps(summary["future_blockers_by_subtype"], sort_keys=True)
    )
    for name, ok in summary["invariants"].items():
        print(f"INVARIANT {name}={ok}")
    print(f"NEXT_RECOMMENDED_LANE={summary['recommended_next_lane']}")
    print(
        "BLOCKED_UNTIL_RUNTIME_SOURCE_CLEAN="
        + ",".join(summary["blocked_until_runtime_source_clean"])
    )
    return 0 if summary["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
