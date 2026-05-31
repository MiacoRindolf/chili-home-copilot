from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ah-trades-api-cutover-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ah_trades_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tie_order_acceptance_requires_same_rows_and_same_entry_date() -> None:
    module = _load_module()

    assert module._order_diff_is_only_within_equal_entry_dates(
        [
            {"id": 1, "entry_date": "2026-05-31T01:00:00", "ticker": "A"},
            {"id": 2, "entry_date": "2026-05-31T01:00:00", "ticker": "B"},
        ],
        [
            {"id": 2, "entry_date": "2026-05-31T01:00:00", "ticker": "B"},
            {"id": 1, "entry_date": "2026-05-31T01:00:00", "ticker": "A"},
        ],
    ) is True

    assert module._order_diff_is_only_within_equal_entry_dates(
        [
            {"id": 1, "entry_date": "2026-05-31T01:00:00", "ticker": "A"},
            {"id": 2, "entry_date": "2026-05-31T02:00:00", "ticker": "B"},
        ],
        [
            {"id": 2, "entry_date": "2026-05-31T02:00:00", "ticker": "B"},
            {"id": 1, "entry_date": "2026-05-31T01:00:00", "ticker": "A"},
        ],
    ) is False


def test_probe_rejects_non_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    try:
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("expected live database to require explicit opt-in")
