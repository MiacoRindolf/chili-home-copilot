"""DB-free guards for the Phase H position-sizer writer."""
from __future__ import annotations

import json

import pytest

from app.services.trading.position_sizer_model import PositionSizerInput
from app.services.trading.position_sizer_writer import (
    _divergence_bps,
    _finite_float,
    write_proposal,
)


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


class _FakeScalarResult:
    def scalar_one(self) -> int:
        return 77


class _FakeWriterDb:
    def __init__(self) -> None:
        self.params = None
        self.commits = 0

    def execute(self, _stmt, params):
        self.params = dict(params)
        return _FakeScalarResult()

    def commit(self) -> None:
        self.commits += 1


def test_writer_payload_persists_probability_input_evidence() -> None:
    db = _FakeWriterDb()
    inp = PositionSizerInput(
        ticker="SPY",
        direction="long",
        asset_class="options",
        entry_price=1.25,
        stop_price=0.75,
        capital=10_000.0,
        calibrated_prob=0.75,
        payoff_fraction=0.4,
        loss_per_unit=0.4,
        expected_net_pnl=0.1,
        unit_multiplier=100.0,
        probability_input={
            "source_surface": "position_sizer_emitter.signal_confidence",
            "parser": "position_sizer_emitter._clamp01",
            "raw_value": 75.0,
            "accepted_scale": "percent_0_100",
            "normalized_probability": 0.75,
            "parser_outcome": "accepted",
            "rejection_reason": None,
        },
    )

    result = write_proposal(
        db,
        inp=inp,
        source="unit.writer",
        mode_override="shadow",
    )

    assert result is not None
    assert db.commits == 1
    payload = json.loads(db.params["payload_json"])
    assert payload["probability_input"] == inp.probability_input
