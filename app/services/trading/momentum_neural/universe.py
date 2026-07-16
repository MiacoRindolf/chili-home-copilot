"""Per-setup trading universe profiles + builders.

The screener TAILORS the instrument universe to the strategy — the universe is
PART of the strategy, not one global list. Operator architecture (2026-06-08):
*"dapat ... kaya niya i-tailor ... yung universe na gagamitin ng lane base sa
pattern o strategy o lane or setup"*. Each setup/lane declares the instrument
PROFILE it wants; a builder resolves that profile against the live market.

First instance — the Ross-Cameron momentum lane. Ross trades **low-float
small-cap GAPPERS** ($1-$20, already in play, liquid-enough to exit), NOT the
static large-cap ``DEFAULT_SCAN_TICKERS`` (KLAC ~$2,100 / MU ~$950 / NVDA) the
equity lane was forced onto. Those mega-caps move 2-8%; Ross's names move
20-100%+ in a day. ``build_equity_universe`` screens the full-market snapshot
(~12,776 tickers) down to that Ross universe; the existing per-ticker enrichment
(``intraday_signals.scan_momentum_continuation``) + ``ross_momentum.score_universe``
then rank within it. The lane's scans accept an explicit ``tickers=`` list, so
this swaps only the UNIVERSE SOURCE — the entry gate / stop / sizing are unchanged.

Adaptive / no-magic (operator principle #1): the profile carries ONE documented
knob per instrument dimension and nothing scattered —

  * ``price_min`` / ``price_max``  — the small-cap *definition* (what instrument
    CLASS this setup trades), NOT a performance cap. Ross's stated $1-$20 range.
  * ``min_dollar_volume``          — tradability floor (can you actually exit?).
  * ``min_change_pct``             — an "in play" FLOOR; the percentile ranking
    in ``score_universe`` does the real selection ABOVE it.
  * ``max_universe``               — candidate-pool cap; selection is adaptive
    (top-N by move here → percentile rank downstream), so the system discovers
    the best names rather than obeying a fixed RVOL cut.

Reference points (Ross $1-$20, 5x RVOL) are FLOORS, not ceilings — other
lanes/setups register their own ``UniverseProfile`` and the same builder serves
them. See docs/DESIGN/MOMENTUM_LANE.md.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ...symbol_hygiene import normalize_equity_symbol
from .volume_pace import snapshot_volume_pace

logger = logging.getLogger(__name__)


_ROSS_BLOCKED_ETP_SYMBOLS = frozenset({
    # Leveraged/inverse ETFs and volatility products can pass price/change/volume
    # screens, but they are not Ross-style small-cap common-stock gappers.
    "SOXL",
    "SOXS",
    "SQQQ",
    "TQQQ",
    "UVXY",
    "VXX",
    "SPXS",
    "SPXL",
    "LABD",
    "LABU",
    "TZA",
    "TNA",
    "FAS",
    "FAZ",
})


def _normalize_ross_common_stock_symbol(symbol: object) -> str:
    sym = normalize_equity_symbol(str(symbol or ""))
    if not sym or sym in _ROSS_BLOCKED_ETP_SYMBOLS:
        return ""
    return sym


@dataclass(frozen=True)
class UniverseProfile:
    """The instrument profile a setup/lane wants the screener to build.

    A typed spec so *what kind of instrument* a strategy trades is declared once,
    as data, decoupled from *what pattern* it then runs. ``None`` on any band/floor
    means "no constraint on this dimension".
    """

    profile_id: str
    asset_class: str  # "equity" | "crypto"
    label: str
    price_min: float | None = None
    price_max: float | None = None
    min_dollar_volume: float | None = None  # price * shares traded today
    min_change_pct: float | None = None  # signed % change floor (long bias: positive)
    max_universe: int = 50
    low_float_bias: bool = False  # prefer low float (proxy via price when float absent)
    catalyst_required: bool = False
    # Freshness: tighten the shared snapshot TTL for this profile so newly-igniting
    # names surface while a clean first-pullback entry still exists (None = 30min default).
    snapshot_max_age_seconds: float | None = None
    notes: str = ""


# ── Ross-Cameron momentum lane: low-float small-cap gappers ───────────────────
# Each field is a documented Ross criterion, framed as a FLOOR / class-definition,
# not a magic number. Operator can tune this ONE spec; downstream selection is
# adaptive (top-N by move → percentile rank in score_universe).
EQUITY_ROSS_SMALLCAP = UniverseProfile(
    profile_id="equity_ross_smallcap",
    asset_class="equity",
    label="Ross small-cap momentum gappers",
    # Ross's instrument CLASS: low-priced small-caps. Sub-$1 = manipulative /
    # halt-prone penny tape; >$20 = retail can't size + not low-float. This is
    # the strategy's universe DEFINITION, not a performance cap.
    price_min=1.0,
    price_max=20.0,
    # Tradability: a name with < ~$1M of turnover today can't be entered AND
    # exited cleanly on size. Floor only — the move, not the dollar-volume, ranks.
    min_dollar_volume=1_000_000.0,
    # "In play": Ross never trades what isn't already moving. A modest floor so
    # the dead tape is dropped; score_universe percentile-ranks the survivors.
    min_change_pct=5.0,
    # Candidate pool the day's strongest movers; the bridge + ross score keep the
    # most explosive leaders, the live-eligibility gate narrows further.
    max_universe=50,
    low_float_bias=True,
    catalyst_required=False,
    # Catch igniters EARLY: re-pull the snapshot at ~the equity-refresh cadence
    # (5min) instead of riding the shared 30-min cache, so a name that just
    # started moving is screened while a clean first-pullback entry still exists.
    snapshot_max_age_seconds=300.0,
    notes="Snapshot-screened Ross universe; replaces the static large-cap list for the momentum lane.",
)


def _f(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _snapshot_price(s: dict) -> float | None:
    """Robust current price from a Massive snapshot row.

    Prefer the last trade, then today's consolidated close / VWAP, then the
    latest minute close — premarket rows can have a sparse ``day`` block.
    """
    lt = s.get("lastTrade") or {}
    p = _f(lt.get("p"))
    if p and p > 0:
        return p
    day = s.get("day") or {}
    for k in ("c", "vw"):
        p = _f(day.get(k))
        if p and p > 0:
            return p
    mn = s.get("min") or {}
    p = _f(mn.get("c"))
    return p if (p and p > 0) else None


def _pos_in_range(s: dict, price: float | None) -> float:
    """Cheap intraday freshness proxy from the snapshot's day OHLC: where the
    current price sits in today's range. ~1.0 = at/near the high-of-day (FRESH,
    still working — a shallow pullback can still form and break); ~0.0 = rolled
    to the low (FADED — already ran and reversed). Neutral 0.5 when there is no
    intraday range yet (premarket / flat). This is the day-level analogue of
    ``ross_momentum.intraday_impulse_freshness`` (which is precise but needs a
    per-ticker bar fetch); used here only to RANK the candidate pool so fresh
    near-high names are preferred into it — the precise recent-bar freshness
    filter still runs at arm time.
    """
    day = s.get("day") or {}
    hi = _f(day.get("h"))
    lo = _f(day.get("l"))
    if price is None or hi is None or lo is None or hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (price - lo) / (hi - lo)))


def _snapshot_adv_shares(s: dict) -> float | None:
    """Prior-day share-volume baseline from a snapshot row."""
    prev = s.get("prevDay") or {}
    v = _f(prev.get("v"))
    return v if (v is not None and v > 0) else None


def _snapshot_today_shares(s: dict) -> float | None:
    """Today's accumulated shares, using extended-hours minute accumulation when needed."""
    day = s.get("day") or {}
    mn = s.get("min") or {}
    v = max(_f(day.get("v")) or 0.0, _f(mn.get("av")) or 0.0)
    return v if v > 0 else None


