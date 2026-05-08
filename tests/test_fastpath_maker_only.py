"""f-fastpath-maker-only execution-mode dispatch tests.

Covers the cost-aware admission gate's table-and-fee dispatch on
``execution_mode``:

  - taker mode (default) -> uses cost_aware_taker_fee_bps + reads from
    fast_signal_decay (existing behaviour, bit-identical).
  - maker_only mode -> uses cost_aware_maker_fee_bps + reads from
    fast_signal_decay_maker_filled (new path).
  - maker_first_then_taker mode -> same fee/table as maker_only because
    the gate is checking economic feasibility under the most-favourable
    achievable fee tier; if maker doesn't clear the maker-decay-table's
    bar, the trade isn't worth taking.

Also pins ``_fetch_bucket_rows`` parameter handling (rejects unknown
table names; default = fast_signal_decay).

Helper-level tests with mocked settings + mocked calibration helpers.
No DB / no broker.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.services.trading.fast_path.gates import (
    ExecContext,
    gate_cost_aware_admission,
)


def _alert(*, ticker="ICP-USD", alert_type="volume_breakout_long",
           signal_score=0.85):
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "signal_score": signal_score,
        "fired_at": datetime.utcnow(),
    }


def _ctx(*, spread_bps=5.0, engine="fake_engine_object"):
    return ExecContext(
        now_wall=datetime.utcnow(),
        best_bid=100.0,
        best_ask=100.05,
        spread_bps=spread_bps,
        engine=engine,
    )


def _stub_settings(
    *, enabled: bool = True, execution_mode: str = "taker",
    taker_fee_bps: float = 60.0, maker_fee_bps: float = 40.0,
):
    from app.services.trading.fast_path.settings import FastPathSettings

    return FastPathSettings(
        cost_aware_admission_enabled=enabled,
        cost_aware_taker_fee_bps=taker_fee_bps,
        cost_aware_maker_fee_bps=maker_fee_bps,
        execution_mode=execution_mode,
    )


# ---------------------------------------------------------------------------
# Mode dispatch: taker -> taker fee + fast_signal_decay
# ---------------------------------------------------------------------------

def test_taker_mode_uses_taker_fee_and_default_table():
    """Default taker mode reads from fast_signal_decay and uses the
    taker fee. Bit-identical to the prior cost-aware-gate behaviour."""
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.020, "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_settings(execution_mode="taker"),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ) as fetch_mock, patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))

    assert result.detail["execution_mode"] == "taker"
    assert result.detail["decay_table"] == "fast_signal_decay"
    # fee = 60 (taker), spread = 5 -> cost = 2 * (60 + 5) = 130 bps
    assert result.detail["fee_bps"] == pytest.approx(60.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(130.0, abs=0.01)
    # Verify the table param was forwarded to the calibration helper
    call = fetch_mock.call_args
    assert call.kwargs.get("table") == "fast_signal_decay"


# ---------------------------------------------------------------------------
# Mode dispatch: maker_only -> maker fee + fast_signal_decay_maker_filled
# ---------------------------------------------------------------------------

def test_maker_only_mode_uses_maker_fee_and_filled_table():
    """maker_only mode reads from fast_signal_decay_maker_filled and
    uses the maker fee. Brief's load-bearing dispatch test."""
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.010, "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_settings(execution_mode="maker_only"),
    ), patch(
        "app.services.trading.fast_path.calibration._fetch_bucket_rows",
        return_value=[fake_row],
    ) as fetch_mock, patch(
        "app.services.trading.fast_path.calibration._best_sharpe_row",
        return_value=fake_row,
    ), patch(
        "app.services.trading.fast_path.decay_miner.score_bucket",
        return_value="high",
    ):
        result = gate_cost_aware_admission(_alert(), _ctx(spread_bps=5.0))

    assert result.detail["execution_mode"] == "maker_only"
    assert result.detail["decay_table"] == "fast_signal_decay_maker_filled"
    # fee = 40 (maker), spread = 5 -> cost = 2 * (40 + 5) = 90 bps
    assert result.detail["fee_bps"] == pytest.approx(40.0, abs=0.01)
    assert result.detail["cost_bps"] == pytest.approx(90.0, abs=0.01)
    call = fetch_mock.call_args
    assert call.kwargs.get("table") == "fast_signal_decay_maker_filled"


