"""Tests for AutoTrader synergy / scale-in planning."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.trading import Trade
from app.config import AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE
from app.services.trading.auto_trader_synergy import (
    SCALE_IN_ALERT_IDS_SNAPSHOT_KEY,
    SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY,
    maybe_scale_in,
)

EXPLICIT_SCALE_IN_NOTIONAL = 150.0
FRACTIONAL_SCALE_IN_CAP = 40.0
FRACTIONAL_SCALE_IN_FRACTION = 0.25
MAX_SCALE_INS_PER_TRADE = 1
POSITION_ENTRY_PRICE = 10.0
POSITION_QUANTITY = 30.0
CONFIRMING_PRICE = 11.0
POSITION_STOP = 9.0
POSITION_TARGET = 12.0
CONFIRMING_STOP = 9.5
CONFIRMING_TARGET = 13.0
FLOAT_TOLERANCE = 1e-6


def test_maybe_scale_in_disabled():
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = False
    assert (
        maybe_scale_in(
            db,
            user_id=1,
            ticker="AAA",
            new_scan_pattern_id=2,
            new_stop=9.0,
            new_target=50.0,
            current_price=10.0,
            settings=settings,
        )
        is None
    )


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_computes_weighted_entry(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=0,
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = EXPLICIT_SCALE_IN_NOTIONAL

    plan = maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    )
    assert plan is not None
    assert plan.new_stop == POSITION_STOP
    assert plan.new_target == CONFIRMING_TARGET
    add_q = EXPLICIT_SCALE_IN_NOTIONAL / CONFIRMING_PRICE
    expected_avg = (
        POSITION_ENTRY_PRICE * POSITION_QUANTITY
        + CONFIRMING_PRICE * add_q
    ) / (POSITION_QUANTITY + add_q)
    assert abs(plan.new_avg_entry - expected_avg) < FLOAT_TOLERANCE


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_fractional_default_is_capped(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=0,
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = 0.0
    settings.chili_autotrader_synergy_fraction = FRACTIONAL_SCALE_IN_FRACTION
    settings.chili_autotrader_synergy_max_notional_usd = FRACTIONAL_SCALE_IN_CAP

    plan = maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    )

    assert plan is not None
    assert plan.add_notional_usd == FRACTIONAL_SCALE_IN_CAP


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_respects_max_scale_in_cap(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=MAX_SCALE_INS_PER_TRADE,
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = EXPLICIT_SCALE_IN_NOTIONAL
    settings.chili_autotrader_synergy_max_scale_ins_per_trade = MAX_SCALE_INS_PER_TRADE

    assert maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    ) is None


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_default_cap_is_derived_not_legacy_one_and_done(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=1,
        indicator_snapshot={SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY: [2]},
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = EXPLICIT_SCALE_IN_NOTIONAL

    plan = maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=3,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    )

    assert AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE > MAX_SCALE_INS_PER_TRADE
    assert plan is not None
    assert plan.confirming_pattern_id == 3


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_blocks_reused_confirming_pattern(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=1,
        indicator_snapshot={SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY: [2]},
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = EXPLICIT_SCALE_IN_NOTIONAL
    settings.chili_autotrader_synergy_max_scale_ins_per_trade = (
        AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE
    )

    assert maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    ) is None


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_blocks_legacy_reused_confirming_pattern(mock_find):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [(2,)]
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=POSITION_ENTRY_PRICE,
        quantity=POSITION_QUANTITY,
        status="open",
        stop_loss=POSITION_STOP,
        take_profit=POSITION_TARGET,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=1,
        indicator_snapshot={SCALE_IN_ALERT_IDS_SNAPSHOT_KEY: [99]},
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_synergy_scale_notional_usd = EXPLICIT_SCALE_IN_NOTIONAL
    settings.chili_autotrader_synergy_max_scale_ins_per_trade = (
        AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE
    )

    assert maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=CONFIRMING_STOP,
        new_target=CONFIRMING_TARGET,
        current_price=CONFIRMING_PRICE,
        settings=settings,
    ) is None