def _intraday_rvol(today_shares: float | None, adv_shares: float | None) -> float | None:
    """Trusted time-normalized intraday RVOL from cumulative shares and ADV."""
    pace = snapshot_volume_pace(today_shares=today_shares, adv_shares=adv_shares)
    try:
        value = pace.get("rvol_pace") if isinstance(pace, dict) else None
        return float(value) if value is not None and float(value) > 0 else None
    except (TypeError, ValueError):
        return None


def _snapshot_volume_pace(s: dict, **kwargs) -> dict:
    """Time-normalized volume-pace telemetry for an equity snapshot row."""
    return snapshot_volume_pace(
        today_shares=_snapshot_today_shares(s),
        adv_shares=_snapshot_adv_shares(s),
        **kwargs,
    )


def _first_num(mapping: dict | None, *keys: str) -> float | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = _f(mapping.get(key))
        if value is not None:
            return value
    return None


def _signal_price(signal: dict | None) -> float | None:
    return _first_num(
        signal,
        "price",
        "last_price",
        "alert_price",
        "hod_price",
        "close",
        "last",
        "last_trade_price",
        "lastTradePrice",
    )


def _signal_change_pct(signal: dict | None) -> float | None:
    return _first_num(
        signal,
        "daily_change_pct",
        "todays_change_perc",
        "todaysChangePerc",
        "change_pct",
        "percent_change",
        "gap_pct",
        "pct_change",
        "change",
    )


