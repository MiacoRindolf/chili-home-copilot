"""f-brain-phase2-producer-completion (2026-05-09).

Pin the watchdog-style mining producer wired into
`run_brain_work_dispatch_round`:

  * INTEGRATION (LIVE PATH): module-level
    `_LAST_DISPATCH_MARKET_SNAPSHOTS_AT` reset; mock
    `run_scheduled_market_snapshots` to return a deterministic
    payload; call `run_brain_work_dispatch_round`; assert a
    `market_snapshots_batch` row lands in `brain_work_events`.
    Run ALONE first (lesson from tonight's three "tests-pass-but-
    system-fails" instances).

  * Helper-level: interval gate skips when called twice in
    quick succession; disable flag short-circuits; failure
    surfaces in the result dict without poisoning the round.

Run with ``-p no:asyncio`` (pre-existing pytest-asyncio plugin
collection bug).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.config import Settings
from app.services.trading.brain_work import dispatcher as disp
from app.services.trading.brain_work.dispatcher import (
    _maybe_run_dispatch_market_snapshots,
    run_brain_work_dispatch_round,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def reset_dispatch_state():
    disp._LAST_DISPATCH_MARKET_SNAPSHOTS_AT = 0.0
    yield
    disp._LAST_DISPATCH_MARKET_SNAPSHOTS_AT = 0.0


def _patch_run_snapshots(monkeypatch, *, daily=10, intraday=5, universe=42):
    """Stub `run_scheduled_market_snapshots` so tests don't hit the
    real broker / market-data fetchers."""
    def _fake(db, user_id):
        return {
            "ok": True,
            "snapshots_taken_daily": daily,
            "intraday_snapshots_taken": intraday,
            "snapshots_taken": daily + intraday,
            "universe_size": universe,
            "snapshot_driver": "stub",
            "tickers": [],
            "vitals_refresh": {},
        }

    monkeypatch.setattr(
        "app.services.trading.learning.run_scheduled_market_snapshots", _fake,
    )


# ── INTEGRATION TEST (LIVE PATH) ─────────────────────────────────────


def test_profitability_dispatch_batch_settings_drive_limits(monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_WORK_EDGE_RELIABILITY_BATCH_SIZE", "6")
    monkeypatch.setenv("BRAIN_WORK_RECERT_RESCUE_BATCH_SIZE", "5")
    monkeypatch.setenv("BRAIN_WORK_EXIT_VARIANT_BATCH_SIZE", "4")
    monkeypatch.setenv("BRAIN_WORK_PROVENANCE_BATCH_SIZE", "3")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(disp, "settings", settings)

    limits = dict(disp._dispatch_limits())

    assert settings.brain_work_edge_reliability_batch_size == 6
    assert settings.brain_work_recert_rescue_batch_size == 5
    assert settings.brain_work_exit_variant_batch_size == 4
    assert settings.brain_work_provenance_batch_size == 3
    assert limits["edge_reliability_refresh"] == 6
    assert limits["recert_rescue_refresh"] == 5
    assert limits["exit_variant_refresh"] == 4
    assert limits["provenance_backfill"] == 3

    overrides = dict(
        disp._dispatch_limits(
            max_edge_reliability=1,
            max_recert_rescue=0,
            max_exit_variant=2,
            max_provenance=0,
        )
    )
    assert overrides["edge_reliability_refresh"] == 1
    assert overrides["recert_rescue_refresh"] == 0
    assert overrides["exit_variant_refresh"] == 2
    assert overrides["provenance_backfill"] == 0


def test_integration_dispatch_round_emits_market_snapshots_batch(
    db, reset_dispatch_state, monkeypatch,
):
    """Seed: dispatch state cleared. Stub run_scheduled_market_snapshots.
    Call run_brain_work_dispatch_round. Assert:
      * round returns ok=True with market_snapshots not skipped
      * a market_snapshots_batch row lands in brain_work_events
    """
    _patch_run_snapshots(monkeypatch, daily=12, intraday=8, universe=50)

    res = run_brain_work_dispatch_round(db, user_id=None)

    assert res.get("ok") is True
    assert "market_snapshots" in res
    ms = res["market_snapshots"]
    assert ms.get("ok") is True
    assert ms.get("skipped") is False
    assert ms.get("snapshots_taken_daily") == 12
    assert ms.get("intraday_snapshots_taken") == 8
    assert ms.get("universe_size") == 50

    # Row landed in brain_work_events.
    row = db.execute(text("""
        SELECT id, event_type, status, payload
          FROM brain_work_events
         WHERE event_type = 'market_snapshots_batch'
         ORDER BY id DESC LIMIT 1
    """)).fetchone()
    assert row is not None, (
        "market_snapshots_batch did NOT land in brain_work_events; "
        "the producer wiring is broken at the LIVE path."
    )
    assert row.event_type == "market_snapshots_batch"


# ── Helper-level: interval gate ──────────────────────────────────────


def test_interval_gate_skips_second_call_within_window(
    db, reset_dispatch_state, monkeypatch,
):
    """Second call to _maybe_run_dispatch_market_snapshots within
    the interval window must skip with reason='interval_gate'."""
    _patch_run_snapshots(monkeypatch)

    first = _maybe_run_dispatch_market_snapshots(db, user_id=None)
    assert first["skipped"] is False
    assert first["ok"] is True

    second = _maybe_run_dispatch_market_snapshots(db, user_id=None)
    assert second["skipped"] is True
    assert second["reason"] == "interval_gate"
    assert second["remaining_secs"] >= 0


def test_interval_zero_disables_gate(
    db, reset_dispatch_state, monkeypatch,
):
    """Setting interval=0 means run on every dispatch round."""
    _patch_run_snapshots(monkeypatch)
    monkeypatch.setattr(
        "app.config.settings.chili_brain_dispatch_market_snapshots_interval_secs",
        0, raising=False,
    )

    first = _maybe_run_dispatch_market_snapshots(db, user_id=None)
    assert first["skipped"] is False

    second = _maybe_run_dispatch_market_snapshots(db, user_id=None)
    assert second["skipped"] is False


def test_disable_flag_short_circuits(
    db, reset_dispatch_state, monkeypatch,
):
    """Setting enabled=False short-circuits without running snapshots
    or emitting an event."""
    sweep_called = []

    def _spy(db, user_id):
        sweep_called.append(True)
        return {
            "ok": True, "snapshots_taken_daily": 0,
            "intraday_snapshots_taken": 0, "snapshots_taken": 0,
            "universe_size": 0, "snapshot_driver": "spy",
            "tickers": [], "vitals_refresh": {},
        }

    monkeypatch.setattr(
        "app.services.trading.learning.run_scheduled_market_snapshots", _spy,
    )
    monkeypatch.setattr(
        "app.config.settings.chili_brain_dispatch_market_snapshots_enabled",
        False, raising=False,
    )

    res = _maybe_run_dispatch_market_snapshots(db, user_id=None)
    assert res["skipped"] is True
    assert res["reason"] == "disabled_by_setting"
    assert sweep_called == []


# ── Helper-level: failure surfaces, doesn't poison the round ─────────


def test_snapshots_failure_does_not_poison_round(
    db, reset_dispatch_state, monkeypatch,
):
    """If run_scheduled_market_snapshots raises, the dispatch round
    must still return ok=True and the failure surfaces in
    result['market_snapshots'].ok=false."""

    def _boom(db, user_id):
        raise RuntimeError("simulated snapshot crash")

    monkeypatch.setattr(
        "app.services.trading.learning.run_scheduled_market_snapshots", _boom,
    )

    res = run_brain_work_dispatch_round(db, user_id=None)
    assert res.get("ok") is True
    assert res["market_snapshots"]["ok"] is False
    assert "simulated snapshot crash" in res["market_snapshots"].get("error", "")


# ── Helper-level: result-dict surface contract ───────────────────────


def test_round_result_dict_has_market_snapshots_key(
    db, reset_dispatch_state, monkeypatch,
):
    """Every round result MUST carry `market_snapshots` so ops grep +
    downstream consumers see the watchdog state."""
    _patch_run_snapshots(monkeypatch)
    res = run_brain_work_dispatch_round(db, user_id=None)
    assert "market_snapshots" in res
    assert isinstance(res["market_snapshots"], dict)
    assert "ok" in res["market_snapshots"]
    assert "skipped" in res["market_snapshots"]


def test_fast_lane_can_disable_heavy_producers(
    reset_dispatch_state, monkeypatch,
):
    """Fast-lane dispatcher processes lightweight queues only.

    It must not emit market snapshots or run the thin-evidence sweep;
    those stay on the main brain-worker.
    """
    sweep_called = []
    thin_called = []
    time_decay_called = []

    def _spy_snapshots(db, user_id):
        sweep_called.append(True)
        return {
            "ok": True,
            "snapshots_taken_daily": 1,
            "intraday_snapshots_taken": 1,
            "snapshots_taken": 2,
            "universe_size": 1,
            "snapshot_driver": "spy",
            "tickers": [],
            "vitals_refresh": {},
        }

    def _spy_thin(db):
        thin_called.append(True)
        return {"ok": True, "demoted": 0, "demoted_ids": []}

    def _spy_time_decay(db):
        time_decay_called.append(True)
        return {"ok": True, "queued": 0, "checked": 0}

    db = MagicMock()
    monkeypatch.setattr(disp, "brain_work_ledger_enabled", lambda: True)
    monkeypatch.setattr(disp, "release_stale_leases", lambda _db: 0)
    monkeypatch.setattr(disp, "recover_retryable_dead_work", lambda _db: {})
    monkeypatch.setattr(disp, "coalesce_duplicate_open_work", lambda _db: {})
    monkeypatch.setattr(disp, "claim_work_batch", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.services.trading.learning.run_scheduled_market_snapshots",
        _spy_snapshots,
    )
    monkeypatch.setattr(
        "app.services.trading.learning.run_thin_evidence_demote",
        _spy_thin,
    )
    monkeypatch.setattr(
        "app.services.trading.brain_work.execution_hooks.enqueue_recent_time_decay_exit_variant_work",
        _spy_time_decay,
    )

    res = run_brain_work_dispatch_round(
        db,
        user_id=None,
        max_backtest=0,
        max_exec_feedback=0,
        max_edge_reliability=0,
        max_recert_rescue=0,
        max_exit_variant=0,
        max_provenance=0,
        max_mine=0,
        max_cpcv_gate=0,
        max_promote=0,
        max_trade_close=0,
        run_thin_evidence_sweep=False,
        run_time_decay_exit_variant_sweep=False,
        run_market_snapshots_watchdog=False,
    )

    assert res.get("ok") is True
    assert res["market_snapshots"] == {
        "ok": True,
        "skipped": True,
        "reason": "disabled_by_caller",
    }
    assert res["thin_evidence_sweep"]["skipped"] is True
    assert res["time_decay_exit_variant_sweep"]["skipped"] is True
    assert sweep_called == []
    assert thin_called == []
    assert time_decay_called == []
