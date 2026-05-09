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
