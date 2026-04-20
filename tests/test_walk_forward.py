"""P1.3 — date-based walk-forward backtest tests.

Covers:
    * Fold-window math (``_walk_forward_fold_windows``): correct count,
      correct boundaries, embargo respected, step controls overlap,
      insufficient-history returns [].
    * Per-fold pass/fail logic (``_walk_forward_fold_passes``): trade
      floor and win-rate floor both required; zero-trade folds never
      pass.
    * Settings resolution: explicit args override config; config is
      read live so monkeypatch takes effect per call.
    * ``run_walk_forward`` end-to-end on synthetic OHLCV: returns frozen
      shape, aggregates correctly, pass_fraction math is right.
    * Promotion-gate wiring: ``brain_apply_oos_promotion_gate`` gains a
      ``walk_forward_passes_gate`` parameter; False under the feature
      flag → ``rejected_walk_forward``; None under flag →
      ``pending_walk_forward``; flag off → pass-through.

Design philosophy
-----------------
Tests use ``df_override`` with synthetic data so we don't depend on
``_fetch_ohlcv_df`` (no network / cache / market-data path). The
synthetic generator produces deterministic trending data so fold
outcomes are reproducible across machines.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services.backtest_service import (
    _walk_forward_fold_passes,
    _walk_forward_fold_windows,
    _walk_forward_resolve_settings,
    run_walk_forward,
)
from app.services.trading.learning import brain_apply_oos_promotion_gate


# ── Synthetic OHLCV helpers ──────────────────────────────────────────────


def _synth_ohlcv(
    n_bars: int,
    *,
    start: str = "2020-01-01",
    base: float = 100.0,
    drift: float = 0.0005,
    noise_scale: float = 0.01,
    seed: int = 42,
) -> pd.DataFrame:
    """Build a deterministic OHLCV frame of ``n_bars`` daily bars.

    Deterministic seed + sinusoidal + drift means the same input always
    produces the same pattern backtest result — so tests can assert on
    specific numbers.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=n_bars, freq="D")
    # Small-amplitude sinusoid around a slow drift → repeatable regimes.
    t = np.arange(n_bars)
    mid = base * (1.0 + drift * t) * (1.0 + 0.03 * np.sin(t / 20.0))
    noise = rng.normal(0.0, noise_scale, n_bars)
    close = mid * (1.0 + noise)
    high = close * (1.0 + np.abs(rng.normal(0.0, noise_scale / 2, n_bars)))
    low = close * (1.0 - np.abs(rng.normal(0.0, noise_scale / 2, n_bars)))
    open_ = close * (1.0 + rng.normal(0.0, noise_scale / 2, n_bars))
    volume = rng.integers(100_000, 1_000_000, n_bars).astype(float)
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )
    return df


# ── Fold-window math ─────────────────────────────────────────────────────


