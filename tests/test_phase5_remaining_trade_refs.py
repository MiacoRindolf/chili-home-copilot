from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "analyze_phase5_remaining_trade_refs.py"


EXPECTED_RUNTIME_COMPAT_WRITER_PATHS = [
    "app/services/coinbase_service.py",
    "app/services/trading/auto_trader.py",
    "app/services/trading/bracket_reconciliation_service.py",
    "app/services/trading/options/exit_monitor.py",
]

EXPECTED_RUNTIME_COMPAT_RELATION_SYMBOL_PATHS = [
    "app/models/trade_relation_symbols.py",
]

EXPECTED_ORM_CONTRACT_GROUP_COUNTS = {
    "learning_research_reporting": 39,
    "live_action_broker_reconcile": 15,
    "private_helper_type_only": 7,
    "public_ui_schema_contract": 14,
    "risk_capital_gate": 18,
}

EXPECTED_ORM_CONTRACT_GROUP_REPRESENTATIVES = {
    "learning_research_reporting": [
        "app/services/backtest_service.py",
        "app/services/trading/edge_reliability.py",
        "app/services/trading/learning.py",
        "app/services/trading/net_edge_ranker.py",
        "app/services/trading/pattern_imminent_alerts.py",
    ],
    "live_action_broker_reconcile": [
        "app/services/broker_service.py",
        "app/services/trading/bracket_intent_writer.py",
        "app/services/trading/crypto/exit_monitor.py",
        "app/services/trading/position_integrity.py",
        "app/services/trading/robinhood_exit_execution.py",
    ],
    "private_helper_type_only": [
        "app/models/trading.py",
        "app/services/trading/__init__.py",
        "app/services/trading/auto_trader_position_overrides.py",
        "app/services/trading/autopilot_scope.py",
        "app/services/trading/autotrader_desk.py",
    ],
    "public_ui_schema_contract": [
        "app/routers/trading.py",
        "app/routers/trading_sub/trades.py",
        "app/schemas/trading.py",
        "app/static/js/brain-trading-desk.js",
        "app/templates/trading/_tab_trades.html",
    ],
    "risk_capital_gate": [
        "app/services/trading/auto_trader_rules.py",
        "app/services/trading/cash_deployment.py",
        "app/services/trading/emergency_liquidation.py",
        "app/services/trading/options/portfolio_budget.py",
        "app/services/trading/portfolio_risk.py",
    ],
}


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5_trade_refs", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classifies_known_reference_owners() -> None:
    module = _load_module()

    assert module.classify_reference_contract(
        "app/services/trading/new_reader.py",
        {
            "raw_readers": ["FROM trading_trades"],
            "raw_mutations": [],
            "table_symbols": [],
            "model_symbols": [],
        },
    ).bucket == "unexpected_runtime_reader"
    assert module.classify_reference_contract(
        "app/services/trading/auto_trader.py",
        {
            "raw_readers": [],
            "raw_mutations": ["UPDATE trading_trades"],
            "table_symbols": [],
            "model_symbols": [],
        },
    ).bucket == (
        "allowed_compatibility_writer_update"
    )
    assert module.classify_reference_contract(
        "app/services/trading/venue/coinbase_orphan_adopt.py",
        {
            "raw_readers": [],
            "raw_mutations": [],
            "table_symbols": [],
            "model_symbols": ["Trade"],
        },
    ).bucket == (
        "orm_trade_symbol_compat"
    )
    assert module.classify_path("tests/test_position_identity.py").bucket == (
        "compatibility_migration_test_history"
    )
    assert module.classify_path("docs/RUNBOOKS/phase5.md").bucket == "docs_runbooks"
    assert module.classify_path("scripts/analyze_phase5_remaining_trade_refs.py").bucket == (
        "compatibility_migration_test_history"
    )
    assert module.classify_path("app/services/trading/new_reader.py").bucket == (
        "unclassified_trade_surface_reference"
    )


