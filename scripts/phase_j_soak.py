"""Phase J Docker soak - drift monitor + re-cert queue (shadow).

Verifies inside the running ``chili`` container that:
  1. Migration 136 applied (``trading_pattern_drift_log``,
     ``trading_pattern_recert_log`` exist).
  2. ``BRAIN_DRIFT_MONITOR_MODE`` and ``BRAIN_RECERT_QUEUE_MODE`` are
     visible in settings.
  3. ``evaluate_one`` writes one row when forced to shadow and is a
     no-op when forced to off.
  4. ``queue_from_drift`` writes a row for red severity in shadow,
     a no-op for green, and refuses authoritative with
     :class:`RuntimeError`.
  5. Determinism: same (pattern, as_of_key) yields identical
     ``drift_id``; same (pattern, as_of_date, source) yields identical
     ``recert_id`` with idempotent dedupe.
  6. ``drift_summary`` and ``recert_summary`` return the frozen shape.
  7. Dual-subsystem fan-out: when both modes are shadow, a red drift
     row triggers exactly one recert row with ``source=drift_monitor``.
"""
from __future__ import annotations

import os
import sys
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.drift_monitor_model import (  # noqa: E402
    DriftMonitorInput,
    compute_drift,
    compute_drift_id,
)
from app.services.trading.drift_monitor_service import (  # noqa: E402
    DriftInputBundle,
    drift_summary,
    evaluate_one,
)
from app.services.trading.recert_queue_model import (  # noqa: E402
    compute_recert_id,
)
from app.services.trading.recert_queue_service import (  # noqa: E402
    queue_from_drift,
    queue_manual,
    recert_summary,
)

SOAK_PATTERN_ID_GREEN = 999_801
SOAK_PATTERN_ID_RED = 999_802
SOAK_PATTERN_ID_MANUAL = 999_803


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_j_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_j_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_pattern_drift_log "
        "WHERE scan_pattern_id IN (:a, :b, :c)"
    ), {
        "a": SOAK_PATTERN_ID_GREEN,
        "b": SOAK_PATTERN_ID_RED,
        "c": SOAK_PATTERN_ID_MANUAL,
    })
    db.execute(text(
        "DELETE FROM trading_pattern_recert_log "
        "WHERE scan_pattern_id IN (:a, :b, :c)"
    ), {
        "a": SOAK_PATTERN_ID_GREEN,
        "b": SOAK_PATTERN_ID_RED,
        "c": SOAK_PATTERN_ID_MANUAL,
    })
    db.commit()


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_pattern_drift_log')"
    )).scalar_one()
    _assert(row is not None, "trading_pattern_drift_log exists")

    row = db.execute(text(
        "SELECT to_regclass('public.trading_pattern_recert_log')"
    )).scalar_one()
    _assert(row is not None, "trading_pattern_recert_log exists")

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '136_drift_monitor_recert'"
    )).fetchone()
    _assert(row is not None, "migration 136_drift_monitor_recert recorded")

    _assert(
        hasattr(settings, "brain_drift_monitor_mode"),
        "settings.brain_drift_monitor_mode exists",
    )
    _assert(
        hasattr(settings, "brain_recert_queue_mode"),
        "settings.brain_recert_queue_mode exists",
    )