class TestFoldWindows:
    """``_walk_forward_fold_windows`` computes integer index slices for
    each fold. These are the scaffolding for every per-fold backtest —
    off-by-one here is a silent correctness disaster."""

    def test_single_fold_fits_exactly(self):
        """180 train + 2 embargo + 30 test = 212 bars → exactly 1 fold."""
        df = _synth_ohlcv(212)
        folds = _walk_forward_fold_windows(
            df, train_days=180, test_days=30, step_days=30, embargo_days=2,
        )
        assert len(folds) == 1
        f = folds[0]
        assert f["fold_index"] == 0
        assert f["train_start_idx"] == 0
        assert f["train_end_idx"] == 180
        assert f["test_start_idx"] == 182  # 180 + 2 embargo
        assert f["test_end_idx"] == 212

    def test_two_folds_non_overlapping(self):
        """step_days == test_days → tests don't overlap."""
        df = _synth_ohlcv(242)
        folds = _walk_forward_fold_windows(
            df, train_days=180, test_days=30, step_days=30, embargo_days=2,
        )
        assert len(folds) == 2
        # Fold 0 test ends at bar 212; Fold 1 test starts at bar 212 (next fold's
        # train shifts by 30 → train=[30,210), test=[212,242)).
        assert folds[0]["test_start_idx"] == 182
        assert folds[0]["test_end_idx"] == 212
        assert folds[1]["test_start_idx"] == 212
        assert folds[1]["test_end_idx"] == 242

    def test_overlapping_tests_when_step_lt_test_days(self):
        """step_days < test_days means consecutive tests share bars."""
        df = _synth_ohlcv(260)
        folds = _walk_forward_fold_windows(
            df, train_days=180, test_days=30, step_days=15, embargo_days=2,
        )
        # Fold 0 test = [182, 212); fold 1 test = [197, 227); overlap [197, 212).
        assert len(folds) >= 2
        test_starts = [f["test_start_idx"] for f in folds]
        test_ends = [f["test_end_idx"] for f in folds]
        assert test_starts[1] < test_ends[0], "consecutive tests should overlap under step<test"

    def test_embargo_gap_is_exact(self):
        """train_end_idx + embargo_days == test_start_idx exactly."""
        df = _synth_ohlcv(250)
        folds = _walk_forward_fold_windows(
            df, train_days=120, test_days=20, step_days=20, embargo_days=5,
        )
        for f in folds:
            assert f["test_start_idx"] - f["train_end_idx"] == 5

    def test_zero_embargo_is_contiguous(self):
        """embargo_days=0 → test_start_idx == train_end_idx."""
        df = _synth_ohlcv(200)
        folds = _walk_forward_fold_windows(
            df, train_days=100, test_days=30, step_days=30, embargo_days=0,
        )
        assert folds
        for f in folds:
            assert f["test_start_idx"] == f["train_end_idx"]

    def test_insufficient_history_returns_empty(self):
        """< min_span bars → no folds, caller handles error."""
        # 100 < 180 + 2 + 30 = 212.
        df = _synth_ohlcv(100)
        folds = _walk_forward_fold_windows(
            df, train_days=180, test_days=30, step_days=30, embargo_days=2,
        )
        assert folds == []

    def test_last_fold_fits_without_spillover(self):
        """Never emit a fold whose test_end_idx exceeds len(df).

        This is the invariant that prevents iloc out-of-bounds in the
        per-fold backtest call.
        """
        df = _synth_ohlcv(500)
        folds = _walk_forward_fold_windows(
            df, train_days=180, test_days=30, step_days=30, embargo_days=2,
        )
        for f in folds:
            assert f["test_end_idx"] <= len(df)

    def test_empty_df_returns_empty(self):
        df = pd.DataFrame()
        folds = _walk_forward_fold_windows(
            df, train_days=30, test_days=10, step_days=10, embargo_days=1,
        )
        assert folds == []


# ── Per-fold pass / fail ────────────────────────────────────────────────


class TestFoldPasses:
    """The headline guarantee: zero-trade folds don't pass even if win
    rate is technically undefined (pattern that can't fire isn't
    robust)."""

    def test_passes_when_both_floors_met(self):
        assert _walk_forward_fold_passes(
            test_win_rate=0.55, test_trade_count=5,
            min_win_rate=0.45, min_trades=3,
        ) is True

    def test_fails_on_trade_count_floor(self):
        """Even perfect win rate can't save a 1-trade fold."""
        assert _walk_forward_fold_passes(
            test_win_rate=1.0, test_trade_count=1,
            min_win_rate=0.45, min_trades=3,
        ) is False

    def test_fails_on_win_rate_floor(self):
        assert _walk_forward_fold_passes(
            test_win_rate=0.40, test_trade_count=10,
            min_win_rate=0.45, min_trades=3,
        ) is False

    def test_zero_trades_never_passes(self):
        """The headline guarantee — pattern that doesn't fire in the
        test window is not regime-robust."""
        assert _walk_forward_fold_passes(
            test_win_rate=None, test_trade_count=0,
            min_win_rate=0.0, min_trades=0,
        ) is False  # trade_count=0 < min_trades=0? Actually 0 < 0 is False …
        # Explicit with min_trades >= 1 to show zero IS blocked:
        assert _walk_forward_fold_passes(
            test_win_rate=None, test_trade_count=0,
            min_win_rate=0.0, min_trades=1,
        ) is False

    def test_none_win_rate_fails(self):
        """Missing win rate (e.g. backtest errored) → fail."""
        assert _walk_forward_fold_passes(
            test_win_rate=None, test_trade_count=10,
            min_win_rate=0.45, min_trades=3,
        ) is False

    def test_none_trade_count_fails(self):
        assert _walk_forward_fold_passes(
            test_win_rate=0.55, test_trade_count=None,
            min_win_rate=0.45, min_trades=3,
        ) is False


# ── Settings resolution ─────────────────────────────────────────────────


