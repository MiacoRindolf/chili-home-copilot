"""Theme / sympathy detector for the Ross momentum lane (E7).

Ross's biggest (1000%-class) moves come in SYMPATHY clusters: when a LEADER squeezes
on a catalyst, OTHER names sharing the same theme run too — STI dragging ASTC, a
"SpaceX synergies" headline lifting every space name, an FDA-day biotech dragging its
sector peers. CHILI already clusters by SIC SECTOR (catalyst.sympathy_peer_symbols);
this module adds the COMPLEMENTARY axis Ross actually reads off the tape: a shared
CATALYST KEYWORD across the batch's fresh news headlines (a "theme") combined with
genuine co-movement among the top movers.

Design (additive, soft, fail-open):
  * Cluster the batch's IN-PLAY movers by a salient keyword shared across their fresh
    news headlines (``(ticker, headline)`` pairs from the scanner's news feed).
  * A cluster is a real THEME only if it has a genuine LEADER (top % gainer clears a
    documented floor) AND at least ``min_cluster`` movers share the keyword (genuine
    co-movement, not one name + noise).
  * The non-leader members are SYMPATHY peers — they get a SMALL additive viability
    boost. The leader is already ranked on its own move, so it is NOT a peer.
  * Pure + side-effect-free. Crypto (-USD) has no equity news theme -> never a peer.

This NEVER penalises and NEVER gates: a sympathy peer is a confirming tilt, not a
requirement. Absent news / thin data -> empty set -> a no-op (byte-identical to the
flag-off / input-absent path). The wiring (viability.py) reads the flag; this module
stays pure so it is trivially testable.

ONE documented base each: the leader floor, the min cluster size, and the boost weight
(a small fraction of the catalyst tilt — a secondary, corroborating signal). All three
override via settings so nothing is a buried magic number.
docs/STRATEGY/CC_REPORTS/2026-06-15_ross-playlist-gap-roadmap.md
"""
from __future__ import annotations

import logging
import re

from ....config import settings

logger = logging.getLogger(__name__)

# ── Documented bases (each overridable via settings; no buried magic numbers) ──
# A theme needs a genuine LEADER: the top % gainer in the keyword cluster must clear
# this floor, else it is not a "squeeze that drags peers" — just co-incident noise.
THEME_LEADER_FLOOR_PCT = 15.0
# A theme needs genuine CO-MOVEMENT: at least this many movers must share the keyword
# (the leader + >=1 peer). Below it, one hot name + a keyword match is not a cluster.
THEME_MIN_CLUSTER = 2
# The sympathy boost weight. A theme peer is a SECONDARY corroborating signal (the
# leader's move is primary), so the boost is a small fraction of the catalyst tilt.
# Kept deliberately small so the detector can never out-vote the real pillars.
THEME_SYMPATHY_BOOST = 0.05

# Stop-words stripped from headlines before keyword extraction. Generic finance/news
# noise that would otherwise mis-cluster unrelated names ("Inc reports stock today").
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "with", "from", "that", "this", "have",
        "has", "had", "will", "its", "into", "after", "over", "amid", "says", "said",
        "inc", "corp", "ltd", "plc", "co", "group", "holdings", "company", "companies",
        "stock", "stocks", "share", "shares", "shareholder", "shareholders", "today",
        "news", "report", "reports", "reported", "announces", "announced", "announce",
        "update", "updates", "new", "now", "more", "than", "per", "vs", "amid",
        "market", "markets", "nasdaq", "nyse", "trading", "trade", "investor",
        "investors", "price", "target", "rating", "analyst", "analysts", "buy", "sell",
        "earnings", "quarter", "quarterly", "year", "results", "guidance",
    }
)

# Salient-keyword extraction: alpha tokens of >=4 chars (drops tickers, numbers, noise).
_TOKEN_RE = re.compile(r"[a-z]{4,}")


