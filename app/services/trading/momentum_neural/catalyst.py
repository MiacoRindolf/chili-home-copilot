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
import re

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
#
# REVERSE-SPLIT and PRIVATE-PLACEMENT are kept in this list so the DEFAULT / flag-OFF /
# context-absent path stays byte-identical (the conservative weak prior). Their SIGN is
# refined ONLY in weak_catalyst_symbols(): a RECENT reverse split with fresh REAL news and a
# low post-split float is a Ross SS101 low-float squeeze (REMOVED from the de-boost +
# BOOSTED), and a private placement priced AT/ABOVE market is institutional confidence
# (REMOVED from the de-boost). Both refinements fail-open back to weak when their data
# (split date / PP price / float) is absent — see _REVERSE_SPLIT_KEYWORDS / _PP_KEYWORDS.
_WEAK_CATALYST_KEYWORDS = (
    "offering", "registered direct", "at-the-market", "atm facility", "dilut",
    "reverse split", "reverse stock split", "going concern", "regain compliance",
    "compliance with", "notice of delisting", "delisting", "bankrupt", "chapter 11",
    "default", "restatement", "securities fraud", "class action", "investigation",
    "subpoena", "private placement", "warrant exercise", "shelf registration",
)

# Reverse-split + private-placement headline markers — the SUBSET of the weak list whose SIGN
# is corp-action / price dependent (refined in weak_catalyst_symbols, not the pure classifier).
_REVERSE_SPLIT_KEYWORDS = ("reverse split", "reverse stock split")
_PRIVATE_PLACEMENT_KEYWORDS = ("private placement",)

# SS101 low-float-squeeze (Ross): the recency window a reverse split must fall inside to count
# as a FRESH low-float reset (one documented base; the Polygon/SEC split feed carries the
# execution date). 30 calendar days ≈ Ross's "<~1mo".
REVERSE_SPLIT_RECENCY_DAYS = 30

# A reverse split's post-split float only earns the squeeze re-rank when it sits in the LOW tail
# of the day's equity-mover floats — an ADAPTIVE within-batch percentile, not a fixed share
# count (Ross's 573k-float example is a FLOOR, not a magic ceiling). One documented base: the
# percentile cut (lowest third) of the batch's known floats.
REVERSE_SPLIT_LOW_FLOAT_PCTL = 0.34


def _has_reverse_split_kw(title: str) -> bool:
    """True when a headline is specifically a REVERSE-SPLIT corp action. Pure; fail-open False."""
    t = str(title or "").lower()
    return any(k in t for k in _REVERSE_SPLIT_KEYWORDS)


def _has_private_placement_kw(title: str) -> bool:
    """True when a headline is specifically a PRIVATE PLACEMENT. Pure; fail-open False."""
    t = str(title or "").lower()
    return any(k in t for k in _PRIVATE_PLACEMENT_KEYWORDS)


def _is_weak_catalyst(title: str) -> bool:
    """True when a headline is a WEAK / distrusted catalyst (dilution, compliance, legal).
    Pure; fail-open to False (an unreadable title is never down-graded)."""
    t = str(title or "").lower()
    return any(k in t for k in _WEAK_CATALYST_KEYWORDS)


def _reverse_split_recency_days() -> int:
    """SS101 reverse-split recency window (days). One documented knob; default 30."""
    try:
        v = int(getattr(settings, "chili_momentum_reverse_split_recency_days", REVERSE_SPLIT_RECENCY_DAYS))
    except (TypeError, ValueError):
        return REVERSE_SPLIT_RECENCY_DAYS
    return max(1, min(v, 120))


def _low_float_threshold(floats: dict[str, float] | None) -> float | None:
    """ADAPTIVE low-float cut: the REVERSE_SPLIT_LOW_FLOAT_PCTL percentile of the batch's known
    floats (no magic share count). Returns None when fewer than 2 floats are known (can't rank
    a batch of one -> fail-open, no squeeze re-rank). Pure."""
    vals = sorted(float(v) for v in (floats or {}).values() if isinstance(v, (int, float)) and v > 0)
    if len(vals) < 2:
        return None
    idx = max(0, min(len(vals) - 1, int(round(REVERSE_SPLIT_LOW_FLOAT_PCTL * (len(vals) - 1)))))
    return vals[idx]