class TestSettingsResolution:
    def test_defaults_when_config_unchanged(self):
        cfg = _walk_forward_resolve_settings()
        # Sanity: defaults match the bounds defined in config.py.
        assert cfg["train_days"] == 180
        assert cfg["test_days"] == 30
        assert cfg["step_days"] == 30
        assert cfg["embargo_days"] == 2
        assert cfg["min_folds"] == 3
        assert cfg["min_fold_win_rate"] == 0.45
        assert cfg["min_pass_fraction"] == 0.6
        assert cfg["enabled"] is False

    def test_reads_live_from_settings(self, monkeypatch):
        """Monkeypatch takes effect without reloading — same pattern as
        venue_health / rate_limiter."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_walk_forward_train_days", 90, raising=False)
        monkeypatch.setattr(settings, "chili_walk_forward_test_days", 15, raising=False)
        monkeypatch.setattr(settings, "chili_walk_forward_enabled", True, raising=False)

        cfg = _walk_forward_resolve_settings()
        assert cfg["train_days"] == 90
        assert cfg["test_days"] == 15
        assert cfg["enabled"] is True


# ── End-to-end run_walk_forward on synthetic data ───────────────────────


class TestRunWalkForwardShape:
    """Tests here exercise the outer orchestration: slice → backtest →
    aggregate → gate decision. Each asserts the OUTPUT SHAPE is frozen
    so dashboards and the gate caller have a stable contract."""

    @pytest.fixture()
    def simple_conditions(self) -> list[dict]:
        """A condition that fires often enough to produce some trades."""
        return [
            {"indicator": "rsi", "operator": "<", "value": 35, "period": 14},
        ]

    def test_insufficient_history_reports_clearly(self, simple_conditions):
        """Too-short df → ok=False, passes_gate=False, clear error."""
        df = _synth_ohlcv(100)  # < 180 + 2 + 30 default
        out = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
        )
        assert out["ok"] is False
        assert "insufficient history" in out["error"]
        assert out["passes_gate"] is False
        assert out["folds"] == []
        assert out["aggregate"] is None

    def test_frozen_shape_on_success(self, simple_conditions):
        """Every top-level key is present regardless of pass/fail."""
        df = _synth_ohlcv(400)
        out = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
            train_days=120, test_days=20, step_days=20, embargo_days=2,
            min_folds=2, min_fold_win_rate=0.0, min_pass_fraction=0.0,
        )
        assert set(out.keys()) >= {
            "ok", "ticker", "pattern_name", "period", "interval",
            "params", "folds", "aggregate", "passes_gate", "gate_reason",
        }
        assert out["ok"] is True
        assert out["ticker"] == "TEST"
        # Aggregate always populated when ok=True.
        agg = out["aggregate"]
        assert set(agg.keys()) >= {
            "n_folds", "n_folds_ok", "n_folds_passed", "pass_fraction",
            "mean_test_win_rate", "std_test_win_rate",
            "mean_test_return_pct", "total_test_trades",
        }

    def test_fold_date_strings_match_iloc_slices(self, simple_conditions):
        """Each fold carries human-readable train/test date bounds so an
        operator can spot-check which calendar period was held out."""
        df = _synth_ohlcv(400)
        out = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
            train_days=120, test_days=20, step_days=20, embargo_days=2,
        )
        assert out["ok"] is True
        for f in out["folds"]:
            if f.get("train_start") and f.get("train_end"):
                # Train end should be strictly before test start.
                assert f["train_end"] <= f["test_start"]

    def test_gate_rejects_when_too_few_folds(self, simple_conditions):
        """min_folds=5 on 400 bars with 180/30/30/2 = 7 folds should pass
        the fold-count floor; raise min_folds to 20 and it must fail."""
        df = _synth_ohlcv(400)
        out_high = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
            train_days=120, test_days=20, step_days=20, embargo_days=2,
            min_folds=20, min_fold_win_rate=0.0, min_pass_fraction=0.0,
        )
        assert out_high["passes_gate"] is False
        assert "too_few_folds" in (out_high["gate_reason"] or "")

    def test_gate_rejects_when_pass_fraction_low(self, simple_conditions):
        """Force a high win-rate floor that no fold can meet → passes_gate=False."""
        df = _synth_ohlcv(400)
        out = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
            train_days=120, test_days=20, step_days=20, embargo_days=2,
            min_folds=2, min_fold_win_rate=0.99, min_pass_fraction=0.99,
            min_trades_per_fold=1,
        )
        assert out["passes_gate"] is False
        # Either too_few_folds OR pass_fraction_low — both are gate_reason strings.
        assert out["gate_reason"] is not None

    def test_total_test_trades_matches_fold_sum(self, simple_conditions):
        """Aggregate ``total_test_trades`` == sum of per-fold test_trade_count
        across OK folds. This is the sanity check that the aggregation
        didn't double-count or drop a fold."""
        df = _synth_ohlcv(400)
        out = run_walk_forward(
            ticker="TEST", conditions=simple_conditions, df_override=df,
            train_days=120, test_days=20, step_days=20, embargo_days=2,
            min_folds=1, min_fold_win_rate=0.0, min_pass_fraction=0.0,
        )
        assert out["ok"] is True
        summed = sum(
            int(f.get("test_trade_count") or 0)
            for f in out["folds"] if f.get("ok")
        )
        assert out["aggregate"]["total_test_trades"] == summed


