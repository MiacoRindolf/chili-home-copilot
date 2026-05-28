from __future__ import annotations

from app.services.trading.options.contracts import (
    complete_greeks,
    missing_greeks,
    normalize_expiration,
    normalize_option_meta,
    occ_symbol,
    option_contract_key,
    option_price_domains_snapshot,
    parse_contract_quantity,
    validate_single_leg_option_meta,
)


def test_normalize_option_meta_adds_contract_identity_and_price_domains() -> None:
    meta = normalize_option_meta(
        {
            "strike": "729",
            "expiration": "2026-06-19",
            "option_type": "C",
            "limit_price": "4.01",
            "quantity": "1",
        },
        underlying="spy",
        current_underlying_price=715.37,
        quote={
            "bid_price": "3.95",
            "ask_price": "4.05",
            "greeks": {"delta": "0.42", "gamma": "0.03", "theta": "-0.08", "vega": "0.11"},
        },
    )

    assert meta["underlying"] == "SPY"
    assert meta["option_type"] == "call"
    assert meta["contract_key"] == "SPY:2026-06-19:call:729.000"
    assert meta["occ_symbol"] == "SPY260619C00729000"
    assert meta["price_domain"] == "option_premium"
    assert meta["underlying_price_domain"] == "underlying_spot"
    assert meta["underlying_price_at_entry"] == 715.37
    assert meta["quote_snapshot"]["mid"] == 4.0
    assert meta["delta"] == 0.42
    assert option_price_domains_snapshot()["stop_loss"] == "underlying_spot"


def test_option_contract_key_and_occ_symbol_are_stable() -> None:
    assert (
        option_contract_key(
            underlying="Spy",
            expiration="20260619",
            strike="729",
            option_type="put",
        )
        == "SPY:2026-06-19:put:729.000"
    )
    assert (
        occ_symbol(
            underlying="Spy",
            expiration="20260619",
            strike="729",
            option_type="put",
        )
        == "SPY260619P00729000"
    )


def test_single_leg_validation_requires_tradeable_contract_fields() -> None:
    missing = validate_single_leg_option_meta(
        {
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        }
    )

    assert "limit_price" in missing
    assert "quantity" in missing


def test_invalid_expiration_is_not_silently_accepted() -> None:
    assert normalize_expiration("2026-06-19junk") is None
    assert normalize_expiration("2026-06-19T16:00:00Z") == "2026-06-19"

    missing = validate_single_leg_option_meta(
        {
            "underlying": "SPY",
            "expiration": "2026-06-19junk",
            "strike": 729.0,
            "option_type": "call",
            "limit_price": 4.01,
            "quantity": 1,
        }
    )

    assert "expiration" in missing
    assert "contract_key" in missing


def test_contract_quantity_must_be_positive_integer_contracts() -> None:
    assert parse_contract_quantity("2") == 2
    assert parse_contract_quantity(2.0) == 2
    assert parse_contract_quantity("1.5") is None
    assert parse_contract_quantity(0) is None
    assert parse_contract_quantity(None) is None

    missing = validate_single_leg_option_meta(
        {
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
            "limit_price": 4.01,
            "quantity": "1.5",
        }
    )

    assert "quantity" in missing


def test_quote_snapshot_merge_tolerates_malformed_existing_snapshot() -> None:
    meta = normalize_option_meta(
        {
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
            "limit_price": 4.01,
            "quantity": 1,
            "quote_snapshot": "bad-old-shape",
        },
        quote={"bid_price": "3.95", "ask_price": "4.05"},
    )

    assert meta["quote_snapshot"]["bid"] == 3.95
    assert meta["quote_snapshot"]["ask"] == 4.05
    assert meta["quote_snapshot"]["mid"] == 4.0


def test_complete_greeks_require_all_finite_values() -> None:
    complete = {
        "delta": "0.42",
        "gamma": "0.03",
        "theta": "-0.08",
        "vega": "0.11",
    }
    via_snapshot = {
        "quote_snapshot": {
            "delta": 0.42,
            "gamma": 0.03,
            "theta": -0.08,
            "vega": 0.11,
        }
    }
    missing = {"delta": 0.42, "gamma": 0.03, "theta": -0.08}

    assert complete_greeks(complete) is True
    assert complete_greeks(via_snapshot) is True
    assert complete_greeks(missing) is False
    assert missing_greeks(missing) == ["vega"]
