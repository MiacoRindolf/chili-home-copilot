from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.fast_path.healthz import (
    BOOT_GRACE_S,
    EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S,
    EXECUTOR_LEARNING_MAX_LAG_S,
    FAST_LEARNING_FRESHNESS_KEY,
    HEALTH_REASON_EXECUTOR_LEARNING_STALE,
    HEALTH_REASON_NO_SUBSCRIBED_PAIRS,
    LEARNING_ALERT_TO_DECISION_LAG_S_KEY,
    LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY,
    LEARNING_LATEST_DECISION_AT_KEY,
    LEARNING_LATEST_ALERT_AT_KEY,
    LEARNING_LATEST_EXECUTION_AT_KEY,
    LEARNING_LATEST_EXIT_AT_KEY,
    LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY,
    LEARNING_LATEST_MAKER_FILL_AT_KEY,
    LEARNING_LATEST_MAKER_OUTCOME_AT_KEY,
    LEARNING_LATEST_MAKER_OUTCOME_KEY,
    LEARNING_MAKER_ATTEMPTS_WINDOW_KEY,
    LEARNING_MAKER_CANCELS_WINDOW_KEY,
    LEARNING_MAKER_FILLS_WINDOW_KEY,
    LEARNING_MAKER_OUTCOME_WINDOW_S_KEY,
    LEARNING_MAKER_PENDING_WINDOW_KEY,
    LEARNING_MAKER_REJECTED_WINDOW_KEY,
    LEARNING_MAKER_REPLACED_WINDOW_KEY,
    HealthzServer,
)

SAMPLE_PAIR = "SUI-USD"
ROTATED_PAIR = "TAO-USD"


def test_healthz_fails_after_boot_when_universe_has_no_pairs():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    ok, body = server._evaluate({
        "enabled": True,
        "writer": {
            "queue_depth": 0,
            "queue_max": 100,
            "consecutive_batch_failures": 0,
        },
        "status": {"pairs": {}},
        "ws": {"book": {}},
    })

    assert ok is False
    assert body["reason"] == HEALTH_REASON_NO_SUBSCRIBED_PAIRS
    assert body["details"]["subscribed_pairs"] == 0


def test_healthz_fails_when_fresh_alerts_are_not_becoming_executions():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    now = _utcnow_naive()
    alert_at = now - timedelta(seconds=5.0)
    execution_at = alert_at - timedelta(
        seconds=EXECUTOR_LEARNING_MAX_LAG_S + 1.0
    )

    ok, body = server._evaluate(_healthy_snapshot(
        now=now,
        learning={
            "ok": True,
            LEARNING_LATEST_ALERT_AT_KEY: alert_at.isoformat(),
            LEARNING_LATEST_EXECUTION_AT_KEY: execution_at.isoformat(),
            LEARNING_LATEST_EXIT_AT_KEY: None,
            LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY: (
                EXECUTOR_LEARNING_MAX_LAG_S + 1.0
            ),
        },
    ))

    assert ok is False
    assert body["executor_learning_freshness"] is False
    assert body["reason"] == HEALTH_REASON_EXECUTOR_LEARNING_STALE
    assert body["details"]["executor_learning_phase"] == "stale_learning_decision"


def test_healthz_accepts_fresh_maker_attempt_as_learning_decision():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    now = _utcnow_naive()
    alert_at = now - timedelta(seconds=5.0)
    maker_attempt_at = now - timedelta(seconds=4.0)
    maker_outcome_at = now - timedelta(seconds=2.0)
    execution_at = alert_at - timedelta(
        seconds=EXECUTOR_LEARNING_MAX_LAG_S + 1.0
    )

    ok, body = server._evaluate(_healthy_snapshot(
        now=now,
        learning={
            "ok": True,
            LEARNING_LATEST_ALERT_AT_KEY: alert_at.isoformat(),
            LEARNING_LATEST_EXECUTION_AT_KEY: execution_at.isoformat(),
            LEARNING_LATEST_MAKER_ATTEMPT_AT_KEY: maker_attempt_at.isoformat(),
            LEARNING_LATEST_MAKER_FILL_AT_KEY: None,
            LEARNING_LATEST_MAKER_OUTCOME_AT_KEY: maker_outcome_at.isoformat(),
            LEARNING_LATEST_MAKER_OUTCOME_KEY: "cancelled",
            LEARNING_LATEST_DECISION_AT_KEY: maker_attempt_at.isoformat(),
            LEARNING_LATEST_EXIT_AT_KEY: None,
            LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY: (
                EXECUTOR_LEARNING_MAX_LAG_S + 1.0
            ),
            LEARNING_ALERT_TO_DECISION_LAG_S_KEY: 0.0,
            LEARNING_MAKER_OUTCOME_WINDOW_S_KEY: 900,
            LEARNING_MAKER_ATTEMPTS_WINDOW_KEY: 3,
            LEARNING_MAKER_FILLS_WINDOW_KEY: 1,
            LEARNING_MAKER_CANCELS_WINDOW_KEY: 2,
            LEARNING_MAKER_REPLACED_WINDOW_KEY: 0,
            LEARNING_MAKER_REJECTED_WINDOW_KEY: 0,
            LEARNING_MAKER_PENDING_WINDOW_KEY: 0,
        },
    ))

    assert ok is True
    assert body["executor_learning_freshness"] is True
    assert body["details"]["executor_learning_phase"] == "ok"
    assert body["details"]["subscribed_pairs"] == 1
    assert body["details"][LEARNING_LATEST_MAKER_OUTCOME_KEY] == "cancelled"
    assert (
        body["details"][LEARNING_LATEST_MAKER_OUTCOME_AT_KEY]
        == maker_outcome_at.isoformat()
    )
    assert body["details"]["latest_maker_outcome_age_s"] is not None
    assert body["details"][LEARNING_MAKER_ATTEMPTS_WINDOW_KEY] == 3
    assert body["details"][LEARNING_MAKER_FILLS_WINDOW_KEY] == 1
    assert body["details"][LEARNING_MAKER_CANCELS_WINDOW_KEY] == 2