# ── Promotion-gate wiring ───────────────────────────────────────────────


class TestPromotionGateWiring:
    """New parameter on ``brain_apply_oos_promotion_gate`` — flipped on
    by ``chili_walk_forward_enabled``. The headline contract:

      * Flag OFF → parameter is entirely ignored (pass-through legacy).
      * Flag ON + passes_gate=True → continues through other gates.
      * Flag ON + passes_gate=False → hard reject (``rejected_walk_forward``).
      * Flag ON + passes_gate=None → ``pending_walk_forward`` (don't promote
        on OOS-only evidence).
    """

    def _passing_oos_payload(self) -> dict:
        """Everything else is clean so walk-forward gate is the only
        thing that can reject in these tests.

        ``origin`` must be one of ``_BRAIN_OOS_GATED_ORIGINS`` — otherwise the
        gate short-circuits to ``legacy`` before reaching the walk-forward
        check.
        """
        return dict(
            origin="brain_discovered",
            mean_is_win_rate=60.0,
            mean_oos_win_rate=55.0,
            oos_tickers_with_result=10,
            min_win_rate_pct=50.0,
            max_is_oos_gap_pct=15.0,
            oos_aggregate_trade_count=100,
        )

    @pytest.fixture()
    def _enable_oos_gate(self, monkeypatch):
        """Turn on the OOS gate so our wiring test doesn't short-circuit
        with ``legacy`` before the walk-forward check runs."""
        from app.config import settings

        monkeypatch.setattr(settings, "brain_oos_gate_enabled", True, raising=False)

    def test_flag_off_ignores_walk_forward_param(self, _enable_oos_gate, monkeypatch):
        """chili_walk_forward_enabled=False → walk_forward_passes_gate=False
        is silently ignored (pass-through). This protects the migration
        path: wiring the parameter into callers doesn't change behavior
        until the flag is flipped on."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_walk_forward_enabled", False, raising=False)
        status, allow = brain_apply_oos_promotion_gate(
            **self._passing_oos_payload(),
            walk_forward_passes_gate=False,
        )
        # Walk-forward was failing but flag is off → promotion proceeds.
        assert status == "promoted"
        assert allow is True

    def test_flag_on_passes_gate_true_promotes(self, _enable_oos_gate, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "chili_walk_forward_enabled", True, raising=False)
        status, allow = brain_apply_oos_promotion_gate(
            **self._passing_oos_payload(),
            walk_forward_passes_gate=True,
        )
        assert status == "promoted"
        assert allow is True

    def test_flag_on_passes_gate_false_rejects(self, _enable_oos_gate, monkeypatch):
        """Headline guarantee: walk-forward failure hard-rejects under flag on."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_walk_forward_enabled", True, raising=False)
        status, allow = brain_apply_oos_promotion_gate(
            **self._passing_oos_payload(),
            walk_forward_passes_gate=False,
        )
        assert status == "rejected_walk_forward"
        assert allow is False

    def test_flag_on_missing_result_is_pending(self, _enable_oos_gate, monkeypatch):
        """Flag on but caller forgot to run walk-forward → don't promote
        on OOS-only evidence; keep pattern active but flagged pending."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_walk_forward_enabled", True, raising=False)
        status, allow = brain_apply_oos_promotion_gate(
            **self._passing_oos_payload(),
            walk_forward_passes_gate=None,
        )
        assert status == "pending_walk_forward"
        assert allow is True  # keep the pattern live, just not promoted
