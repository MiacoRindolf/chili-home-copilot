from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5o-learning-envelope-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_learning_probe", SCRIPT_PATH)
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


def test_coverage_pct_matches_learning_contract() -> None:
    module = _load_module()

    assert module._coverage_pct(0, 0) is None
    assert module._coverage_pct(4, 1) == 25.0
    assert module._coverage_pct(3, 2) == 66.67


def test_scope_values_compare_expected_learning_scopes(monkeypatch) -> None:
    module = _load_module()
    calls: list[str] = []

    def fake_actual(_db, *, relation_name):
        calls.append(f"actual:{relation_name}")
        return []

    def fake_coverage(_db, *, relation_name):
        calls.append(f"coverage:{relation_name}")
        return []

    def fake_closed(_db, *, relation_name):
        calls.append(f"closed:{relation_name}")
        return []

    def fake_evidence_rows(_db, *, relation_name, cutoff):
        calls.append(f"evidence_rows:{relation_name}:{cutoff.year}")
        return []

    def fake_buckets(_db, *, relation_name, cutoff):
        calls.append(f"buckets:{relation_name}:{cutoff.year}")
        return []

    def fake_vitals(_db, *, relation_name, cutoff):
        calls.append(f"vitals:{relation_name}:{cutoff.year}")
        return []

    monkeypatch.setattr(module, "_actual_trade_count_by_pattern", fake_actual)
    monkeypatch.setattr(module, "_attribution_coverage_by_user", fake_coverage)
    monkeypatch.setattr(module, "_closed_trade_analysis_rows", fake_closed)
    monkeypatch.setattr(module, "_evidence_correction_closed_rows", fake_evidence_rows)
    monkeypatch.setattr(module, "_evidence_pattern_buckets", fake_buckets)
    monkeypatch.setattr(module, "_setup_vitals_closed_join", fake_vitals)

    values = module._scope_values(
        object(),
        relation_name=module.LEGACY_TRADES_COMPAT_RELATION,
        cutoff=module.datetime(2026, 5, 31),
    )

    assert sorted(values) == [
        "actual_trade_count_by_pattern",
        "attribution_coverage_by_user",
        "closed_trade_analysis_rows",
        "evidence_correction_closed_rows",
        "evidence_pattern_buckets",
        "setup_vitals_closed_join",
    ]
    assert calls == [
        "actual:trading_trades",
        "coverage:trading_trades",
        "closed:trading_trades",
        "evidence_rows:trading_trades:2026",
        "buckets:trading_trades:2026",
        "vitals:trading_trades:2026",
    ]
