"""Phase M.2-autopilot Docker soak — verifies the auto-advance engine
inside the running ``chili`` container.

Usage (inside the container):

    docker compose exec chili python scripts/phase_m2_autopilot_soak.py

Each check is printed as ``[PASS]`` or ``[FAIL]``. Non-zero exit on
any failure so the CI-style runner can gate on it.

Checks:

1.  Migration 146 applied (schema_version row).
2.  ``trading_brain_runtime_modes`` table present with expected
    columns (slice_name PK, mode, updated_at, updated_by, reason,
    payload_json).
3.  ``trading_pattern_regime_autopilot_log`` table present with
    expected columns (event, from_mode, to_mode, reason_code,
    gates_json, evidence_json, approval_id, days_in_stage).
4.  ``BrainRuntimeMode`` + ``PatternRegimeAutopilotLog`` ORM models
    importable from ``app.models.trading``.
5.  15+ ``brain_pattern_regime_autopilot_*`` settings visible on the
    ``settings`` singleton.
6.  Ops-log module importable + prefix is exactly
    ``[pattern_regime_autopilot_ops]``.
7.  ``format_autopilot_ops_line`` emits expected prefix + event=,
    mode=, slice_name=, reason_code= fields.
8.  ``runtime_mode_override.get_runtime_mode_override`` returns None
    for an unset slice.
9.  ``runtime_mode_override.set_runtime_mode_override`` + read-back
    round-trips cleanly.
10. Cache invalidation: after ``invalidate_cache``, a stale read is
    refreshed from DB.
11. Clear override: ``clear_runtime_mode_override`` removes the row.
12. Pure model: ``evaluate_slice_gates`` returns ``skipped`` for
    off-mode slice.
13. Pure model: authoritative without approval -> revert action.
14. Pure model: tilt never order-locked.
15. Pure model: killswitch order-locked when tilt != authoritative.
16. Service: ``is_enabled()`` reflects the flag.
17. Service: ``run_autopilot_tick`` is a no-op when flag=False.
18. Service: ``diagnostics_summary`` returns the frozen shape.
19. Service: all three slices present in diagnostics.slices.
20. Scheduler: ``_run_pattern_regime_autopilot_tick_job`` importable
    without error (function exists in ``trading_scheduler`` module).
21. Scheduler: ``_run_pattern_regime_autopilot_weekly_job``
    importable without error.
22. Router: ``/api/trading/brain/m2-autopilot/status`` endpoint
    registered on the FastAPI app.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import date
from typing import Any, Callable

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

os.environ.setdefault("BRAIN_PATTERN_REGIME_AUTOPILOT_OPS_LOG_ENABLED", "false")

FAILS = 0
TOTAL = 0


def _run(label: str, fn: Callable[[], None]) -> None:
    global FAILS, TOTAL
    TOTAL += 1
    try:
        fn()
        print(f"[PASS] {label}")
    except AssertionError as exc:
        FAILS += 1
        print(f"[FAIL] {label}: {exc}")
    except Exception as exc:  # pragma: no cover
        FAILS += 1
        tb = traceback.format_exc().splitlines()[-3:]
        print(f"[FAIL] {label}: unexpected {type(exc).__name__}: {exc}")
        for line in tb:
            print(f"         {line}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_migration_146_applied() -> None:
    from sqlalchemy import text as sql_text

    from app.db import engine

    with engine.connect() as conn:
        row = conn.execute(
            sql_text(
                "SELECT version_id FROM schema_version "
                "WHERE version_id = '146_m2_autopilot'"
            )
        ).fetchone()
        assert row is not None, "migration 146 not recorded"


def check_runtime_modes_table() -> None:
    from sqlalchemy import inspect

    from app.db import engine

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "trading_brain_runtime_modes" in tables, "runtime_modes missing"
    cols = {c["name"] for c in insp.get_columns("trading_brain_runtime_modes")}
    for col in (
        "slice_name",
        "mode",
        "updated_at",
        "updated_by",
        "reason",
        "payload_json",
    ):
        assert col in cols, f"runtime_modes missing column: {col}"


def check_autopilot_log_table() -> None:
    from sqlalchemy import inspect

    from app.db import engine

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert (
        "trading_pattern_regime_autopilot_log" in tables
    ), "autopilot_log missing"
    cols = {
        c["name"]
        for c in insp.get_columns("trading_pattern_regime_autopilot_log")
    }
    for col in (
        "id",
        "as_of_date",
        "evaluated_at",
        "slice_name",
        "event",
        "from_mode",
        "to_mode",
        "reason_code",
        "gates_json",
        "evidence_json",
        "approval_id",
        "days_in_stage",
    ):
        assert col in cols, f"autopilot_log missing column: {col}"


def check_orm_models_importable() -> None:
    from app.models.trading import BrainRuntimeMode, PatternRegimeAutopilotLog

    assert BrainRuntimeMode.__tablename__ == "trading_brain_runtime_modes"
    assert (
        PatternRegimeAutopilotLog.__tablename__
        == "trading_pattern_regime_autopilot_log"
    )


def check_settings_flags() -> None:
    from app.config import settings

    for name in (
        "brain_pattern_regime_autopilot_enabled",
        "brain_pattern_regime_autopilot_kill",
        "brain_pattern_regime_autopilot_ops_log_enabled",
        "brain_pattern_regime_autopilot_cron_hour",
        "brain_pattern_regime_autopilot_cron_minute",
        "brain_pattern_regime_autopilot_weekly_cron_hour",
        "brain_pattern_regime_autopilot_weekly_cron_dow",
        "brain_pattern_regime_autopilot_shadow_days",
        "brain_pattern_regime_autopilot_compare_days",
        "brain_pattern_regime_autopilot_min_decisions",
        "brain_pattern_regime_autopilot_tilt_mult_min",
        "brain_pattern_regime_autopilot_tilt_mult_max",
        "brain_pattern_regime_autopilot_promo_block_max_ratio",
        "brain_pattern_regime_autopilot_ks_max_fires_per_day",
        "brain_pattern_regime_autopilot_approval_days",
    ):
        assert hasattr(settings, name), f"missing setting: {name}"


def check_ops_log_prefix() -> None:
    from app.trading_brain.infrastructure.pattern_regime_autopilot_ops_log import (
        CHILI_PATTERN_REGIME_AUTOPILOT_OPS_PREFIX,
        format_autopilot_ops_line,
    )

    assert (
        CHILI_PATTERN_REGIME_AUTOPILOT_OPS_PREFIX
        == "[pattern_regime_autopilot_ops]"
    )
    line = format_autopilot_ops_line(
        event="autopilot_advance",
        mode="enabled",
        slice_name="pattern_regime_tilt",
        from_mode="shadow",
        to_mode="compare",
        reason_code="advanced_to_compare",
    )
    assert "[pattern_regime_autopilot_ops]" in line
    assert "event=autopilot_advance" in line
    assert "mode=enabled" in line
    assert "slice=pattern_regime_tilt" in line
    assert "reason_code=advanced_to_compare" in line


def check_runtime_override_unset() -> None:
    from app.services.trading.runtime_mode_override import (
        get_runtime_mode_override,
        invalidate_cache,
    )

    invalidate_cache()
    val = get_runtime_mode_override(
        "pattern_regime_tilt_nonexistent_slice_zzz", bypass_cache=True
    )
    assert val is None, f"expected None for unset slice, got {val!r}"


def check_runtime_override_roundtrip() -> None:
    from app.db import SessionLocal
    from app.services.trading.runtime_mode_override import (
        clear_runtime_mode_override,
        get_runtime_mode_override,
        invalidate_cache,
        set_runtime_mode_override,
    )

    db = SessionLocal()
    try:
        slice_name = "pattern_regime_tilt"
        existing = get_runtime_mode_override(slice_name, db=db, bypass_cache=True)
        set_runtime_mode_override(
            db,
            slice_name=slice_name,
            mode="shadow",
            updated_by="soak-test",
            reason="phase_m2_autopilot_soak",
            payload={"test": True},
        )
        db.commit()
        invalidate_cache(slice_name)
        got = get_runtime_mode_override(slice_name, db=db, bypass_cache=True)
        assert got == "shadow", f"round-trip failed: {got!r}"
        if existing is None:
            clear_runtime_mode_override(db, slice_name=slice_name)
        else:
            set_runtime_mode_override(
                db,
                slice_name=slice_name,
                mode=existing,
                updated_by="soak-restore",
                reason="restore",
            )
        db.commit()
        invalidate_cache(slice_name)
    finally:
        db.close()


def check_clear_override() -> None:
    from app.db import SessionLocal
    from app.services.trading.runtime_mode_override import (
        clear_runtime_mode_override,
        get_runtime_mode_override,
        invalidate_cache,
        set_runtime_mode_override,
    )

    db = SessionLocal()
    try:
        s = "pattern_regime_promotion"
        original = get_runtime_mode_override(s, db=db, bypass_cache=True)
        set_runtime_mode_override(
            db, slice_name=s, mode="compare", updated_by="soak-clear-test"
        )
        db.commit()
        invalidate_cache(s)
        assert get_runtime_mode_override(s, db=db, bypass_cache=True) == "compare"
        clear_runtime_mode_override(db, slice_name=s)
        db.commit()
        invalidate_cache(s)
        assert get_runtime_mode_override(s, db=db, bypass_cache=True) is None
        if original is not None:
            set_runtime_mode_override(
                db, slice_name=s, mode=original, updated_by="soak-restore"
            )
            db.commit()
            invalidate_cache(s)
    finally:
        db.close()


def check_pure_skipped_for_off() -> None:
    from app.services.trading.pattern_regime_autopilot_model import (
        AutopilotConfig,
        OrderLockState,
        SliceEvidence,
        compute_order_lock_state,
        evaluate_slice_gates,
    )

    cfg = AutopilotConfig(shadow_days=5, compare_days=10, min_decisions=100,
                          tilt_mult_min=0.85, tilt_mult_max=1.25,
                          promo_block_max_ratio=0.10, ks_max_fires_per_day=1.0,
                          approval_days=30)
    ev = SliceEvidence(
        slice_name="tilt", current_mode="off", days_in_stage=0,
        total_decisions=0, last_advance_date=None,
        today_utc=date(2026, 4, 16),
        diagnostics_healthy=True, diagnostics_stale_hours=0.0,
        release_blocker_clean=True, scan_status_frozen_ok=True,
    )
    lock = compute_order_lock_state(tilt_mode="off", killswitch_mode="off",
                                     promotion_mode="off")
    out = evaluate_slice_gates(ev, cfg, lock)
    assert out.action == "hold" and out.reason_code == "off_stays_off", out


def check_pure_revert_auth_no_approval() -> None:
    from app.services.trading.pattern_regime_autopilot_model import (
        AutopilotConfig,
        SliceEvidence,
        compute_order_lock_state,
        evaluate_slice_gates,
    )

    cfg = AutopilotConfig(shadow_days=5, compare_days=10, min_decisions=100,
                          tilt_mult_min=0.85, tilt_mult_max=1.25,
                          promo_block_max_ratio=0.10, ks_max_fires_per_day=1.0,
                          approval_days=30)
    ev = SliceEvidence(
        slice_name="tilt", current_mode="authoritative", days_in_stage=15,
        total_decisions=5000, last_advance_date=date(2026, 4, 1),
        today_utc=date(2026, 4, 16),
        diagnostics_healthy=True, diagnostics_stale_hours=0.0,
        release_blocker_clean=True, scan_status_frozen_ok=True,
        authoritative_approval_missing=True, approval_live=False,
    )
    lock = compute_order_lock_state(tilt_mode="authoritative",
                                     killswitch_mode="shadow",
                                     promotion_mode="shadow")
    out = evaluate_slice_gates(ev, cfg, lock)
    assert out.action == "revert", out
    assert out.reason_code == "authoritative_approval_missing", out
    assert out.to_mode == "compare", out


def check_pure_tilt_not_locked() -> None:
    from app.services.trading.pattern_regime_autopilot_model import (
        compute_order_lock_state,
    )

    lock = compute_order_lock_state(tilt_mode="shadow", killswitch_mode="shadow",
                                     promotion_mode="shadow")
    assert lock.can_advance_beyond_shadow("tilt") is True


def check_pure_killswitch_order_lock() -> None:
    from app.services.trading.pattern_regime_autopilot_model import (
        compute_order_lock_state,
    )

    lock = compute_order_lock_state(tilt_mode="compare",
                                     killswitch_mode="shadow",
                                     promotion_mode="shadow")
    assert lock.can_advance_beyond_shadow("killswitch") is False, lock
    lock2 = compute_order_lock_state(tilt_mode="authoritative",
                                      killswitch_mode="shadow",
                                      promotion_mode="shadow")
    assert lock2.can_advance_beyond_shadow("killswitch") is True, lock2


def check_service_is_enabled_reflects_flag() -> None:
    from app.config import settings
    from app.services.trading.pattern_regime_autopilot_service import is_enabled

    # By default the flag is False, kill False -> is_enabled == False.
    assert isinstance(is_enabled(), bool)
    assert is_enabled() == bool(
        getattr(settings, "brain_pattern_regime_autopilot_enabled", False)
    ) and not bool(
        getattr(settings, "brain_pattern_regime_autopilot_kill", False)
    )


def check_service_tick_noop_when_disabled() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_autopilot_service import (
        run_autopilot_tick,
    )

    db = SessionLocal()
    try:
        out = run_autopilot_tick(db)
    finally:
        db.close()
    assert isinstance(out, dict)
    # When flag False, skipped True OR enabled False.
    if not out.get("enabled", False):
        assert out.get("skipped") is True, out


def check_diagnostics_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_autopilot_service import (
        diagnostics_summary,
    )

    db = SessionLocal()
    try:
        payload = diagnostics_summary(db)
    finally:
        db.close()
    for k in ("enabled", "kill", "cron_hour", "cron_minute", "slices"):
        assert k in payload, f"missing key in diagnostics: {k}"
    for s in ("tilt", "promotion", "killswitch"):
        assert s in payload["slices"], f"missing slice: {s}"
        for sub in (
            "stage",
            "days_in_stage",
            "last_advance_date",
            "approval_live",
            "env_mode",
            "override_present",
        ):
            assert sub in payload["slices"][s], f"slice {s} missing {sub}"


def check_scheduler_job_importable() -> None:
    import app.services.trading_scheduler as sched

    assert hasattr(sched, "_run_pattern_regime_autopilot_tick_job")
    assert hasattr(sched, "_run_pattern_regime_autopilot_weekly_job")


def check_router_endpoint_registered() -> None:
    from app.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/trading/brain/m2-autopilot/status" in paths, (
        "m2-autopilot status endpoint not registered"
    )


CHECKS: list[tuple[str, Callable[[], None]]] = [
    ("migration_146_applied", check_migration_146_applied),
    ("runtime_modes_table_present", check_runtime_modes_table),
    ("autopilot_log_table_present", check_autopilot_log_table),
    ("orm_models_importable", check_orm_models_importable),
    ("settings_flags_present", check_settings_flags),
    ("ops_log_prefix_and_format", check_ops_log_prefix),
    ("runtime_override_unset_is_none", check_runtime_override_unset),
    ("runtime_override_roundtrip", check_runtime_override_roundtrip),
    ("runtime_override_clear_works", check_clear_override),
    ("pure_off_mode_skipped", check_pure_skipped_for_off),
    ("pure_auth_without_approval_reverts", check_pure_revert_auth_no_approval),
    ("pure_tilt_never_locked", check_pure_tilt_not_locked),
    ("pure_killswitch_order_lock", check_pure_killswitch_order_lock),
    ("service_is_enabled_reflects_flag", check_service_is_enabled_reflects_flag),
    ("service_tick_noop_when_disabled", check_service_tick_noop_when_disabled),
    ("diagnostics_shape_frozen", check_diagnostics_shape),
    ("scheduler_jobs_importable", check_scheduler_job_importable),
    ("router_endpoint_registered", check_router_endpoint_registered),
]


def main() -> int:
    for label, fn in CHECKS:
        _run(label, fn)
    print()
    print(f"Phase M.2-autopilot soak: {TOTAL - FAILS}/{TOTAL} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
