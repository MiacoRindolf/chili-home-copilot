from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.services.trading.pattern_quality_score import (
    _resolve_weights,
    compute_quality_composite_score,
    realized_evidence_score,
    realized_pnl_score,
)


_WEIGHTS = {
    "cpcv_sharpe": 0.10,
    "deflated_sharpe": 0.05,
    "pbo_inverse": 0.05,
    "directional_wr": 0.35,
    "decay_inverse": 0.10,
    "realized": 0.35,
    "realized_evidence_tau": 30.0,
}


def _pattern(**overrides):
    base = {
        "cpcv_median_sharpe": 2.0,
        "deflated_sharpe": 1.0,
        "pbo": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_realized_pnl_score_rejects_boolean_and_nonfinite_inputs() -> None:
    assert realized_pnl_score(True, 0.01) is None
    assert realized_pnl_score(0.01, True) is None
    assert realized_pnl_score(math.nan, 0.01) is None
    assert realized_pnl_score(math.inf, 0.01) is None
    assert realized_pnl_score(0.0, 0.01) == pytest.approx(0.5)


def test_realized_evidence_score_rejects_boolean_and_negative_inputs() -> None:
    with pytest.raises(TypeError):
        realized_evidence_score(True, 30.0)
    with pytest.raises(TypeError):
        realized_evidence_score(5, True)
    with pytest.raises(ValueError):
        realized_evidence_score(-1, 30.0)


def test_quality_composite_rejects_boolean_required_metrics() -> None:
    assert compute_quality_composite_score(
        _pattern(cpcv_median_sharpe=True),
        directional_wr=0.60,
        decay=0.10,
        weights=_WEIGHTS,
    ) is None
    assert compute_quality_composite_score(
        _pattern(),
        directional_wr=True,
        decay=0.10,
        weights=_WEIGHTS,
    ) is None


def test_quality_composite_treats_boolean_realized_score_as_absent() -> None:
    no_realized = compute_quality_composite_score(
        _pattern(),
        directional_wr=0.60,
        decay=0.10,
        weights=_WEIGHTS,
        realized_pnl_score=None,
        realized_n_trades=0,
    )
    bogus_realized = compute_quality_composite_score(
        _pattern(),
        directional_wr=0.60,
        decay=0.10,
        weights=_WEIGHTS,
        realized_pnl_score=True,
        realized_n_trades=10,
    )
    valid_realized = compute_quality_composite_score(
        _pattern(),
        directional_wr=0.60,
        decay=0.10,
        weights=_WEIGHTS,
        realized_pnl_score=1.0,
        realized_n_trades=10,
    )

    assert bogus_realized == pytest.approx(no_realized)
    assert valid_realized != pytest.approx(bogus_realized)


def test_resolve_weights_rejects_boolean_settings_values() -> None:
    settings = SimpleNamespace(
        chili_cohort_score_weight_cpcv_sharpe=True,
        chili_cohort_score_realized_pnl_normalizer_pct=True,
        chili_cohort_score_realized_window_days=True,
    )

    weights = _resolve_weights(settings)

    assert weights["cpcv_sharpe"] == pytest.approx(0.10)
    assert weights["realized_pnl_normalizer_pct"] == pytest.approx(0.01)
    assert weights["realized_window_days"] == 90