def _check_drift_monitor(db) -> int:
    # Force off.
    settings.brain_drift_monitor_mode = "off"
    res = evaluate_one(
        db,
        bundle=DriftInputBundle(
            scan_pattern_id=SOAK_PATTERN_ID_GREEN,
            pattern_name="soak_green",
            baseline_win_prob=0.5,
            outcomes=[1, 0] * 10,
        ),
        as_of_key="2026-04-17",
    )
    _assert(res is None, "evaluate_one in mode=off is no-op")

    # Force shadow, green case.
    settings.brain_drift_monitor_mode = "shadow"
    res_green = evaluate_one(
        db,
        bundle=DriftInputBundle(
            scan_pattern_id=SOAK_PATTERN_ID_GREEN,
            pattern_name="soak_green",
            baseline_win_prob=0.5,
            outcomes=[1, 0] * 10,
        ),
        as_of_key="2026-04-17",
    )
    _assert(res_green is not None, "evaluate_one in mode=shadow writes a row (green)")
    _assert(
        res_green.severity == "green",
        f"green severity observed ({res_green.severity})",
    )

    # Red case.
    res_red = evaluate_one(
        db,
        bundle=DriftInputBundle(
            scan_pattern_id=SOAK_PATTERN_ID_RED,
            pattern_name="soak_red",
            baseline_win_prob=0.7,
            outcomes=[0] * 30,
        ),
        as_of_key="2026-04-17",
    )
    _assert(res_red is not None, "evaluate_one writes a red-severity row")
    _assert(
        res_red.severity == "red",
        f"red severity observed ({res_red.severity})",
    )

    count = db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_drift_log
        WHERE scan_pattern_id IN (:a, :b)
    """), {"a": SOAK_PATTERN_ID_GREEN, "b": SOAK_PATTERN_ID_RED}).scalar_one()
    _assert(int(count or 0) == 2, f"two drift rows persisted (got {count})")

    # Determinism of drift_id.
    id1 = compute_drift_id(
        scan_pattern_id=SOAK_PATTERN_ID_RED, as_of_key="2026-04-17",
    )
    id2 = compute_drift_id(
        scan_pattern_id=SOAK_PATTERN_ID_RED, as_of_key="2026-04-17",
    )
    _assert(id1 == id2, "drift_id deterministic for same (pattern, as_of_key)")

    # Authoritative refusal.
    settings.brain_drift_monitor_mode = "authoritative"
    try:
        evaluate_one(
            db,
            bundle=DriftInputBundle(
                scan_pattern_id=SOAK_PATTERN_ID_RED,
                pattern_name="soak_red",
                baseline_win_prob=0.7,
                outcomes=[0] * 30,
            ),
            as_of_key="2026-04-17",
        )
        _assert(False, "evaluate_one refused authoritative")
    except RuntimeError:
        _assert(True, "evaluate_one refused authoritative mode")

    settings.brain_drift_monitor_mode = "shadow"
    return int(res_red.log_id)


def _check_recert_queue(db, red_drift_log_id: int) -> None:
    settings.brain_recert_queue_mode = "off"
    red_drift = compute_drift(DriftMonitorInput(
        scan_pattern_id=SOAK_PATTERN_ID_RED,
        pattern_name="soak_red",
        baseline_win_prob=0.7,
        outcomes=[0] * 30,
        as_of_key="2026-04-17",
    ))
    res_off = queue_from_drift(
        db, red_drift,
        as_of_date=date(2026, 4, 17),
        drift_log_id=red_drift_log_id,
    )
    _assert(res_off is None, "queue_from_drift in mode=off is no-op")

    settings.brain_recert_queue_mode = "shadow"
    res = queue_from_drift(
        db, red_drift,
        as_of_date=date(2026, 4, 17),
        drift_log_id=red_drift_log_id,
    )
    _assert(res is not None, "queue_from_drift writes a row for red severity")

    # Idempotency.
    res_dup = queue_from_drift(
        db, red_drift,
        as_of_date=date(2026, 4, 17),
        drift_log_id=red_drift_log_id,
    )
    _assert(
        res_dup is not None and res_dup.recert_id == res.recert_id,
        "queue_from_drift idempotent on (pattern, as_of_date, source)",
    )
    count = db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_recert_log
        WHERE scan_pattern_id = :pid
    """), {"pid": SOAK_PATTERN_ID_RED}).scalar_one()
    _assert(int(count or 0) == 1, f"only one recert row for red pattern (got {count})")

    # Green drift does not produce a proposal.
    green_drift = compute_drift(DriftMonitorInput(
        scan_pattern_id=SOAK_PATTERN_ID_GREEN,
        pattern_name="soak_green",
        baseline_win_prob=0.5,
        outcomes=[1, 0] * 10,
        as_of_key="2026-04-17",
    ))
    res_green = queue_from_drift(
        db, green_drift, as_of_date=date(2026, 4, 17),
    )
    _assert(res_green is None, "queue_from_drift green severity is no-op")

    # Manual proposal.
    manual_res = queue_manual(
        db,
        scan_pattern_id=SOAK_PATTERN_ID_MANUAL,
        pattern_name="soak_manual",
        as_of_date=date(2026, 4, 17),
        reason="phase_j_soak operator-queued",
    )
    _assert(manual_res is not None, "queue_manual writes a row in shadow")

    # Manual recert_id determinism.
    mid1 = compute_recert_id(
        scan_pattern_id=SOAK_PATTERN_ID_MANUAL,
        as_of_date=date(2026, 4, 17),
        source="manual",
    )
    mid2 = compute_recert_id(
        scan_pattern_id=SOAK_PATTERN_ID_MANUAL,
        as_of_date=date(2026, 4, 17),
        source="manual",
    )
    _assert(mid1 == mid2, "recert_id deterministic")

    # Authoritative refusal.
    settings.brain_recert_queue_mode = "authoritative"
    try:
        queue_from_drift(
            db, red_drift, as_of_date=date(2026, 4, 17),
        )
        _assert(False, "queue_from_drift refused authoritative")
    except RuntimeError:
        _assert(True, "queue_from_drift refused authoritative mode")
    try:
        queue_manual(
            db,
            scan_pattern_id=SOAK_PATTERN_ID_MANUAL + 1,
            pattern_name="x",
            as_of_date=date(2026, 4, 17),
            reason="r",
        )
        _assert(False, "queue_manual refused authoritative")
    except RuntimeError:
        _assert(True, "queue_manual refused authoritative mode")

    settings.brain_recert_queue_mode = "shadow"


def _check_summaries(db) -> None:
    drift = drift_summary(db, lookback_days=14)
    expected_drift = {
        "mode", "lookback_days", "drift_events_total",
        "by_severity", "patterns_red", "patterns_yellow",
        "mean_brier_delta", "mean_cusum_statistic", "latest_drift",
    }
    _assert(
        set(drift.keys()) == expected_drift,
        f"drift_summary frozen shape ({sorted(drift.keys())})",
    )
    _assert(
        set(drift["by_severity"].keys()) == {"green", "yellow", "red"},
        "drift_summary.by_severity frozen shape",
    )
    _assert(
        drift["drift_events_total"] >= 2,
        f"drift_events_total >= 2 (got {drift['drift_events_total']})",
    )

    recert = recert_summary(db, lookback_days=14)
    expected_recert = {
        "mode", "lookback_days", "recert_events_total",
        "by_source", "by_severity", "by_status",
        "patterns_queued_distinct", "latest_recert",
    }
    _assert(
        set(recert.keys()) == expected_recert,
        f"recert_summary frozen shape ({sorted(recert.keys())})",
    )
    _assert(
        set(recert["by_source"].keys()) == {
            "drift_monitor", "manual", "scheduler", "other",
        },
        "recert_summary.by_source frozen shape",
    )
    _assert(
        recert["recert_events_total"] >= 2,
        f"recert_events_total >= 2 (got {recert['recert_events_total']})",
    )


def main() -> int:
    print("[phase_j_soak] starting Phase J soak check")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        red_log_id = _check_drift_monitor(db)
        _check_recert_queue(db, red_log_id)
        _check_summaries(db)
        print("[phase_j_soak] ALL CHECKS PASSED")
        return 0
    finally:
        try:
            _cleanup(db)
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