def test_sql_reference_matching_ignores_slash_shorthand_and_detects_schema_targets() -> None:
    module = _load_module()

    assert module._reference_matches(
        "Runtime app code must not add raw FROM/JOIN trading_trades readers"
    )["raw_readers"] == []
    assert module._reference_matches(
        "SELECT * FROM public.trading_trades WHERE id = :id"
    )["raw_readers"] == ["FROM public.trading_trades"]
    assert module._reference_matches(
        "UPDATE ONLY trading_trades SET status = 'closed'"
    )["raw_mutations"] == ["UPDATE ONLY trading_trades"]


def test_runtime_app_compatibility_contract_surface_is_pinned() -> None:
    module = _load_module()

    report = module.build_inventory(REPO_ROOT, include_dirs=("app",))
    writer_paths = sorted(
        entry["path"]
        for entry in report["entries"]
        if entry["bucket"] == "allowed_compatibility_writer_update"
    )
    relation_symbol_paths = sorted(
        entry["path"]
        for entry in report["entries"]
        if entry["bucket"] == "compatibility_relation_symbol"
    )
    raw_reader_history_paths = sorted(
        entry["path"]
        for entry in report["entries"]
        if entry["raw_sql_references"]
    )

    assert report["ok"] is True
    assert report["unexpected_runtime_readers"] == []
    assert report["unexpected_runtime_mutations"] == []
    assert writer_paths == EXPECTED_RUNTIME_COMPAT_WRITER_PATHS
    assert relation_symbol_paths == EXPECTED_RUNTIME_COMPAT_RELATION_SYMBOL_PATHS
    assert raw_reader_history_paths == ["app/migrations.py"]


def test_groups_legacy_trade_orm_symbols_by_contract() -> None:
    module = _load_module()

    assert module.classify_orm_contract_group("app/routers/trading_sub/trades.py") == (
        "public_ui_schema_contract"
    )
    assert module.classify_orm_contract_group("app/services/broker_service.py") == (
        "live_action_broker_reconcile"
    )
    assert module.classify_orm_contract_group("app/services/trading/compliance.py") == (
        "risk_capital_gate"
    )
    assert module.classify_orm_contract_group("app/services/trading/learning.py") == (
        "learning_research_reporting"
    )
    assert module.classify_orm_contract_group("app/services/trading/autopilot_scope.py") == (
        "private_helper_type_only"
    )


def test_runtime_orm_symbol_contract_groups_are_pinned() -> None:
    module = _load_module()

    report = module.build_inventory(REPO_ROOT, include_dirs=("app",))
    grouped_paths: dict[str, list[str]] = {}
    for entry in report["entries"]:
        if entry["bucket"] != "orm_trade_symbol_compat":
            continue
        group = entry["contract_group"]
        assert group is not None
        grouped_paths.setdefault(group, []).append(entry["path"])

    assert report["orm_contract_groups"] == EXPECTED_ORM_CONTRACT_GROUP_COUNTS
    assert sum(report["orm_contract_groups"].values()) == 93
    for group, representative_paths in EXPECTED_ORM_CONTRACT_GROUP_REPRESENTATIVES.items():
        assert set(representative_paths).issubset(set(grouped_paths[group]))


def test_build_inventory_scans_sources_and_skips_workspace_noise(tmp_path: Path) -> None:
    module = _load_module()
    app_dir = tmp_path / "app" / "services" / "trading"
    app_dir.mkdir(parents=True)
    (app_dir / "attribution_service.py").write_text(
        "SELECT * FROM trading_trades WHERE status = 'closed'\n", encoding="utf-8"
    )
    (app_dir / "new_reader.py").write_text("Trade.query.filter_by(id=1)\n", encoding="utf-8")
    project_ws_dir = tmp_path / "project_ws" / "Risk"
    project_ws_dir.mkdir(parents=True)
    (project_ws_dir / "noise.py").write_text("FROM trading_trades\n", encoding="utf-8")

    report = module.build_inventory(tmp_path, include_dirs=("app", "project_ws"))

    assert report["ok"] is False
    assert report["file_count"] == 2
    assert report["raw_sql_file_count"] == 1
    assert report["raw_reader_buckets"] == {"unexpected_runtime_reader": 1}
    assert report["buckets"] == {
        "orm_trade_symbol_compat": 1,
        "unexpected_runtime_reader": 1,
    }
    assert report["unexpected_runtime_readers"] == [
        "app/services/trading/attribution_service.py"
    ]
    assert [entry["path"] for entry in report["entries"]] == [
        "app/services/trading/attribution_service.py",
        "app/services/trading/new_reader.py",
    ]
    assert [entry["reference_kind"] for entry in report["entries"]] == [
        "raw_sql_reader",
        "orm_symbol",
    ]


