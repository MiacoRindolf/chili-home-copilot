import importlib.util
from pathlib import Path


def load_audit_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "d-brain-pipeline-audit.py"
    spec = importlib.util.spec_from_file_location("d_brain_pipeline_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_pattern_projection_avoids_removed_columns():
    module = load_audit_module()
    columns = {
        "id",
        "name",
        "lifecycle_stage",
        "promotion_status",
        "active",
        "win_rate",
        "evidence_count",
        "trade_count",
        "updated_at",
        "avg_return_pct",
    }

    projection = module.scan_pattern_projection(columns)

    assert projection["name_expr"] == "name"
    assert projection["sample_expr"] == "COALESCE(evidence_count, trade_count, 0)"
    assert projection["last_expr"] == "updated_at"
    assert projection["avg_return_expr"] == "avg_return_pct"
    assert "ticker" not in projection.values()
    assert "sample_size" not in projection.values()
    assert "last_validated_at" not in projection.values()


def test_scan_pattern_projection_falls_back_when_optional_columns_absent():
    module = load_audit_module()

    projection = module.scan_pattern_projection({"id", "created_at"})

    assert projection["name_expr"] == "id::text"
    assert projection["scope_expr"] == "'?'"
    assert projection["sample_expr"] == "0"
    assert projection["last_expr"] == "created_at"
    assert projection["avg_return_expr"] == "NULL"


def test_activity_time_column_prefers_current_schema_columns(monkeypatch):
    module = load_audit_module()

    monkeypatch.setattr(
        module,
        "table_columns",
        lambda table_name: {"id", "snapshot_date", "decided_at"},
    )

    assert (
        module.activity_time_column(
            "pattern_survival_features",
            ("created_at", "snapshot_date", "promoted_at"),
        )
        == "snapshot_date"
    )
