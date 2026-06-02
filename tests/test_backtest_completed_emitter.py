"""Tests for f-fix-backtest-completed-emitter (Phase 5 of f-overnight-cleanup)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. New emitter exists with the expected signature
# ---------------------------------------------------------------------------

def test_emit_backtest_completed_outcome_exists():
    from app.services.trading.brain_work.emitters import (
        emit_backtest_completed_outcome,
    )
    import inspect
    sig = inspect.signature(emit_backtest_completed_outcome)
    params = set(sig.parameters)
    # scan_pattern_id is the only required arg per cpcv_gate's contract.
    assert "scan_pattern_id" in params
    assert "user_id" in params
    assert "backtests_run" in params
    assert "win_rate" in params
    assert "avg_return" in params


# ---------------------------------------------------------------------------
# 2. backtest_queue_worker references the emit + wraps in try/except
# ---------------------------------------------------------------------------

def test_completion_site_references_emit():
    src = (REPO / "app/services/trading/backtest_queue_worker.py").read_text()
    assert "emit_backtest_completed_outcome" in src, (
        "backtest_queue_worker.py must reference emit_backtest_completed_outcome"
    )
    # Pin the try/except wrapper.
    idx = src.find("emit_backtest_completed_outcome(")
    assert idx >= 0
    preceding = src[max(0, idx - 800):idx]
    assert "try:" in preceding, (
        "emit_backtest_completed_outcome call must be in a try: block"
    )


# ---------------------------------------------------------------------------
# 3. cpcv_gate handler still subscribes to backtest_completed (regression)
# ---------------------------------------------------------------------------

def test_cpcv_gate_still_subscribes_to_backtest_completed():
    src = (REPO / "app/services/trading/brain_work/dispatcher.py").read_text()
    assert "\"backtest_completed\"" in src, (
        "dispatcher.py must still dispatch backtest_completed events"
    )
    src2 = (
        REPO / "app/services/trading/brain_work/handlers/cpcv_gate.py"
    ).read_text()
    assert "handle_backtest_completed" in src2
    assert "promotion_gate_passed" in src2


# ---------------------------------------------------------------------------
# 4. Emitter call builds the expected enqueue payload when invoked directly.
# ---------------------------------------------------------------------------


def _capture_backtest_completed_payload(monkeypatch, **kwargs):
    from app.services.trading.brain_work import emitters

    captured = {}

    def _fake_enqueue(_db, *, event_type, dedupe_key, payload, **_kwargs):
        captured["event_type"] = event_type
        captured["dedupe_key"] = dedupe_key
        captured["payload"] = payload
        return 99

    monkeypatch.setattr(emitters, "enqueue_outcome_event", _fake_enqueue)
    event_id = emitters.emit_backtest_completed_outcome(object(), **kwargs)
    assert event_id == 99
    return captured


def test_emitter_marks_missing_lineage_incomplete(monkeypatch):
    captured = _capture_backtest_completed_payload(
        monkeypatch,
        scan_pattern_id=12345,
        user_id=None,
        backtests_run=7,
        win_rate=0.6,
        avg_return=1.2,
    )

    assert captured["event_type"] == "backtest_completed"
    payload = captured["payload"]
    assert payload.get("scan_pattern_id") == 12345
    assert payload.get("backtests_run") == 7
    assert payload.get("win_rate") == 0.6
    assert payload.get("avg_return") == 1.2
    assert payload.get("lineage_status") == "incomplete"
    assert payload.get("promotion_grade_provenance") is False
    assert "run_lineage" in payload.get("lineage_missing_fields", [])


def test_emitter_marks_complete_lineage_promotion_grade(monkeypatch):
    captured = _capture_backtest_completed_payload(
        monkeypatch,
        scan_pattern_id=54321,
        user_id=9,
        backtests_run=2,
        win_rate=0.5,
        avg_return=0.7,
        extra={
            "backtest_result_ids": [11, 12],
            "backtest_param_set_ids": [21, 22],
            "backtest_param_hashes": ["hash-a", "hash-b"],
            "settings_hash": "settings-hash",
            "conditions_hash": "conditions-hash",
            "exit_config_hash": "exit-config-hash",
            "selected_tickers_hash": "tickers-hash",
            "code_version": "test-version",
            "run_lineage": "lineage-hash",
            "complete_ticker_attempts": True,
        },
    )

    payload = captured["payload"]
    assert payload["lineage_status"] == "complete"
    assert payload["lineage_missing_fields"] == []
    assert payload["promotion_grade_provenance"] is True
    assert payload["backtest_param_hashes"] == ["hash-a", "hash-b"]
    assert payload["conditions_hash"] == "conditions-hash"
