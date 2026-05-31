"""DB-free guards for the Phase H position-sizer writer."""
from __future__ import annotations

import pytest

from app.services.trading.position_sizer_writer import _divergence_bps, _finite_float


def test_writer_finite_float_rejects_bool_and_nonfinite_values() -> None:
    assert _finite_float(True) is None
    assert _finite_float(False) is None
    assert _finite_float("NaN") is None
    assert _finite_float("Infinity") is None
    assert _finite_float("12.5") == pytest.approx(12.5)


def test_writer_divergence_rejects_malformed_legacy_notional() -> None:
    assert _divergence_bps(100.0, True) is None
    assert _divergence_bps(100.0, "NaN") is None
    assert _divergence_bps(100.0, "Infinity") is None


def test_writer_divergence_preserves_zero_legacy_sentinel() -> None:
    assert _divergence_bps(100.0, 0.0) == pytest.approx(1_000_000.0)
    assert _divergence_bps(0.0, 0.0) == pytest.approx(0.0)
