"""DB-free guards for the Phase H position-sizer emitter."""
from __future__ import annotations

import pytest

from app.services.trading.position_sizer_emitter import (
    EmitterSignal,
    _build_input,
    _clamp01,
    _finite_float,
    _infer_asset_class,
    _settings_float,
)
from app.services.trading.position_sizer_model import compute_proposal


def _signal(**overrides) -> EmitterSignal:
    base = {
        "source": "unit.emitter",
        "ticker": "SPY",
        "direction": "long",
        "entry_price": 1.25,
        "stop_price": 0.75,
        "capital": 10_000.0,
        "target_price": 1.75,
        "asset_class": "options",
        "confidence": 0.6,
    }
    base.update(overrides)
    return EmitterSignal(**base)


def test_emitter_finite_float_rejects_bool_and_nonfinite_values() -> None:
    assert _finite_float(True) is None
    assert _finite_float(False) is None
    assert _finite_float("NaN") is None
    assert _finite_float("1.25") == pytest.approx(1.25)


def test_emitter_clamp01_rejects_bool_confidence() -> None:
    assert _clamp01(True) == pytest.approx(0.55)
    assert _clamp01(False) == pytest.approx(0.55)
    assert _clamp01(75.0) == pytest.approx(0.75)


def test_emitter_boolean_prices_do_not_become_one_dollar_sizer_inputs() -> None:
    inp = _build_input(
        _signal(entry_price=True, stop_price=False, capital=True),
        net_edge_score=None,
    )

    assert inp.entry_price == 0.0
    assert inp.stop_price == 0.0
    assert inp.capital == 0.0

    out = compute_proposal(inp=inp, source="unit.emitter")
    assert out.proposed_notional == 0.0
    assert out.reasoning.get("reject_reason") == "invalid_prices_or_capital"


def test_emitter_settings_float_defaults_malformed_values(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.settings.brain_position_sizer_kelly_scale",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.settings.brain_position_sizer_max_risk_pct",
        "NaN",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.settings.brain_position_sizer_equity_bucket_cap_pct",
        -5.0,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.settings.brain_position_sizer_crypto_bucket_cap_pct",
        "Infinity",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.settings.brain_position_sizer_single_ticker_cap_pct",
        False,
        raising=False,
    )

    inp = _build_input(_signal(), net_edge_score=None)

    assert inp.kelly_scale == pytest.approx(0.25)
    assert inp.max_risk_pct == pytest.approx(2.0)
    assert inp.equity_bucket_cap_pct == pytest.approx(15.0)
    assert inp.crypto_bucket_cap_pct == pytest.approx(10.0)
    assert inp.single_ticker_cap_pct == pytest.approx(7.5)
    assert _settings_float("brain_position_sizer_kelly_scale", 0.25) == pytest.approx(0.25)


def test_emitter_option_aliases_use_contract_multiplier() -> None:
    inp = _build_input(_signal(asset_class="robinhood_options"), net_edge_score=None)

    assert inp.asset_class == "options"
    assert inp.unit_multiplier == pytest.approx(100.0)

    out = compute_proposal(inp=inp, source="unit.emitter")
    assert out.proposed_notional == pytest.approx(
        out.proposed_quantity * inp.entry_price * 100.0,
        rel=1e-9,
        abs=1e-4,
    )


def test_emitter_infers_crypto_but_canonicalizes_option_alias() -> None:
    assert _infer_asset_class("BTC-USD", None) == "crypto"
    assert _infer_asset_class("SPY", "option_contract") == "options"
