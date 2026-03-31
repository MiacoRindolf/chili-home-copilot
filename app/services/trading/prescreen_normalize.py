"""Canonical ticker form for prescreen DB rows (align with app crypto conventions)."""
from __future__ import annotations

from ..massive_client import is_crypto


def normalize_prescreen_ticker(raw: str) -> str:
    """Uppercase/strip; bare crypto ``BTCUSD`` -> ``BTC-USD``."""
    t = raw.upper().strip()
    if not t:
        return ""
    if is_crypto(t) and "-" not in t and t.endswith("USD") and len(t) > 3:
        base = t[:-3]
        if base.isalnum() and 2 <= len(base) <= 15:
            return f"{base}-USD"
    return t