def weak_catalyst_symbols(
    *,
    recent_split_symbols: set[str] | None = None,
    floats: dict[str, float] | None = None,
    strong_news_symbols: set[str] | None = None,
    private_placement_at_or_above_market: set[str] | None = None,
) -> set[str]:
    """Normalized tickers whose freshest fresh-news headline is a WEAK catalyst (the
    de-boost set). Uses the title-carrying news fetch; fail-open to empty (no de-boost)
    when the news feed is unavailable, so a missing feed never strips the catalyst tilt.

    SIGN REFINEMENTS (additive — any kwarg absent / its flag OFF leaves the result
    byte-identical to the bare keyword classification):

      * RECENT REVERSE SPLIT (Ross SS101 low-float squeeze): a reverse-split headline whose
        split executed within the recency window (``recent_split_symbols``) AND that carries
        FRESH REAL news (``strong_news_symbols`` — a non-dilution catalyst) AND a LOW post-split
        float (adaptive batch percentile of ``floats``) is a low-float SQUEEZE, not a fade —
        it is REMOVED from the de-boost set (and surfaced separately for a BOOST via
        ``recent_reverse_split_squeeze_symbols``). A BARE reverse split (no real news / not
        recent / float unknown) stays weak. Gated by ``chili_momentum_reverse_split_recency_enabled``.

      * PRIVATE PLACEMENT priced AT/ABOVE market (``private_placement_at_or_above_market``):
        institutional confidence, not dilution — REMOVED from the de-boost. A below-market /
        unknown-price PP stays weak (the dilutive fade). Gated by
        ``chili_momentum_private_placement_sign_enabled``.
    """
    try:
        from ...massive_client import get_recent_news_items

        items = get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
    except Exception:
        logger.debug("[catalyst] weak-catalyst grade fetch failed", exc_info=True)
        return set()

    rs_flag = bool(getattr(settings, "chili_momentum_reverse_split_recency_enabled", True))
    pp_flag = bool(getattr(settings, "chili_momentum_private_placement_sign_enabled", True))
    recent_split_symbols = recent_split_symbols or set()
    strong_news_symbols = strong_news_symbols or set()
    pp_bullish = private_placement_at_or_above_market or set()
    low_float_cut = _low_float_threshold(floats) if (rs_flag and floats) else None

    out: set[str] = set()
    for tk, title in items:
        if not _is_weak_catalyst(title):
            continue
        sym = _norm(tk)
        # (A) recent-reverse-split squeeze exception: only when ALL three confirmations hold,
        # and only if this headline's weakness comes SOLELY from the reverse-split marker (a
        # name that ALSO has a dilution/offering headline stays weak — dilution dominates).
        if (
            rs_flag
            and _has_reverse_split_kw(title)
            and not _other_weak_than(title, _REVERSE_SPLIT_KEYWORDS)
            and sym in recent_split_symbols
            and sym in strong_news_symbols
            and low_float_cut is not None
            and _float_at_or_below(sym, floats, low_float_cut)
        ):
            continue  # squeeze -> not weak (boost surfaced by recent_reverse_split_squeeze_symbols)
        # (B) private-placement-at/above-market exception: only when the weakness is SOLELY the
        # private-placement marker (a PP that is ALSO an at-the-market/dilution headline stays weak).
        if (
            pp_flag
            and _has_private_placement_kw(title)
            and not _other_weak_than(title, _PRIVATE_PLACEMENT_KEYWORDS)
            and sym in pp_bullish
        ):
            continue  # at/above-market PP = institutional confidence -> not weak
        out.add(sym)
    return out


