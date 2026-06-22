"""Structural leveraged/inverse-ETF detector (2026-06-22 selection down-weight, choice A)."""
from __future__ import annotations

from app.services.trading.momentum_neural.leveraged_etf import (
    is_leveraged_etf_name,
    symbol_is_leveraged_etf,
)


def test_flags_leveraged_and_inverse_by_name():
    assert is_leveraged_etf_name("Direxion Daily MSCI Real Estate Bull 3X Shares")  # DRN
    assert is_leveraged_etf_name("Direxion Daily Semiconductor Bull 3X Shares")     # SOXL
    assert is_leveraged_etf_name("ProShares UltraPro QQQ")                          # 3x
    assert is_leveraged_etf_name("ProShares UltraShort S&P500")                     # -2x
    assert is_leveraged_etf_name("ProShares Ultra S&P500")                          # 2x (issuer phrase)
    assert is_leveraged_etf_name("Direxion Daily S&P 500 Bear 1X Shares")           # inverse 1x
    assert is_leveraged_etf_name("Defiance Daily Target 2X Long MSTR ETF")          # SMCX-class
    assert is_leveraged_etf_name("GraniteShares 1.5x Long TSLA Daily ETF")          # 1.5x
    # TRUNCATED short_name (verified live: ~31 chars, "3X Shares" cut off) — caught by the
    # surviving issuer-series phrase "Direxion Daily" (the geared series by construction).
    assert is_leveraged_etf_name("Direxion Daily Real Estate Bull")                 # DRN (truncated)
    assert is_leveraged_etf_name("Direxion Daily Semiconductor Bu")                 # SOXL (truncated)


def test_does_not_flag_real_companies_or_plain_etfs():
    assert not is_leveraged_etf_name("Apple Inc.")
    assert not is_leveraged_etf_name("Carvana Co.")
    assert not is_leveraged_etf_name("2U, Inc.")                  # "2U" — letter, not X
    assert not is_leveraged_etf_name("3M Company")                # "3M" — letter, not X
    assert not is_leveraged_etf_name("Box, Inc.")
    assert not is_leveraged_etf_name("SPDR S&P 500 ETF Trust")    # plain index ETF (no gearing)
    assert not is_leveraged_etf_name("Invesco QQQ Trust")         # plain index ETF
    assert not is_leveraged_etf_name("Ultra Clean Holdings, Inc.")  # "Ultra" company, not ProShares
    assert not is_leveraged_etf_name("K-Tech Solutions Company Limited")  # KMRK — a REAL small-cap, NOT leveraged
    assert not is_leveraged_etf_name(None)
    assert not is_leveraged_etf_name("")


def test_symbol_resolver_excludes_crypto_and_empties():
    # crypto is never an equity leveraged ETF; empties fail-open to False (no fundamentals fetch)
    assert symbol_is_leveraged_etf("BTC-USD") is False
    assert symbol_is_leveraged_etf("ETH-USD") is False
    assert symbol_is_leveraged_etf("") is False
    assert symbol_is_leveraged_etf(None) is False
