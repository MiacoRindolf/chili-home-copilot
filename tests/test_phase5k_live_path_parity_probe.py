from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "d-phase5k-live-path-parity-probe.py"


def _load_probe_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase5k_probe", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_phase5k_probe_is_read_only_and_intentionally_compares_both_relations() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "trading_trades" in source
    assert "trading_management_envelopes" in source
    assert "psycopg2.connect" in source

    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|VACUUM|ANALYZE)\b",
        re.IGNORECASE,
    )
    assert forbidden.search(source) is None


def test_phase5k_probe_has_expected_live_path_checks() -> None:
    module = _load_probe_module()

    assert module.CHECKS == (
        "coinbase_cap",
        "pdt_day_trades",
        "promotion_realized",
        "pattern_quality",
        "portfolio_risk_open",
        "position_integrity_open",
    )


def test_phase5k_probe_detects_old_new_row_mismatch(monkeypatch) -> None:
    module = _load_probe_module()

    calls: list[tuple[str, str]] = []

    def fake_fetch_all(_conn, sql, _params=()):
        relation = (
            module.NEW_RELATION
            if module.NEW_RELATION in sql
            else module.OLD_RELATION
        )
        calls.append(("new" if relation == module.NEW_RELATION else "old", sql))
        if relation == module.NEW_RELATION:
            return [{"open_count": 2, "open_notional": "20"}]
        return [{"open_count": 1, "open_notional": "10"}]

    monkeypatch.setattr(module, "_fetch_all", fake_fetch_all)

    result = module._run_check(object(), "coinbase_cap")

    assert result["matched"] is False
    assert result["old_rows"] == [{"open_count": 1, "open_notional": "10"}]
    assert result["new_rows"] == [{"open_count": 2, "open_notional": "20"}]
    assert [kind for kind, _sql in calls] == ["old", "new"]