def _other_weak_than(title: str, exempt: tuple[str, ...]) -> bool:
    """True when a headline matches a weak keyword OTHER than the ``exempt`` markers — i.e. the
    name is weak for an INDEPENDENT reason (dilution/compliance/legal) beyond the corp-action
    being sign-refined, so the exemption must NOT apply (dilution dominates). Pure."""
    t = str(title or "").lower()
    return any(k in t for k in _WEAK_CATALYST_KEYWORDS if k not in exempt)


def _float_at_or_below(sym: str, floats: dict[str, float] | None, cut: float) -> bool:
    """True when ``sym`` has a known float at/below the adaptive cut. Unknown float -> False
    (fail-open: an unknown float can't prove a LOW float, so no squeeze re-rank). Pure."""
    if not floats:
        return False
    v = floats.get(sym) or floats.get(str(sym or "").upper())
    try:
        return v is not None and float(v) > 0 and float(v) <= float(cut)
    except (TypeError, ValueError):
        return False


def recent_reverse_split_squeeze_symbols(
    *,
    recent_split_symbols: set[str] | None = None,
    floats: dict[str, float] | None = None,
    strong_news_symbols: set[str] | None = None,
) -> set[str]:
    """Normalized tickers that qualify as a Ross SS101 RECENT-REVERSE-SPLIT LOW-FLOAT SQUEEZE:
    a reverse-split headline whose split is recent (``recent_split_symbols``), that ALSO carries
    fresh REAL news (``strong_news_symbols``), with a LOW post-split float (adaptive batch
    percentile of ``floats``). These earn a BOOST (folded into the STRONG-catalyst set by the
    pipeline so the existing grade delta carries it — viability needs no change). Empty when the
    flag is OFF or any input is absent (byte-identical). Fail-open."""
    if not bool(getattr(settings, "chili_momentum_reverse_split_recency_enabled", True)):
        return set()
    recent_split_symbols = recent_split_symbols or set()
    strong_news_symbols = strong_news_symbols or set()
    low_float_cut = _low_float_threshold(floats) if floats else None
    if not recent_split_symbols or not strong_news_symbols or low_float_cut is None:
        return set()
    try:
        from ...massive_client import get_recent_news_items

        items = get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
    except Exception:
        logger.debug("[catalyst] reverse-split-squeeze fetch failed", exc_info=True)
        return set()
    out: set[str] = set()
    for tk, title in items:
        if not _has_reverse_split_kw(title):
            continue
        if _other_weak_than(title, _REVERSE_SPLIT_KEYWORDS):
            continue  # also diluting/compliance/legal -> not a clean squeeze
        sym = _norm(tk)
        if (
            sym in recent_split_symbols
            and sym in strong_news_symbols
            and _float_at_or_below(sym, floats, low_float_cut)
        ):
            out.add(sym)
    return out


# Private-placement SIGN (Ross): a PP priced AT/ABOVE the prevailing market is institutional
# CONFIDENCE (smart money paying up — bullish), the opposite of a below-market raise that
# dilutes and fades. The headline usually states the per-share price ("priced at $2.50 per
# share"); we compare it to the live last price. The price-vs-market read is what flips the
# sign — when EITHER the parsed PP price OR the live quote is absent, we FAIL OPEN to "still
# weak" (the conservative dilution prior), so a missing price never falsely un-penalizes.
_PP_PRICE_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:per\s+share|/\s*share|a\s+share)")


