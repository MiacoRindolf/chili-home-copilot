from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "analyze_phase5_remaining_trade_refs.py"


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

    assert module.classify_path("app/services/trading/attribution_service.py").bucket == (
        "evidence_model_capital_reader"
    )
    assert module.classify_path("app/services/trading/venue/coinbase_orphan_adopt.py").bucket == (
        "live_writer_order_broker_reconcile"
    )
    assert module.classify_path("tests/test_position_identity.py").bucket == (
        "compatibility_migration_test_history"
    )
    assert module.classify_path("scripts/analyze_phase5_remaining_trade_refs.py").bucket == (
        "compatibility_migration_test_history"
    )
    assert module.classify_path("app/services/trading/new_reader.py").bucket == (
        "unclassified_trade_surface_reference"
    )


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

    assert report["ok"] is True
    assert report["file_count"] == 2
    assert report["buckets"] == {
        "evidence_model_capital_reader": 1,
        "unclassified_trade_surface_reference": 1,
    }
    assert [entry["path"] for entry in report["entries"]] == [
        "app/services/trading/attribution_service.py",
        "app/services/trading/new_reader.py",
    ]


def test_live_order_paths_are_kept_distinct_from_analytics_readers(tmp_path: Path) -> None:
    module = _load_module()
    venue_dir = tmp_path / "app" / "services" / "trading" / "venue"
    venue_dir.mkdir(parents=True)
    (venue_dir / "coinbase_orphan_adopt.py").write_text(
        "JOIN trading_trades tt ON tt.id = order_id\n", encoding="utf-8"
    )

    report = module.build_inventory(tmp_path, include_dirs=("app",))

    assert report["buckets"] == {"live_writer_order_broker_reconcile": 1}
    assert report["entries"][0]["owner"] == "Algo Trader Architect / Risk"
