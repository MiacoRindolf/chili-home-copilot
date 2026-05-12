"""Phase 3 handler-idempotency tests for ``quality_score``.

Mirrors the Phase 1b hard-gate test scaffold in
``tests/test_brain_work_handler_idempotency.py``: each test invokes the
handler entry function TWICE with the same synthetic event payload and
verifies that the handler boundary introduces no extra side effects
beyond a stable call into the wrapped compute/persist path.

The wrapped pure compute (``compute_quality_composite_score``) is
deterministic given the same inputs, and the handler's
write-only-on-change branch means a second call against the same DB
state must produce zero writes / zero emits.

Brief: docs/STRATEGY/QUEUED/f-composite-quality-event-driven.md
Parent: docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch


class _FakeEvent:
    """Minimal stand-in for ``BrainWorkEvent`` (handler reads .id / .payload)."""

    def __init__(self, *, event_id: int, payload: dict[str, Any]) -> None:
        self.id = event_id
        self.payload = payload


def _install_fake_app_db(monkeypatch, *, session_factory):
    fake_app_db = types.ModuleType("app.db")
    fake_app_db.SessionLocal = session_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)


def _patch_pattern_quality_score(monkeypatch, *, weights):
    """Patch the pure compute module the handler delegates to."""
    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "_resolve_weights", MagicMock(return_value=weights))
    return pqs


def _new_session(pattern, *, dq_row=None, decay_row=None):
    """Build a MagicMock session whose execute(...).fetchone() returns
    canned rows in order: directional-WR row first, then decay row.

    Each call to ``session.execute(...).fetchone()`` advances a single
    shared queue so the two SQL queries inside the handler get the
    correct row.
    """
    s = MagicMock()
    s.get.return_value = pattern
    queue: list[Any] = [dq_row, decay_row]
    counter = {"i": 0}

    def _execute(*_args, **_kwargs):
        rv = MagicMock()
        idx = counter["i"]
        counter["i"] = idx + 1
        # Each execute returns a fetcher tied to one queue slot.
        slot = queue[idx] if idx < len(queue) else None
        rv.fetchone.return_value = slot
        return rv

    s.execute.side_effect = _execute
    return s


# ---------------------------------------------------------------------------
# 1. Idempotent — no change between calls (composite stays stable)
# ---------------------------------------------------------------------------


def test_quality_handler_idempotent_no_change(monkeypatch) -> None:
    """Score equal to existing → no write, no emit, on both calls."""
    pid = 11111
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "candidate"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = 0.5  # matches the compute result

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "compute_quality_composite_score",
                        MagicMock(return_value=0.5))

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),  # newer_wr, older_wr, newer_n, older_n
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=1, payload={"scan_pattern_id": pid})

    handle_backtest_completed_quality(MagicMock(), ev, None)
    handle_backtest_completed_quality(MagicMock(), ev, None)

    assert len(sessions) == 2
    # Pattern field never reassigned when score is unchanged.
    assert pattern.quality_composite_score == 0.5
    # No commits — flush+commit is only on the change branch.
    for s in sessions:
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Writes on score change; second call is idempotent against new state
# ---------------------------------------------------------------------------


def test_quality_handler_writes_on_score_change(monkeypatch) -> None:
    pid = 22222
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "backtested"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = None  # NULL → first call writes 0.5

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "compute_quality_composite_score",
                        MagicMock(return_value=0.5))

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    # Spy on enqueue_outcome_event so we can assert emit counts.
    import app.services.trading.brain_work.handlers.quality_score as qs
    import app.services.trading.brain_work.ledger as ledger

    emit_mock = MagicMock(return_value=42)
    # The handler imports enqueue_outcome_event lazily from ..ledger.
    monkeypatch.setattr(ledger, "enqueue_outcome_event", emit_mock)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=2, payload={"scan_pattern_id": pid})

    # First call: NULL → 0.5 → write + emit.
    handle_backtest_completed_quality(MagicMock(), ev, None)
    # Simulate persistence by advancing the in-memory mock pattern.
    pattern.quality_composite_score = 0.5

    # Second call: identical state → no write, no emit.
    handle_backtest_completed_quality(MagicMock(), ev, None)

    assert emit_mock.call_count == 1
    # First session committed; second session did not.
    assert sessions[0].commit.call_count == 1
    sessions[1].commit.assert_not_called()


# ---------------------------------------------------------------------------
# 3. NULL when cpcv / dsr / pbo evidence missing
# ---------------------------------------------------------------------------


def test_quality_handler_null_when_missing_evidence(monkeypatch) -> None:
    pid = 33333
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "candidate"
    pattern.cpcv_median_sharpe = None
    pattern.deflated_sharpe = None
    pattern.pbo = None
    pattern.quality_composite_score = None

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    # compute_quality_composite_score returns None when evidence is missing.
    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "compute_quality_composite_score",
                        MagicMock(return_value=None))

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=3, payload={"scan_pattern_id": pid})

    handle_backtest_completed_quality(MagicMock(), ev, None)
    handle_backtest_completed_quality(MagicMock(), ev, None)

    # NULL → NULL → no change → no commits, no flush of the change branch.
    for s in sessions:
        s.commit.assert_not_called()
    assert pattern.quality_composite_score is None


# ---------------------------------------------------------------------------
# 4. NULL when rolling_sample_n < 30 (thin directional)
# ---------------------------------------------------------------------------


def test_quality_handler_null_when_thin_directional(monkeypatch) -> None:
    pid = 44444
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "candidate"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = None

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    sessions: list[Any] = []

    def _factory() -> Any:
        # sample_n=12 (< 30) → handler computes NULL without calling
        # compute_quality_composite_score.
        s = _new_session(
            pattern,
            dq_row=(0.55, 12),
            decay_row=(0.5, 0.5, 0, 0),  # newer_n != 15 → decay None
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_trade_closed_quality,
    )

    ev = _FakeEvent(event_id=4, payload={"scan_pattern_id": pid})

    handle_trade_closed_quality(MagicMock(), ev, None)
    handle_trade_closed_quality(MagicMock(), ev, None)

    # Pattern already NULL → no change → no commits.
    for s in sessions:
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Retired patterns short-circuit without recompute
# ---------------------------------------------------------------------------


def test_quality_handler_skip_retired(monkeypatch) -> None:
    pid = 55555
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "retired"
    pattern.quality_composite_score = None

    sessions: list[Any] = []

    def _factory() -> Any:
        s = MagicMock()
        s.get.return_value = pattern
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    import app.services.trading.pattern_quality_score as pqs

    compute_mock = MagicMock(return_value=0.9)
    monkeypatch.setattr(pqs, "compute_quality_composite_score", compute_mock)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=5, payload={"scan_pattern_id": pid})

    handle_backtest_completed_quality(MagicMock(), ev, None)
    handle_backtest_completed_quality(MagicMock(), ev, None)

    # Each call opens a session, calls .get, then short-circuits.
    assert len(sessions) == 2
    compute_mock.assert_not_called()
    for s in sessions:
        s.execute.assert_not_called()
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Missing pattern (payload pid doesn't resolve)
# ---------------------------------------------------------------------------


def test_quality_handler_missing_pattern(monkeypatch) -> None:
    sessions: list[Any] = []

    def _factory() -> Any:
        s = MagicMock()
        s.get.return_value = None  # pattern not found
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=6, payload={"scan_pattern_id": 99999})

    # Should not raise; handler logs warning and returns.
    handle_backtest_completed_quality(MagicMock(), ev, None)
    handle_backtest_completed_quality(MagicMock(), ev, None)

    assert len(sessions) == 2
    for s in sessions:
        s.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 7. backtest_completed payload shape
# ---------------------------------------------------------------------------


def test_quality_handler_handles_backtest_completed_event(monkeypatch) -> None:
    pid = 77777
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "backtested"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = None

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "compute_quality_composite_score",
                        MagicMock(return_value=0.7))

    import app.services.trading.brain_work.ledger as ledger

    emit_mock = MagicMock(return_value=99)
    monkeypatch.setattr(ledger, "enqueue_outcome_event", emit_mock)

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(
        event_id=7,
        payload={
            "scan_pattern_id": pid,
            "parent_work_event_id": 100,
            "backtests_run": 4,
        },
    )

    handle_backtest_completed_quality(MagicMock(), ev, None)

    # Score changed (None → 0.7) → 1 write + 1 emit.
    assert sessions[0].commit.call_count == 1
    assert emit_mock.call_count == 1
    call = emit_mock.call_args
    assert call.kwargs["event_type"] == "pattern_quality_recomputed"
    assert call.kwargs["payload"]["scan_pattern_id"] == pid
    assert call.kwargs["payload"]["new_score"] == 0.7
    assert call.kwargs["payload"]["source"] == "backtest_completed"


# ---------------------------------------------------------------------------
# 8. Trade-close event shapes
# ---------------------------------------------------------------------------


def test_quality_handler_handles_trade_close_events(monkeypatch) -> None:
    pid = 88888
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "promoted"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = 0.4

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    import app.services.trading.pattern_quality_score as pqs

    monkeypatch.setattr(pqs, "compute_quality_composite_score",
                        MagicMock(return_value=0.6))

    import app.services.trading.brain_work.ledger as ledger
    emit_mock = MagicMock(return_value=None)
    monkeypatch.setattr(ledger, "enqueue_outcome_event", emit_mock)

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_trade_closed_quality,
    )

    # live_trade_closed
    ev1 = _FakeEvent(event_id=10, payload={
        "trade_id": 1, "user_id": 1, "ticker": "AAPL",
        "source": "auto_trader", "scan_pattern_id": pid,
    })
    handle_trade_closed_quality(MagicMock(), ev1, None)
    pattern.quality_composite_score = 0.6  # simulate persist
    pqs.compute_quality_composite_score = MagicMock(return_value=0.6)

    # paper_trade_closed
    ev2 = _FakeEvent(event_id=11, payload={
        "paper_trade_id": 2, "user_id": 1, "scan_pattern_id": pid,
        "ticker": "AAPL", "pnl": 1.2, "exit_reason": "target",
    })
    handle_trade_closed_quality(MagicMock(), ev2, None)

    # broker_fill_closed
    ev3 = _FakeEvent(event_id=12, payload={
        "trade_id": 3, "user_id": 1, "ticker": "AAPL",
        "broker_source": "robinhood", "source": "sync",
        "scan_pattern_id": pid,
    })
    handle_trade_closed_quality(MagicMock(), ev3, None)

    # Three sessions, exactly one commit (the first call wrote 0.6).
    assert len(sessions) == 3
    assert sessions[0].commit.call_count == 1


# ---------------------------------------------------------------------------
# 9. Inner exception swallowed at the handler boundary
# ---------------------------------------------------------------------------


def test_quality_handler_swallows_inner_exception(monkeypatch) -> None:
    """A broken compute path must NOT propagate — composite is informational."""
    pid = 99999
    pattern = MagicMock()
    pattern.id = pid
    pattern.lifecycle_stage = "candidate"
    pattern.cpcv_median_sharpe = 2.0
    pattern.deflated_sharpe = 1.0
    pattern.pbo = 0.1
    pattern.quality_composite_score = None

    _patch_pattern_quality_score(
        monkeypatch,
        weights={
            "cpcv_sharpe": 0.30, "deflated_sharpe": 0.20,
            "pbo_inverse": 0.15, "directional_wr": 0.25,
            "decay_inverse": 0.10,
        },
    )

    import app.services.trading.pattern_quality_score as pqs

    def _raise(*_a, **_kw):
        raise RuntimeError("compute exploded")

    monkeypatch.setattr(pqs, "compute_quality_composite_score", _raise)

    sessions: list[Any] = []

    def _factory() -> Any:
        s = _new_session(
            pattern,
            dq_row=(0.55, 30),
            decay_row=(0.5, 0.5, 15, 15),
        )
        sessions.append(s)
        return s

    _install_fake_app_db(monkeypatch, session_factory=_factory)

    from app.services.trading.brain_work.handlers.quality_score import (
        handle_backtest_completed_quality,
    )

    ev = _FakeEvent(event_id=20, payload={"scan_pattern_id": pid})

    # Must not raise.
    handle_backtest_completed_quality(MagicMock(), ev, None)
    handle_backtest_completed_quality(MagicMock(), ev, None)

    # Rollback called on each session (exception path).
    for s in sessions:
        s.rollback.assert_called()
        s.commit.assert_not_called()