def _parse_pp_price(title: str) -> float | None:
    """Per-share price stated in a private-placement headline ("$2.50 per share"), else None.
    Pure; fail-open None."""
    m = _PP_PRICE_RE.search(str(title or "").lower())
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def private_placement_at_or_above_market_symbols(
    *, last_prices: dict[str, float] | None = None
) -> set[str]:
    """Normalized tickers whose fresh PRIVATE-PLACEMENT headline is priced AT/ABOVE the live
    market (institutional confidence — the bullish PP). ``last_prices`` = ``{ticker: last}``
    (e.g. from get_quotes_batch's ``last_price``). A PP whose headline price is >= the live
    last price is removed from the weak de-boost (via ``weak_catalyst_symbols``). Empty when
    the flag is OFF or ``last_prices`` is absent (byte-identical). Fail-open: a PP with no
    parsable price or no live quote is NOT included (stays weak — the dilution prior)."""
    if not bool(getattr(settings, "chili_momentum_private_placement_sign_enabled", True)):
        return set()
    if not last_prices:
        return set()
    try:
        from ...massive_client import get_recent_news_items

        items = get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
    except Exception:
        logger.debug("[catalyst] private-placement sign fetch failed", exc_info=True)
        return set()
    out: set[str] = set()
    for tk, title in items:
        if not _has_private_placement_kw(title):
            continue
        if _other_weak_than(title, _PRIVATE_PLACEMENT_KEYWORDS):
            continue  # also an ATM/offering/dilution headline -> stays weak
        pp_px = _parse_pp_price(title)
        if pp_px is None:
            continue  # no stated price -> can't prove at/above market -> stays weak
        sym = _norm(tk)
        mkt = last_prices.get(sym) or last_prices.get(str(tk or "").upper())
        try:
            if mkt is not None and float(mkt) > 0 and pp_px >= float(mkt):
                out.add(sym)
        except (TypeError, ValueError):
            continue
    return out


# E2 catalyst GRADING (Ross course study, build_order #3, videos 06/36): Ross DISTRUSTS
# weak catalysts (dilution/compliance/legal — fade predictors, above) and FAVORS strong
# catalysts — a binary trial result, FDA decision, partnership, contract, M&A. A strong
# headline is a real reason a low-float runs; a strong-titled mover is a higher-grade Ross
# setup than a no-news spike. Keyword list (mirrors the weak list's structure), no magic
# constants. STRONG / WEAK / (everything else = MEDIUM/neutral).
_STRONG_CATALYST_KEYWORDS = (
    "fda approval", "fda approves", "fda clearance", "fda grants", "breakthrough therapy",
    "phase 3", "phase iii", "phase 2", "phase ii", "topline results", "primary endpoint",
    "met its primary", "positive results", "trial results", "clinical trial",
    "partnership", "strategic partnership", "collaboration agreement", "definitive agreement",
    "merger", "acquisition", "to acquire", "to be acquired", "buyout", "takeover",
    "awarded contract", "wins contract", "contract award", "government contract",
    "defense contract", "purchase order", "letter of intent", "joint venture",
    "record revenue", "raises guidance", "beats", "earnings beat", "tender offer",
)


def _is_strong_catalyst(title: str) -> bool:
    """True when a headline is a STRONG / trusted catalyst (FDA/trial/partnership/contract/
    M&A/beat). Pure; fail-open to False (an unreadable title is never up-graded)."""
    t = str(title or "").lower()
    return any(k in t for k in _STRONG_CATALYST_KEYWORDS)


def strong_catalyst_symbols() -> set[str]:
    """Normalized tickers whose freshest fresh-news headline is a STRONG catalyst (the
    boost set). Same title-carrying fetch as ``weak_catalyst_symbols``; fail-open to empty
    (no boost) when the news feed is unavailable. A weak headline that ALSO matches a strong
    keyword is intentionally NOT excluded here — the consumer treats weak as the dominant
    (suppressing) grade (Ross distrusts a name that is BOTH diluting and 'partnering')."""
    try:
        from ...massive_client import get_recent_news_items

        return {
            _norm(tk)
            for tk, title in get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
            if _is_strong_catalyst(title)
        }
    except Exception:
        logger.debug("[catalyst] strong-catalyst grade fetch failed", exc_info=True)
        return set()


