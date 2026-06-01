import pytest

from app.services.trading.perps.features import funding_annualized
from app.services.trading.perps.strategies import funding_carry


def test_funding_carry_uses_decimal_apy_contract() -> None:
    assert funding_annualized(0.0001) == pytest.approx(0.1095)

    assert funding_carry(
        symbol="BTC-PERP",
        venue="binance",
        perp_price=100_000.0,
        funding_apy_decimal=0.1095,
        basis_z_score_value=2.0,
    ) is None

    proposal = funding_carry(
        symbol="BTC-PERP",
        venue="binance",
        perp_price=100_000.0,
        funding_apy_decimal=0.20,
        basis_z_score_value=2.0,
    )

    assert proposal is not None
    assert proposal.meta["funding_unit"] == "decimal_apy"
    assert proposal.meta["funding_apy_decimal"] == pytest.approx(0.20)
    assert proposal.meta["funding_annualized_pct"] == pytest.approx(20.0)
    assert proposal.meta["entry_threshold_apy_decimal"] == pytest.approx(0.15)
    assert "Funding APY 20.0% > 15.0%" in proposal.rationale


def test_funding_carry_accepts_legacy_percent_keywords() -> None:
    proposal = funding_carry(
        symbol="ETH-PERP",
        venue="binance",
        perp_price=4_000.0,
        funding_annualized_pct=20.0,
        entry_threshold_apy=15.0,
        basis_z_score_value=2.0,
    )

    assert proposal is not None
    assert proposal.meta["funding_apy_decimal"] == pytest.approx(0.20)
    assert proposal.meta["entry_threshold_apy_decimal"] == pytest.approx(0.15)
    assert proposal.meta["funding_source"] == "funding_annualized_pct_legacy"
    assert proposal.meta["threshold_source"] == "entry_threshold_apy_legacy"


def test_funding_carry_rejects_malformed_apy() -> None:
    assert funding_carry(
        symbol="SOL-PERP",
        venue="binance",
        perp_price=150.0,
        funding_apy_decimal=float("nan"),
        basis_z_score_value=2.0,
    ) is None
