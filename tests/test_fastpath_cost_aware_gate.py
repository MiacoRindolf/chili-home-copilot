"""Tests for f-fastpath-universe-rotation gate_cost_aware_admission.

Helper-level tests with mocked settings + mocked calibration helpers.
No DB / no broker.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services.trading.fast_path.gates import (
    ExecContext,
    gate_cost_aware_admission,
    gate_calibrated_tradeability,
    gate_maker_attempt_adverse_selection,
    gate_negative_edge_excluded,
    gate_recency,
    gate_live_alpha_evidence,
    gate_pullback_ticker_allowed,
    gate_universe_active_for_live,
)
from app.services.trading.fast_path.universe_status import (
    UNIVERSE_STATUS_ACTIVE,
    UNIVERSE_STATUS_SHADOW,
)


def _alert(*, ticker="BTC-USD", alert_type="volume_breakout_long",
           signal_score=0.85, fired_at=None, features=None):
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "signal_score": signal_score,
        "fired_at": fired_at or datetime.utcnow(),
        "features": features or {},
    }


def _ctx(*, spread_bps=5.0, engine="fake_engine_object", mode="paper"):
    return ExecContext(
        now_wall=datetime.utcnow(),
        best_bid=100.0,
        best_ask=100.05,
        spread_bps=spread_bps,
        engine=engine,
        mode=mode,
    )


def _stub_fp_settings(
    *, enabled: bool = True, taker_fee_bps: float = 5.0,
    maker_fee_bps: float = 0.0,
    execution_mode: str = "taker",
    live_alpha_gate: bool = True, live_min_samples: int = 50,
    live_min_net_bps: float = 0.0,
    universe_rotation_enabled: bool = False,
    live_fee_enabled: bool = False,
):
    """Patch fast_path.settings.load to return a stub with the given knobs."""
    from app.services.trading.fast_path.settings import FastPathSettings

    return FastPathSettings(
        cost_aware_admission_enabled=enabled,
        cost_aware_taker_fee_bps=taker_fee_bps,
        cost_aware_maker_fee_bps=maker_fee_bps,
        execution_mode=execution_mode,
        live_alpha_evidence_gate_enabled=live_alpha_gate,
        live_alpha_min_samples=live_min_samples,
        live_alpha_min_net_bps=live_min_net_bps,
        universe_rotation_enabled=universe_rotation_enabled,
        cost_aware_live_fee_enabled=live_fee_enabled,
    )


class _FakeUniverseEngine:
    def __init__(self, status):
        self.status = status

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return self

    def mappings(self):
        return self

    def one_or_none(self):
        if self.status is None:
            return None
        return {"status": self.status}


# ---------------------------------------------------------------------------
# Disabled flag short-circuit
# ---------------------------------------------------------------------------

def test_gate_disabled_returns_allow_with_disabled_verdict():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=False),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail.get("verdict") == "disabled"


def test_calibrated_tradeability_defers_when_cost_aware_enabled():
    """Avoid the stale static cost bar shadowing the dynamic cost gate."""
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration.is_score_tradeable",
        side_effect=AssertionError("should not consult static tradeability"),
    ):
        result = gate_calibrated_tradeability(_alert(), _ctx())
    assert result.allow is True
    assert result.detail.get("verdict") == "deferred_to_cost_aware_admission"


def test_negative_edge_uses_maker_filled_table_in_maker_mode():
    evidence = {
        "score_bucket": "med",
        "scope": "pooled",
        "decay_table": "fast_signal_decay_maker_filled",
        "verdict": "negative_edge",
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True,
            execution_mode="maker_only",
        ),
    ), patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(True, evidence),
    ) as lookup:
        result = gate_negative_edge_excluded(
            _alert(alert_type="imbalance_long", signal_score=0.5),
            _ctx(),
        )

    assert result.allow is False
    assert result.reason == "negative_edge"
    assert result.detail["scope"] == "pooled"
    assert result.detail["decay_table"] == "fast_signal_decay_maker_filled"
    assert lookup.call_args.kwargs["table"] == "fast_signal_decay_maker_filled"
    assert lookup.call_args.kwargs["allow_pooled"] is True


def test_negative_edge_falls_back_to_pooled_bucket_when_ticker_is_sparse():
    from app.services.trading.fast_path.calibration import is_negative_edge_excluded

    pooled_row = {
        "horizon_s": 5,
        "sample_count": 80,
        "mean_return": -0.0003,
        "m2_return": 0.0000008,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[pooled_row],
    ):
        excluded, evidence = is_negative_edge_excluded(
            "fake_engine",
            ticker="NEW-USD",
            alert_type="imbalance_long",
            signal_score=0.5,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is True
    assert evidence["scope"] == "pooled"
    assert evidence["decay_table"] == "fast_signal_decay_maker_filled"
    assert evidence["verdict"] == "negative_edge"


def test_negative_edge_blocks_decisive_sparse_bucket_without_sample_quota():
    from app.services.trading.fast_path.calibration import is_negative_edge_excluded

    pooled_row = {
        "horizon_s": 5,
        "sample_count": 18,
        "mean_return": -0.00019,
        "m2_return": 0.00000005,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[pooled_row],
    ):
        excluded, evidence = is_negative_edge_excluded(
            "fake_engine",
            ticker="NEW-USD",
            alert_type="imbalance_long",
            signal_score=0.9,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is True
    assert evidence["scope"] == "pooled"
    assert evidence["sample_count"] == 18
    assert evidence["upper_ci"] < 0.0
    assert evidence["verdict"] == "negative_edge"


def test_negative_edge_ignores_zero_variance_rows_as_not_statistical():
    from app.services.trading.fast_path.calibration import is_negative_edge_excluded

    duplicate_like_row = {
        "horizon_s": 1,
        "sample_count": 100,
        "mean_return": -0.01,
        "m2_return": 0.0,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[duplicate_like_row],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[],
    ):
        excluded, evidence = is_negative_edge_excluded(
            "fake_engine",
            ticker="BTC-USD",
            alert_type="imbalance_long",
            signal_score=0.9,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is False
    assert evidence["verdict"] == "insufficient_statistical_evidence"
    assert evidence["minimum_requirement"] == "sample_count>=2 and nonzero_variance"


def test_negative_edge_pooled_block_does_not_override_decisive_ticker_positive():
    from app.services.trading.fast_path.calibration import is_negative_edge_excluded

    ticker_positive = {
        "horizon_s": 5,
        "sample_count": 12,
        "mean_return": 0.002,
        "m2_return": 0.00000002,
    }
    pooled_negative = {
        "horizon_s": 5,
        "sample_count": 80,
        "mean_return": -0.0003,
        "m2_return": 0.0000008,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[ticker_positive],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[pooled_negative],
    ) as pooled_lookup:
        excluded, evidence = is_negative_edge_excluded(
            "fake_engine",
            ticker="BTC-USD",
            alert_type="imbalance_long",
            signal_score=0.9,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is False
    assert evidence["scope"] == "ticker"
    assert evidence["verdict"] == "positive_edge"
    pooled_lookup.assert_not_called()


def test_cost_barrier_falls_back_to_pooled_bucket_when_ticker_is_sparse():
    from app.services.trading.fast_path.calibration import is_cost_barrier_excluded

    pooled_below_cost = {
        "horizon_s": 5,
        "sample_count": 30,
        "mean_return": 0.0002,
        "m2_return": 0.00000001,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[pooled_below_cost],
    ):
        excluded, evidence = is_cost_barrier_excluded(
            "fake_engine",
            ticker="NEW-USD",
            alert_type="book_pressure_reclaim_long",
            signal_score=0.5,
            cost_bps=20.0,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is True
    assert evidence["scope"] == "pooled"
    assert evidence["verdict"] == "below_cost"
    assert evidence["decision_upper_net_bps"] < 0.0


def test_cost_barrier_pooled_block_does_not_override_ticker_cost_positive():
    from app.services.trading.fast_path.calibration import is_cost_barrier_excluded

    ticker_positive = {
        "horizon_s": 5,
        "sample_count": 30,
        "mean_return": 0.003,
        "m2_return": 0.00000001,
    }
    pooled_below_cost = {
        "horizon_s": 5,
        "sample_count": 80,
        "mean_return": 0.0002,
        "m2_return": 0.00000001,
    }
    with patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[ticker_positive],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[pooled_below_cost],
    ) as pooled_lookup:
        excluded, evidence = is_cost_barrier_excluded(
            "fake_engine",
            ticker="BTC-USD",
            alert_type="book_pressure_reclaim_long",
            signal_score=0.5,
            cost_bps=20.0,
            table="fast_signal_decay_maker_filled",
        )

    assert excluded is False
    assert evidence["scope"] == "ticker"
    assert evidence["verdict"] == "positive_edge_candidate"
    pooled_lookup.assert_not_called()


def test_maker_attempt_filter_blocks_adverse_fills_and_missed_moves():
    from app.services.trading.fast_path.calibration import (
        maker_attempt_adverse_selection_excluded,
    )

    rows = [
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -5.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -6.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -4.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 3.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 4.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "replaced", "mid_drift_bps": 5.0,
         "signal_score": 0.5},
    ]
    with patch(
        "app.services.trading.fast_path.calibration._fetch_maker_attempt_drift_rows",
        return_value=rows,
    ):
        excluded, evidence = maker_attempt_adverse_selection_excluded(
            "fake_engine",
            ticker="BTC-USD",
            alert_type="imbalance_long",
            signal_score=0.5,
            window_hours=24,
        )

    assert excluded is True
    assert evidence["verdict"] == "adverse_selection"
    assert evidence["score_bucket"] == "med"
    assert "maker_fills_adversely" in evidence["blocked_reasons"]
    assert "maker_misses_favorable_moves" in evidence["blocked_reasons"]
    assert evidence["filled_evidence"]["upper_side_mid_drift_bps"] < 0.0
    assert (
        evidence["unfilled_terminal_evidence"]["lower_side_mid_drift_bps"]
        > 0.0
    )


def test_maker_attempt_filter_accepts_score_bucket_directly():
    from app.services.trading.fast_path.calibration import (
        maker_attempt_adverse_selection_excluded_for_bucket,
    )

    rows = [
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 3.0,
         "signal_score": 0.75},
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 4.0,
         "signal_score": 0.75},
        {"side": "buy", "fill_outcome": "replaced", "mid_drift_bps": 5.0,
         "signal_score": 0.75},
    ]
    with patch(
        "app.services.trading.fast_path.calibration._fetch_maker_attempt_drift_rows",
        return_value=rows,
    ):
        excluded, evidence = maker_attempt_adverse_selection_excluded_for_bucket(
            "fake_engine",
            ticker="AERO-USD",
            alert_type="imbalance_long",
            score_bucket_name="high",
            window_hours=24,
            allow_pooled=False,
        )

    assert excluded is True
    assert evidence["score_bucket"] == "high"
    assert evidence["verdict"] == "adverse_selection"
    assert evidence["blocked_reasons"] == ["maker_misses_favorable_moves"]


def test_maker_attempt_filter_ignores_zero_variance_drift():
    from app.services.trading.fast_path.calibration import (
        maker_attempt_adverse_selection_excluded,
    )

    rows = [
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -5.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -5.0,
         "signal_score": 0.5},
    ]
    with patch(
        "app.services.trading.fast_path.calibration._fetch_maker_attempt_drift_rows",
        return_value=rows,
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_maker_attempt_drift_rows",
        return_value=[],
    ):
        excluded, evidence = maker_attempt_adverse_selection_excluded(
            "fake_engine",
            ticker="BTC-USD",
            alert_type="imbalance_long",
            signal_score=0.5,
        )

    assert excluded is False
    assert evidence["verdict"] == "insufficient_statistical_evidence"
    assert evidence["minimum_requirement"] == "sample_count>=2 and nonzero_variance"


def test_maker_attempt_filter_falls_back_to_pooled_when_ticker_is_sparse():
    from app.services.trading.fast_path.calibration import (
        maker_attempt_adverse_selection_excluded,
    )

    ticker_rows = [
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -5.0,
         "signal_score": 0.5},
    ]
    pooled_rows = [
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -5.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -6.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": -4.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 3.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "cancelled", "mid_drift_bps": 4.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "replaced", "mid_drift_bps": 5.0,
         "signal_score": 0.5},
    ]
    with patch(
        "app.services.trading.fast_path.calibration._fetch_maker_attempt_drift_rows",
        return_value=ticker_rows,
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_maker_attempt_drift_rows",
        return_value=pooled_rows,
    ):
        excluded, evidence = maker_attempt_adverse_selection_excluded(
            "fake_engine",
            ticker="TEST-USD",
            alert_type="imbalance_long",
            signal_score=0.5,
            window_hours=24,
        )

    assert excluded is True
    assert evidence["scope"] == "pooled"
    assert evidence["ticker_scope_verdict"] == "insufficient_statistical_evidence"
    assert evidence["ticker_scope_attempts"] == 1
    assert "maker_fills_adversely" in evidence["blocked_reasons"]
    assert "maker_misses_favorable_moves" in evidence["blocked_reasons"]


def test_maker_attempt_filter_keeps_decisive_ticker_not_excluded_over_pooled():
    from app.services.trading.fast_path.calibration import (
        maker_attempt_adverse_selection_excluded,
    )

    ticker_rows = [
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": 4.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": 6.0,
         "signal_score": 0.5},
        {"side": "buy", "fill_outcome": "filled", "mid_drift_bps": 5.0,
         "signal_score": 0.5},
    ]
    with patch(
        "app.services.trading.fast_path.calibration._fetch_maker_attempt_drift_rows",
        return_value=ticker_rows,
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_maker_attempt_drift_rows",
        side_effect=AssertionError("pooled should not be queried"),
    ):
        excluded, evidence = maker_attempt_adverse_selection_excluded(
            "fake_engine",
            ticker="TEST-USD",
            alert_type="imbalance_long",
            signal_score=0.5,
        )

    assert excluded is False
    assert evidence["scope"] == "ticker"
    assert evidence["verdict"] == "not_excluded"


def test_maker_attempt_gate_denies_in_maker_mode_when_evidence_excludes():
    evidence = {
        "verdict": "adverse_selection",
        "blocked_reasons": ["maker_fills_adversely"],
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(execution_mode="maker_only"),
    ), patch(
        "app.services.trading.fast_path.calibration.maker_attempt_adverse_selection_excluded",
        return_value=(True, evidence),
    ) as lookup:
        result = gate_maker_attempt_adverse_selection(
            _alert(alert_type="imbalance_long", signal_score=0.5),
            _ctx(),
        )

    assert result.allow is False
    assert result.reason == "maker_adverse_selection"
    assert result.detail["execution_mode"] == "maker_only"
    assert lookup.call_args.kwargs["window_hours"] == 24


def test_maker_attempt_gate_skips_taker_mode():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(execution_mode="taker"),
    ), patch(
        "app.services.trading.fast_path.calibration.maker_attempt_adverse_selection_excluded",
        side_effect=AssertionError("maker evidence should not be queried"),
    ):
        result = gate_maker_attempt_adverse_selection(_alert(), _ctx())

    assert result.allow is True
    assert result.detail["verdict"] == "non_maker_mode"


def test_pullback_gate_defers_btc_to_learned_edge_gates():
    result = gate_pullback_ticker_allowed(
        _alert(ticker="BTC-USD", alert_type="volume_breakout_pullback_long"),
        _ctx(),
    )
    assert result.allow is True
    assert result.detail["verdict"] == "deferred_to_learned_edge_gates"
    assert result.detail["ticker"] == "BTC-USD"


def test_pullback_gate_defers_sol_to_learned_edge_gates():
    result = gate_pullback_ticker_allowed(
        _alert(ticker="SOL-USD", alert_type="volume_breakout_pullback_long"),
        _ctx(),
    )
    assert result.allow is True
    assert result.detail["verdict"] == "deferred_to_learned_edge_gates"


def test_recency_blocks_pullback_when_original_signal_is_stale():
    now = datetime(2026, 5, 23, 14, 57, 53)
    result = gate_recency(
        _alert(
            alert_type="volume_breakout_pullback_long",
            fired_at=now,
            features={
                "original_fired_at": (now - timedelta(minutes=10)).isoformat(),
                "delay_s": 25.0,
            },
        ),
        ExecContext(now_wall=now),
    )
    assert result.allow is False
    assert result.reason == "original_alert_too_old"


def test_recency_allows_pullback_with_original_signal_inside_delay_window():
    now = datetime(2026, 5, 23, 14, 57, 53)
    result = gate_recency(
        _alert(
            alert_type="volume_breakout_pullback_long",
            fired_at=now,
            features={
                "original_fired_at": (now - timedelta(seconds=80)).isoformat(),
                "delay_s": 25.0,
            },
        ),
        ExecContext(now_wall=now),
    )
    assert result.allow is True
    assert result.detail["max_original_age_s"] == pytest.approx(85.0)


def test_live_alpha_evidence_allows_paper_exploration_without_engine():
    result = gate_live_alpha_evidence(_alert(), _ctx(engine=None, mode="paper"))
    assert result.allow is True
    assert result.detail["verdict"] == "paper_mode"


def test_live_alpha_evidence_blocks_live_without_engine():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(live_alpha_gate=True),
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(engine=None, mode="live"))
    assert result.allow is False
    assert result.reason == "no_engine"


def test_live_alpha_evidence_blocks_insufficient_decay_samples():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True, taker_fee_bps=5.0, live_min_samples=50,
        ),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=None,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(spread_bps=2.0, mode="live"))
    assert result.allow is False
    assert result.reason == "insufficient_decay_evidence"


def test_live_alpha_evidence_preserves_zero_sample_floor():
    best = {"horizon_s": 60, "sample_count": 1, "mean_return": 0.003}
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True, taker_fee_bps=5.0, live_min_samples=0,
            live_min_net_bps=0.0,
        ),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[best],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=best,
    ) as best_row, patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(spread_bps=0.0, mode="live"))

    assert result.allow is True
    assert result.detail["sample_count"] == 1
    best_row.assert_called_once_with([best], min_samples=0)


def test_live_alpha_evidence_allows_live_when_bucket_clears_cost():
    best = {"horizon_s": 60, "sample_count": 75, "mean_return": 0.003}
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True, taker_fee_bps=5.0, live_min_samples=50,
            live_min_net_bps=0.0,
        ),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[best],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=best,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_live_alpha_evidence(_alert(), _ctx(spread_bps=2.0, mode="live"))
    assert result.allow is True
    assert result.detail["net_bps"] == pytest.approx(16.0)


def test_universe_status_allows_paper_shadow_learning_without_engine():
    result = gate_universe_active_for_live(_alert(), _ctx(engine=None, mode="paper"))
    assert result.allow is True
    assert result.detail["verdict"] == "paper_mode"


def test_universe_status_allows_live_when_rotation_disabled():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(universe_rotation_enabled=False),
    ):
        result = gate_universe_active_for_live(_alert(), _ctx(engine=None, mode="live"))
    assert result.allow is True
    assert result.detail["verdict"] == "rotation_disabled"


def test_universe_status_allows_live_active_pair():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(universe_rotation_enabled=True),
    ):
        result = gate_universe_active_for_live(
            _alert(ticker="SOL-USD"),
            _ctx(engine=_FakeUniverseEngine(UNIVERSE_STATUS_ACTIVE), mode="live"),
        )
    assert result.allow is True
    assert result.detail["verdict"] == "active"


def test_universe_status_blocks_live_shadow_pair():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(universe_rotation_enabled=True),
    ):
        result = gate_universe_active_for_live(
            _alert(ticker="SOL-USD"),
            _ctx(engine=_FakeUniverseEngine(UNIVERSE_STATUS_SHADOW), mode="live"),
        )
    assert result.allow is False
    assert result.reason == "universe_not_active:shadow"


# ---------------------------------------------------------------------------
# No engine -> allow with verdict='no_engine'
# ---------------------------------------------------------------------------

def test_gate_no_engine_allows_through():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(engine=None))
    assert result.allow is True
    assert result.detail.get("verdict") == "no_engine"


# ---------------------------------------------------------------------------
# Best-row mean clears 2x cost -> allow
# ---------------------------------------------------------------------------

def test_gate_clears_when_mean_above_round_trip_cost():
    """Cost = 2 * (5 bps fee + 5 bps spread) = 20 bps = 0.002.
    Mean = 30 bps = 0.003 -> clears."""
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.003,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))
    assert result.allow is True
    assert result.detail["verdict"] == "cleared"
    assert result.detail["mean_return_bps"] == pytest.approx(30.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(20.0, abs=0.01)


def test_gate_uses_live_coinbase_fee_tier_when_enabled():
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.003,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True,
            taker_fee_bps=5.0,
            live_fee_enabled=True,
        ),
    ), patch(
        "app.services.coinbase_service.get_fee_rates_bps",
        return_value={
            "maker_fee_bps": 40.0,
            "taker_fee_bps": 80.0,
            "pricing_tier": "Intro 2",
        },
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))

    assert result.allow is False
    assert result.reason == "below_round_trip_cost"
    assert result.detail["fee_source"] == "coinbase_live"
    assert result.detail["pricing_tier"] == "Intro 2"
    assert result.detail["fee_bps"] == pytest.approx(80.0)
    assert result.detail["cost_bps"] == pytest.approx(170.0)


def test_gate_falls_back_to_static_fee_when_live_fee_unavailable():
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.003,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True,
            taker_fee_bps=5.0,
            live_fee_enabled=True,
        ),
    ), patch(
        "app.services.coinbase_service.get_fee_rates_bps",
        return_value={},
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))

    assert result.allow is True
    assert result.detail["fee_source"] == "settings_fallback"
    assert result.detail["fee_error"] == "live_fee_unavailable"
    assert result.detail["fee_bps"] == pytest.approx(5.0)
    assert result.detail["cost_bps"] == pytest.approx(20.0)


@pytest.mark.parametrize("bad_live_taker_fee", [-80.0, "nan", "inf", "bad"])
def test_gate_falls_back_to_static_fee_when_live_taker_fee_invalid(
    bad_live_taker_fee,
):
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.003,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(
            enabled=True,
            taker_fee_bps=5.0,
            live_fee_enabled=True,
        ),
    ), patch(
        "app.services.coinbase_service.get_fee_rates_bps",
        return_value={
            "maker_fee_bps": 40.0,
            "taker_fee_bps": bad_live_taker_fee,
            "pricing_tier": "Broken",
        },
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))

    assert result.allow is True
    assert result.detail["fee_source"] == "settings_fallback"
    assert result.detail["fee_error"] == "live_fee_invalid:taker_fee_bps"
    assert result.detail["fee_bps"] == pytest.approx(5.0)
    assert result.detail["cost_bps"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Best-row mean below 2x cost -> reject
# ---------------------------------------------------------------------------

def test_gate_rejects_when_mean_below_round_trip_cost():
    """Cost = 2 * (5 + 5) = 20 bps. Mean = 10 bps = 0.001 -> reject."""
    fake_row = {
        "horizon_s": 60,
        "sample_count": 100,
        "mean_return": 0.001,
        "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))
    assert result.allow is False
    assert result.reason == "below_round_trip_cost"
    assert result.detail["verdict"] == "below_cost"
    assert result.detail["mean_return_bps"] == pytest.approx(10.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(20.0, abs=0.01)


# ---------------------------------------------------------------------------
# No data -> allow with no_data verdict (cold-start safety)
# ---------------------------------------------------------------------------

def test_gate_no_data_allows_through_with_no_data_verdict():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_pooled_bucket_rows",
        return_value=[],
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail["verdict"] == "no_data"


# ---------------------------------------------------------------------------
# Lookup failure -> allow (mirrors gate_calibrated_tradeability behaviour)
# ---------------------------------------------------------------------------

def test_gate_lookup_failure_allows_through():
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        side_effect=RuntimeError("simulated DB failure"),
    ):
        result = gate_cost_aware_admission(_alert(), _ctx())
    assert result.allow is True
    assert result.detail["verdict"] == "lookup_failed"


# ---------------------------------------------------------------------------
# Live spread surfaces in detail (non-zero spread reflected)
# ---------------------------------------------------------------------------

def test_gate_uses_live_spread_from_ctx():
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.002, "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_fp_settings(enabled=True, taker_fee_bps=5.0),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ), patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        # Spread = 15 bps -> cost = 2 * (5 + 15) = 40 bps
        result = gate_cost_aware_admission(
            _alert(), _ctx(spread_bps=15.0),
        )
    assert result.detail["spread_bps"] == pytest.approx(15.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(40.0, abs=0.01)
    # Mean = 20 bps, cost = 40 bps -> below
    assert result.allow is False
