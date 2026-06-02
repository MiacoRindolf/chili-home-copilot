from types import SimpleNamespace

import pytest

from app.services.trading.return_math import (
    paper_trade_realized_pnl,
    paper_trade_return_pct,
    price_return_pct,
    trade_realized_pnl,
    trade_return_pct,
)


def test_price_return_pct_is_direction_aware_for_shorts() -> None:
    assert price_return_pct(100.0, 80.0, "short") == pytest.approx(20.0)
    assert price_return_pct(100.0, 120.0, "short") == pytest.approx(-20.0)


def test_trade_return_pct_option_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_option_includes_partial_leg_in_opening_notional() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert trade_return_pct(trade) == pytest.approx(4.0)


def test_trade_realized_pnl_option_includes_partial_leg() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert trade_realized_pnl(trade) == pytest.approx(10.0)


def test_trade_return_pct_uses_filled_quantity_for_realized_notional() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        filled_quantity=1.0,
        pnl=20.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_partial_leg_requires_complete_evidence() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=None,
    )

    assert trade_return_pct(trade) is None


def test_trade_return_pct_snapshot_asset_kind_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_kind": "option"},
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_snapshot_asset_class_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_class": "options"},
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_snapshot_option_alias_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_class": "robinhood_options"},
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_snapshot_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"option_contract_multiplier": 100.0},
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_snapshot_price_domain_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_nested_snapshot_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"breakout_alert": {"contract_multiplier": 100.0}},
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_rejects_boolean_prices_and_pnl() -> None:
    trade = SimpleNamespace(
        entry_price=True,
        exit_price=1.45,
        quantity=2.0,
        pnl=True,
        direction="long",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_type": "stock"},
    )

    assert trade_return_pct(trade) is None


def test_trade_return_pct_option_price_fallback_requires_premium_domain() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert trade_return_pct(trade) == pytest.approx(16.0)


def test_trade_return_pct_option_rejects_ambiguous_price_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
    )

    assert trade_return_pct(trade) is None


def test_trade_return_pct_option_rejects_implausible_premium_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert trade_return_pct(trade) is None


def test_paper_trade_return_pct_option_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=None,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_stock_includes_partial_leg() -> None:
    trade = SimpleNamespace(
        entry_price=100.0,
        exit_price=95.0,
        quantity=5.0,
        pnl=-25.0,
        pnl_pct=-5.0,
        direction="long",
        signal_json={"asset_type": "stock"},
        partial_taken=True,
        partial_taken_qty=5.0,
        partial_taken_price=105.0,
    )

    assert paper_trade_return_pct(trade) == pytest.approx(0.0)


def test_paper_trade_return_pct_option_partial_leg_can_flip_directional_label() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        pnl_pct=-8.0,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert paper_trade_return_pct(trade) == pytest.approx(4.0)


def test_paper_trade_realized_pnl_option_includes_partial_leg() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        pnl_pct=-8.0,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert paper_trade_realized_pnl(trade) == pytest.approx(10.0)


def test_paper_trade_return_pct_asset_kind_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"asset_kind": "option"},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_asset_class_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"asset_class": "options"},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_option_alias_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"asset_class": "contract-options"},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_nested_asset_kind_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json='{"breakout_alert":{"asset_kind":"option"}}',
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"contract_multiplier": 100.0}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_options_path_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"options_path": True}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_option_meta_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"option_meta": {"strike": 500.0}}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_asset_class_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"asset_class": "option"}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_option_alias_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"asset_class": "robinhood_options"}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_option_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"_paper_meta": {"option_contract_multiplier": 100.0}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_signal_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"option_contract_multiplier": 100.0},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_price_domains_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_price_domains_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={
            "_paper_meta": {
                "price_domains": {
                    "entry_price": "option_premium",
                    "exit_price": "option_premium",
                },
            },
        },
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_paper_meta_price_domains_confirms_price_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        pnl_pct=999.0,
        direction="long",
        signal_json={
            "_paper_meta": {
                "price_domains": {
                    "entry_price": "option_premium",
                    "exit_price": "option_premium",
                },
            },
        },
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_nested_multiplier_uses_contract_multiplier() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=1600.0,
        direction="long",
        signal_json={"breakout_alert": {"contract_multiplier": 100.0}},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_rejects_boolean_prices_and_pnl() -> None:
    trade = SimpleNamespace(
        entry_price=True,
        exit_price=1.45,
        quantity=2.0,
        pnl=True,
        pnl_pct=None,
        direction="long",
        signal_json={"asset_type": "stock"},
    )

    assert paper_trade_return_pct(trade) is None


def test_paper_trade_return_pct_option_rejects_ambiguous_price_fallback() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        pnl_pct=None,
        direction="long",
        signal_json={"asset_type": "options"},
    )

    assert paper_trade_return_pct(trade) is None


def test_paper_trade_return_pct_option_ignores_legacy_stored_pct_without_pnl() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        pnl_pct=17755.61,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 700.0}},
    )

    assert paper_trade_return_pct(trade) is None


def test_paper_trade_return_pct_option_prefers_confirmed_premium_prices() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        pnl_pct=999.0,
        direction="long",
        signal_json={
            "asset_type": "options",
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )

    assert paper_trade_return_pct(trade) == pytest.approx(16.0)


def test_paper_trade_return_pct_stock_keeps_legacy_stored_pct() -> None:
    trade = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=3.5,
        direction="long",
        signal_json={"asset_type": "stock"},
    )

    assert paper_trade_return_pct(trade) == pytest.approx(3.5)
