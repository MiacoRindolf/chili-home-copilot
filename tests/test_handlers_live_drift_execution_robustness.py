"""Tests for f-handler-live-drift + f-handler-execution-robustness
(Phase 6 BUNDLE of f-overnight-jumbo).

Both handlers subscribe to the same three trade-close events. Bundle-
shipped because the scaffolding is identical. Source-text guards pin
the wiring; no DB-dependent integration tests (added later if the
runtime profile shows specific contract concerns).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Module imports
# ---------------------------------------------------------------------------

def test_live_drift_handler_imports_cleanly():
    from app.services.trading.brain_work.handlers import live_drift
    for fn in (
        live_drift.handle_paper_trade_closed,
        live_drift.handle_live_trade_closed,
        live_drift.handle_broker_fill_closed,
    ):
        assert callable(fn)


def test_execution_robustness_handler_imports_cleanly():
    from app.services.trading.brain_work.handlers import execution_robustness
    for fn in (
        execution_robustness.handle_paper_trade_closed,
        execution_robustness.handle_live_trade_closed,
        execution_robustness.handle_broker_fill_closed,
    ):
        assert callable(fn)


# ---------------------------------------------------------------------------
# Absolute-imports guard (depth-5 risk class)
# ---------------------------------------------------------------------------

def test_live_drift_uses_absolute_imports():
    src = (REPO / "app/services/trading/brain_work/handlers/live_drift.py").read_text()
    assert "from app.db import SessionLocal" in src
    assert "from app.services.trading.live_drift import" in src
    assert "from ....db" not in src


def test_execution_robustness_uses_absolute_imports():
    src = (REPO / "app/services/trading/brain_work/handlers/execution_robustness.py").read_text()
    assert "from app.db import SessionLocal" in src
    assert "from app.services.trading.execution_robustness import" in src
    assert "from ....db" not in src


# ---------------------------------------------------------------------------
# Dispatcher wiring
# ---------------------------------------------------------------------------

def test_dispatcher_wires_both_handlers_in_close_branch():
    """Source guard: both handlers are dispatched in the close-event
    branch alongside pattern_stats / demote / regime_ledger."""
    src = (REPO / "app/services/trading/brain_work/dispatcher.py").read_text()
    # The fanout must reference both modules + import their three close-event
    # handler names.
    assert "from .handlers.live_drift import" in src
    assert "from .handlers.execution_robustness import" in src


def test_dispatcher_handlers_run_after_demote():
    """Source guard: live_drift + execution_robustness fire AFTER
    demote so the EV-gate has already run before drift/robustness see
    the close. They're observability, not lifecycle gates -- order
    correct prevents drift/robustness from racing the gate."""
    src = (REPO / "app/services/trading/brain_work/dispatcher.py").read_text()
    demote_pos = src.find("from .handlers.demote import handle_trade_closed")
    drift_pos = src.find("from .handlers.live_drift import")
    robustness_pos = src.find("from .handlers.execution_robustness import")
    assert demote_pos > 0
    assert drift_pos > 0
    assert robustness_pos > 0
    assert demote_pos < drift_pos, (
        "live_drift handler must dispatch AFTER demote"
    )
    assert demote_pos < robustness_pos, (
        "execution_robustness handler must dispatch AFTER demote"
    )


# ---------------------------------------------------------------------------
# Failure swallowing
# ---------------------------------------------------------------------------

def test_handlers_have_try_except_inside_helper():
    """Each handler's _run_refresh helper has try/except so a failed
    refresh can't propagate out of the handler boundary."""
    for path in (
        "app/services/trading/brain_work/handlers/live_drift.py",
        "app/services/trading/brain_work/handlers/execution_robustness.py",
    ):
        src = (REPO / path).read_text()
        assert "def _run_refresh(" in src
        idx = src.find("def _run_refresh(")
        body = src[idx:idx + 1200]
        assert "try:" in body
        assert "except Exception" in body
        assert "finally:" in body


# ---------------------------------------------------------------------------
# Config settings
# ---------------------------------------------------------------------------

def test_config_settings_added():
    from app.config import settings
    assert hasattr(settings, "brain_work_live_drift_batch_size")
    assert hasattr(settings, "brain_work_execution_robustness_batch_size")
