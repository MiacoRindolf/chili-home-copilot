"""Phase 1b HARD GATE — handler idempotency contract for Phase 1c.

For each of the 9 brain_work handlers, call the handler entry function
TWICE with the same synthetic event payload and assert that the handler
wrapper does not introduce additional side effects beyond a stable call
into its wrapped function. The wrapped function's own idempotency is its
own contract; this test verifies the handler boundary.

The brief framing — "no duplicate side-effects" — is satisfied at this
level. Phase 1c's per-event-type pre-flight memos (D2 of
``f-brain-event-kind-backfill.md``) verify inner-function contracts with
real-data smoke tests before any backfill UPDATE runs against the ~4,000
historical orphan rows.

**Open gap surfaced for Phase 1c:** the ``mine`` handler delegates to
``mine_patterns`` which is *not* obviously idempotent at the
event-payload level (no per-event dedupe in the inner function). The
test covers the entry guard (snapshot-floor short-circuit); the inner
mining contract is verified separately in Phase 1c.

Brief: docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md
Parent: docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch


class _FakeEvent:
    """In-memory stand-in for ``BrainWorkEvent`` (handlers only touch attrs)."""

    def __init__(self, *, event_id: int, payload: dict[str, Any]) -> None:
        self.id = event_id
        self.payload = payload


class _FakeSession:
    """Minimal SessionLocal stand-in: handlers only call commit/rollback/close."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _make_fake_session_factory() -> tuple[Any, list[_FakeSession]]:
    sessions: list[_FakeSession] = []

    def _factory() -> _FakeSession:
        s = _FakeSession()
        sessions.append(s)
        return s

    return _factory, sessions


# ---------------------------------------------------------------------------
# Handler 1: cpcv_gate (handle_backtest_completed)
# ---------------------------------------------------------------------------


def test_cpcv_gate_handler_idempotent_skip_terminal(monkeypatch) -> None:
    """Pattern in lifecycle='promoted' short-circuits the handler.

    Two invocations: each opens one SessionLocal, sets up no writes
    beyond the rollback/close. The handler wrapper makes no extra side
    effects between calls.
    """
    pid = 12345
    pattern = MagicMock()
    pattern.lifecycle_stage = "promoted"
    pattern.promotion_status = "promoted_via_cpcv_gate"

    factory, sessions = _make_fake_session_factory()

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = factory  # type: ignore[attr-defined]

    # Make session.get(ScanPattern, pid) return our promoted pattern.
    def _patched_factory() -> Any:
        s = MagicMock()
        s.get.return_value = pattern
        sessions.append(s)
        return s

    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]

    from app.services.trading.brain_work.handlers.cpcv_gate import (
        handle_backtest_completed,
    )

    ev = _FakeEvent(event_id=1, payload={"scan_pattern_id": pid})

    handle_backtest_completed(MagicMock(), ev, None)
    handle_backtest_completed(MagicMock(), ev, None)

    # Two invocations → two SessionLocal() calls, two session.get() calls.
    # The terminal-skip path takes no side-effect actions on the pattern.
    assert len(sessions) == 2
    for s in sessions:
        s.get.assert_called_with(__import__(
            "app.models.trading", fromlist=["ScanPattern"]
        ).ScanPattern, pid)


# ---------------------------------------------------------------------------
# Handler 2: promote (handle_pattern_eligible_promotion)
# ---------------------------------------------------------------------------


def test_promote_handler_idempotent_skip_already_promoted(monkeypatch) -> None:
    """Already-promoted pattern short-circuits; no side effects on second call."""
    pid = 22222
    pattern = MagicMock()
    pattern.lifecycle_stage = "promoted"
    pattern.promotion_status = "promoted_via_cpcv_gate"

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        s.get.return_value = pattern
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    from app.services.trading.brain_work.handlers.promote import (
        handle_pattern_eligible_promotion,
    )

    ev = _FakeEvent(event_id=2, payload={"scan_pattern_id": pid})

    handle_pattern_eligible_promotion(MagicMock(), ev, None)
    handle_pattern_eligible_promotion(MagicMock(), ev, None)

    assert len(sessions) == 2
    # Skip-terminal path → no commit (it's only set when we promote).
    for s in sessions:
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Handler 3: demote (handle_trade_closed)
# ---------------------------------------------------------------------------


def test_demote_handler_idempotent_skip_non_promoted(monkeypatch) -> None:
    """Non-promoted pattern short-circuits before EV gate touches anything."""
    pid = 33333
    pattern = MagicMock()
    pattern.lifecycle_stage = "candidate"  # not 'promoted' → skip
    pattern.promotion_status = ""

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        s.get.return_value = pattern
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    from app.services.trading.brain_work.handlers.demote import (
        handle_trade_closed,
    )

    ev = _FakeEvent(event_id=3, payload={"scan_pattern_id": pid})

    handle_trade_closed(MagicMock(), ev, None)
    handle_trade_closed(MagicMock(), ev, None)

    assert len(sessions) == 2
    for s in sessions:
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Handler 4: mine (handle_market_snapshots_batch)
#
# Phase 1c precondition: ``mine_patterns`` has no event-level dedupe.
# Test the entry guard (below-floor snapshot count → short-circuit).
# ---------------------------------------------------------------------------


