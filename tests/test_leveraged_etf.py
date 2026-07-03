"""Structural leveraged/inverse-ETF detector (2026-06-22 selection down-weight, choice A)."""
from __future__ import annotations

from app.services.trading.momentum_neural.leveraged_etf import (
    is_excluded_fund_name,
    is_leveraged_etf_name,
    symbol_is_excluded_fund,
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


# ── A8: REIT / closed-end-fund NAME token (Ross CLRO-lesson 2026-07-02, WHLR pass) ──


def test_flags_reit_and_fund_structures_by_name():
    # Ross's exact pass at [01:55]: "Wheeler ... Real Estate Investment Trust ... Not interested"
    assert is_excluded_fund_name("Wheeler Real Estate Investment Trust")
    assert is_excluded_fund_name("Some Bancorp REIT")                  # word-bounded " REIT" token
    assert is_excluded_fund_name("Acme Reit Inc.")                     # trailing token, case-insensitive
    assert is_excluded_fund_name("BlackRock Closed-End Fund")          # closed-end fund
    assert is_excluded_fund_name("Nuveen Closed End Fund Trust")       # unhyphenated variant


def test_does_not_flag_operating_companies_or_reit_substrings():
    # Realty Income is a REIT-structured operating company by charter, but its NAME carries
    # NO fund/trust token — the filter keys on the stated STRUCTURE token, not the sector.
    assert not is_excluded_fund_name("Realty Income Corp")
    assert not is_excluded_fund_name("Apple Inc.")
    assert not is_excluded_fund_name("Carvana Co.")
    assert not is_excluded_fund_name("Reitmans (Canada) Limited")      # "Reit" is a substring, NOT bounded
    assert not is_excluded_fund_name("Streit Industries")             # "reit" mid-word, NOT bounded
    assert not is_excluded_fund_name(None)
    assert not is_excluded_fund_name("")


def test_fund_symbol_resolver_excludes_crypto_and_empties():
    # crypto is never an equity fund; empties fail-open to False (no fundamentals fetch)
    assert symbol_is_excluded_fund("BTC-USD") is False
    assert symbol_is_excluded_fund("ETH-USD") is False
    assert symbol_is_excluded_fund("") is False
    assert symbol_is_excluded_fund(None) is False
