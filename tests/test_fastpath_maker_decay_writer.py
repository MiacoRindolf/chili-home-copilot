"""Tests for the decay_miner maker-filled writer
(f-fastpath-maker-only-executor, 2026-05-08).

Covers:
  * `_PendingObs.is_maker_filled` defaults False (existing taker-mode
    schedules unchanged).
  * `record_maker_outcome` schedules 8-horizon obs flagged
    `is_maker_filled=True` only on 'filled' / 'partial'.
  * `record_maker_outcome` is a no-op for 'cancelled' / 'replaced' /
    'rejected' (fill-rate is sourced from the executor's
    `fast_path_maker_attempts` writes, not the decay miner).
  * `_finalize_one_obs` dispatches to `_welford_upsert_maker_filled`
    when the flag is True; otherwise to the existing
    `_welford_upsert`.

Helper-level. We mock the engine + the welford-upsert methods so no
DB is required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.trading.fast_path.decay_miner import (
    HORIZONS_S,
    FastPathDecayMiner,
    _PendingObs,
)
from app.services.trading.fast_path.settings import FastPathSettings


def _make_miner():
    settings = FastPathSettings(enabled=True)
    engine = MagicMock(name="engine")
    miner = FastPathDecayMiner(settings, engine, max_pending_obs=10_000)
    return miner


def _make_fired_at():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# _PendingObs default flag
# ---------------------------------------------------------------------------

def test_pending_obs_is_maker_filled_defaults_false():
    obs = _PendingObs(
        deadline_unix=0.0, seq=1, alert_id=1, ticker="BTC-USD",
        alert_type="imbalance_long", score_bucket_value="high",
        horizon_s=60, entry_at_alert=100.0, direction="long",
        fired_at=_make_fired_at(),
    )
    assert obs.is_maker_filled is False


# ---------------------------------------------------------------------------
# record_maker_outcome: 'filled' schedules 8 horizons w/ flag True
# ---------------------------------------------------------------------------

def test_record_maker_outcome_filled_schedules_eight_horizons():
    miner = _make_miner()
    miner.record_maker_outcome(
        alert_id=1, ticker="BTC-USD", alert_type="imbalance_long",
        signal_score=0.85, fired_at=_make_fired_at(),
        fill_outcome="filled", entry_at_alert=100.05,
    )
    # 8 horizons in HORIZONS_S, all flagged is_maker_filled=True.
    assert len(miner._pending) == len(HORIZONS_S)
    assert all(o.is_maker_filled for o in miner._pending)
    assert miner._metrics.maker_obs_scheduled == len(HORIZONS_S)
    assert miner._metrics.maker_outcomes_received == 1
    horizons_scheduled = sorted(o.horizon_s for o in miner._pending)
    assert horizons_scheduled == sorted(HORIZONS_S)


def test_record_maker_outcome_partial_also_schedules():
    miner = _make_miner()
    miner.record_maker_outcome(
        alert_id=1, ticker="BTC-USD", alert_type="imbalance_long",
        signal_score=0.85, fired_at=_make_fired_at(),
        fill_outcome="partial", entry_at_alert=100.05,
    )
    assert len(miner._pending) == len(HORIZONS_S)


# ---------------------------------------------------------------------------
# record_maker_outcome: cancelled / replaced / rejected — no schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["cancelled", "replaced", "rejected", "weird"])
def test_record_maker_outcome_unfilled_is_noop(outcome):
    miner = _make_miner()
    miner.record_maker_outcome(
        alert_id=1, ticker="BTC-USD", alert_type="imbalance_long",
        signal_score=0.85, fired_at=_make_fired_at(),
        fill_outcome=outcome, entry_at_alert=100.05,
    )
    assert miner._pending == []
    assert miner._metrics.maker_obs_scheduled == 0
    # The 'received' counter still increments — we observed an event.
    assert miner._metrics.maker_outcomes_received == 1


# ---------------------------------------------------------------------------
# Defensive guards: missing alert_id / non-positive entry / blank metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"alert_id": 0},                        # bad alert_id
    {"ticker": ""},                         # empty ticker
    {"alert_type": ""},                     # empty alert_type
    {"entry_at_alert": 0.0},                # non-positive entry
    {"entry_at_alert": -1.0},               # negative entry
])
def test_record_maker_outcome_rejects_malformed_inputs(kwargs):
    miner = _make_miner()
    base = dict(
        alert_id=1, ticker="BTC-USD", alert_type="imbalance_long",
        signal_score=0.85, fired_at=_make_fired_at(),
        fill_outcome="filled", entry_at_alert=100.05,
    )
    base.update(kwargs)
    miner.record_maker_outcome(**base)
    assert miner._pending == []


# ---------------------------------------------------------------------------
# Heap cap enforced
# ---------------------------------------------------------------------------

def test_record_maker_outcome_respects_heap_cap():
    settings = FastPathSettings(enabled=True)
    engine = MagicMock(name="engine")
    # Cap = 4 means even the very first call (which would push 8
    # horizons) must be refused since it'd overflow.
    miner = FastPathDecayMiner(settings, engine, max_pending_obs=4)
    miner.record_maker_outcome(
        alert_id=1, ticker="BTC-USD", alert_type="imbalance_long",
        signal_score=0.85, fired_at=_make_fired_at(),
        fill_outcome="filled", entry_at_alert=100.05,
    )
    assert miner._pending == []
    assert miner._metrics.maker_obs_dropped_overcap == len(HORIZONS_S)


# ---------------------------------------------------------------------------
# _finalize_one_obs dispatches on is_maker_filled
# ---------------------------------------------------------------------------

def test_finalize_dispatches_to_maker_filled_when_flag_set():
    miner = _make_miner()

    # Mock both upsert paths so we can assert which was hit.
    miner._welford_upsert = MagicMock()
    miner._welford_upsert_maker_filled = MagicMock()

    # Fake the book lookup so _finalize_one_obs has a quote pair.
    fake_book_row = {
        "bid_levels": [["100.20", "1.0"]],
        "ask_levels": [["100.30", "1.0"]],
    }

    class _StubResult:
        def mappings(self):
            return self
        def one_or_none(self):
            return fake_book_row

    class _StubConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def execute(self, *a, **kw):
            return _StubResult()

    miner._engine.begin = MagicMock(return_value=_StubConn())

    obs = _PendingObs(
        deadline_unix=0.0, seq=1, alert_id=1, ticker="BTC-USD",
        alert_type="imbalance_long", score_bucket_value="high",
        horizon_s=60, entry_at_alert=100.0, direction="long",
        fired_at=_make_fired_at(),
        is_maker_filled=True,
    )
    miner._finalize_one_obs(obs)
    miner._welford_upsert.assert_not_called()
    miner._welford_upsert_maker_filled.assert_called_once()
    assert miner._metrics.maker_obs_finalized == 1


def test_finalize_dispatches_to_default_table_when_flag_unset():
    miner = _make_miner()
    miner._welford_upsert = MagicMock()
    miner._welford_upsert_maker_filled = MagicMock()

    fake_book_row = {
        "bid_levels": [["100.20", "1.0"]],
        "ask_levels": [["100.30", "1.0"]],
    }

    class _StubResult:
        def mappings(self):
            return self
        def one_or_none(self):
            return fake_book_row

    class _StubConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def execute(self, *a, **kw):
            return _StubResult()

    miner._engine.begin = MagicMock(return_value=_StubConn())

    obs = _PendingObs(
        deadline_unix=0.0, seq=1, alert_id=1, ticker="BTC-USD",
        alert_type="imbalance_long", score_bucket_value="high",
        horizon_s=60, entry_at_alert=100.0, direction="long",
        fired_at=_make_fired_at(),
        is_maker_filled=False,
    )
    miner._finalize_one_obs(obs)
    miner._welford_upsert_maker_filled.assert_not_called()
    miner._welford_upsert.assert_called_once()
    assert miner._metrics.maker_obs_finalized == 0


def test_exit_validation_updates_maker_filled_table_for_maker_entry():
    miner = _make_miner()
    execute_calls = []
    alert_row = {
        "ticker": "SUI-USD",
        "alert_type": "volume_breakout_long",
        "signal_score": 0.35,
        "maker_filled_entry": True,
    }

    class _StubResult:
        def __init__(self, row=None):
            self._row = row
        def mappings(self):
            return self
        def first(self):
            return self._row

    class _StubConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def execute(self, statement, params=None):
            sql = str(statement)
            execute_calls.append((sql, params or {}))
            if "SELECT a.ticker" in sql:
                return _StubResult(alert_row)
            return _StubResult()

    miner._engine.begin = MagicMock(return_value=_StubConn())

    miner._handle_exit_inserted({
        "entry_execution_id": 144392,
        "realized_return_pct": -0.6637,
        "holding_period_s": 1217.8,
    })

    writes = [sql for sql, _params in execute_calls if "INSERT INTO" in sql]
    assert any("INSERT INTO fast_signal_decay " in sql for sql in writes)
    assert any(
        "INSERT INTO fast_signal_decay_maker_filled " in sql
        for sql in writes
    )
    select_params = execute_calls[0][1]
    assert select_params["maker_filled_outcomes"] == ["filled", "partial"]
    assert miner._metrics.validations_recorded == 1


def test_exit_validation_skips_maker_filled_table_for_non_maker_entry():
    miner = _make_miner()
    execute_calls = []
    alert_row = {
        "ticker": "DOGE-USD",
        "alert_type": "spread_squeeze",
        "signal_score": 0.55,
        "maker_filled_entry": False,
    }

    class _StubResult:
        def __init__(self, row=None):
            self._row = row
        def mappings(self):
            return self
        def first(self):
            return self._row

    class _StubConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def execute(self, statement, params=None):
            sql = str(statement)
            execute_calls.append((sql, params or {}))
            if "SELECT a.ticker" in sql:
                return _StubResult(alert_row)
            return _StubResult()

    miner._engine.begin = MagicMock(return_value=_StubConn())

    miner._handle_exit_inserted({
        "entry_execution_id": 7,
        "realized_return_pct": 0.25,
        "holding_period_s": 60.0,
    })

    writes = [sql for sql, _params in execute_calls if "INSERT INTO" in sql]
    assert any("INSERT INTO fast_signal_decay " in sql for sql in writes)
    assert not any(
        "INSERT INTO fast_signal_decay_maker_filled " in sql
        for sql in writes
    )
    assert miner._metrics.validations_recorded == 1


# ---------------------------------------------------------------------------
# stats() surfaces the new counters
# ---------------------------------------------------------------------------

def test_stats_includes_maker_counters():
    miner = _make_miner()
    s = miner.stats()
    assert "maker_outcomes_received" in s
    assert "maker_obs_scheduled" in s
    assert "maker_obs_finalized" in s
    assert "maker_obs_dropped_overcap" in s