# FAKE-CATALYST credibility guard (Ross AS101/HVM101: he DISTRUSTS unverified / hacked-PR /
# unsolicited-buyout / rumor headlines — they round-trip FULLY to the pre-move price with no
# clean re-entry, so a fill on one is a likely fade-trap). This is a DIFFERENT angle from the
# WEAK grade (dilution/compliance/legal, above) and the STRONG grade (real FDA/trial/M&A): a
# fake catalyst can WEAR a strong costume ("rumor of buyout", "in talks to be acquired",
# "confirms" a pump) — credibility, not catalyst TYPE. A flagged headline earns a SOFT
# DOWN-WEIGHT (conservative, low over-veto — never a hard veto), and it DOMINATES the strong
# boost (a rumored buyout is not a real M&A catalyst). Keyword list, no magic constants;
# fail-open. Kill-switch: chili_momentum_fake_catalyst_guard_enabled (default ON).
_FAKE_CATALYST_KEYWORDS = (
    "rumor", "rumour", "rumored", "rumoured", "unconfirmed", "unverified",
    "speculation", "speculated", "reportedly", "alleged", "allegedly",
    "in talks", "in discussions", "exploring a sale", "considering a sale",
    "unsolicited", "non-binding", "nonbinding", "preliminary proposal",
    "expression of interest", "hacked", "hack", "compromised account",
    "fake press release", "fabricated", "fraudulent press", "spoofed",
    "pump and dump", "pump-and-dump", "stock promotion", "promoted stock",
    "paid promotion", "newsletter touts", "social media buzz",
)


def _is_fake_catalyst(title: str) -> bool:
    """True when a headline reads as an UNVERIFIED / hacked-PR / unsolicited-buyout / rumor /
    pump-style catalyst (low credibility — Ross's full-round-trip fade-trap). Pure; fail-open
    to False (an unreadable title is never down-weighted)."""
    t = str(title or "").lower()
    return any(k in t for k in _FAKE_CATALYST_KEYWORDS)


def fake_catalyst_symbols() -> set[str]:
    """Normalized tickers whose freshest fresh-news headline reads as a FAKE / low-credibility
    catalyst (the credibility-de-weight set). Same title-carrying fetch as the weak/strong
    grades; fail-open to empty (no de-weight) when the news feed is unavailable, so a missing
    feed never strips credibility from real catalysts."""
    try:
        from ...massive_client import get_recent_news_items

        return {
            _norm(tk)
            for tk, title in get_recent_news_items(max_age_min=_news_catalyst_max_age_min())
            if _is_fake_catalyst(title)
        }
    except Exception:
        logger.debug("[catalyst] fake-catalyst grade fetch failed", exc_info=True)
        return set()


def fake_catalyst_viability_delta(symbol: str, fake_symbols: set[str] | None) -> float:
    """SOFT credibility DOWN-WEIGHT for a FAKE / unverified / hacked-PR / rumor / unsolicited-
    buyout headline (Ross AS101/HVM101 distrust). Negative half-tilt — the same magnitude the
    catalyst boost uses, so a fabricated catalyst's positive tilt is neutralized rather than the
    name hard-vetoed (conservative, low over-veto; the lane keeps the name eligible, just
    de-prioritized). Crypto (-USD) / absent set / flag OFF -> 0 (never penalizes, byte-identical
    when the guard is disabled). Pure + fail-open."""
    if not fake_symbols or "-USD" in str(symbol or "").upper():
        return 0.0
    if not bool(getattr(settings, "chili_momentum_fake_catalyst_guard_enabled", True)):
        return 0.0
    return -(_catalyst_tilt() * 0.5) if _norm(symbol) in fake_symbols else 0.0


