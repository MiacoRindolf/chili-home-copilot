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