# ---------------------------------------------------------------------------
# Mode dispatch: maker_first_then_taker -> same as maker_only for gate
# ---------------------------------------------------------------------------

def test_maker_first_then_taker_uses_maker_fee_for_admission():
    """Hybrid mode uses maker economics for the admission gate. The
    rationale: the gate is asking 'is this trade worth doing under the
    BEST achievable execution?'; if maker doesn't clear, the fallback
    to taker won't either (taker fee is higher)."""
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.010, "m2_return": 0.0001,
    }
    with patch(
        "app.services.trading.fast_path.settings.load",
        return_value=_stub_settings(execution_mode="maker_first_then_taker"),
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
    assert result.detail["execution_mode"] == "maker_first_then_taker"
    assert result.detail["decay_table"] == "fast_signal_decay_maker_filled"
    assert result.detail["fee_bps"] == pytest.approx(40.0, abs=0.01)


# ---------------------------------------------------------------------------
# Maker fee allows trades that taker fee would reject
# ---------------------------------------------------------------------------

def test_maker_clears_when_taker_would_reject_same_signal():
    """Same signal: taker mode rejects (mean = 100bps < cost = 130bps),
    maker mode clears (mean = 100bps >= cost = 90bps). This is the
    economic motivation for the entire maker-only feature."""
    fake_row = {
        "horizon_s": 60, "sample_count": 100,
        "mean_return": 0.010,  # 100 bps
        "m2_return": 0.0001,
    }
    common_patches = [
        patch(
            "app.services.trading.fast_path.calibration._fetch_bucket_rows",
            return_value=[fake_row],
        ),
        patch(
            "app.services.trading.fast_path.calibration._best_sharpe_row",
            return_value=fake_row,
        ),
        patch(
            "app.services.trading.fast_path.decay_miner.score_bucket",
            return_value="high",
        ),
    ]

    # taker mode -> rejects
    for p in common_patches: p.start()
    try:
        with patch(
            "app.services.trading.fast_path.settings.load",
            return_value=_stub_settings(execution_mode="taker"),
        ):
            taker_result = gate_cost_aware_admission(
                _alert(), _ctx(spread_bps=5.0)
            )
    finally:
        for p in common_patches: p.stop()
    assert taker_result.allow is False
    assert taker_result.reason == "below_round_trip_cost"

    # maker_only mode -> clears
    common_patches2 = [
        patch(
            "app.services.trading.fast_path.calibration._fetch_bucket_rows",
            return_value=[fake_row],
        ),
        patch(
            "app.services.trading.fast_path.calibration._best_sharpe_row",
            return_value=fake_row,
        ),
        patch(
            "app.services.trading.fast_path.decay_miner.score_bucket",
            return_value="high",
        ),
    ]
    for p in common_patches2: p.start()
    try:
        with patch(
            "app.services.trading.fast_path.settings.load",
            return_value=_stub_settings(execution_mode="maker_only"),
        ):
            maker_result = gate_cost_aware_admission(
                _alert(), _ctx(spread_bps=5.0)
            )
    finally:
        for p in common_patches2: p.stop()
    assert maker_result.allow is True
    assert maker_result.detail["verdict"] == "cleared"


# ---------------------------------------------------------------------------
# _fetch_bucket_rows defensive table-name handling
# ---------------------------------------------------------------------------

def test_fetch_bucket_rows_rejects_unknown_table_name():
    """SQL injection defence: only the two allowlisted tables work."""
    from app.services.trading.fast_path.calibration import _fetch_bucket_rows

    class _FakeEngine:
        def connect(self):
            raise AssertionError("must not reach DB on rejected table name")

    with pytest.raises(ValueError, match="unsupported decay table"):
        _fetch_bucket_rows(
            _FakeEngine(), ticker="X", alert_type="y", bucket="high",
            table="DROP TABLE users; --",
        )


def test_fetch_bucket_rows_default_table_is_fast_signal_decay():
    """Backwards-compat: callers that omit ``table`` get the no-friction
    table (the prior single-table behaviour)."""
    from app.services.trading.fast_path.calibration import _fetch_bucket_rows
    import inspect

    sig = inspect.signature(_fetch_bucket_rows)
    table_param = sig.parameters.get("table")
    assert table_param is not None
    assert table_param.default == "fast_signal_decay"
