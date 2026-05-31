from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5o-live-drift-envelope-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_live_drift_probe", SCRIPT_PATH)
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


def test_bind_int_list_uses_named_parameters() -> None:
    module = _load_module()

    sql, params = module._bind_int_list("sp", [5, 8])

    assert sql == ":sp_0, :sp_1"
    assert params == {"sp_0": 5, "sp_1": 8}


def test_probe_user_id_prefers_explicit_override(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setenv(module.PROBE_USER_ID_ENV, "42")

    assert module._probe_user_id() == 42


def test_scope_values_compares_live_drift_inputs(monkeypatch) -> None:
    module = _load_module()
    calls: list[str] = []

    def fake_runtime(_db, *, relation_name, scan_pattern_ids, user_id, since):
        calls.append(f"runtime:{relation_name}:{scan_pattern_ids}:{user_id}:{since.year}")
        return []

    def fake_slippage(_db, *, relation_name, scan_pattern_ids, user_id, since):
        calls.append(f"slippage:{relation_name}:{scan_pattern_ids}:{user_id}:{since.year}")
        return []

    def fake_wins(_db, *, relation_name, scan_pattern_ids, user_id, since):
        calls.append(f"wins:{relation_name}:{scan_pattern_ids}:{user_id}:{since.year}")
        return []

    monkeypatch.setattr(module, "_live_runtime_rows", fake_runtime)
    monkeypatch.setattr(module, "_slippage_inputs", fake_slippage)
    monkeypatch.setattr(module, "_live_win_counts", fake_wins)

    values = module._scope_values(
        object(),
        relation_name=module.LEGACY_TRADES_COMPAT_RELATION,
        scan_pattern_ids=[1, 2],
        user_id=7,
        since=module.datetime(2026, 5, 31),
    )

    assert sorted(values) == [
        "live_runtime_rows",
        "live_slippage_inputs",
        "live_win_counts",
    ]
    assert calls == [
        "runtime:trading_trades:[1, 2]:7:2026",
        "slippage:trading_trades:[1, 2]:7:2026",
        "wins:trading_trades:[1, 2]:7:2026",
    ]