def _signal_dollar_volume(signal: dict | None, price: float | None) -> float | None:
    dollar_volume = _first_num(
        signal,
        "dollar_volume",
        "dollar_vol",
        "todays_dollar_volume",
        "today_dollar_volume",
        "day_dollar_volume",
        "turnover",
        "notional_volume",
    )
    if dollar_volume is not None and dollar_volume > 0:
        return dollar_volume
    shares = _first_num(
        signal,
        "volume",
        "day_volume",
        "todays_volume",
        "today_volume",
        "cumulative_volume",
        "volume_today",
        "share_volume",
        "shares_traded_today",
        "v",
    )
    if price is None or shares is None:
        return None
    value = float(price) * float(shares)
    return value if value > 0 else None


def ross_smallcap_profile_evidence(
    symbol: str,
    *,
    signal: dict | None = None,
    snapshot_row: dict | None = None,
    profile: UniverseProfile = EQUITY_ROSS_SMALLCAP,
) -> tuple[bool, str, dict]:
    """Hard Ross equity instrument-class gate for live auto-arm.

    ``ross_tick_scalp_evidence_ok`` validates the setup pillars. This helper
    validates the *universe*: low-priced, liquid-enough, already moving equities.
    Missing numeric proof fails closed here so a broad live-eligible mega-cap
    cannot pass just because a generic Ross/source tag is present.
    """
    raw_sym = str(symbol or "").strip().upper()
    sym = _normalize_ross_common_stock_symbol(raw_sym)
    if profile.asset_class != "equity":
        return False, "wrong_profile_asset_class", {"profile_id": profile.profile_id}
    if not sym or "-USD" in raw_sym:
        return False, "not_equity_symbol", {"symbol": raw_sym}

    snap_price = _snapshot_price(snapshot_row) if isinstance(snapshot_row, dict) else None
    sig_price = _signal_price(signal)
    price = snap_price if snap_price is not None else sig_price

    snap_change = (
        _f(snapshot_row.get("todaysChangePerc"))
        if isinstance(snapshot_row, dict)
        else None
    )
    sig_change = _signal_change_pct(signal)
    change_pct = snap_change if snap_change is not None else sig_change

    snap_dollar_volume = None
    if isinstance(snapshot_row, dict):
        snap_shares = _snapshot_today_shares(snapshot_row)
        if snap_price is not None and snap_shares is not None:
            snap_dollar_volume = float(snap_price) * float(snap_shares)
    sig_dollar_volume = _signal_dollar_volume(signal, sig_price)
    dollar_volume = (
        snap_dollar_volume if snap_dollar_volume is not None else sig_dollar_volume
    )

    debug = {
        "symbol": sym,
        "profile_id": profile.profile_id,
        "price": price,
        "price_source": "snapshot" if snap_price is not None else ("signal" if sig_price is not None else None),
        "change_pct": change_pct,
        "change_source": "snapshot" if snap_change is not None else ("signal" if sig_change is not None else None),
        "dollar_volume": dollar_volume,
        "dollar_volume_source": (
            "snapshot"
            if snap_dollar_volume is not None
            else ("signal" if sig_dollar_volume is not None else None)
        ),
        "price_min": profile.price_min,
        "price_max": profile.price_max,
        "min_dollar_volume": profile.min_dollar_volume,
        "min_change_pct": profile.min_change_pct,
    }

    if price is None:
        return False, "ross_universe_missing_price", debug
    if price <= 0:
        return False, "ross_universe_invalid_price", debug
    if profile.price_min is not None and price < profile.price_min:
        return False, "ross_universe_price_below_profile", debug
    if profile.price_max is not None and price > profile.price_max:
        return False, "ross_universe_price_above_profile", debug

    if dollar_volume is None:
        return False, "ross_universe_missing_dollar_volume", debug
    if profile.min_dollar_volume is not None and dollar_volume < profile.min_dollar_volume:
        return False, "ross_universe_dollar_volume_below_profile", debug

    if change_pct is None:
        return False, "ross_universe_missing_change_pct", debug
    if profile.min_change_pct is not None and change_pct < profile.min_change_pct:
        return False, "ross_universe_change_below_profile", debug

    return True, "ross_universe_profile_ok", debug


