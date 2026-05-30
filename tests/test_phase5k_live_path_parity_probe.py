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
    assert "PARITY_GROUPS" in source
    assert "MISMATCH_GROUPS" in source

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


def test_phase5k_probe_enforces_read_only_transaction() -> None:
    module = _load_probe_module()

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str, *_args, **_kwargs) -> None:
            self.executed.append(sql)

        def fetchone(self):
            return ("on",)

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()
            self.session_args = None

        def set_session(self, **kwargs) -> None:
            self.session_args = kwargs

        def cursor(self):
            return self.cursor_obj

    conn = _Conn()

    module._enforce_read_only(conn)

    assert conn.session_args == {"readonly": True, "autocommit": False}
    assert conn.cursor_obj.executed == [
        "SET TRANSACTION READ ONLY",
        "SHOW transaction_read_only",
    ]


def test_phase5k_probe_promotion_realized_uses_option_aware_return_sql() -> None:
    module = _load_probe_module()

    sql, _params = module._query_for_check("promotion_realized", module.OLD_RELATION)

    assert "100.0" in sql
    assert "option_meta" in sql
    assert "asset_kind" in sql
    assert "NULLIF(entry_price * quantity, 0)" not in sql


def test_phase5k_probe_pattern_quality_matches_live_realized_scope() -> None:
    module = _load_probe_module()

    sql, _params = module._query_for_check("pattern_quality", module.NEW_RELATION)

    assert "scan_pattern_id != -1" in sql
    assert "pnl IS NOT NULL" in sql
    assert "entry_price > 0" in sql
    assert "quantity > 0" in sql
    assert "avg_return_fraction" in sql
    assert "total_pnl" in sql
    assert "100.0" in sql


def test_phase5k_probe_pdt_parity_is_total_and_user_scoped() -> None:
    module = _load_probe_module()

    sql, _params = module._query_for_check("pdt_day_trades", module.OLD_RELATION)

    assert "'all' AS scope" in sql
    assert "'user' AS scope" in sql
    assert "GROUP BY user_id" in sql
    assert "UNION ALL" in sql


def test_phase5k_probe_position_integrity_covers_full_invariant_groups() -> None:
    module = _load_probe_module()

    sql, _params = module._query_for_check("position_integrity_open", module.NEW_RELATION)

    for invariant in (
        "open_positions_without_open_trade",
        "open_trades_without_open_position",
        "open_positions_missing_current_envelope",
        "current_envelope_mismatches",
        "repairable_current_envelope_links",
    ):
        assert invariant in sql


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