def _norm(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    return s.split("-", 1)[0] if "-" in s else s


def _theme_leader_floor_pct() -> float:
    try:
        v = float(
            getattr(settings, "chili_momentum_theme_leader_floor_pct", THEME_LEADER_FLOOR_PCT)
        )
    except (TypeError, ValueError):
        return THEME_LEADER_FLOOR_PCT
    return max(0.0, v)


def _theme_min_cluster() -> int:
    try:
        v = int(getattr(settings, "chili_momentum_theme_min_cluster", THEME_MIN_CLUSTER))
    except (TypeError, ValueError):
        return THEME_MIN_CLUSTER
    return max(2, v)


def _theme_sympathy_boost() -> float:
    """The additive sympathy boost magnitude. Capped small (<=0.20) so a theme tilt is
    always a secondary corroborator, never able to override the real selection pillars."""
    try:
        v = float(getattr(settings, "chili_momentum_theme_sympathy_boost", THEME_SYMPATHY_BOOST))
    except (TypeError, ValueError):
        return THEME_SYMPATHY_BOOST
    return max(0.0, min(v, 0.20))


def _salient_keywords(title: str) -> set[str]:
    """Salient catalyst keywords in a headline title: alpha tokens >=4 chars, minus
    generic finance/news stop-words. Pure; lower-cased."""
    if not title:
        return set()
    return {t for t in _TOKEN_RE.findall(str(title).lower()) if t not in _STOPWORDS}


def theme_sympathy_symbols(
    movers: dict[str, float],
    news_items: list[tuple[str, str]] | None,
    *,
    leader_floor_pct: float | None = None,
    min_cluster: int | None = None,
) -> set[str]:
    """Normalized tickers that are KEYWORD-THEME SYMPATHY PEERS of a hot cluster.

    A theme = a salient catalyst KEYWORD shared across the fresh headlines of >=
    ``min_cluster`` in-play movers, whose strongest member (the LEADER) clears
    ``leader_floor_pct``. The leader is excluded (already ranked on its own move); the
    rest are sympathy peers and get the soft viability boost.

    ``movers``     = ``{ticker: change_pct}`` for the batch's in-play movers.
    ``news_items`` = ``[(ticker, headline_title), ...]`` fresh-news pairs (the scanner's
                     ``get_recent_news_items`` feed). A ticker with no fresh headline
                     simply can't join a theme cluster (no penalty).

    Pure + side-effect-free; fail-open (empty/thin input -> ``set()``). Crypto pairs
    (``-USD``) are dropped (no equity news theme).
    """
    if not movers or not news_items:
        return set()

    floor = _theme_leader_floor_pct() if leader_floor_pct is None else float(leader_floor_pct)
    min_n = _theme_min_cluster() if min_cluster is None else max(2, int(min_cluster))

    # change% per equity mover (bare ticker; crypto excluded).
    chg_by_sym: dict[str, float] = {}
    for sym, chg in movers.items():
        su = str(sym or "").upper()
        if su.endswith("-USD"):
            continue
        try:
            chg_by_sym[_norm(su)] = float(chg)
        except (TypeError, ValueError):
            continue
    if not chg_by_sym:
        return set()

    # keyword -> {tickers in play that share it in a fresh headline}.
    by_keyword: dict[str, set[str]] = {}
    for ticker, title in news_items:
        sym = _norm(ticker)
        if sym not in chg_by_sym:
            continue  # only cluster names that are actually moving (genuine co-movement)
        for kw in _salient_keywords(title):
            by_keyword.setdefault(kw, set()).add(sym)

    peers: set[str] = set()
    for members in by_keyword.values():
        if len(members) < min_n:
            continue
        ranked = sorted(members, key=lambda s: chg_by_sym.get(s, 0.0), reverse=True)
        if chg_by_sym.get(ranked[0], 0.0) < floor:
            continue  # no genuine leader -> not a real theme squeeze
        for sym in ranked[1:]:
            peers.add(sym)
    return peers


def theme_sympathy_viability_delta(symbol: str, theme_symbols: set[str] | None) -> float:
    """Additive viability boost for a KEYWORD-THEME sympathy peer (E7). Small, additive,
    never a penalty. Crypto (-USD) / absent set -> 0.0 (byte-identical no-op)."""
    if not theme_symbols or "-USD" in str(symbol or "").upper():
        return 0.0
    return _theme_sympathy_boost() if _norm(symbol) in theme_symbols else 0.0