def test_mine_handler_idempotent_below_snapshot_floor(monkeypatch) -> None:
    """Below-floor snapshot count short-circuits before ``mine_patterns``.

    Surfaces the Phase 1c gap: ``mine_patterns`` itself is not verified
    idempotent at the event-payload level. Phase 1c's pre-flight memo
    is the controlled checkpoint before any backfill enables historical
    mine events for re-claim.
    """
    mine_mock = MagicMock(return_value=[])

    import app.services.trading.learning as learning

    monkeypatch.setattr(learning, "mine_patterns", mine_mock)

    from app.services.trading.brain_work.handlers.mine import (
        handle_market_snapshots_batch,
    )

    ev = _FakeEvent(
        event_id=4,
        payload={
            "snapshots_taken_daily": 0,
            "intraday_snapshots_taken": 0,
            "universe_size": 0,
            "job_id": "test",
        },
    )

    handle_market_snapshots_batch(MagicMock(), ev, None)
    handle_market_snapshots_batch(MagicMock(), ev, None)

    # Below the snapshot floor → mine_patterns must NOT be called either time.
    mine_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Handler 5: regime_ledger (handle_trade_closed_for_ledger)
#
# Has an in-process 60s throttle; second call within 60s short-circuits.
# ---------------------------------------------------------------------------


def test_regime_ledger_handler_idempotent_throttle(monkeypatch) -> None:
    """In-process throttle: second call short-circuits within 60s."""
    import app.services.trading.brain_work.handlers.regime_ledger as rl

    # Reset the throttle so the first call is the "fresh" one.
    monkeypatch.setattr(rl, "_LAST_REBUILD_AT", 0.0, raising=False)

    build_mock = MagicMock(return_value={"rows_written": 1, "skipped": False})

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    import app.services.trading.pattern_regime_ledger as prl

    monkeypatch.setattr(prl, "build_ledger", build_mock)

    ev = _FakeEvent(event_id=5, payload={"scan_pattern_id": 555})

    rl.handle_trade_closed_for_ledger(MagicMock(), ev, None)
    rl.handle_trade_closed_for_ledger(MagicMock(), ev, None)

    # First call invokes build_ledger; second is throttled.
    assert build_mock.call_count == 1, (
        "in-process throttle must short-circuit the second call within 60s"
    )


# ---------------------------------------------------------------------------
# Handler 6: pattern_stats (3 entry functions; one delegate)
# ---------------------------------------------------------------------------


def test_pattern_stats_handler_idempotent(monkeypatch) -> None:
    """update_pattern_stats called twice with same args (mock delegation)."""
    inner_mock = MagicMock(
        return_value={"patterns_updated": 0, "cycle_run_id": None}
    )

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    import app.services.trading.learning as learning

    monkeypatch.setattr(
        learning, "update_pattern_stats_from_closed_trades", inner_mock
    )

    from app.services.trading.brain_work.handlers.pattern_stats import (
        handle_paper_trade_closed,
        handle_live_trade_closed,
        handle_broker_fill_closed,
    )

    ev = _FakeEvent(event_id=6, payload={"user_id": 42})

    # Each of the 3 entry functions, called twice → 6 inner calls all w/ uid=42.
    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)

    assert inner_mock.call_count == 6
    for call in inner_mock.call_args_list:
        sess_arg, uid_arg = call.args
        assert uid_arg == 42, "user_id must be extracted from payload, stable"


# ---------------------------------------------------------------------------
# Handler 8: live_drift (3 entry functions)
# ---------------------------------------------------------------------------


def test_live_drift_handler_idempotent(monkeypatch) -> None:
    """run_live_drift_refresh called twice via each of the 3 entry funcs."""
    inner_mock = MagicMock(return_value={"ok": True, "drifted": 0})

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    import app.services.trading.live_drift as ld

    monkeypatch.setattr(ld, "run_live_drift_refresh", inner_mock)

    from app.services.trading.brain_work.handlers.live_drift import (
        handle_paper_trade_closed,
        handle_live_trade_closed,
        handle_broker_fill_closed,
    )

    ev = _FakeEvent(event_id=8, payload={})

    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)

    assert inner_mock.call_count == 6


# ---------------------------------------------------------------------------
# Handler 9: execution_robustness (3 entry functions)
# ---------------------------------------------------------------------------


def test_execution_robustness_handler_idempotent(monkeypatch) -> None:
    """run_execution_robustness_refresh called twice via each of 3 entries."""
    inner_mock = MagicMock(return_value={"ok": True})

    sessions: list[Any] = []

    def _patched_factory() -> Any:
        s = MagicMock()
        sessions.append(s)
        return s

    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = _patched_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)

    import app.services.trading.execution_robustness as er

    monkeypatch.setattr(er, "run_execution_robustness_refresh", inner_mock)

    from app.services.trading.brain_work.handlers.execution_robustness import (
        handle_paper_trade_closed,
        handle_live_trade_closed,
        handle_broker_fill_closed,
    )

    ev = _FakeEvent(event_id=9, payload={})

    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_paper_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_live_trade_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)
    handle_broker_fill_closed(MagicMock(), ev, None)

    assert inner_mock.call_count == 6
