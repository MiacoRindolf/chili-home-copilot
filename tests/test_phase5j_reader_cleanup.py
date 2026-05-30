from __future__ import annotations

import inspect
from pathlib import Path

from app.routers import brain


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_brain_health_kpi_uses_management_envelope_reader() -> None:
    """Phase 5J: KPI analytics should read the semantic envelope surface."""

    source = inspect.getsource(brain.api_brain_health_kpi)

    assert "FROM trading_management_envelopes" in source
    assert "FROM trading_trades" not in source


def test_phase5j_probe_scripts_use_management_envelope_reader() -> None:
    for relative_path in (
        "app/services/trading/management_envelopes.py",
        "scripts/d-cb-phase6-soak-probe.py",
        "scripts/d-maker-only-tca-probe.py",
        "scripts/d-imminent-silence-audit.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "FROM trading_management_envelopes" in source
        assert "FROM trading_trades" not in source


def test_phase5j_slice2_analytics_use_management_envelope_reader() -> None:
    for relative_path in (
        "app/services/trading/decision_packet_coverage.py",
        "app/services/trading/divergence_service.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "trading_management_envelopes" in source
        assert "FROM trading_trades" not in source
        assert "JOIN trading_trades" not in source


def test_phase5j_slice3_learning_readers_use_management_envelopes() -> None:
    for relative_path in (
        "app/services/trading/dynamic_priors.py",
        "app/services/trading/ticker_scope_autotune.py",
        "app/services/trading/pattern_stats_recompute.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "trading_management_envelopes" in source
        assert "FROM trading_trades" not in source
        assert "JOIN trading_trades" not in source


def test_phase5j_slice4_realized_and_sizing_readers_use_management_envelopes() -> None:
    for relative_path in (
        "app/services/trading/realized_stats_sync.py",
        "app/services/trading/hrp_sizing.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "trading_management_envelopes" in source
        assert "FROM trading_trades" not in source
        assert "JOIN trading_trades" not in source


def test_phase5j_slice5_routes_and_watchers_use_management_envelopes() -> None:
    for relative_path in (
        "app/routers/admin.py",
        "app/routers/trading_sub/ai.py",
        "app/services/trading/brain_work/handlers/quality_score.py",
        "scripts/d-pid537-watcher.py",
        "scripts/walkforward_monthly_dd_breaker.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "trading_management_envelopes" in source
        assert "FROM trading_trades" not in source
        assert "JOIN trading_trades" not in source
