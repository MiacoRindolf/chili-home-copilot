from __future__ import annotations

from scripts.verify_ross_event_admission_runtime import evaluate_recent_ross_admissions


def test_recent_ross_admissions_accept_immediate_tick() -> None:
    ok, reason, detail = evaluate_recent_ross_admissions(
        [
            {
                "session_id": 10377,
                "ts": "2026-07-01T22:10:00",
                "payload": {"symbol": "CANF", "source": "iqfeed_l1", "ticked": 1},
            }
        ]
    )

    assert ok is True
    assert reason == "ross_event_admission_runtime_ok"
    assert detail["checked"] == 1
    assert detail["bad"] == []


def test_recent_ross_admissions_reject_zero_tick_live_source() -> None:
    ok, reason, detail = evaluate_recent_ross_admissions(
        [
            {
                "session_id": 10377,
                "ts": "2026-07-01T21:54:52",
                "payload": {"symbol": "CANF", "source": "iqfeed_l1", "ticked": 0, "latency_ms": 3896.2},
            }
        ]
    )

    assert ok is False
    assert reason == "ross_event_admission_missing_immediate_tick"
    assert detail["checked"] == 1
    assert detail["bad"][0]["symbol"] == "CANF"
    assert detail["bad"][0]["ticked"] == 0


def test_recent_ross_admissions_ignore_non_live_source() -> None:
    ok, reason, detail = evaluate_recent_ross_admissions(
        [
            {
                "session_id": 1,
                "ts": "2026-07-01T22:10:00",
                "payload": {"symbol": "TEST", "source": "manual_audit", "ticked": 0},
            }
        ]
    )

    assert ok is True
    assert reason == "ross_event_admission_runtime_ok"
    assert detail["checked"] == 0


def test_recent_ross_admissions_can_require_live_event_evidence() -> None:
    ok, reason, detail = evaluate_recent_ross_admissions(
        [
            {
                "session_id": 1,
                "ts": "2026-07-01T22:10:00",
                "payload": {"symbol": "TEST", "source": "manual_audit", "ticked": 0},
            }
        ],
        min_checked=1,
    )

    assert ok is False
    assert reason == "ross_event_admission_no_recent_live_events"
    assert detail["checked"] == 0
    assert detail["min_checked"] == 1


def test_recent_ross_admissions_meets_required_live_event_evidence() -> None:
    ok, reason, detail = evaluate_recent_ross_admissions(
        [
            {
                "session_id": 10377,
                "ts": "2026-07-01T22:10:00",
                "payload": {"symbol": "CANF", "source": "iqfeed_l1", "ticked": 1},
            }
        ],
        min_checked=1,
    )

    assert ok is True
    assert reason == "ross_event_admission_runtime_ok"
    assert detail["checked"] == 1
    assert detail["min_checked"] == 1
