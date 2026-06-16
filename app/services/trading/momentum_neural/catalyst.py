"""News-catalyst pillar for the Ross momentum lane (E5).

Ross's selection edge is RVOL + gap + low-float + a NEWS CATALYST. The first three
pillars are scored by ross_momentum; this module adds the catalyst: a mover with a
known news event (earnings) is far more likely to be a real Ross gapper than a
random spike. Applied as an ADDITIVE viability tilt (boost catalyst names, never
penalise — crypto + many stocks have no earnings). docs/DESIGN/MOMENTUM_LANE.md

Earnings is the most accessible catalyst (Benzinga via Massive); FDA/PR/contract
feeds are a later extension. Best-effort + cached: returns an empty set (no boost)
when the data plan lacks Benzinga, so the lane degrades gracefully.
"""
from __future__ import annotations

import logging

from ....config import settings

logger = logging.getLogger(__name__)

# ONE documented knob: how strongly a news catalyst tilts viability (vs Ross's 0.20
# selection tilt). News is one confirming signal, so a smaller boost.
CATALYST_VIABILITY_TILT = 0.10

# Freshness window for a NEWS headline to count as a live catalyst (minutes). Ross's
# sympathy/theme ignition is a recent headline; a stale story is not a catalyst.
NEWS_CATALYST_MAX_AGE_MIN = 120


def _catalyst_tilt() -> float:
    try:
        v = float(getattr(settings, "chili_momentum_catalyst_viability_tilt", CATALYST_VIABILITY_TILT))
    except (TypeError, ValueError):
        return CATALYST_VIABILITY_TILT
    return max(0.0, min(v, 0.5))