def test_live_order_paths_are_kept_distinct_from_analytics_readers(tmp_path: Path) -> None:
    module = _load_module()
    venue_dir = tmp_path / "app" / "services" / "trading" / "venue"
    venue_dir.mkdir(parents=True)
    (venue_dir / "coinbase_orphan_adopt.py").write_text(
        "JOIN trading_trades tt ON tt.id = order_id\n", encoding="utf-8"
    )

    report = module.build_inventory(tmp_path, include_dirs=("app",))

    assert report["ok"] is False
    assert report["buckets"] == {"unexpected_runtime_reader": 1}
    assert report["raw_reader_buckets"] == {"unexpected_runtime_reader": 1}
    assert report["raw_sql_file_count"] == 1
    assert report["unexpected_runtime_readers"] == [
        "app/services/trading/venue/coinbase_orphan_adopt.py"
    ]


def test_writer_updates_are_allowed_when_owned_by_live_broker_path(tmp_path: Path) -> None:
    module = _load_module()
    trading_dir = tmp_path / "app" / "services" / "trading"
    trading_dir.mkdir(parents=True)
    (trading_dir / "auto_trader.py").write_text(
        "UPDATE trading_trades SET status = 'open' WHERE id = :id\n",
        encoding="utf-8",
    )

    report = module.build_inventory(tmp_path, include_dirs=("app",))

    assert report["ok"] is True
    assert report["buckets"] == {"allowed_compatibility_writer_update": 1}
    assert report["entries"][0]["reference_kind"] == "raw_sql_mutation"
    assert report["entries"][0]["raw_mutation_references"] == ["UPDATE trading_trades"]


def test_raw_sql_only_filters_symbol_and_doc_references(tmp_path: Path) -> None:
    module = _load_module()
    app_dir = tmp_path / "app" / "services" / "trading"
    app_dir.mkdir(parents=True)
    (app_dir / "doc_only.py").write_text(
        '"""Mentions trading_trades and Trade without reading it."""\n',
        encoding="utf-8",
    )
    (app_dir / "pattern_regime_ledger.py").write_text(
        "SELECT * FROM trading_trades t WHERE t.status = 'closed'\n",
        encoding="utf-8",
    )

    report = module.build_inventory(tmp_path, include_dirs=("app",), raw_sql_only=True)

    assert report["file_count"] == 1
    assert report["raw_sql_file_count"] == 1
    assert report["entries"][0]["path"] == "app/services/trading/pattern_regime_ledger.py"
    assert report["entries"][0]["reference_kind"] == "raw_sql_reader"
    assert report["entries"][0]["raw_sql_references"] == ["FROM trading_trades"]


def test_bucket_filter_keeps_global_verdict_and_narrows_entries(tmp_path: Path) -> None:
    module = _load_module()
    app_dir = tmp_path / "app" / "services" / "trading"
    app_dir.mkdir(parents=True)
    (app_dir / "reader.py").write_text(
        "SELECT * FROM trading_trades WHERE id = :id\n", encoding="utf-8"
    )
    (app_dir / "symbol.py").write_text("from app.models.trading import Trade\n", encoding="utf-8")

    report = module.build_inventory(tmp_path, include_dirs=("app",))
    narrowed = module.filter_inventory_by_bucket(report, "orm_trade_symbol_compat")

    assert report["ok"] is False
    assert narrowed["ok"] is False
    assert narrowed["file_count"] == 1
    assert narrowed["buckets"] == {"orm_trade_symbol_compat": 1}
    assert [entry["path"] for entry in narrowed["entries"]] == [
        "app/services/trading/symbol.py"
    ]
