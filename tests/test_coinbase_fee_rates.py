from __future__ import annotations


def test_get_fee_rates_prefers_current_tier_without_promotion(monkeypatch):
    from app.services import coinbase_service

    monkeypatch.setattr(
        coinbase_service,
        "get_transaction_summary_raw",
        lambda **_kw: {
            "fee_tier": {
                "pricing_tier": "Intro 1",
                "maker_fee_rate": "0.006",
                "taker_fee_rate": "0.010",
            },
            "fee_tier_without_promotion": {
                "current_tier": {
                    "pricing_tier": "Intro 2",
                    "maker_fee_rate": "0.004",
                    "taker_fee_rate": "0.008",
                },
            },
        },
    )

    fees = coinbase_service.get_fee_rates_bps()

    assert fees["pricing_tier"] == "Intro 2"
    assert fees["maker_fee_bps"] == 40.0
    assert fees["taker_fee_bps"] == 80.0


def test_get_fee_rates_falls_back_to_fee_tier(monkeypatch):
    from app.services import coinbase_service

    monkeypatch.setattr(
        coinbase_service,
        "get_transaction_summary_raw",
        lambda **_kw: {
            "fee_tier": {
                "pricing_tier": "Intro 1",
                "maker_fee_rate": "0.006",
                "taker_fee_rate": "0.010",
            },
        },
    )

    fees = coinbase_service.get_fee_rates_bps()

    assert fees["pricing_tier"] == "Intro 1"
    assert fees["maker_fee_bps"] == 60.0
    assert fees["taker_fee_bps"] == 100.0