def _norm(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    # equities are bare tickers; crypto pairs carry -USD (never have earnings).
    return s.split("-", 1)[0] if "-" in s else s


def _news_catalyst_max_age_min() -> int:
    """How fresh a news headline must be to count as a live catalyst (minutes).

    Ross's sympathy/theme ignition is a RECENT headline; a day-old story is not a
    catalyst. One documented knob; default 120 min."""
    try:
        v = int(getattr(settings, "chili_momentum_news_catalyst_max_age_min", NEWS_CATALYST_MAX_AGE_MIN))
    except (TypeError, ValueError):
        return NEWS_CATALYST_MAX_AGE_MIN
    return max(15, min(v, 720))


def earnings_catalyst_symbols() -> set[str]:
    """Tickers with a near-term EARNINGS catalyst (best-effort, cached upstream).

    Returns an empty set when Benzinga is unavailable so the catalyst tilt is a
    no-op rather than an error.
    """
    try:
        from ...massive_client import get_benzinga_earnings

        rows = get_benzinga_earnings(limit=200) or []
        return {_norm(t) for t in rows if t}
    except Exception:
        logger.debug("[catalyst] earnings fetch failed; no catalyst boost this pass", exc_info=True)
        return set()


def news_catalyst_symbols() -> set[str]:
    """Tickers with a FRESH general NEWS catalyst (headline within the freshness window).

    Ross's biggest sympathy/theme plays — the +1000-3500% movers, e.g. a low-float
    small-cap that 10x'd on a 'SpaceX synergies' headline (vid 4tOf-A3MaOE) — are
    ignited by a fresh news HEADLINE, not just scheduled earnings. This pulls the
    recent-news tickers so the catalyst tilt prefers explosive movers that ALSO just
    printed news. Best-effort + cached; empty set (no boost) when news is unavailable.
    docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        from ...massive_client import get_recent_news_tickers

        rows = get_recent_news_tickers(limit=200, max_age_min=_news_catalyst_max_age_min()) or []
        return {_norm(t) for t in rows if t}
    except Exception:
        logger.debug("[catalyst] news fetch failed; no news-catalyst boost this pass", exc_info=True)
        return set()


def all_catalyst_symbols() -> set[str]:
    """Union of EARNINGS + fresh-NEWS catalyst tickers — the full catalyst set the
    viability tilt boosts. Each source is independently fail-open, so the loss of one
    feed never zeroes the other. (Ross: RVOL + gap + low-float + a NEWS CATALYST.)"""
    return earnings_catalyst_symbols() | news_catalyst_symbols()


def catalyst_score(symbol: str, catalyst_symbols: set[str] | None) -> float:
    """[0,1] catalyst score: 1.0 when the symbol has a known catalyst, else 0.5
    (neutral — no penalty). Crypto (-USD) is always neutral (no earnings)."""
    if not catalyst_symbols:
        return 0.5
    if "-USD" in str(symbol or "").upper():
        return 0.5
    return 1.0 if _norm(symbol) in catalyst_symbols else 0.5


def theme_catalyst_symbols() -> set[str]:
    """Tickers in the ACTIVE EVENT THEME (keyword-driven, operator-set window).

    ``chili_momentum_event_theme_keywords`` (comma-separated; empty = no theme)
    names the day's dominant narrative — e.g. "space,satellite,rocket,orbit,
    launch,aerospace,spacex" for the SpaceX IPO window. Theme names keep their
    catalyst boost even in a HOT tape (the inversion neutralizes only GENERIC
    news — Ross expects the event-theme headlines to perform). Fail-open."""
    raw = str(getattr(settings, "chili_momentum_event_theme_keywords", "") or "")
    kws = [k.strip() for k in raw.split(",") if k.strip()]
    if not kws:
        return set()
    try:
        from ...massive_client import get_theme_news_tickers

        return {_norm(t) for t in get_theme_news_tickers(kws)}
    except Exception:
        logger.debug("[catalyst] theme news fetch failed", exc_info=True)
        return set()


# Catalyst-TYPE grading (Ross gap #12, videos 06/36): a cash-raise / compliance / legal
# headline is a WEAK (often bearish) "catalyst" Ross DISTRUSTS — a cash-poor low-float that
# just funded a deal will issue shares and fade (CTNT vs SNTI). It must NOT earn the same
# catalyst boost a trial / M&A / contract (STRONG) does. We only DE-BOOST the weak class
# (to 0); strong/medium keep the existing tilt (minimal change, fail-open). Keyword list,
# no magic constants.
_WEAK_CATALYST_KEYWORDS = (
    "offering", "registered direct", "at-the-market", "atm facility", "dilut",
    "reverse split", "reverse stock split", "going concern", "regain compliance",
    "compliance with", "notice of delisting", "delisting", "bankrupt", "chapter 11",
    "default", "restatement", "securities fraud", "class action", "investigation",
    "subpoena", "private placement", "warrant exercise", "shelf registration",
)


def _is_weak_catalyst(title: str) -> bool:
    """True when a headline is a WEAK / distrusted catalyst (dilution, compliance, legal).
    Pure; fail-open to False (an unreadable title is never down-graded)."""
    t = str(title or "").lower()
    return any(k in t for k in _WEAK_CATALYST_KEYWORDS)


def weak_catalyst_symbols() -> set[str]:
    """Normalized tickers whose freshest fresh-news headline is a WEAK catalyst (the
    de-boost set). Uses the title-carrying news fetch; fail-open to empty (no de-boost)
    when the news feed is unavailable, so a missing feed never strips the catalyst tilt."""
    try:
        from ...massive_client import get_recent_news_items

        return {
            _norm(tk)
            for tk, title in get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
            if _is_weak_catalyst(title)
        }
    except Exception:
        logger.debug("[catalyst] weak-catalyst grade fetch failed", exc_info=True)
        return set()


# A "big mover" = a LULD-scale day move. Ross's hot days (2026-06-09/10) print
# MULTIPLE +30%..+1000% names rotating ("hot potato"); a normal day has 0-1.
HOT_TAPE_BIG_MOVE_PCT = 30.0


def hot_tape_regime(ross_signals: dict | None) -> bool:
    """HOT-tape detector: several LULD-scale movers at once, derived from the
    scanner bridge's OWN signals (no extra fetch). The floor is the one
    documented knob (``chili_momentum_hot_tape_min_big_movers``, default 3)."""
    if not isinstance(ross_signals, dict) or not ross_signals:
        return False
    floor = int(getattr(settings, "chili_momentum_hot_tape_min_big_movers", 3) or 3)
    n = 0
    for sig in ross_signals.values():
        if not isinstance(sig, dict):
            continue
        try:
            chg = float(sig.get("daily_change_pct") or sig.get("gap_pct") or 0.0)
        except (TypeError, ValueError):
            continue
        if chg >= HOT_TAPE_BIG_MOVE_PCT:
            n += 1
            if n >= floor:
                return True
    return False


def catalyst_viability_delta(
    symbol: str,
    catalyst_symbols: set[str] | None,
    *,
    hot_tape: bool = False,
    hq_country: str | None = None,
    theme_symbols: set[str] | None = None,
    weak_symbols: set[str] | None = None,
) -> float:
    """The additive viability tilt for a symbol — REGIME-AWARE.

    NORMAL tape (default; unchanged behavior): +tilt/2 for a catalyst name, 0
    otherwise — a mover with a real catalyst is more likely a true Ross gapper.

    HOT tape (Ross 2026-06-10 recap, his biggest 2026 day): the leaders are
    NO-NEWS foreign small caps with room to speculate, while the US name WITH
    news rejected (KIDZ) — the read INVERTS: no-news gets the boost (full
    tilt/2 when the HQ country is non-US, half when US/unknown) and news names
    go NEUTRAL (never penalized — absence-of-news evidence is weaker than its
    presence). Same ±tilt/2 magnitude as ever — no new constants."""
    if "-USD" in str(symbol or "").upper():
        return 0.0
    half = _catalyst_tilt() * 0.5
    has_news = bool(catalyst_symbols) and _norm(symbol) in catalyst_symbols
    # Gap #12: a WEAK catalyst (dilution / compliance / legal) earns NO boost — Ross
    # distrusts a cash-raise/compliance headline (the name will issue shares and fade).
    # Only the weak class is stripped; strong/medium keep the tilt below.
    if has_news and weak_symbols and _norm(symbol) in weak_symbols:
        return 0.0
    if not hot_tape:
        return half if has_news else 0.0
    if has_news:
        # EVENT-THEME exemption: news matching the day's dominant narrative
        # (e.g. space headlines in the SpaceX IPO window) KEEPS the boost in a
        # hot tape — only generic news is neutralized.
        if theme_symbols and _norm(symbol) in theme_symbols:
            return half
        return 0.0
    foreign = bool(hq_country) and str(hq_country).strip().lower() not in (
        "united states", "united states of america", "usa", "us",
    )
    return half if foreign else half * 0.5
