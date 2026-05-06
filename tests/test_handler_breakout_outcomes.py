"""Tests for f-handler-breakout-outcomes (Phase 3 of f-overnight-jumbo).

Wires learn_from_breakout_outcomes into an event handler subscribed to
breakout_alert_resolved events. Source-text guards keep the wiring
intact; integration test exercises the handler with the real function.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_handler_module_imports_cleanly():
    from app.services.trading.brain_work.handlers import breakout_outcomes
    assert callable(breakout_outcomes.handle_breakout_alert_resolved)


def test_handler_uses_absolute_imports():
    """Source guard: handlers/X.py at depth 5 must use absolute
    imports (the f-handler-pattern-stats finding -- 4-dot relative
    resolves to nonexistent app.services.X)."""
    src = (REPO / "app/services/trading/brain_work/handlers/breakout_outcomes.py").read_text()
    assert "from app.db import SessionLocal" in src
    assert "from app.services.trading.learning import" in src
    # Anti-pattern check: 4-dot relative imports must NOT appear.
    assert "from ....db" not in src
    assert "from ....services" not in src


def test_handler_swallows_exceptions(caplog):
    """A broken learn_from_breakout_outcomes must not propagate out
    of the handler (would mark the event as failed and retry forever)."""
    import logging
    from app.services.trading.brain_work.handlers import breakout_outcomes

    def _boom(sess, user_id):
        raise RuntimeError("simulated learn_from_breakout_outcomes failure")

    with patch(
        "app.services.trading.learning.learn_from_breakout_outcomes",
        side_effect=_boom,
    ), patch("app.db.SessionLocal", return_value=type("FakeSess", (), {
        "close": lambda self: None,
    })()):
        ev = SimpleNamespace(id=1, payload={})
        # Must NOT raise.
        breakout_outcomes.handle_breakout_alert_resolved(
            None, ev, user_id=None,
        )

    # Verify the failure was logged.
    assert any(
        "breakout_outcomes" in (rec.name or "") and rec.levelname in ("ERROR",)
        for rec in caplog.records
    ) or any(
        "simulated learn_from_breakout_outcomes failure" in rec.message
        for rec in caplog.records
    )


def test_dispatcher_wires_breakout_alert_resolved():
    """Source guard: dispatcher.py must include the
    breakout_alert_resolved branch + the handler-import line."""
    src = (REPO / "app/services/trading/brain_work/dispatcher.py").read_text()
    assert '"breakout_alert_resolved"' in src
    assert "handle_breakout_alert_resolved" in src
    # Wire into the _dispatch_limits return list too.
    assert '("breakout_alert_resolved", max(0, bo))' in src


def test_emitter_exists_with_expected_signature():
    from app.services.trading.brain_work.emitters import (
        emit_breakout_alert_resolved_outcome,
    )
    import inspect
    sig = inspect.signature(emit_breakout_alert_resolved_outcome)
    params = set(sig.parameters)
    assert "alert_id" in params
    assert "scan_pattern_id" in params
    assert "ticker" in params
    assert "outcome" in params


def test_scheduler_emits_on_alert_resolution():
    """Source guard: trading_scheduler.py's breakout-outcome-check
    must call emit_breakout_alert_resolved_outcome for newly-resolved
    alerts (winner/loser/fakeout) AND for stale auto-expired alerts."""
    src = (REPO / "app/services/trading_scheduler.py").read_text()
    assert "emit_breakout_alert_resolved_outcome" in src
    # Must reference the function in BOTH the resolved-pending loop
    # AND the stale-expired loop.
    assert src.count("emit_breakout_alert_resolved_outcome(") >= 2


def test_config_setting_added():
    from app.config import settings
    assert hasattr(settings, "brain_work_breakout_outcomes_batch_size")
    assert isinstance(settings.brain_work_breakout_outcomes_batch_size, int)
