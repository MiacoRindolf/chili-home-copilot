from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_phase5_remaining_trade_refs.py"
MAP_PATH = REPO_ROOT / "docs" / "STRATEGY" / "phase5o_remaining_runtime_compat_map.json"

EXPECTED_PHASE5O_BUCKET_COUNTS = {
    "adapter_candidate": 9,
    "future_rename_blocker": 41,
    "leave_alone": 16,
}


def _load_analyzer():
    spec = importlib.util.spec_from_file_location("phase5_trade_refs", ANALYZER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_phase5o_map_covers_current_orm_compat_inventory() -> None:
    analyzer = _load_analyzer()
    report = analyzer.build_inventory(REPO_ROOT, include_dirs=("app",))
    current_paths = {
        entry["path"]
        for entry in report["entries"]
        if entry["bucket"] == "orm_trade_symbol_compat"
    }

    payload = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    mapped_paths = {item["path"] for item in payload["items"]}

    assert payload["orm_trade_symbol_compat_count"] == 66
    assert payload["phase5o_bucket_counts"] == EXPECTED_PHASE5O_BUCKET_COUNTS
    assert mapped_paths == current_paths


def test_phase5o_map_uses_only_supported_action_buckets() -> None:
    payload = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    allowed = set(EXPECTED_PHASE5O_BUCKET_COUNTS)

    for item in payload["items"]:
        assert item["phase5o_bucket"] in allowed
        assert item["contract_group"]
        assert item["phase5o_subtype"]
        assert item["rationale"]
