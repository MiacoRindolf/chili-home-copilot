"""Phase M.2 pure-model unit tests (no DB, no I/O).

Covers the three slices — tilt, promotion, killswitch — and the
shared ``ResolvedContext`` hashing / summary helpers.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.trading.pattern_regime_ledger_lookup import (
    LedgerCell,
    ResolvedContext,
    resolved_context_hash,
    summarise_context,
)
from app.services.trading.pattern_regime_tilt_model import (
    TiltConfig,
    classify_diff,
    compute_tilt_multiplier,
)
from app.services.trading.pattern_regime_promotion_model import (
    PromotionConfig,
    evaluate_promotion,
)
from app.services.trading.pattern_regime_killswitch_model import (
    DailyExpectancyPoint,
    KillSwitchConfig,
    compute_consecutive_streak,
    evaluate_killswitch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cell(dim: str, label: str, expectancy: float, n_trades: int = 20) -> LedgerCell:
    return LedgerCell(
        pattern_id=1,
        regime_dimension=dim,
        regime_label=label,
        as_of_date=date(2026, 4, 16),
        window_days=90,
        n_trades=n_trades,
        hit_rate=0.55,
        mean_pnl_pct=0.01,
        expectancy=expectancy,
        profit_factor=1.2,
        has_confidence=True,
    )


def _ctx(cells_by_dim: dict[str, LedgerCell], *, pattern_id: int = 1) -> ResolvedContext:
    return ResolvedContext(
        pattern_id=pattern_id,
        as_of_date=date(2026, 4, 16),
        max_staleness_days=5,
        cells_by_dimension=cells_by_dim,
        unavailable_dimensions=(),
        stale_dimensions=(),
    )


# ---------------------------------------------------------------------------
# Shared lookup helpers
# ---------------------------------------------------------------------------


class TestResolvedContextHash:
    def test_deterministic_for_same_inputs(self) -> None:
        a = _ctx({"macro_regime": _cell("macro_regime", "risk_on", 0.01)})
        b = _ctx({"macro_regime": _cell("macro_regime", "risk_on", 0.01)})
        assert resolved_context_hash(a) == resolved_context_hash(b)

    def test_changes_when_label_changes(self) -> None:
        a = _ctx({"macro_regime": _cell("macro_regime", "risk_on", 0.01)})
        b = _ctx({"macro_regime": _cell("macro_regime", "risk_off", 0.01)})
        assert resolved_context_hash(a) != resolved_context_hash(b)

    def test_changes_when_pattern_id_changes(self) -> None:
        a = _ctx(
            {"macro_regime": _cell("macro_regime", "risk_on", 0.01)},
            pattern_id=1,
        )
        b = _ctx(
            {"macro_regime": _cell("macro_regime", "risk_on", 0.01)},
            pattern_id=2,
        )
        assert resolved_context_hash(a) != resolved_context_hash(b)

    def test_order_independent(self) -> None:
        a = _ctx(
            {
                "macro_regime": _cell("macro_regime", "risk_on", 0.01),
                "session_label": _cell("session_label", "power_hour", 0.02),
            }
        )
        b = _ctx(
            {
                "session_label": _cell("session_label", "power_hour", 0.02),
                "macro_regime": _cell("macro_regime", "risk_on", 0.01),
            }
        )
        assert resolved_context_hash(a) == resolved_context_hash(b)


class TestContextAggregates:
    def test_negative_expectancy_dimensions(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.01),
                "b": _cell("b", "y", 0.02),
                "c": _cell("c", "z", -0.03),
            }
        )
        neg = ctx.negative_expectancy_dimensions(threshold=0.0)
        assert sorted(neg) == ["a", "c"]

    def test_mean_expectancy_none_when_empty(self) -> None:
        ctx = _ctx({})
        assert ctx.mean_expectancy() is None

    def test_summarise_context_shape(self) -> None:
        ctx = _ctx({"a": _cell("a", "x", 0.01)})
        summary = summarise_context(ctx)
        assert summary["n_confident_dimensions"] == 1
        assert summary["expectancies"] == {"a": 0.01}
        assert summary["mean_expectancy"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# M.2.a tilt model
# ---------------------------------------------------------------------------


class TestTiltConfigValidation:
    def test_rejects_bad_bounds(self) -> None:
        with pytest.raises(ValueError):
            TiltConfig(min_multiplier=2.0, max_multiplier=1.0)

    def test_rejects_negative_noise_floor(self) -> None:
        with pytest.raises(ValueError):
            TiltConfig(noise_floor=-0.1)


class TestTiltComputation:
    def test_insufficient_coverage_returns_neutral(self) -> None:
        ctx = _ctx({"macro_regime": _cell("macro_regime", "risk_on", 0.1)})
        out = compute_tilt_multiplier(ctx, config=TiltConfig(min_confident_dimensions=3))
        assert out.multiplier == 1.0
        assert out.reason_code == "insufficient_coverage"
        assert out.fallback_used is True

    def test_empty_context_is_neutral(self) -> None:
        ctx = _ctx({})
        out = compute_tilt_multiplier(ctx, config=TiltConfig(min_confident_dimensions=0))
        assert out.multiplier == 1.0
        assert out.reason_code == "no_signal"

    def test_all_zero_expectancy_is_neutral(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.0),
                "b": _cell("b", "y", 0.0),
                "c": _cell("c", "z", 0.0),
            }
        )
        out = compute_tilt_multiplier(ctx, config=TiltConfig(min_confident_dimensions=3))
        assert out.multiplier == 1.0
        assert out.reason_code == "no_signal"

    def test_positive_expectancies_tilt_upward(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.01),
                "b": _cell("b", "y", 0.012),
                "c": _cell("c", "z", 0.015),
            }
        )
        out = compute_tilt_multiplier(
            ctx, config=TiltConfig(min_confident_dimensions=3)
        )
        assert out.multiplier > 1.0
        assert out.reason_code == "applied"
        assert not out.clamped

    def test_negative_expectancies_tilt_downward(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.01),
                "b": _cell("b", "y", -0.02),
                "c": _cell("c", "z", -0.015),
            }
        )
        out = compute_tilt_multiplier(
            ctx, config=TiltConfig(min_confident_dimensions=3)
        )
        assert out.multiplier < 1.0
        assert out.reason_code == "applied"

    def test_high_positive_saturates_at_max(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.5),
                "b": _cell("b", "y", 0.5),
                "c": _cell("c", "z", 0.5),
            }
        )
        out = compute_tilt_multiplier(
            ctx,
            config=TiltConfig(min_confident_dimensions=3, max_multiplier=1.5),
        )
        assert out.multiplier == pytest.approx(1.5)

    def test_deep_negative_saturates_at_min(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.5),
                "b": _cell("b", "y", -0.5),
                "c": _cell("c", "z", -0.5),
            }
        )
        out = compute_tilt_multiplier(
            ctx,
            config=TiltConfig(min_confident_dimensions=3, min_multiplier=0.5),
        )
        assert out.multiplier == pytest.approx(0.5)

    def test_determinism(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.01),
                "b": _cell("b", "y", -0.005),
                "c": _cell("c", "z", 0.02),
            }
        )
        cfg = TiltConfig(min_confident_dimensions=3)
        r1 = compute_tilt_multiplier(ctx, config=cfg)
        r2 = compute_tilt_multiplier(ctx, config=cfg)
        assert r1.multiplier == r2.multiplier
        assert r1.contributing_dimensions == r2.contributing_dimensions

    def test_noise_floor_filters_tiny_cells(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 1e-8),
                "b": _cell("b", "y", 1e-8),
                "c": _cell("c", "z", 1e-8),
            }
        )
        out = compute_tilt_multiplier(
            ctx, config=TiltConfig(min_confident_dimensions=3)
        )
        assert out.reason_code == "no_signal"

    def test_classify_diff(self) -> None:
        assert classify_diff(1000.0, 1050.0, tolerance_bps=25) == "upsize"
        assert classify_diff(1000.0, 950.0, tolerance_bps=25) == "downsize"
        assert classify_diff(1000.0, 1001.0, tolerance_bps=25) == "none"
        assert classify_diff(None, 1000.0) == "unknown"
        assert classify_diff(-1.0, 1.0) == "unknown"


# ---------------------------------------------------------------------------
# M.2.b promotion model
# ---------------------------------------------------------------------------


class TestPromotionModel:
    def test_insufficient_coverage_defers_to_baseline(self) -> None:
        ctx = _ctx({"a": _cell("a", "x", 0.01)})
        out = evaluate_promotion(
            ctx,
            baseline_allow=True,
            config=PromotionConfig(min_confident_dimensions=3),
        )
        assert out.consumer_allow is True
        assert out.reason_code == "baseline_deferred"
        assert out.fallback_used is True

    def test_insufficient_coverage_baseline_none(self) -> None:
        ctx = _ctx({"a": _cell("a", "x", 0.01)})
        out = evaluate_promotion(
            ctx,
            baseline_allow=None,
            config=PromotionConfig(min_confident_dimensions=3),
        )
        assert out.consumer_allow is True
        assert out.reason_code == "insufficient_coverage"

    def test_baseline_block_never_upgraded(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.1),
                "b": _cell("b", "y", 0.2),
                "c": _cell("c", "z", 0.3),
            }
        )
        out = evaluate_promotion(
            ctx,
            baseline_allow=False,
            config=PromotionConfig(),
        )
        assert out.consumer_allow is False
        assert out.reason_code == "baseline_matched"

    def test_two_negative_dimensions_block(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.01),
                "b": _cell("b", "y", -0.02),
                "c": _cell("c", "z", 0.05),
            }
        )
        out = evaluate_promotion(
            ctx,
            baseline_allow=True,
            config=PromotionConfig(
                min_confident_dimensions=3, min_blocking_dimensions=2
            ),
        )
        assert out.consumer_allow is False
        assert out.reason_code == "blocked_negative_dimensions"
        assert set(out.blocking_dimensions.keys()) == {"a", "b"}

    def test_low_mean_expectancy_blocks(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.005),
                "b": _cell("b", "y", 0.001),
                "c": _cell("c", "z", 0.002),
            }
        )
        out = evaluate_promotion(
            ctx,
            baseline_allow=True,
            config=PromotionConfig(
                min_confident_dimensions=3,
                min_blocking_dimensions=5,
                min_mean_expectancy=0.005,
            ),
        )
        assert out.consumer_allow is False
        assert out.reason_code == "blocked_low_mean_expectancy"

    def test_allow_matches_baseline_allow(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.01),
                "b": _cell("b", "y", 0.02),
                "c": _cell("c", "z", 0.03),
            }
        )
        out = evaluate_promotion(
            ctx,
            baseline_allow=True,
            config=PromotionConfig(min_confident_dimensions=3),
        )
        assert out.consumer_allow is True
        assert out.reason_code == "baseline_matched"


# ---------------------------------------------------------------------------
# M.2.c killswitch model
# ---------------------------------------------------------------------------


def _point(d: date, *, n: int = 5, mean_exp: float | None = -0.01) -> DailyExpectancyPoint:
    return DailyExpectancyPoint(
        as_of_date=d,
        n_confident_dimensions=n,
        mean_expectancy=mean_exp,
    )


class TestKillSwitchStreak:
    def test_empty_history_returns_zero(self) -> None:
        assert compute_consecutive_streak([], threshold=-0.005) == 0

    def test_all_negative_streak(self) -> None:
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=-0.03),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        assert compute_consecutive_streak(pts, threshold=-0.005) == 3

    def test_positive_day_breaks_streak(self) -> None:
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=0.01),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        assert compute_consecutive_streak(pts, threshold=-0.005) == 1

    def test_none_breaks_streak(self) -> None:
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=None),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        assert compute_consecutive_streak(pts, threshold=-0.005) == 1

    def test_zero_confident_breaks_streak(self) -> None:
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), n=0, mean_exp=-0.04),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        assert compute_consecutive_streak(pts, threshold=-0.005) == 1


class TestKillSwitchEvaluate:
    def test_insufficient_coverage_no_op(self) -> None:
        ctx = _ctx({"a": _cell("a", "x", -0.1)})
        out = evaluate_killswitch(
            ctx,
            history=[],
            config=KillSwitchConfig(min_confident_dimensions=3),
        )
        assert out.consumer_quarantine is False
        assert out.reason_code == "insufficient_coverage"

    def test_circuit_breaker_short_circuit(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.1),
                "b": _cell("b", "y", -0.1),
                "c": _cell("c", "z", -0.1),
            }
        )
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=-0.03),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        out = evaluate_killswitch(
            ctx,
            history=pts,
            config=KillSwitchConfig(min_confident_dimensions=3),
            at_circuit_breaker=True,
        )
        assert out.consumer_quarantine is False
        assert out.reason_code == "circuit_breaker"

    def test_negative_but_streak_too_short(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.05),
                "b": _cell("b", "y", -0.05),
                "c": _cell("c", "z", -0.05),
            }
        )
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
        ]
        out = evaluate_killswitch(
            ctx,
            history=pts,
            config=KillSwitchConfig(
                min_confident_dimensions=3, consecutive_days_negative=3
            ),
        )
        assert out.consumer_quarantine is False
        assert out.reason_code == "negative_but_streak_too_short"

    def test_three_day_streak_triggers_quarantine(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.05),
                "b": _cell("b", "y", -0.05),
                "c": _cell("c", "z", -0.05),
            }
        )
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=-0.03),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        out = evaluate_killswitch(
            ctx,
            history=pts,
            config=KillSwitchConfig(
                min_confident_dimensions=3, consecutive_days_negative=3
            ),
        )
        assert out.consumer_quarantine is True
        assert out.reason_code == "quarantine"
        assert out.consecutive_days_negative >= 3
        assert out.worst_dimension is not None

    def test_healthy_state(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", 0.02),
                "b": _cell("b", "y", 0.03),
                "c": _cell("c", "z", 0.01),
            }
        )
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=0.01),
            _point(d0 + timedelta(days=1), mean_exp=0.02),
            _point(d0 + timedelta(days=2), mean_exp=0.015),
        ]
        out = evaluate_killswitch(
            ctx,
            history=pts,
            config=KillSwitchConfig(
                min_confident_dimensions=3, consecutive_days_negative=3
            ),
        )
        assert out.consumer_quarantine is False
        assert out.reason_code == "healthy"

    def test_config_validates(self) -> None:
        with pytest.raises(ValueError):
            KillSwitchConfig(consecutive_days_negative=0)
        with pytest.raises(ValueError):
            KillSwitchConfig(max_per_pattern_30d=0)

    def test_worst_dimension_identification(self) -> None:
        ctx = _ctx(
            {
                "a": _cell("a", "x", -0.01),
                "b": _cell("b", "y", -0.05),
                "c": _cell("c", "z", -0.03),
            }
        )
        d0 = date(2026, 4, 14)
        pts = [
            _point(d0, mean_exp=-0.02),
            _point(d0 + timedelta(days=1), mean_exp=-0.03),
            _point(d0 + timedelta(days=2), mean_exp=-0.04),
        ]
        out = evaluate_killswitch(
            ctx,
            history=pts,
            config=KillSwitchConfig(
                min_confident_dimensions=3, consecutive_days_negative=3
            ),
        )
        assert out.worst_dimension == "b"
        assert out.worst_expectancy == pytest.approx(-0.05)
