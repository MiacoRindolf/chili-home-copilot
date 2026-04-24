"""Q1.T3 ``Signal`` Pydantic contract tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.trading.contracts.signal import Signal


def test_signal_valid_minimal() -> None:
    now = datetime.utcnow()
    s = Signal(
        signal_id="sid",
        scanner="top_pick",
        strategy_family="test_pat",
        symbol="SPY",
        venue="US_EQ",
        side="long",
        horizon="swing",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        atr=Decimal("2"),
        expected_return=Decimal("0.1"),
        expected_vol=Decimal("0.02"),
        confidence=0.8,
        features={},
        rule_fires=["breakout"],
    )
    assert s.gate_status == "proposed"
    assert s.gate_reasons == []


def test_expires_must_follow_created() -> None:
    now = datetime.utcnow()
    with pytest.raises(ValidationError):
        Signal(
            signal_id="sid",
            scanner="x",
            strategy_family="y",
            symbol="SPY",
            venue="US_EQ",
            side="long",
            horizon="swing",
            created_at=now,
            expires_at=now,
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            atr=Decimal("1"),
            expected_return=Decimal("0"),
            expected_vol=Decimal("0.01"),
            confidence=0.5,
            features={},
            rule_fires=[],
        )


def test_confidence_bounds() -> None:
    now = datetime.utcnow()
    with pytest.raises(ValidationError):
        Signal(
            signal_id="sid",
            scanner="x",
            strategy_family="y",
            symbol="SPY",
            venue="US_EQ",
            side="long",
            horizon="swing",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            atr=Decimal("1"),
            expected_return=Decimal("0"),
            expected_vol=Decimal("0.01"),
            confidence=1.5,
            features={},
            rule_fires=[],
        )


def test_json_round_trip_decimal_precision() -> None:
    now = datetime.utcnow()
    s = Signal(
        signal_id="sid",
        scanner="top_pick",
        strategy_family="fam",
        symbol="BTC-USD",
        venue="CRYPTO",
        side="long",
        horizon="day",
        created_at=now,
        expires_at=now + timedelta(hours=2),
        entry_price=Decimal("50000.123456789"),
        stop_price=Decimal("49000"),
        take_profit_price=Decimal("52000"),
        atr=Decimal("100.5"),
        expected_return=Decimal("0.04"),
        expected_vol=Decimal("0.002"),
        confidence=0.75,
        features={"a": 1},
        rule_fires=["momentum"],
        gate_status="gated_ok",
        gate_reasons=["x"],
    )
    data = json.loads(s.model_dump_json())
    s2 = Signal.model_validate(data)
    assert s2.entry_price == s.entry_price
    assert s2.atr == s.atr


def test_gate_status_literals_only() -> None:
    now = datetime.utcnow()
    with pytest.raises(ValidationError):
        Signal(
            signal_id="sid",
            scanner="x",
            strategy_family="y",
            symbol="SPY",
            venue="US_EQ",
            side="long",
            horizon="swing",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            atr=Decimal("1"),
            expected_return=Decimal("0"),
            expected_vol=Decimal("0.01"),
            confidence=0.5,
            features={},
            rule_fires=[],
            gate_status="invalid",  # type: ignore[arg-type]
        )


def test_extra_forbidden() -> None:
    now = datetime.utcnow()
    with pytest.raises(ValidationError):
        Signal(
            signal_id="sid",
            scanner="x",
            strategy_family="y",
            symbol="SPY",
            venue="US_EQ",
            side="long",
            horizon="swing",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            atr=Decimal("1"),
            expected_return=Decimal("0"),
            expected_vol=Decimal("0.01"),
            confidence=0.5,
            features={},
            rule_fires=[],
            bogus=1,  # type: ignore[call-arg]
        )
