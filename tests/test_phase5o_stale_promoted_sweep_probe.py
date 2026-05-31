from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-stale-promoted-sweep-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_stale_promoted_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_probe_requires_test_db_or_explicit_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)
    module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili_test")

    try:
        module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili")
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("live DB probe should require explicit opt-in")

    monkeypatch.setenv(module.LIVE_PROBE_OPT_IN, "true")
    module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili")


def test_relation_sql_allowlist() -> None:
    module = _load_module()

    assert module._relation_sql(module.LEGACY_TRADES_COMPAT_RELATION) == "trading_trades"
    assert module._relation_sql(module.MANAGEMENT_ENVELOPES_RELATION) == "trading_management_envelopes"

    try:
        module._relation_sql("trading_decisions")
    except ValueError as exc:
        assert "unsupported relation" in str(exc)
    else:
        raise AssertionError("unexpected relation should fail closed")


def test_stale_candidates_use_cutoff_and_missing_latest_exit() -> None:
    module = _load_module()

    cutoff = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    latest_by_pattern = {
        1: "2026-05-31T12:00:00",
        2: "2026-05-31T13:00:00",
        3: "2026-05-01T12:00:00",
    }

    assert module._stale_candidates(
        latest_by_pattern,
        pattern_ids=[1, 2, 3, 4],
        stale_cutoff=cutoff,
    ) == [3, 4]


def test_latest_exit_map_skips_null_pattern_ids() -> None:
    module = _load_module()

    rows = [
        {"scan_pattern_id": 7, "latest_exit": "2026-05-31T12:00:00"},
        {"scan_pattern_id": None, "latest_exit": "2026-05-31T13:00:00"},
    ]

    assert module._latest_exit_map(rows) == {7: "2026-05-31T12:00:00"}
