from __future__ import annotations

from app.services.trading.fast_path.healthz import (
    BOOT_GRACE_S,
    HEALTH_REASON_NO_SUBSCRIBED_PAIRS,
    HealthzServer,
)


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