def catalyst_grade_selection_delta(
    symbol: str,
    *,
    weak_symbols: set[str] | None = None,
    strong_symbols: set[str] | None = None,
    fake_symbols: set[str] | None = None,
) -> float:
    """E2 catalyst-GRADE viability delta for SELECTION (gap #12, build_order #3) — distinct
    from the regime-aware ``catalyst_viability_delta`` (which boosts ANY catalyst). Grades
    the catalyst TYPE:

      * WEAK (dilution/compliance/legal) -> a SUPPRESSION (negative tilt of the full
        catalyst-tilt magnitude). Weak DOMINATES strong (a name that is both diluting and
        'partnering' is still a dilution fade for Ross).
      * STRONG (FDA/trial/partnership/contract/M&A/beat) -> a BOOST (+ half tilt — the same
        magnitude the news tilt uses; a confirming, not standalone, signal).
      * MEDIUM / no headline / crypto / absent feed -> 0 (neutral, no change).

    FAKE-catalyst dominance (Ross AS101/HVM101): a STRONG-looking headline that is actually
    UNVERIFIED / hacked-PR / unsolicited / rumor (e.g. "rumor of buyout") is in ``fake_symbols``
    too — credibility BEATS catalyst type, so the strong boost is SUPPRESSED to 0 (the dedicated
    ``fake_catalyst_viability_delta`` carries the soft negative). Weak still dominates fake (a
    name that is both diluting and rumored is the stronger fade). ``fake_symbols`` defaults None,
    so an absent set leaves this function byte-identical to its prior behavior.

    Pure + fail-open. The CALLER decides whether a negative delta also drops live
    eligibility (the hard gate) — this only returns the magnitude."""
    if "-USD" in str(symbol or "").upper():
        return 0.0
    sym = _norm(symbol)
    if weak_symbols and sym in weak_symbols:
        return -_catalyst_tilt()
    if fake_symbols and sym in fake_symbols:
        # Credibility veto of the boost: a rumored / hacked / unsolicited "strong" catalyst
        # earns NO boost (the soft penalty lives in fake_catalyst_viability_delta). Gated by
        # the guard flag so flag-OFF restores the byte-identical strong-boost path.
        if bool(getattr(settings, "chili_momentum_fake_catalyst_guard_enabled", True)):
            return 0.0
    if strong_symbols and sym in strong_symbols:
        return _catalyst_tilt() * 0.5
    return 0.0


# Sympathy/theme cluster (Ross gap #4, videos 06/09/12): the day's movers cluster by
# SECTOR; a sector whose LEADER is a big % gainer drags its peers (the "hot potato"
# sympathy run that produces the STI/ASTC-class moves). A sympathy peer — same SIC sector
# as a strong leader, itself in play but less extended — is a Ross sympathy long. Two
# documented bases (the leader floor + the min cluster size); the sector data is reliable
# SEC SIC (low mis-cluster risk).
_SYMPATHY_LEADER_FLOOR_PCT = 15.0
_SYMPATHY_MIN_CLUSTER = 2


def sympathy_peer_symbols(
    movers: dict[str, float],
    sector_of: dict[str, str | None],
    *,
    leader_floor_pct: float = _SYMPATHY_LEADER_FLOOR_PCT,
    min_cluster: int = _SYMPATHY_MIN_CLUSTER,
) -> set[str]:
    """Normalized tickers that are SYMPATHY PEERS of a hot sector cluster: same SIC sector
    as a cluster whose LEADER (top % gainer) clears ``leader_floor_pct`` and that holds at
    least ``min_cluster`` movers. The leader is NOT a peer (it is already ranked on its own
    move); the rest get the sympathy tilt. ``movers`` = ``{ticker: change_pct}``,
    ``sector_of`` = ``{ticker: sic_sector|None}``. Pure + side-effect-free."""
    by_sector: dict[str, list[tuple[str, float]]] = {}
    for sym, chg in (movers or {}).items():
        sec = sector_of.get(str(sym).upper()) if sector_of else None
        if not sec:
            continue
        try:
            by_sector.setdefault(sec, []).append((str(sym).upper(), float(chg)))
        except (TypeError, ValueError):
            continue
    peers: set[str] = set()
    for members in by_sector.values():
        if len(members) < int(min_cluster):
            continue
        members.sort(key=lambda x: x[1], reverse=True)
        if members[0][1] < float(leader_floor_pct):
            continue  # no strong leader -> not a hot cluster, no sympathy drag
        for sym, _chg in members[1:]:
            peers.add(_norm(sym))
    return peers


