"""Phase M.2 Docker soak — verifies the three pattern x regime
authoritative-consumer slices (tilt / promotion / kill-switch) inside
the running ``chili`` container.

Usage (inside the container):

    docker compose exec chili python scripts/phase_m2_soak.py

Each check is printed as ``[PASS]`` or ``[FAIL]``. Non-zero exit on
any failure so the CI-style runner can gate on it.

Checks cover:

1.  Migration 145 applied (schema_version row).
2.  All three new log tables present with expected columns.
3.  ``trading_governance_approvals.expires_at`` column added.
4.  ``PositionSizerLog`` additive columns present.
5.  21 ``brain_pattern_regime_*`` settings visible.
6.  Ops-log module importable + 3 distinct prefixes.
7.  Ops-log formatters emit expected prefix + event=, mode= fields.
8.  ``normalize_mode`` / ``mode_is_*`` helpers behave.
9.  ``make_evaluation_id`` is deterministic across repeat calls.
10. ``has_live_approval`` is False when no rows exist.
11. ``load_resolved_context`` returns empty context for unseen pid.
12. ``resolved_context_hash`` deterministic for same context.
13. Tilt model: empty context -> insufficient_coverage, mult=1.0.
14. Tilt model: zero expectancies -> no_signal, mult=1.0.
15. Tilt model: positive expectancies -> multiplier > 1.0 & finite.
16. Tilt model: negative expectancies -> multiplier < 1.0 & finite.
17. Promotion model: low coverage defers to baseline.
18. Promotion model: baseline=False never upgraded.
19. Promotion model: 2+ negative dims blocks a baseline-allow.
20. Kill-switch model: empty history -> no quarantine.
21. Kill-switch model: streak threshold -> quarantine.
22. Kill-switch model: at_circuit_breaker=True -> no quarantine.
23. Tilt service: mode=off returns None (no-op).
24. Tilt service: mode=authoritative w/o approval -> refused.
25. Promotion service: mode=off returns None.
26. Promotion service: mode=authoritative w/o approval -> refused +
    consumer_allow falls back to baseline.
27. Kill-switch service: mode=off returns None.
28. Kill-switch service: ``run_daily_sweep`` in mode=off returns
    skipped-no-op shape.
29. Diagnostics summary: tilt has frozen top-level keys.
30. Diagnostics summary: promotion has frozen top-level keys.
31. Diagnostics summary: killswitch has frozen top-level keys.
32. Additive-only: M.1 ledger row count unchanged around M.2 ops.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import date, timedelta
from typing import Any, Callable, Dict, List

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

os.environ.setdefault("BRAIN_PATTERN_REGIME_TILT_OPS_LOG_ENABLED", "false")
os.environ.setdefault("BRAIN_PATTERN_REGIME_PROMOTION_OPS_LOG_ENABLED", "false")
os.environ.setdefault("BRAIN_PATTERN_REGIME_KILLSWITCH_OPS_LOG_ENABLED", "false")

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
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    n_dims: int = 4,
    expectancy: float = 0.0,
    labels_by_dim: Dict[str, str] | None = None,
):
    from app.services.trading.pattern_regime_ledger_lookup import (
        LedgerCell,
        ResolvedContext,
    )
    from app.services.trading.pattern_regime_performance_model import (
        DEFAULT_DIMENSIONS,
    )

    cells: Dict[str, LedgerCell] = {}
    for d in DEFAULT_DIMENSIONS[:n_dims]:
        cells[d] = LedgerCell(
            pattern_id=99999,
            regime_dimension=d,
            regime_label=(labels_by_dim or {}).get(d, "lab"),
            as_of_date=date(2024, 5, 1),
            window_days=90,
            n_trades=5,
            hit_rate=0.6,
            mean_pnl_pct=0.01,
            expectancy=expectancy,
            profit_factor=1.5,
            has_confidence=True,
        )
    return ResolvedContext(
        pattern_id=99999,
        as_of_date=date(2024, 5, 10),
        max_staleness_days=5,
        cells_by_dimension=cells,
        unavailable_dimensions=tuple(),
        stale_dimensions=tuple(),
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_migration_145_applied() -> None:
    from sqlalchemy import text as sql_text

    from app.db import engine

    with engine.connect() as conn:
        row = conn.execute(
            sql_text(
                "SELECT version_id FROM schema_version "
                "WHERE version_id = '145_pattern_regime_m2_consumers'"
            )
        ).fetchone()
        assert row is not None, "migration 145 not recorded"


def check_log_tables_present() -> None:
    from sqlalchemy import inspect

    from app.db import engine

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for t in (
        "trading_pattern_regime_tilt_log",
        "trading_pattern_regime_promotion_log",
        "trading_pattern_regime_killswitch_log",
    ):
        assert t in tables, f"table missing: {t}"

    tilt_cols = {c["name"] for c in insp.get_columns("trading_pattern_regime_tilt_log")}
    expected = {
        "evaluation_id",
        "as_of_date",
        "pattern_id",
        "mode",
        "applied",
        "baseline_size_dollars",
        "consumer_size_dollars",
        "multiplier",
        "reason_code",
        "diff_category",
        "context_hash",
    }
    missing = expected - tilt_cols
    assert not missing, f"tilt log missing cols: {missing}"


def check_governance_approvals_expires_at() -> None:
    from sqlalchemy import inspect

    from app.db import engine

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("trading_governance_approvals")}
    assert "expires_at" in cols, "trading_governance_approvals.expires_at missing"


def check_position_sizer_log_additive_cols() -> None:
    from sqlalchemy import inspect

    from app.db import engine

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("trading_position_sizer_log")}
    for c in ("pattern_regime_tilt_multiplier", "pattern_regime_tilt_reason"):
        assert c in cols, f"position_sizer_log missing additive col: {c}"


def check_settings_visible() -> None:
    from app.config import settings

    required = [
        "brain_pattern_regime_tilt_mode",
        "brain_pattern_regime_tilt_ops_log_enabled",
        "brain_pattern_regime_tilt_kill",
        "brain_pattern_regime_tilt_min_multiplier",
        "brain_pattern_regime_tilt_max_multiplier",
        "brain_pattern_regime_tilt_min_confident_dimensions",
        "brain_pattern_regime_tilt_max_staleness_days",
        "brain_pattern_regime_promotion_mode",
        "brain_pattern_regime_promotion_ops_log_enabled",
        "brain_pattern_regime_promotion_kill",
        "brain_pattern_regime_promotion_min_confident_dimensions",
        "brain_pattern_regime_promotion_block_on_negative_dimensions",
        "brain_pattern_regime_promotion_min_mean_expectancy",
        "brain_pattern_regime_killswitch_mode",
        "brain_pattern_regime_killswitch_ops_log_enabled",
        "brain_pattern_regime_killswitch_kill",
        "brain_pattern_regime_killswitch_cron_hour",
        "brain_pattern_regime_killswitch_cron_minute",
        "brain_pattern_regime_killswitch_consecutive_days",
        "brain_pattern_regime_killswitch_neg_expectancy_threshold",
        "brain_pattern_regime_killswitch_max_per_pattern_30d",
    ]
    missing = [k for k in required if not hasattr(settings, k)]
    assert not missing, f"missing settings: {missing}"


def check_ops_log_module() -> None:
    from app.trading_brain.infrastructure.pattern_regime_m2_ops_log import (
        CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX,
        CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX,
        CHILI_PATTERN_REGIME_TILT_OPS_PREFIX,
        format_killswitch_ops_line,
        format_promotion_ops_line,
        format_tilt_ops_line,
    )

    prefixes = {
        CHILI_PATTERN_REGIME_TILT_OPS_PREFIX,
        CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX,
        CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX,
    }
    assert len(prefixes) == 3, "ops-log prefixes must be distinct"

    line = format_tilt_ops_line(event="tilt_computed", mode="shadow", pattern_id=1)
    assert CHILI_PATTERN_REGIME_TILT_OPS_PREFIX in line
    assert "event=tilt_computed" in line
    assert "mode=shadow" in line

    line = format_promotion_ops_line(
        event="promotion_evaluated", mode="shadow", pattern_id=2
    )
    assert CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX in line
    assert "event=promotion_evaluated" in line

    line = format_killswitch_ops_line(
        event="killswitch_evaluated", mode="shadow", pattern_id=3
    )
    assert CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX in line


def check_mode_helpers() -> None:
    from app.services.trading.pattern_regime_m2_common import (
        mode_is_active,
        mode_is_authoritative,
        normalize_mode,
    )

    assert normalize_mode("OFF") == "off"
    assert normalize_mode("garbage") == "off"
    assert normalize_mode("Shadow") == "shadow"
    assert normalize_mode("authoritative") == "authoritative"
    assert mode_is_active("off") is False
    assert mode_is_active("shadow") is True
    assert mode_is_authoritative("shadow") is False
    assert mode_is_authoritative("authoritative") is True


def check_make_evaluation_id_deterministic() -> None:
    from app.services.trading.pattern_regime_m2_common import make_evaluation_id

    a = make_evaluation_id(
        slice_name="tilt",
        pattern_id=7,
        as_of_date=date(2024, 5, 10),
        context_hash="abc",
    )
    b = make_evaluation_id(
        slice_name="tilt",
        pattern_id=7,
        as_of_date=date(2024, 5, 10),
        context_hash="abc",
    )
    c = make_evaluation_id(
        slice_name="promotion",
        pattern_id=7,
        as_of_date=date(2024, 5, 10),
        context_hash="abc",
    )
    assert a == b, "same inputs must yield same id"
    assert a != c, "slice_name must matter"
    assert len(a) == 16


def check_has_live_approval_no_rows() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_m2_common import has_live_approval

    db = SessionLocal()
    try:
        assert (
            has_live_approval(db, action_type="pattern_regime_tilt_NONEXISTENT")
            is False
        )
    finally:
        db.close()


def check_load_resolved_context_empty() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_ledger_lookup import (
        load_resolved_context,
    )

    db = SessionLocal()
    try:
        ctx = load_resolved_context(
            db,
            pattern_id=-987654321,
            as_of_date=date(2024, 5, 10),
            max_staleness_days=5,
        )
        assert ctx.n_confident_dimensions == 0
        assert ctx.mean_expectancy() is None
    finally:
        db.close()


def check_resolved_context_hash_deterministic() -> None:
    from app.services.trading.pattern_regime_ledger_lookup import (
        resolved_context_hash,
    )

    ctx_a = _make_ctx(n_dims=3, expectancy=0.01)
    ctx_b = _make_ctx(n_dims=3, expectancy=0.01)
    ctx_c = _make_ctx(n_dims=3, expectancy=-0.01)
    h_a = resolved_context_hash(ctx_a)
    h_b = resolved_context_hash(ctx_b)
    h_c = resolved_context_hash(ctx_c)
    assert h_a == h_b, "same inputs must produce same hash"
    assert h_a != h_c, "different expectancies must produce different hash"
    assert len(h_a) == 16


def check_tilt_insufficient_coverage() -> None:
    from app.services.trading.pattern_regime_tilt_model import (
        TiltConfig,
        compute_tilt_multiplier,
    )

    ctx = _make_ctx(n_dims=1, expectancy=0.02)
    out = compute_tilt_multiplier(
        ctx, config=TiltConfig(min_confident_dimensions=3)
    )
    assert out.reason_code == "insufficient_coverage"
    assert out.multiplier == 1.0
    assert out.fallback_used is True


def check_tilt_no_signal() -> None:
    from app.services.trading.pattern_regime_tilt_model import (
        TiltConfig,
        compute_tilt_multiplier,
    )

    ctx = _make_ctx(n_dims=4, expectancy=0.0)
    out = compute_tilt_multiplier(
        ctx, config=TiltConfig(min_confident_dimensions=3, noise_floor=1e-5)
    )
    assert out.reason_code == "no_signal"
    assert out.multiplier == 1.0


def check_tilt_positive_signal() -> None:
    from app.services.trading.pattern_regime_tilt_model import (
        TiltConfig,
        compute_tilt_multiplier,
    )

    ctx = _make_ctx(n_dims=4, expectancy=0.02)
    cfg = TiltConfig(min_confident_dimensions=3, max_multiplier=2.0, min_multiplier=0.25)
    out = compute_tilt_multiplier(ctx, config=cfg)
    assert out.reason_code == "applied"
    assert 1.0 < out.multiplier <= 2.0, f"unexpected multiplier {out.multiplier}"


def check_tilt_negative_signal() -> None:
    from app.services.trading.pattern_regime_tilt_model import (
        TiltConfig,
        compute_tilt_multiplier,
    )

    ctx = _make_ctx(n_dims=4, expectancy=-0.02)
    cfg = TiltConfig(min_confident_dimensions=3, max_multiplier=2.0, min_multiplier=0.25)
    out = compute_tilt_multiplier(ctx, config=cfg)
    assert out.reason_code == "applied"
    assert 0.25 <= out.multiplier < 1.0, f"unexpected multiplier {out.multiplier}"


def check_promotion_low_coverage_defers() -> None:
    from app.services.trading.pattern_regime_promotion_model import (
        PromotionConfig,
        evaluate_promotion,
    )

    ctx = _make_ctx(n_dims=1, expectancy=0.01)
    out = evaluate_promotion(
        ctx,
        baseline_allow=True,
        config=PromotionConfig(min_confident_dimensions=3),
    )
    assert out.reason_code in ("baseline_deferred", "insufficient_coverage")
    assert out.consumer_allow is True
    assert out.fallback_used is True


def check_promotion_never_upgrades_block() -> None:
    from app.services.trading.pattern_regime_promotion_model import (
        PromotionConfig,
        evaluate_promotion,
    )

    ctx = _make_ctx(n_dims=4, expectancy=0.05)
    out = evaluate_promotion(
        ctx,
        baseline_allow=False,
        config=PromotionConfig(min_confident_dimensions=3),
    )
    assert out.consumer_allow is False, "must not upgrade a baseline block"


def check_promotion_blocks_on_negatives() -> None:
    from app.services.trading.pattern_regime_ledger_lookup import (
        LedgerCell,
        ResolvedContext,
    )
    from app.services.trading.pattern_regime_performance_model import (
        DEFAULT_DIMENSIONS,
    )
    from app.services.trading.pattern_regime_promotion_model import (
        PromotionConfig,
        evaluate_promotion,
    )

    cells: Dict[str, LedgerCell] = {}
    for i, d in enumerate(DEFAULT_DIMENSIONS[:4]):
        cells[d] = LedgerCell(
            pattern_id=1,
            regime_dimension=d,
            regime_label="lab",
            as_of_date=date(2024, 5, 1),
            window_days=90,
            n_trades=5,
            hit_rate=0.5,
            mean_pnl_pct=-0.01,
            expectancy=-0.02 if i < 3 else 0.001,
            profit_factor=0.8,
            has_confidence=True,
        )
    ctx = ResolvedContext(
        pattern_id=1,
        as_of_date=date(2024, 5, 10),
        max_staleness_days=5,
        cells_by_dimension=cells,
    )
    out = evaluate_promotion(
        ctx,
        baseline_allow=True,
        config=PromotionConfig(
            min_confident_dimensions=3,
            min_blocking_dimensions=2,
            block_on_negative_expectancy_threshold=0.0,
        ),
    )
    assert out.consumer_allow is False
    assert out.reason_code == "blocked_negative_dimensions"
    assert len(out.blocking_dimensions) >= 2


def check_killswitch_empty_history() -> None:
    from app.services.trading.pattern_regime_killswitch_model import (
        KillSwitchConfig,
        evaluate_killswitch,
    )

    ctx = _make_ctx(n_dims=4, expectancy=-0.02)
    out = evaluate_killswitch(
        ctx,
        history=[],
        config=KillSwitchConfig(consecutive_days_negative=3),
    )
    assert out.consumer_quarantine is False
    assert out.reason_code in ("healthy", "negative_but_streak_too_short")


def check_killswitch_streak_triggers() -> None:
    from app.services.trading.pattern_regime_killswitch_model import (
        DailyExpectancyPoint,
        KillSwitchConfig,
        evaluate_killswitch,
    )

    ctx = _make_ctx(n_dims=4, expectancy=-0.02)
    base_day = date(2024, 5, 5)
    history = [
        DailyExpectancyPoint(
            as_of_date=base_day + timedelta(days=i),
            n_confident_dimensions=4,
            mean_expectancy=-0.02,
        )
        for i in range(3)
    ]
    out = evaluate_killswitch(
        ctx,
        history=history,
        config=KillSwitchConfig(
            consecutive_days_negative=3, neg_expectancy_threshold=-0.005
        ),
    )
    assert out.consumer_quarantine is True
    assert out.reason_code == "quarantine"
    assert out.consecutive_days_negative >= 3


def check_killswitch_circuit_breaker() -> None:
    from app.services.trading.pattern_regime_killswitch_model import (
        DailyExpectancyPoint,
        KillSwitchConfig,
        evaluate_killswitch,
    )

    ctx = _make_ctx(n_dims=4, expectancy=-0.05)
    history = [
        DailyExpectancyPoint(
            as_of_date=date(2024, 5, 5) + timedelta(days=i),
            n_confident_dimensions=4,
            mean_expectancy=-0.05,
        )
        for i in range(5)
    ]
    out = evaluate_killswitch(
        ctx,
        history=history,
        config=KillSwitchConfig(consecutive_days_negative=3),
        at_circuit_breaker=True,
    )
    assert out.consumer_quarantine is False
    assert out.reason_code == "circuit_breaker"


def _with_setting(name: str, value: Any) -> Any:
    from app.config import settings

    prev = getattr(settings, name, None)
    try:
        object.__setattr__(settings, name, value)
    except Exception:
        setattr(settings, name, value)
    return prev


def _restore_setting(name: str, prev: Any) -> None:
    from app.config import settings

    try:
        object.__setattr__(settings, name, prev)
    except Exception:
        setattr(settings, name, prev)


def check_tilt_service_off_returns_none() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_tilt_service import (
        evaluate_tilt_for_proposal,
    )

    prev = _with_setting("brain_pattern_regime_tilt_mode", "off")
    db = SessionLocal()
    try:
        out = evaluate_tilt_for_proposal(
            db,
            pattern_id=1,
            ticker="AAPL",
            source="test",
            baseline_notional=1000.0,
        )
        assert out is None
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_tilt_mode", prev)


def check_tilt_service_authoritative_without_approval_refused() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_tilt_service import (
        evaluate_tilt_for_proposal,
    )

    prev_mode = _with_setting("brain_pattern_regime_tilt_mode", "authoritative")
    prev_ops = _with_setting("brain_pattern_regime_tilt_ops_log_enabled", False)
    db = SessionLocal()
    try:
        out = evaluate_tilt_for_proposal(
            db,
            pattern_id=-777777,
            ticker="AAPL",
            source="test",
            baseline_notional=1000.0,
            as_of_date=date(2024, 5, 10),
            persist=False,
        )
        assert out is not None
        assert out.mode == "authoritative"
        assert out.applied is False
        assert out.reason_code == "refused_authoritative"
        assert out.multiplier == 1.0
        assert out.fallback_used is True
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_tilt_mode", prev_mode)
        _restore_setting("brain_pattern_regime_tilt_ops_log_enabled", prev_ops)


def check_promotion_service_off_returns_none() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_promotion_service import (
        evaluate_promotion_for_pattern,
    )

    prev = _with_setting("brain_pattern_regime_promotion_mode", "off")
    db = SessionLocal()
    try:
        out = evaluate_promotion_for_pattern(
            db,
            pattern_id=1,
            baseline_allow=True,
        )
        assert out is None
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_promotion_mode", prev)


def check_promotion_service_authoritative_without_approval_refused() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_promotion_service import (
        evaluate_promotion_for_pattern,
    )

    prev_mode = _with_setting("brain_pattern_regime_promotion_mode", "authoritative")
    prev_ops = _with_setting(
        "brain_pattern_regime_promotion_ops_log_enabled", False
    )
    db = SessionLocal()
    try:
        out = evaluate_promotion_for_pattern(
            db,
            pattern_id=-888888,
            baseline_allow=True,
            as_of_date=date(2024, 5, 10),
            persist=False,
        )
        assert out is not None
        assert out.mode == "authoritative"
        assert out.applied is False
        assert out.reason_code == "refused_authoritative"
        # Authoritative w/o approval must NOT upgrade or block:
        # consumer_allow reflects baseline (True here).
        assert out.consumer_allow is True
        assert out.fallback_used is True
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_promotion_mode", prev_mode)
        _restore_setting(
            "brain_pattern_regime_promotion_ops_log_enabled", prev_ops
        )


def check_killswitch_service_off_returns_none() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_killswitch_service import (
        evaluate_pattern_killswitch,
    )

    prev = _with_setting("brain_pattern_regime_killswitch_mode", "off")
    db = SessionLocal()
    try:
        out = evaluate_pattern_killswitch(
            db,
            pattern_id=1,
            baseline_status="live",
        )
        assert out is None
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_killswitch_mode", prev)


def check_run_daily_sweep_off_mode() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_killswitch_service import (
        run_daily_sweep,
    )

    prev = _with_setting("brain_pattern_regime_killswitch_mode", "off")
    db = SessionLocal()
    try:
        out = run_daily_sweep(db, as_of_date=date(2024, 5, 10))
        assert isinstance(out, dict)
        assert out.get("skipped") is True
        assert out.get("reason") == "mode_off"
    finally:
        db.close()
        _restore_setting("brain_pattern_regime_killswitch_mode", prev)


def _assert_keys(actual: Dict[str, Any], expected: set[str], label: str) -> None:
    actual_set = set(actual.keys())
    missing = expected - actual_set
    extra = actual_set - expected
    assert not missing, f"{label} missing keys: {missing}"
    assert not extra, f"{label} extra keys: {extra}"


def check_tilt_diagnostics_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_tilt_service import (
        diagnostics_summary,
    )

    db = SessionLocal()
    try:
        s = diagnostics_summary(db, lookback_hours=1)
    finally:
        db.close()
    _assert_keys(
        s,
        {
            "mode",
            "approval_live",
            "lookback_hours",
            "total_evaluations",
            "by_reason_code",
            "by_diff_category",
            "mean_multiplier",
            "mean_confident_dimensions",
            "latest",
        },
        "tilt diagnostics",
    )


def check_promotion_diagnostics_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_promotion_service import (
        diagnostics_summary,
    )

    db = SessionLocal()
    try:
        s = diagnostics_summary(db, lookback_hours=1)
    finally:
        db.close()
    _assert_keys(
        s,
        {
            "mode",
            "approval_live",
            "lookback_hours",
            "total_evaluations",
            "total_consumer_blocks",
            "by_reason_code",
            "by_diff_category",
            "latest",
        },
        "promotion diagnostics",
    )


def check_killswitch_diagnostics_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_killswitch_service import (
        diagnostics_summary,
    )

    db = SessionLocal()
    try:
        s = diagnostics_summary(db, lookback_hours=1)
    finally:
        db.close()
    _assert_keys(
        s,
        {
            "mode",
            "approval_live",
            "lookback_hours",
            "total_evaluations",
            "total_consumer_quarantines",
            "by_reason_code",
            "latest",
        },
        "killswitch diagnostics",
    )


def check_additive_only_m1_ledger_unchanged() -> None:
    from sqlalchemy import text as sql_text

    from app.db import SessionLocal
    from app.services.trading.pattern_regime_tilt_service import (
        evaluate_tilt_for_proposal,
    )

    db = SessionLocal()
    try:
        before = int(
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM trading_pattern_regime_performance_daily"
                )
            ).scalar_one()
        )
        # Flip tilt to off, exercise, flip back — should not write to M.1.
        prev = _with_setting("brain_pattern_regime_tilt_mode", "off")
        try:
            evaluate_tilt_for_proposal(
                db,
                pattern_id=1,
                ticker="T",
                source="soak",
                baseline_notional=1000.0,
            )
        finally:
            _restore_setting("brain_pattern_regime_tilt_mode", prev)
        after = int(
            db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM trading_pattern_regime_performance_daily"
                )
            ).scalar_one()
        )
        assert before == after, (
            f"M.1 ledger must be additive-only; before={before} after={after}"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    _run("01. migration 145 applied", check_migration_145_applied)
    _run("02. M.2 log tables + columns present", check_log_tables_present)
    _run(
        "03. trading_governance_approvals.expires_at present",
        check_governance_approvals_expires_at,
    )
    _run(
        "04. trading_position_sizer_log additive cols",
        check_position_sizer_log_additive_cols,
    )
    _run("05. 21 brain_pattern_regime_* settings visible", check_settings_visible)
    _run("06. ops-log module distinct prefixes + formatters", check_ops_log_module)
    _run("07. mode helpers (normalize/active/authoritative)", check_mode_helpers)
    _run(
        "08. make_evaluation_id deterministic per slice",
        check_make_evaluation_id_deterministic,
    )
    _run("09. has_live_approval false for missing rows", check_has_live_approval_no_rows)
    _run(
        "10. load_resolved_context returns empty for unseen pid",
        check_load_resolved_context_empty,
    )
    _run(
        "11. resolved_context_hash deterministic",
        check_resolved_context_hash_deterministic,
    )
    _run(
        "12. tilt model insufficient_coverage -> mult=1.0",
        check_tilt_insufficient_coverage,
    )
    _run("13. tilt model no_signal -> mult=1.0", check_tilt_no_signal)
    _run("14. tilt model positive signal -> >1.0", check_tilt_positive_signal)
    _run("15. tilt model negative signal -> <1.0", check_tilt_negative_signal)
    _run(
        "16. promotion model low coverage defers to baseline",
        check_promotion_low_coverage_defers,
    )
    _run(
        "17. promotion model never upgrades baseline block",
        check_promotion_never_upgrades_block,
    )
    _run(
        "18. promotion model blocks on N negative dimensions",
        check_promotion_blocks_on_negatives,
    )
    _run("19. killswitch model empty history no quarantine", check_killswitch_empty_history)
    _run(
        "20. killswitch model streak triggers quarantine",
        check_killswitch_streak_triggers,
    )
    _run(
        "21. killswitch model circuit breaker respected",
        check_killswitch_circuit_breaker,
    )
    _run("22. tilt service mode=off returns None", check_tilt_service_off_returns_none)
    _run(
        "23. tilt service authoritative w/o approval refused",
        check_tilt_service_authoritative_without_approval_refused,
    )
    _run(
        "24. promotion service mode=off returns None",
        check_promotion_service_off_returns_none,
    )
    _run(
        "25. promotion service authoritative w/o approval refused (baseline passthrough)",
        check_promotion_service_authoritative_without_approval_refused,
    )
    _run(
        "26. killswitch service mode=off returns None",
        check_killswitch_service_off_returns_none,
    )
    _run("27. run_daily_sweep off mode returns skipped", check_run_daily_sweep_off_mode)
    _run("28. tilt diagnostics frozen shape", check_tilt_diagnostics_shape)
    _run("29. promotion diagnostics frozen shape", check_promotion_diagnostics_shape)
    _run(
        "30. killswitch diagnostics frozen shape",
        check_killswitch_diagnostics_shape,
    )
    _run(
        "31. additive-only: M.1 ledger row count unchanged",
        check_additive_only_m1_ledger_unchanged,
    )

    print()
    print(f"Phase M.2 soak: {TOTAL - FAILS}/{TOTAL} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
