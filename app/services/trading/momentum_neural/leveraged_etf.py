"""Structural leveraged/inverse-ETF detector for the momentum SELECTION down-weight.

Leveraged/inverse ETFs (DRN, KMRK, SOXL, SQQQ, ...) keep topping the Ross viability ranking on
raw RVOL/gap, but they are GEARED INDEX products — not the low-float company squeezes the lane
trades — and they decay intraday + already cost the lane (KMRK -$58 on 2026-06-22). This detects
them ADAPTIVELY from the issuer's structural NAMING convention (a leveraged/inverse ETF's name
states its multiple and/or an explicit leverage word, by SEC/issuer convention), NOT a hardcoded
ticker list — so it generalises to any new "Daily ... 3X" / "UltraPro" / "Inverse" product. The
detected names are DOWN-WEIGHTED (not banned) in the arm queue: a real mover outranks them, but
they still arm if nothing better is up. [operator 2026-06-22 choice A] [[feedback_adaptive_no_magic]]
"""
from __future__ import annotations

import logging
import re
import time as _time
from typing import Optional

logger = logging.getLogger(__name__)

# A leveraged/inverse ETF's NAME states its gearing. The near-zero-false-positive marker is a
# stated MULTIPLE ("2X" / "3X" / "-1X" / "1.5X") bounded as a standalone token — plain companies
# almost never carry "3X" in their name ("3M"/"2U" have a letter, not X, after the digit). Backed
# up by explicit issuer leverage/inverse qualifiers. CRITICAL (verified live 2026-06-22): the
# fundamentals ``short_name`` is TRUNCATED to ~31 chars, which CUTS OFF the trailing multiple of
# Direxion's geared series ("Direxion Daily Real Estate Bull" — the "3X Shares" is gone). So the
# multiple alone misses them; the issuer-series PHRASES that survive truncation ("Direxion Daily"
# = the geared series by construction; "ProShares Ultra*") catch them. We deliberately do NOT flag
# plain index ETFs ("SPDR S&P 500 ETF Trust", "Invesco QQQ Trust").
_MULT = re.compile(r"(?<![A-Za-z0-9])-?\d(?:\.\d+)?[xX](?![A-Za-z0-9])")
_LEV_WORDS = (
    "leveraged", "inverse", "ultrapro", "ultrashort", "ultra short", "ultra pro",
    "proshares ultra", "direxion daily", "-1x", "1.5x",
)


def is_leveraged_etf_name(name: Optional[str]) -> bool:
    """True if the instrument NAME is a leveraged/inverse ETF by its structural naming.
    Pure + side-effect-free. Conservative: a stated multiple OR an explicit leverage word."""
    if not name or not isinstance(name, str):
        return False
    if _MULT.search(name):
        return True
    low = name.lower()
    return any(t in low for t in _LEV_WORDS)


# A8 (Ross CLRO-lesson 2026-07-02) — REIT / closed-end-fund NAME tokens. A REIT or a
# closed-end fund is a yield/portfolio vehicle, NOT the low-float operating-company
# squeeze the Ross lane trades (Ross passed "Wheeler Real Estate Investment Trust" at a
# glance; CHILI wasted a watch slot arming WHLR). " reit" is word-BOUNDED so it never
# matches inside an ordinary word. We deliberately do NOT flag plain equities that merely
# hold real estate ("Realty Income Corp" is a REIT-structured operating company by charter
# but its NAME carries no fund/trust token, so it is NOT demoted — the filter keys on the
# stated STRUCTURE token, not the sector).
_FUND_WORDS = (
    "real estate investment trust",
    "closed-end fund",
    "closed end fund",
)
# " reit" bounded as a standalone token (leading space + word boundary) — matches
# "... REIT" / "... Reit Inc" but never a substring of another word.
_REIT_TOKEN = re.compile(r"(?<![A-Za-z0-9])reit(?![A-Za-z0-9])", re.IGNORECASE)


def is_excluded_fund_name(name: Optional[str]) -> bool:
    """True if the instrument NAME is a REIT / closed-end fund by its structural naming
    token. Pure + side-effect-free. Conservative token match: an explicit fund/trust
    phrase OR the word-bounded ``REIT`` token. Fail-open (False) on a missing/empty name
    so a real mover with an unresolved name is never demoted."""
    if not name or not isinstance(name, str):
        return False
    low = name.lower()
    if any(t in low for t in _FUND_WORDS):
        return True
    return bool(_REIT_TOKEN.search(name))


# Symbol->bool cache. Instrument type is static, so a long TTL + hard size cap (CLAUDE.md
# requires both). On overflow the whole cache clears (cheap; repopulates from the 24h
# fundamentals cache).
_CACHE: dict[str, tuple[bool, float]] = {}
_CACHE_TTL_SEC = 86_400.0
_CACHE_MAX = 4096
_FUND_CACHE: dict[str, tuple[bool, float]] = {}


def symbol_is_leveraged_etf(symbol: Optional[str]) -> bool:
    """Resolve a symbol's instrument NAME (via the 24h-cached get_fundamentals) and classify it.
    Equity-only (crypto ``-USD`` is never an equity ETF). Fail-OPEN (False) on any miss so a real
    mover is never wrongly demoted. Non-blocking in practice: get_fundamentals is 24h-cached and
    rate-limit-breaker-guarded (returns None under pressure -> classifies False)."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if not s or s.endswith("-USD"):
        return False
    now = _time.monotonic()
    hit = _CACHE.get(s)
    if hit is not None and (now - hit[1]) < _CACHE_TTL_SEC:
        return hit[0]
    res = False
    try:
        from ...yf_session import get_fundamentals

        fund = get_fundamentals(s) or {}
        res = is_leveraged_etf_name(fund.get("short_name"))
    except Exception:
        res = False
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[s] = (res, now)
    return res


def symbol_is_excluded_fund(symbol: Optional[str]) -> bool:
    """A8: resolve a symbol's instrument NAME (via the 24h-cached get_fundamentals) and
    classify it as a REIT / closed-end fund. Equity-only (crypto ``-USD`` never a fund).
    Fail-OPEN (False) on any miss so a real mover is never wrongly demoted. Non-blocking:
    get_fundamentals is 24h-cached + rate-limit-breaker-guarded (returns None under
    pressure -> classifies False). Its own cache (separate from the leveraged-ETF cache)."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if not s or s.endswith("-USD"):
        return False
    now = _time.monotonic()
    hit = _FUND_CACHE.get(s)
    if hit is not None and (now - hit[1]) < _CACHE_TTL_SEC:
        return hit[0]
    res = False
    try:
        from ...yf_session import get_fundamentals

        fund = get_fundamentals(s) or {}
        res = is_excluded_fund_name(fund.get("short_name"))
    except Exception:
        res = False
    if len(_FUND_CACHE) >= _CACHE_MAX:
        _FUND_CACHE.clear()
    _FUND_CACHE[s] = (res, now)
    return res
