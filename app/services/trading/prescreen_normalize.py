"""Canonical ticker form for prescreen DB rows (align with app crypto conventions)."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from ..massive_client import is_crypto
from ..symbol_hygiene import normalize_equity_symbol, strip_ticker_decoration

_BARE_CRYPTO_MIN_BASE_LEN = 2
_BARE_CRYPTO_MAX_BASE_LEN = 15
_VALID_TICKER_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)?$")
_NON_TICKER_SENTINELS = frozenset(
    {
        "ALL",
        "CRYPTO",
        "CRYPTOCURRENCY",
        "EQUITIES",
        "EQUITY",
        "STOCK",
        "STOCKS",
    }
)
_JSON_COLLECTION_PREFIXES = ("[", "{")


def normalize_prescreen_ticker(raw: Any) -> str:
    """Uppercase/strip; bare crypto ``BTCUSD`` -> ``BTC-USD``.

    Invalid JSON/list fragments are rejected instead of being persisted as
    fake stock tickers, which wastes scanner budget and creates yfinance
    failures like ``"INFQ"]``.
    """
    t = strip_ticker_decoration(raw)
    if not t:
        return ""
    if (
        t in _NON_TICKER_SENTINELS
        or any(ch in t for ch in ("[", "]", '"', "'", " "))
        or not _VALID_TICKER_RE.match(t)
    ):
        return ""
    if is_crypto(t) and "-" not in t and t.endswith("USD") and len(t) > 3:
        base = t[:-3]
        if (
            base.isalnum()
            and _BARE_CRYPTO_MIN_BASE_LEN <= len(base) <= _BARE_CRYPTO_MAX_BASE_LEN
        ):
            return f"{base}-USD"
    if not is_crypto(t):
        return normalize_equity_symbol(t)
    return t


def iter_normalized_prescreen_tickers(raw: Any) -> list[str]:
    """Parse CSV, JSON arrays, or iterable ticker inputs into clean symbols."""
    values: Iterable[Any]
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        parsed: Any | None = None
        if stripped[:1] in _JSON_COLLECTION_PREFIXES:
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                parsed = None
        if isinstance(parsed, dict):
            values = parsed.values()
        elif isinstance(parsed, (list, tuple, set)):
            values = parsed
        else:
            values = stripped.replace("\n", ",").split(",")
    elif isinstance(raw, dict):
        values = raw.values()
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = (raw,)

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        tn = normalize_prescreen_ticker(value)
        if not tn or tn in seen:
            continue
        seen.add(tn)
        out.append(tn)
    return out