def test_healthz_ignores_rotated_paused_pair_errors_when_streaming_pairs_are_fresh():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    now = _utcnow_naive()
    snap = _healthy_snapshot(now=now, learning={})
    snap["status"]["pairs"][ROTATED_PAIR] = {
        "state": "paused",
        "last_bar_at": (now - timedelta(minutes=30)).isoformat(),
        "error_count_60s": 5,
        "last_error": "universe_rotated",
    }

    ok, body = server._evaluate(snap)

    assert ok is True
    assert body["ws_connected"] is True
    assert body["details"]["tracked_pairs"] == 2
    assert body["details"]["subscribed_pairs"] == 1
    assert body["details"]["ignored_pair_states"] == {"paused": 1}


def test_healthz_fails_when_every_tracked_pair_is_paused():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    now = _utcnow_naive()
    ok, body = server._evaluate({
        "enabled": True,
        "writer": {
            "queue_depth": 0,
            "queue_max": 100,
            "consecutive_batch_failures": 0,
        },
        "status": {
            "pairs": {
                ROTATED_PAIR: {
                    "state": "paused",
                    "last_bar_at": now.isoformat(),
                    "error_count_60s": 0,
                },
            },
        },
        "ws": {"book": {"last_emit_at_wall": now.isoformat()}},
    })

    assert ok is False
    assert body["reason"] == HEALTH_REASON_NO_SUBSCRIBED_PAIRS
    assert body["details"]["tracked_pairs"] == 1
    assert body["details"]["subscribed_pairs"] == 0


def test_healthz_allows_stale_executions_when_alert_stream_is_quiet():
    server = HealthzServer(port=8090, snapshot_fn=lambda: {})
    server._started_at -= BOOT_GRACE_S + 1.0

    now = _utcnow_naive()
    alert_at = now - timedelta(
        seconds=EXECUTOR_LEARNING_ACTIVE_ALERT_WINDOW_S + 1.0
    )
    execution_at = alert_at - timedelta(
        seconds=EXECUTOR_LEARNING_MAX_LAG_S + 1.0
    )

    ok, body = server._evaluate(_healthy_snapshot(
        now=now,
        learning={
            "ok": True,
            LEARNING_LATEST_ALERT_AT_KEY: alert_at.isoformat(),
            LEARNING_LATEST_EXECUTION_AT_KEY: execution_at.isoformat(),
            LEARNING_LATEST_EXIT_AT_KEY: None,
            LEARNING_ALERT_TO_EXECUTION_LAG_S_KEY: (
                EXECUTOR_LEARNING_MAX_LAG_S + 1.0
            ),
        },
    ))

    assert ok is True
    assert body["executor_learning_freshness"] is True
    assert body["details"]["executor_learning_phase"] == "alert_stream_quiet"


def _healthy_snapshot(*, now: datetime, learning: dict) -> dict:
    now_iso = now.isoformat()
    return {
        "enabled": True,
        "writer": {
            "queue_depth": 0,
            "queue_max": 100,
            "consecutive_batch_failures": 0,
        },
        "status": {
            "pairs": {
                SAMPLE_PAIR: {
                    "state": "streaming",
                    "last_bar_at": now_iso,
                    "error_count_60s": 0,
                }
            }
        },
        "ws": {"book": {"last_emit_at_wall": now_iso}},
        FAST_LEARNING_FRESHNESS_KEY: learning,
    }


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
