"""f-pattern-demote-sweep-wiring-fix (2026-05-09).

Pin the per-cycle wiring of `run_thin_evidence_demote` into
`run_brain_work_dispatch_round`:

  * INTEGRATION (LIVE PATH): seed a thin-evidence scan_patterns row
    in chili_test; call `run_brain_work_dispatch_round` directly;
    assert the pattern is lifecycle_stage='challenged' after the
    round completes. This is the test that tonight's two prior
    bracket_writer briefs lacked — exercises the FULL CHAIN,
    not just the sweep helper in isolation.

  * Helper-level: dispatcher round still completes when the sweep
    raises (try/except wrapping); the result dict surfaces the
    sweep failure so ops can grep it.

  * Helper-level: the sweep call MOVED out of
    `_handle_execution_feedback_digest` (single source of truth).

Run with ``-p no:asyncio`` (workaround for pre-existing pytest-asyncio
plugin collection failure).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services.trading.brain_work.dispatcher import (
    _handle_execution_feedback_digest,
    run_brain_work_dispatch_round,
)


# ── Seed helper (ORM-backed; mirror of Phase D's pattern) ────────────


def _seed_scan_pattern(
    db,
    *,
    pid: int,
    lifecycle_stage: str = "promoted",
    trade_count: int | None = None,
    win_rate: float = 0.25,
    oos_win_rate=None,
    promotion_gate_reasons=("provisional_small_paths",),
    promotion_status: str = "promoted",
    name: str = "Test Pattern",
) -> None:
    from app.config import settings
    from app.models.trading import ScanPattern

    if db.query(ScanPattern).filter(ScanPattern.id == pid).first() is not None:
        return
    if trade_count is None:
        trade_count = int(getattr(settings, "chili_pattern_demote_min_realized_trades", 30))

    sp = ScanPattern(
        id=pid,
        name=name,
        lifecycle_stage=lifecycle_stage,
        trade_count=trade_count,
        win_rate=win_rate,
        oos_win_rate=oos_win_rate,
        promotion_gate_reasons=list(promotion_gate_reasons),
        promotion_status=promotion_status,
        active=True,
    )
    db.add(sp)
    db.commit()


def _read_pattern(db, pid: int):
    return db.execute(text("""
        SELECT id, lifecycle_stage, demoted_at, promotion_demote_reason
        FROM scan_patterns WHERE id = :id
    """), {"id": pid}).fetchone()


def _run_sweep_only_dispatch_round(db, *, user_id=None):
    return run_brain_work_dispatch_round(
        db,
        user_id=user_id,
        max_backtest=0,
        max_exec_feedback=0,
        max_mine=0,
        max_cpcv_gate=0,
        max_promote=0,
        max_trade_close=0,
        run_market_snapshots_watchdog=False,
    )


# ── INTEGRATION TEST (LIVE PATH) ─────────────────────────────────────


def test_integration_dispatch_round_demotes_thin_evidence_pattern(db):
    """Seed a thin-evidence pattern, call run_brain_work_dispatch_round
    directly, assert the pattern is challenged after the round.

    This is the brief's hard acceptance criterion: NOT just
    `run_thin_evidence_demote(db)` in isolation -- the FULL
    CHAIN through the dispatcher entry point.
    """
    _seed_scan_pattern(db, pid=585, name="Intraday Squeeze + Decl Vol")

    res = _run_sweep_only_dispatch_round(db, user_id=None)

    # Dispatcher round completed.
    assert res.get("ok") is True
    # New surface: thin_evidence_sweep result is in the round dict.
    assert "thin_evidence_sweep" in res
    assert res["thin_evidence_sweep"].get("ok") is True
    assert 585 in res["thin_evidence_sweep"].get("demoted_ids", [])

    # And the row is actually demoted in the DB.
    row = _read_pattern(db, 585)
    assert row.lifecycle_stage == "challenged"
    assert row.demoted_at is not None
    assert row.promotion_demote_reason == "thin_evidence_low_realized_wr"


def test_integration_dispatch_round_idempotent_on_already_challenged(db):
    """Second call within the same test session must NOT re-touch the
    already-demoted row -- the lifecycle != 'promoted' filter inside
    `run_thin_evidence_demote` short-circuits."""
    _seed_scan_pattern(db, pid=586, name="Test pattern 2")

    _run_sweep_only_dispatch_round(db, user_id=None)
    first_row = _read_pattern(db, 586)
    first_demoted_at = first_row.demoted_at

    res = _run_sweep_only_dispatch_round(db, user_id=None)
    assert res["thin_evidence_sweep"].get("ok") is True
    assert 586 not in res["thin_evidence_sweep"].get("demoted_ids", [])

    second_row = _read_pattern(db, 586)
    # demoted_at unchanged on second pass.
    assert second_row.demoted_at == first_demoted_at


def test_integration_dispatch_round_keeps_healthy_pattern(db):
    """Healthy promoted pattern (large sample) is NOT touched by the
    per-cycle sweep -- proves the predicate's selectivity holds via
    the dispatcher path."""
    _seed_scan_pattern(
        db, pid=1011, name="Reddit IBS large",
        trade_count=409, win_rate=0.632,
    )

    res = _run_sweep_only_dispatch_round(db, user_id=None)
    assert 1011 not in res["thin_evidence_sweep"].get("demoted_ids", [])

    row = _read_pattern(db, 1011)
    assert row.lifecycle_stage == "promoted"


# ── Helper-level: sweep failure doesn't poison the round ─────────────


def test_dispatch_round_completes_when_sweep_raises(db, monkeypatch):
    """Per the wiring's try/except contract: if
    run_thin_evidence_demote raises, the round still returns ok=True
    and surfaces the failure in `thin_evidence_sweep.ok=False`.
    Other dispatch work is unaffected (it ran before the sweep)."""

    def _boom(_db):
        raise RuntimeError("simulated sweep crash")

    monkeypatch.setattr(
        "app.services.trading.learning.run_thin_evidence_demote", _boom,
    )

    res = _run_sweep_only_dispatch_round(db, user_id=None)

    # Round still ok.
    assert res.get("ok") is True
    # Sweep failure surfaced.
    assert res["thin_evidence_sweep"].get("ok") is False
    assert "simulated sweep crash" in res["thin_evidence_sweep"].get("error", "")


# ── Helper-level: sweep removed from _handle_execution_feedback_digest
# (single source of truth) ───────────────────────────────────────────


def test_execution_feedback_digest_no_longer_calls_sweep(db, monkeypatch):
    """The brief's single-source-of-truth requirement: the sweep is
    NOT called from `_handle_execution_feedback_digest`. If a
    live_trade_closed event flows through the digest hook, the sweep
    should NOT be invoked again (it ran in the dispatcher round)."""
    sweep_called = []

    def _spy(_db):
        sweep_called.append(True)
        return {"ok": True, "demoted": 0, "demoted_ids": []}

    # Patch BOTH the source AND the import-site so we catch any
    # surviving stale reference.
    monkeypatch.setattr(
        "app.services.trading.learning.run_thin_evidence_demote", _spy,
    )

    # Synthesize a minimal work-event row + a connected user.
    ev = MagicMock()
    ev.id = 9999
    ev.payload = {"user_id": 1, "trigger": "test"}

    # Stub out the heavy components so the digest body runs to
    # completion without external dependencies.
    monkeypatch.setattr(
        "app.services.trading.execution_quality.compute_execution_stats",
        lambda *a, **kw: {"trades_analyzed": 0},
    )
    monkeypatch.setattr(
        "app.services.trading.execution_quality.suggest_adaptive_spread",
        lambda *a, **kw: {"current_spread": None, "suggested_spread": None,
                          "should_update": False, "reason": "stub"},
    )
    monkeypatch.setattr(
        "app.services.trading.learning.run_live_pattern_depromotion",
        lambda _db: {"ok": True, "demoted": 0},
    )
    monkeypatch.setattr(
        "app.services.trading.brain_work.dispatcher."
        "emit_execution_quality_updated_outcome",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.brain_neural_mesh.publisher."
        "publish_brain_work_outcome",
        lambda *a, **kw: None,
    )

    _handle_execution_feedback_digest(db, ev, user_id=1)

    # KEY ASSERTION: the digest hook did NOT invoke the sweep.
    assert sweep_called == [], (
        "run_thin_evidence_demote was called from "
        "_handle_execution_feedback_digest; it should ONLY be "
        "called from run_brain_work_dispatch_round per the wiring fix."
    )


# ── Pin the result-dict surface ──────────────────────────────────────


def test_round_result_dict_has_thin_evidence_sweep_key(db):
    """Pin the new contract: every dispatch round result MUST carry
    `thin_evidence_sweep` so observability + the operator's grep
    pattern (`[learning] thin_evidence sweep`) keep working."""
    res = _run_sweep_only_dispatch_round(db, user_id=None)
    assert "thin_evidence_sweep" in res
    assert isinstance(res["thin_evidence_sweep"], dict)
    assert "ok" in res["thin_evidence_sweep"]
    assert "demoted" in res["thin_evidence_sweep"]
    assert "demoted_ids" in res["thin_evidence_sweep"]