def build_equity_universe(
    profile: UniverseProfile = EQUITY_ROSS_SMALLCAP,
    *,
    snapshot: list[dict] | None = None,
) -> list[str]:
    """Resolve an equity ``UniverseProfile`` against the full-market snapshot.

    Returns the screened ticker list (uppercased, de-duped, capped at
    ``profile.max_universe``), ranked **freshest-strongest-mover first** —
    ``freshness × diminishing-returns(move)``, so a name still pinned near its
    high-of-day outranks one that ran huge and rolled over (Ross enters EARLY,
    not after a +1000% fade). The downstream per-ticker enrichment computes true
    intraday RVOL/gap and ``score_universe`` percentile-ranks; this stage only
    decides WHICH names make the pool.

    ``snapshot`` is injectable for tests; otherwise pulled from Massive at
    ``profile.snapshot_max_age_seconds`` (the Ross profile forces a ~5-min pull
    so igniters surface before their first-pullback entry is gone). Fail-open:
    any error / empty snapshot → ``[]`` so the caller falls back to its default
    universe (no regression).
    """
    if profile.asset_class != "equity":
        return []
    if snapshot is None:
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=profile.snapshot_max_age_seconds
            ) or []
        except Exception:
            logger.debug("[universe] snapshot fetch failed", exc_info=True)
            return []

    rows: list[tuple[str, float, float, float]] = []  # (ticker, rank_score, chg_pct, pos_in_range)
    for s in snapshot or []:
        try:
            if not isinstance(s, dict):
                continue
            ticker = _normalize_ross_common_stock_symbol(s.get("ticker"))
            if not ticker:
                continue

            price = _snapshot_price(s)
            if price is None:
                continue
            if profile.price_min is not None and price < profile.price_min:
                continue
            if profile.price_max is not None and price > profile.price_max:
                continue

            day = s.get("day") or {}
            vol = _f(day.get("v")) or 0.0
            dollar_vol = price * vol
            if profile.min_dollar_volume is not None and dollar_vol < profile.min_dollar_volume:
                continue

            chg = _f(s.get("todaysChangePerc"))
            if chg is None:
                continue
            # Long bias: a positive floor keeps only names moving UP into the day
            # (the momentum lane is long-only; a high-volume dump is not a buy).
            if profile.min_change_pct is not None and chg < profile.min_change_pct:
                continue

            # Ross enters EARLY/fresh, not after a name has run +1000% and rolled
            # over. Rank by freshness (position in the day's range, ~1.0 = at the
            # high) × a diminishing-returns view of the move (log1p, so an
            # over-extended monster does not dwarf a fresh strong mover). A name
            # that ran huge then faded (low pos) is demoted below a fresh +30%
            # still pinned near its high. The min_change floor already dropped the
            # dead tape; this orders WHO makes the capped pool.
            pos = _pos_in_range(s, price)
            rank_score = pos * math.log1p(max(0.0, chg))
            rows.append((ticker, rank_score, chg, pos))
        except Exception:
            continue

    # Freshest, strongest-WORKING movers first (rank_score = freshness × move).
    # No fixed RVOL cut — the percentile ranking in score_universe (RVOL +
    # momentum + low-float) does the fine selection on the enriched survivors.
    rows.sort(key=lambda r: r[1], reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for ticker, *_ in rows:
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
        if len(out) >= max(1, int(profile.max_universe)):
            break
    return out


def snapshot_dollar_volumes(
    symbols: list[str] | tuple[str, ...] | set[str],
    *,
    snapshot: list[dict] | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, float]:
    """Return current snapshot dollar-volume for the requested equity symbols.

    Shared by Ross field builders that already have a Massive full-market
    snapshot in hand. Best-effort: missing/stale/vendor-failed symbols are simply
    omitted so callers can keep their existing fail-open behavior.
    """
    wanted = {str(s or "").strip().upper() for s in symbols or [] if str(s or "").strip()}
    if not wanted:
        return {}
    if snapshot is None:
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=max_age_seconds
                if max_age_seconds is not None
                else EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds
            ) or []
        except Exception:
            logger.debug("[universe] dollar-volume snapshot fetch failed", exc_info=True)
            return {}
    out: dict[str, float] = {}
    for s in snapshot or []:
        if not isinstance(s, dict):
            continue
        ticker = _normalize_ross_common_stock_symbol(s.get("ticker"))
        if ticker not in wanted:
            continue
        price = _snapshot_price(s)
        shares = _snapshot_today_shares(s)
        if price is None or shares is None:
            continue
        dollar_volume = float(price) * float(shares)
        if dollar_volume > 0:
            out[ticker] = dollar_volume
    return out
