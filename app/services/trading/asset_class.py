"""Canonical asset-class helpers for trading pattern routing."""
from __future__ import annotations

PATTERN_ASSET_CLASS_ALL = "all"
PATTERN_ASSET_CLASS_STOCKS = "stocks"
PATTERN_ASSET_CLASS_CRYPTO = "crypto"
PATTERN_ASSET_CLASS_OPTIONS = "options"

_ALL_ASSET_CLASS_ALIASES = frozenset({"", "all", "any", "universal"})
_STOCK_ASSET_CLASS_ALIASES = frozenset({"stock", "stocks", "equity", "equities"})
_CRYPTO_ASSET_CLASS_ALIASES = frozenset({"crypto", "cryptocurrency", "digital_asset"})
_OPTION_ASSET_CLASS_ALIASES = frozenset(
    {
        "option",
        "options",
        "option_contract",
        "option_contracts",
        "options_contract",
        "options_contracts",
        "contract_option",
        "contract_options",
        "equity_option",
        "equity_options",
        "stock_option",
        "stock_options",
        "option_spread",
        "options_spread",
        "option_spreads",
        "options_spreads",
        "optionspread",
        "optionspreads",
        "robinhood_option",
        "robinhood_options",
    }
)


def normalize_pattern_asset_class(value: object) -> str:
    """Return the canonical pattern asset class used by scanners."""
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in _STOCK_ASSET_CLASS_ALIASES:
        return PATTERN_ASSET_CLASS_STOCKS
    if raw in _CRYPTO_ASSET_CLASS_ALIASES:
        return PATTERN_ASSET_CLASS_CRYPTO
    if raw in _OPTION_ASSET_CLASS_ALIASES:
        return PATTERN_ASSET_CLASS_OPTIONS
    if raw in _ALL_ASSET_CLASS_ALIASES:
        return PATTERN_ASSET_CLASS_ALL
    return PATTERN_ASSET_CLASS_ALL


def pattern_asset_class_matches(pattern_asset_class: object, requested_asset_class: object) -> bool:
    """Return whether a pattern should be considered for a requested asset class."""
    pattern = normalize_pattern_asset_class(pattern_asset_class)
    requested = normalize_pattern_asset_class(requested_asset_class)
    if requested == PATTERN_ASSET_CLASS_ALL:
        return pattern == PATTERN_ASSET_CLASS_ALL
    return pattern in {PATTERN_ASSET_CLASS_ALL, requested}