def sympathy_viability_delta(symbol: str, sympathy_symbols: set[str] | None) -> float:
    """Additive viability tilt for a SYMPATHY peer of a hot sector cluster (gap #4). Same
    magnitude as the catalyst half-tilt — a real but secondary confirming signal (the
    leader's move is the primary). Crypto (-USD) / absent set -> 0 (never penalizes)."""
    if not sympathy_symbols or "-USD" in str(symbol or "").upper():
        return 0.0
    return _catalyst_tilt() * 0.5 if _norm(symbol) in sympathy_symbols else 0.0


# Cross-day continuation prior (re-analysis survivor S1, video 43): a stock that CLOSED
# near its high-of-day (and green) into the power hour is far likelier to gap-continue the
# next day — Ross gets warm on it premarket BEFORE the tape forms. This is the only NEW
# *selection* signal the 32-video re-analysis surfaced; it attacks CHILI's proven
# bottleneck (be on the right name early). Weight is the ONE documented base.
CLOSE_STRENGTH_PRIOR_WEIGHT = 0.10


def _close_strength_score(o: float, h: float, lo: float, c: float) -> float:
    """[0,1] daily close-strength: 0.65 x (close position in the day's range) + 0.35 x
    green-close. 1.0 = closed at the HOD and green (strong power-hour close -> continuation
    prior). Pure; 0.5 (neutral) on a degenerate range."""
    rng = float(h) - float(lo)
    if rng <= 0 or not (rng == rng):  # zero/NaN range
        return 0.5
    pos = (float(c) - float(lo)) / rng
    green = 1.0 if float(c) > float(o) else 0.0
    return max(0.0, min(1.0, 0.65 * pos + 0.35 * green))


def close_strength_prior(symbol: str) -> float:
    """[0,1] next-day continuation prior from the most recent daily close-strength. Reuses
    the cached daily bars (get_aggregates_df). Crypto / no-data -> 0.5 (neutral, no tilt).
    Fail-open."""
    if "-USD" in str(symbol or "").upper():
        return 0.5
    try:
        from ...massive_client import get_aggregates_df

        df = get_aggregates_df(symbol, interval="1d", period="7d")
        if df is None or getattr(df, "empty", True) or len(df) < 1:
            return 0.5
        row = df.iloc[-1]
        return _close_strength_score(
            float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        )
    except Exception:
        logger.debug("[catalyst] close-strength prior failed %s", symbol, exc_info=True)
        return 0.5


def close_strength_priors(symbols, *, max_lookups: int = 40) -> dict[str, float]:
    """``{ticker: prior}`` for the equity ``symbols`` (bounded to ``max_lookups`` daily-bar
    reads per call; the cache warms the rest next pass). Only ABOVE/BELOW-neutral priors
    are returned (0.5 is dropped — no tilt) to keep the forwarded map compact. Fail-open."""
    out: dict[str, float] = {}
    looked = 0
    for s in (symbols or []):
        su = str(s or "").upper().strip()
        if not su or su.endswith("-USD") or looked >= int(max_lookups):
            continue
        p = close_strength_prior(su)
        looked += 1
        if abs(p - 0.5) > 1e-9:
            out[su] = round(p, 4)
    return out


def close_strength_viability_delta(symbol: str, priors: dict | None) -> float:
    """Additive viability tilt from the cross-day close-strength prior (S1). Centered at
    0.5 so a strong-close name is boosted and a weak-close one slightly discounted; scaled
    by ``CLOSE_STRENGTH_PRIOR_WEIGHT``. Crypto / absent -> 0."""
    if not priors or "-USD" in str(symbol or "").upper():
        return 0.0
    try:
        p = priors.get(str(symbol or "").upper())
        if p is None:
            return 0.0
        return CLOSE_STRENGTH_PRIOR_WEIGHT * (float(p) - 0.5)
    except (TypeError, ValueError):
        return 0.0


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
