"""Corporate-action hygiene for stock symbols used by scanners."""
from __future__ import annotations

import re
from collections.abc import Iterable

# Verified 2026-05-26 from issuer / acquirer releases:
# - Block Class A common stock changed from SQ to XYZ on 2025-01-21.
# - Capital One completed its Discover acquisition; DFS is no longer a
#   standalone common-stock candidate.
# - Chevron completed its Hess acquisition; HES is no longer a standalone
#   common-stock candidate.
_EQUITY_SYMBOL_ALIASES: dict[str, str] = {
    "SQ": "XYZ",
}

_INACTIVE_EQUITY_SYMBOLS = frozenset({
    "DFS",
    "HES",
})

_COMPACT_PREFERRED_SYMBOL_RE = re.compile(r"^[A-Z]{3,5}P[A-Z]$")


def _is_unsupported_common_stock_symbol(symbol: str) -> bool:
    """Return True for non-common equity forms scanners should not trade."""
    return bool(_COMPACT_PREFERRED_SYMBOL_RE.fullmatch(symbol))


def normalize_equity_symbol(symbol: str) -> str:
    """Return an active common-stock symbol or ``""`` for known inactive names."""
    t = str(symbol or "").upper().strip()
    if not t:
        return ""
    t = _EQUITY_SYMBOL_ALIASES.get(t, t)
    if t in _INACTIVE_EQUITY_SYMBOLS or _is_unsupported_common_stock_symbol(t):
        return ""
    return t


def clean_equity_universe(symbols: Iterable[str]) -> list[str]:
    """Apply corporate-action aliases/tombstones while preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_equity_symbol(symbol)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
