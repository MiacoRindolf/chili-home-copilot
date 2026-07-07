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

logger = logging.getLogger(__name__)


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


def _premarket_change_pct(s: dict) -> float | None:
    """Premarket-honest change% when the snapshot's vendor ``todaysChangePerc`` is
    null. The vendor field is valid premarket for ACTIVELY-printing names, but the
    universe builder drops any row where it is null — which silently starves the
    premarket universe (the day's gappers surface at ~09:40 ET instead of ~04:00).

    Mirrors the PROVEN ``nbbo_tape`` fallback (nbbo_tape.py:92-95): base = today's
    open, else yesterday's close; chg = (live price − base)/base·100. So the
    universe surfaces exactly the premarket movers the NBBO tape already grades.
    Fail-CLOSED: no live price or no usable base ⇒ ``None`` ⇒ the ticker is still
    dropped (no invented mover from a no-print row). Behind a default-ON kill-switch
    so it is trivially reversible; RTH is byte-unchanged (vendor field is populated
    RTH, so this is never reached).
    """
    try:
        from ....config import settings

        if not bool(getattr(settings, "chili_momentum_premarket_change_fallback_enabled", True)):
            return None
    except Exception:
        pass
    price = _snapshot_price(s)
    if price is None or price <= 0:
        return None
    day = s.get("day") or {}
    prev = s.get("prevDay") or {}
    base = _f(day.get("o")) or _f(prev.get("c"))
    if base is None or base <= 0:
        return None
    return (price - base) / base * 100.0


def _snapshot_adv_shares(s: dict) -> float | None:
    """Average-daily-VOLUME proxy (in SHARES) from the snapshot — the causal
    low-ADV "no-market-maker" edge (AS101): EXTREME relative volume only happens
    when the name's baseline turnover is small enough that HFT market-makers are
    absent. CHILI already floors TODAY's $-volume (tradability) but never looks at
    the BASELINE average — a mega-cap printing +10% on huge ADV is a crowded,
    market-made tape, not the retail edge.

    The full-market snapshot carries no multi-day average, so the cleanest proxy
    is ``prevDay.v`` (yesterday's session share volume — a stable, lookahead-free,
    settled prior-day baseline; today's still-accumulating volume is NOT the
    average and would be circular with the move). Fail-OPEN: no usable prevDay
    volume ⇒ ``None`` ⇒ the name is NOT penalized (never block/demote on missing
    data).
    """
    prev = s.get("prevDay") or {}
    v = _f(prev.get("v"))
    return v if (v is not None and v > 0) else None


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated ``q``-percentile (0..1) of an ASCENDING list. Returns
    ``None`` on an empty list so callers can fail-open."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# ADV-ceiling discount FLOOR (AS101 latent-risk defuse): the soft discount must be a
# TIE-BREAKER, not a re-orderer — clamp every weight at/above this floor so the penalty
# can only nudge near-equal names, never demote a strong mover beneath a weaker one.
# 0.7 == at most a -30% rank haircut. Single documented base; everything else adaptive.
_ADV_WEIGHT_FLOOR = 0.7
# A genuinely FRESH front-side mover (high position-in-day-range AND a large day-change
# = a clean day-2 breakout still pinned near its high) is EXEMPT from the ADV penalty:
# its liveness, not its baseline turnover, is what matters. Both gates are within-batch
# adaptive percentiles. Fail-open: absent pos/chg ⇒ no exemption (prior weight stands).
_FRESH_MOVER_POS_PCTL = 0.70
_FRESH_MOVER_CHANGE_PCTL = 0.70

# HOT-MOVER quality bar (the NEXR late-surge re-catch). A name is a "genuinely
# explosive RIGHT NOW" hot mover only when it clears ALL THREE — RVOL, %-move, and
# the profile's $-vol floor — so the guaranteed re-catch (and the sub-$1 exemption)
# can NEVER admit penny junk. The RVOL/%-move bars are within-batch high-percentiles
# (the batch decides what "explosive" means today) clamped UP to the documented config
# floors; below this percentile the batch is too thin to compute a bar so the config
# floor governs alone. Single documented base per dimension; everything else adaptive.
_HOT_MOVER_RVOL_PCTL = 0.90
_HOT_MOVER_CHANGE_PCTL = 0.90


def _snapshot_today_shares(s: dict) -> float | None:
    """TODAY's accumulated session share volume from a snapshot row — the numerator
    of intraday RVOL. Mirrors the universe loop's tradability read: the day aggregate
    (``day.v``) once RTH prints, else the minute bar's accumulated volume (``min.av``,
    which counts extended-hours prints so premarket igniters aren't zero). Fail-open:
    no usable volume ⇒ ``None`` (no fabricated RVOL)."""
    day = s.get("day") or {}
    mn = s.get("min") or {}
    v = max(_f(day.get("v")) or 0.0, _f(mn.get("av")) or 0.0)
    return v if v > 0 else None


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


def _normalize_ross_common_stock_symbol(value: object) -> str:
    sym = str(value or "").strip().upper()
    if not sym or "-USD" in sym or "/" in sym:
        return ""
    return sym


def ross_smallcap_profile_evidence(
    symbol: str,
    *,
    signal: dict | None = None,
    snapshot_row: dict | None = None,
    profile: UniverseProfile = EQUITY_ROSS_SMALLCAP,
) -> tuple[bool, str, dict]:
    """Hard Ross equity instrument-class gate for live auto-arm/admission.

    Setup evidence still lives in the tick/Ross gates. This helper validates the
    universe itself: low-priced, liquid-enough, already-moving equities. Missing
    numeric proof fails closed so a generic broad-equity row cannot pass as Ross.
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


def _intraday_rvol(today_shares: float | None, adv_shares: float | None) -> float | None:
    """Relative volume = today's accumulated shares ÷ the prior-day baseline (the
    ADV proxy). ``None`` (fail-closed) when either side is missing/degenerate — a
    missing baseline must NOT manufacture an explosive name for the hot-mover bar."""
    if today_shares is None or adv_shares is None or adv_shares <= 0 or today_shares <= 0:
        return None
    return today_shares / adv_shares


def _hot_mover_bars(
    rvols: list[float | None],
    chgs: list[float | None],
    *,
    rvol_floor: float,
    change_floor: float,
) -> tuple[float, float]:
    """Adaptive (RVOL, %-move) bars for the hot-mover quality gate: MAX(documented
    config floor, within-batch high-percentile). The percentile only LIFTS the floor
    (so a hot batch raises the bar, a quiet/thin batch falls back to the floor); it can
    never lower it below the documented reference. Needs ≥4 known values to trust a
    percentile, else the floor governs alone. No magic numbers — both floors are config,
    both percentiles are batch-relative."""
    known_rvol = sorted(v for v in rvols if v is not None and v > 0)
    known_chg = sorted(c for c in chgs if c is not None)
    rvol_bar = rvol_floor
    chg_bar = change_floor
    if len(known_rvol) >= 4:
        p = _percentile(known_rvol, _HOT_MOVER_RVOL_PCTL)
        if p is not None:
            rvol_bar = max(rvol_floor, p)
    if len(known_chg) >= 4:
        p = _percentile(known_chg, _HOT_MOVER_CHANGE_PCTL)
        if p is not None:
            chg_bar = max(change_floor, p)
    return rvol_bar, chg_bar


def _adv_ceiling_multipliers(
    advs: list[float | None],
    *,
    poss: list[float | None] | None = None,
    chgs: list[float | None] | None = None,
) -> list[float] | None:
    """ADV-CEILING soft re-rank weights (AS101), one per row, in input order.

    Returns ``None`` to signal "leave the rank untouched" (flag off / degenerate
    data) — the caller then applies NO change, byte-identical to today. Otherwise
    each weight is in ``[_ADV_WEIGHT_FLOOR, 1]``: ``1.0`` for names at/under the
    ceiling (the low-ADV edge we WANT) and a smooth, FLOORED sub-1 discount that
    grows with how far a name's ADV sits ABOVE the ceiling. It is a SOFT
    TIE-BREAKER discount, never a hard drop and never a re-orderer — a high-ADV
    name can still make the pool if nothing better exists (no lane starvation).

    The ceiling is ADAPTIVE and basis-independent: ``max(documented Ross 10M-share
    reference floor, batch ADV high-percentile)``. So in a normal small-cap batch
    the Ross floor governs; in a batch that is ALL liquid (rare), the percentile
    lifts the ceiling so the lane isn't starved by penalizing everyone. No
    scattered magic numbers — the 10M base is the single documented config value
    and everything else derives from the batch.

    LATENT-RISK DEFUSE (two additive guards, both adaptive, both fail-open):
      * FLOOR — every discount is clamped at ``_ADV_WEIGHT_FLOOR`` so the penalty
        is bounded (≤ -30%); it can break a near-tie toward the lower-ADV edge but
        can never re-order a clearly-stronger mover below a weaker one.
      * FRESH-MOVER EXEMPT — when ``poss``/``chgs`` are supplied (parallel to
        ``advs``), a name in BOTH the upper tail of position-in-day-range AND the
        upper tail of day-change is a clean front-side day-2 breakout; its ADV
        weight is reset to ``1.0`` (no penalty) because its liveness, not its
        baseline turnover, governs. Absent that data the exemption is skipped
        (prior floored weight stands — byte-identical to the no-arg call).
    """
    try:
        from ....config import settings as _s

        if not bool(getattr(_s, "chili_momentum_adv_ceiling_enabled", True)):
            return None
        ref_floor = float(getattr(_s, "chili_momentum_adv_ceiling_ref_shares", 10_000_000.0))
    except Exception:
        # Fail-open: any config error ⇒ no re-rank (byte-identical to today).
        return None
    if not (ref_floor > 0):
        return None

    known = sorted(v for v in advs if v is not None and v > 0)
    if not known:
        return None  # no ADV data anywhere ⇒ nothing to bias, leave untouched

    # Adaptive ceiling: the documented Ross floor OR the batch's upper-mid ADV,
    # whichever is HIGHER (don't penalize an entire already-liquid batch into
    # starvation). The 75th percentile is a batch-relative reference, not a
    # tuned cap — it only lifts the ceiling when the batch itself runs liquid.
    pct = _percentile(known, 0.75) or ref_floor
    ceiling = max(ref_floor, pct)
    if not (ceiling > 0):
        return None

    # Adaptive fresh-mover bars (within-batch percentiles). Computed once; None when
    # the dimension is absent/degenerate so the exemption simply doesn't engage.
    pos_bar: float | None = None
    chg_bar: float | None = None
    if poss is not None and chgs is not None and len(poss) == len(advs) and len(chgs) == len(advs):
        known_pos = sorted(p for p in poss if p is not None)
        known_chg = sorted(c for c in chgs if c is not None)
        if len(known_pos) >= 4:
            pos_bar = _percentile(known_pos, _FRESH_MOVER_POS_PCTL)
        if len(known_chg) >= 4:
            chg_bar = _percentile(known_chg, _FRESH_MOVER_CHANGE_PCTL)

    weights: list[float] = []
    for i, v in enumerate(advs):
        if v is None or v <= 0:
            weights.append(1.0)  # fail-open: unknown ADV is never penalized
            continue
        if v <= ceiling:
            weights.append(1.0)  # at/under the edge we want — full weight
            continue
        # SOFT discount that grows with the OVER-ceiling multiple. log keeps it
        # gentle (a 2x-ADV name is nudged, a 50x mega-cap is heavily demoted) and
        # the FLOOR bounds it so a strong mover is never re-ordered below a weaker
        # one — the penalty is a tie-breaker, not a re-rank. excess in (0, inf).
        over = v / ceiling
        w = max(_ADV_WEIGHT_FLOOR, 1.0 / (1.0 + math.log(over)))

        # FRESH-MOVER EXEMPTION: a clean front-side day-2 breakout (near its high
        # of day AND a big move) is exempt — liveness governs, not baseline ADV.
        if pos_bar is not None and chg_bar is not None:
            p = poss[i] if poss is not None else None
            c = chgs[i] if chgs is not None else None
            if p is not None and c is not None and p >= pos_bar and c >= chg_bar:
                w = 1.0
        weights.append(w)
    return weights


def _iqfeed_dollar_volumes(tickers) -> dict[str, float]:
    """IQFeed today $-volume for the given candidate tickers — the ground-truth
    PREMARKET volume. Massive's snapshot day/min aggregates lag pre-open, so a
    JEM-class mover that ticked in IQFeed from ~04:00 ET fails the Massive $-vol
    floor until RTH (JEM: armed 14:50 not ~08:04). Queried via ``symbol = ANY(:syms)``
    so it rides the EXISTING (symbol, observed_at) index — ~115ms for 2000 tickers;
    an observed_at-ONLY aggregate over the 52M-row tape is 70s+ (full scan, would
    STALL the selection loop — that was the reverted deploy-1 bug). Fail-open to {}
    (any error / 3s timeout => Massive-only $-vol, byte-identical to pre-fix)."""
    _syms = sorted({str(t).strip().upper() for t in (tickers or []) if t})
    if not _syms:
        return {}
    try:
        from ....db import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as _s:
            _s.execute(text("SET LOCAL statement_timeout = '3000'"))  # belt-and-suspenders fail-open
            _rows = _s.execute(text(
                "SELECT symbol, sum(price*size) FROM iqfeed_trade_ticks "
                "WHERE symbol = ANY(:syms) AND observed_at >= (now() at time zone 'utc')::date "
                "AND price > 0 GROUP BY symbol"
            ), {"syms": _syms}).fetchall()
        return {str(r[0]).upper(): float(r[1] or 0.0) for r in _rows if r[0]}
    except Exception:
        logger.debug("[universe] iqfeed dollar-volume fetch failed", exc_info=True)
        return {}


def build_equity_universe(
    profile: UniverseProfile = EQUITY_ROSS_SMALLCAP,
    *,
    snapshot: list[dict] | None = None,
) -> list[str]:
    """Resolve an equity ``UniverseProfile`` against the full-market snapshot.

    Returns the screened ticker list (uppercased, de-duped, bounded by either
    ``profile.max_universe`` or — when ``chili_momentum_universe_uncapped_enabled``
    is on — the DB-safety ``chili_momentum_universe_hard_ceiling``), ranked
    **freshest-strongest-mover first** —
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

    # HOT-MOVER RE-CATCH config (read ONCE, lazily, so tests/callers can flip the
    # flag without a re-import — and so no later block re-imports `settings` under a
    # local name that would shadow a module-level reference). OFF ⇒ the price_min
    # filter and truncation below are byte-identical to the historical path.
    try:
        from ....config import settings as _settings
    except Exception:
        _settings = None  # type: ignore[assignment]
    _recatch_on = bool(getattr(_settings, "chili_momentum_hot_mover_recatch_enabled", False)) if _settings else False
    _subdollar_on = _recatch_on and bool(
        getattr(_settings, "chili_momentum_hot_mover_subdollar_enabled", False)
    )
    try:
        _rvol_floor = float(getattr(_settings, "chili_momentum_hot_mover_rvol_floor", 5.0)) if _settings else 5.0
    except Exception:
        _rvol_floor = 5.0
    try:
        _change_floor = float(getattr(_settings, "chili_momentum_hot_mover_change_floor", 20.0)) if _settings else 20.0
    except Exception:
        _change_floor = 20.0

    rows: list[tuple[str, float, float, float]] = []  # (ticker, rank_score, chg_pct, pos_in_range)
    advs: list[float | None] = []  # parallel ADV-shares proxy per row (prevDay.v); None = unknown
    rvols: list[float | None] = []  # parallel intraday RVOL per row (today/prevDay shares); None = unknown
    below_price_min: list[bool] = []  # parallel: True when the row is a sub-price_min exemption candidate
    _iqfeed_dvols = _iqfeed_dollar_volumes(
        [s.get("ticker") for s in (snapshot or []) if isinstance(s, dict)]
    )  # IQFeed ground-truth today $-vol for the snapshot candidates (index-fast via symbol=ANY)
    for s in snapshot or []:
        try:
            if not isinstance(s, dict):
                continue
            ticker = str(s.get("ticker") or "").strip().upper()
            if not ticker:
                continue

            price = _snapshot_price(s)
            if price is None:
                continue
            # SUB-$1 EXEMPTION (deferred): when the sub-dollar exemption is on, a name
            # priced UNDER price_min is NOT dropped here — it is carried as an exemption
            # candidate and the price_min filter is re-applied AFTER the batch hot-mover
            # bar is known, so ONLY an explosive sub-$1 runner (NEXR $0.95→$1.18) survives
            # while ordinary penny tape is still dropped. OFF ⇒ the original early drop.
            _under_min = profile.price_min is not None and price < profile.price_min
            if _under_min and not _subdollar_on:
                continue
            if profile.price_max is not None and price > profile.price_max:
                continue

            day = s.get("day") or {}
            mn = s.get("min") or {}
            # PRE-MARKET truth: the snapshot 'day' aggregate stays zeroed until the
            # RTH open, so the $-volume floor must also read the minute bar's
            # ACCUMULATED volume ('av', counts extended-hours prints). Without this
            # every equity fails the floor pre-market -> empty universe -> nothing
            # to arm in the very window Ross trades (#562's hour gate opened it).
            vol = max(_f(day.get("v")) or 0.0, _f(mn.get("av")) or 0.0)
            # IQFeed ground-truth premarket $-vol (Massive day/min aggregates lag pre-open —
            # JEM-class movers fail the floor until RTH though IQFeed has them from ~04:00 ET).
            # MONOTONIC (max) => can only ADD a missed mover, never remove a current one => no regression.
            dollar_vol = max(price * vol, _iqfeed_dvols.get(ticker, 0.0))
            # The $-vol floor is NEVER relaxed — it is the tradability bar that keeps
            # junk out of BOTH the normal pool and the hot-mover guarantee/exemption.
            if profile.min_dollar_volume is not None and dollar_vol < profile.min_dollar_volume:
                continue

            chg = _f(s.get("todaysChangePerc"))
            if chg is None:
                # premarket fallback (today-open → prev-close vs live print) — proven
                # in nbbo_tape; surfaces already-printing gappers by ~04:00 ET not 09:40
                chg = _premarket_change_pct(s)
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
            _adv = _snapshot_adv_shares(s)
            rows.append((ticker, rank_score, chg, pos))
            advs.append(_adv)
            rvols.append(_intraday_rvol(_snapshot_today_shares(s), _adv))
            below_price_min.append(bool(_under_min))
        except Exception:
            continue

    # SUB-$1 EXEMPTION enforcement (B): re-apply the price_min filter now that the
    # batch is complete — drop every sub-price_min row EXCEPT the genuinely-explosive
    # ones (cleared the hot-mover bar: RVOL AND %-move; the $-vol floor was already
    # enforced in the loop). All parallel lists are filtered together to stay aligned.
    if _subdollar_on and any(below_price_min):
        _hot_rvol_bar, _hot_chg_bar = _hot_mover_bars(
            rvols, [r[2] for r in rows], rvol_floor=_rvol_floor, change_floor=_change_floor
        )
        _keep_rows: list[tuple[str, float, float, float]] = []
        _keep_advs: list[float | None] = []
        _keep_rvols: list[float | None] = []
        _keep_below: list[bool] = []
        for r, a, rv, bm in zip(rows, advs, rvols, below_price_min):
            if bm:
                # explosive sub-$1 only: clear BOTH bars (fail-closed on missing RVOL)
                if rv is None or rv < _hot_rvol_bar or r[2] < _hot_chg_bar:
                    continue
            _keep_rows.append(r)
            _keep_advs.append(a)
            _keep_rvols.append(rv)
            _keep_below.append(bm)
        rows, advs, rvols, below_price_min = _keep_rows, _keep_advs, _keep_rvols, _keep_below

    # ADV-CEILING soft re-rank (AS101): low AVERAGE-daily-volume is the causal
    # mechanism for the no-market-maker edge, so SOFT-discount high-ADV names in
    # the rank (never hard-drop — avoid lane starvation). Returns None when the
    # kill-switch is off or ADV data is degenerate ⇒ rank untouched, byte-identical.
    _adv_w = _adv_ceiling_multipliers(
        advs,
        poss=[r[3] for r in rows],  # pos_in_range, parallel to advs
        chgs=[r[2] for r in rows],  # chg_pct, parallel to advs
    )
    if _adv_w is not None and len(_adv_w) == len(rows):
        rows = [
            (t, rs * w, chg, pos)
            for (t, rs, chg, pos), w in zip(rows, _adv_w)
        ]

    # HOT-MOVER GUARANTEE (A): identify the genuinely-explosive names RIGHT NOW —
    # cleared the RVOL AND %-move bar AND (already) the $-vol floor — BEFORE the
    # freshness×move sort/truncation, so a faded-then-resurging runner (low
    # pos_in_range ⇒ low rank_score ⇒ would truncate out, e.g. NEXR +106%) is kept
    # in the rescoring set. Computed pre-sort so the bar reads the full batch, then
    # the guaranteed tickers are spliced AHEAD of the cap below. Bounded by ONE knob
    # (recatch_slots). OFF ⇒ this set is empty and the path is byte-identical.
    _guaranteed: list[str] = []
    if _recatch_on and rows:
        try:
            _slots = int(getattr(_settings, "chili_momentum_hot_mover_recatch_slots", 15)) if _settings else 15
        except Exception:
            _slots = 15
        _slots = max(1, _slots)
        _rvol_bar, _chg_bar = _hot_mover_bars(
            rvols, [r[2] for r in rows], rvol_floor=_rvol_floor, change_floor=_change_floor
        )
        # Hot set = rows clearing BOTH bars (fail-closed on missing RVOL so a name with
        # no prior-day baseline can never be guaranteed). Rank the hot set by RVOL×move
        # (the explosiveness signal, not freshness — freshness is exactly what a faded
        # surger lacks) and take the top `_slots`. The $-vol floor was enforced in the
        # loop, so no junk can enter here.
        _hot: list[tuple[str, float]] = []
        for (ticker, _rs, chg, _pos), rv in zip(rows, rvols):
            if rv is None or rv < _rvol_bar or chg < _chg_bar:
                continue
            _hot.append((ticker, rv * math.log1p(max(0.0, chg))))
        _hot.sort(key=lambda x: x[1], reverse=True)
        _guaranteed = [t for t, _ in _hot[:_slots]]

    # Freshest, strongest-WORKING movers first (rank_score = freshness × move).
    # No fixed RVOL cut — the percentile ranking in score_universe (RVOL +
    # momentum + low-float) does the fine selection on the enriched survivors.
    rows.sort(key=lambda r: r[1], reverse=True)

    # UNCAPPED (2026-06-15, the CUPR drop): the top-50 count cap truncated 296
    # screened movers to 50, and a name that ran then faded (low pos_in_range,
    # e.g. CUPR +125%) ranked OUT of the pool and never got a viability row at
    # all. With the flag on, surface EVERY screen-passer (ranked order preserved
    # for downstream ``[:N]`` consumers) bounded only by the DB-safety hard
    # ceiling — the adaptive price/$-vol/change screen + the Ross percentile
    # re-rank are the real selection, not a fixed count. Settings read LAZILY so
    # tests/callers can flip the flag without re-import. OFF ⇒ the historical
    # ``max_universe`` (top-50) break, byte-identical to current.
    try:
        _uncapped = bool(
            getattr(_settings, "chili_momentum_universe_uncapped_enabled", False)
        ) if _settings else False
        _hard_ceiling = int(
            getattr(_settings, "chili_momentum_universe_hard_ceiling", 1500)
        ) if _settings else 1500
    except Exception:
        _uncapped = False
        _hard_ceiling = 1500
    cap = max(1, _hard_ceiling) if _uncapped else max(1, int(profile.max_universe))

    # HOT-MOVER GUARANTEE splice (A): emit the guaranteed hot movers FIRST (in their
    # explosiveness order), then the normal freshness×move ranking fills the rest up
    # to `cap`. A guaranteed name already in the top-`cap` is a no-op (the `seen` set
    # de-dupes); a guaranteed name that ranked past `cap` is re-caught WITHOUT evicting
    # the count cap's quality (the guarantee is bounded by recatch_slots). Whether the
    # guaranteed slots count toward `cap` or extend it: they EXTEND it, so the normal
    # top-`cap` pool is never starved by the re-catch — the guarantee is purely additive
    # and bounded. OFF ⇒ `_guaranteed` is empty ⇒ byte-identical to the old loop.
    seen: set[str] = set()
    out: list[str] = []
    for ticker in _guaranteed:
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
    # Additive: re-catch never DISPLACES the ranked pool. But never breach the
    # DB-safety hard ceiling when uncapped (it bounds total row count) — clamp there.
    _effective_cap = cap + len(out)
    if _uncapped:
        _effective_cap = min(_effective_cap, max(1, _hard_ceiling))
    for ticker, *_ in rows:
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
        if len(out) >= _effective_cap:
            break
    return out


def symbols_within_profile_price_band(
    symbols,
    profile: UniverseProfile = EQUITY_ROSS_SMALLCAP,
    *,
    snapshot: list[dict] | None = None,
) -> tuple[set[str], bool]:
    """Keep only ``symbols`` whose CURRENT price sits in the profile's instrument
    CLASS band ``[price_min, price_max]``.

    This is the LIVE-ARM instrument-class gate. ``build_equity_universe`` price-
    screens the equity-viability *refresh*, but large-caps still reach
    ``momentum_symbol_viability`` and go ``live_eligible`` via the BROAD brain
    momentum scoring (``nm_momentum_crypto_intel``) — e.g. MU/MRVL on an earnings
    breakout score "High Ross momentum quality" yet are $70-$100 names. That path
    is NOT price-screened, so without this gate the $1-$20 Ross small-cap lane
    would arm a $100 semiconductor with real money. Reuses the profile's EXISTING
    ``price_min``/``price_max`` knobs (the documented instrument-class definition,
    not a performance cap) — no new thresholds.

    Returns ``(kept, snapshot_ok)``:
      * ``kept``        — subset of ``symbols`` POSITIVELY confirmed in-band.
      * ``snapshot_ok`` — ``False`` only when the full-market snapshot was entirely
        unavailable, so the caller can fail SAFE (a live-money gate must not arm a
        name it cannot confirm is in-class). A symbol present in the snapshot but
        priced out-of-band, or absent from it, is dropped with ``snapshot_ok=True``.

    When the profile declares no price band (both bounds ``None``) every symbol is
    kept — the gate is a no-op for non-price-classed profiles.
    """
    want = {str(s).strip().upper() for s in (symbols or []) if str(s or "").strip()}
    if not want:
        return set(), True
    if profile.price_min is None and profile.price_max is None:
        return want, True  # no instrument-class band declared -> no constraint

    if snapshot is None:
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(
                max_age_seconds=profile.snapshot_max_age_seconds
            ) or []
        except Exception:
            logger.debug("[universe] price-band snapshot fetch failed", exc_info=True)
            snapshot = []
    if not snapshot:
        return set(), False  # total snapshot outage -> caller decides fail-open/safe

    prices: dict[str, float] = {}
    for s in snapshot:
        try:
            if not isinstance(s, dict):
                continue
            t = str(s.get("ticker") or "").strip().upper()
            if t in want:
                p = _snapshot_price(s)
                if p is not None and p > 0:
                    prices[t] = p
        except Exception:
            continue

    kept: set[str] = set()
    for t in want:
        p = prices.get(t)
        if p is None:
            continue  # unknown current price -> drop (fail-safe for a live-arm pool)
        if profile.price_min is not None and p < profile.price_min:
            continue
        if profile.price_max is not None and p > profile.price_max:
            continue
        kept.add(t)
    return kept, True


def snapshot_dollar_volumes(
    symbols,
    *,
    snapshot: list[dict] | None = None,
    max_age_seconds: float | None = EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds,
) -> dict[str, float]:
    """Map each of ``symbols`` to its CURRENT dollar-volume (price * today's share
    volume) from the full-market snapshot — a selection-time LIQUIDITY proxy.

    Higher dollar-volume correlates with a tighter, FILLABLE BBO spread. The live
    lane blocks wide-spread entries, so a trigger on an illiquid name never fills;
    preferring high-dollar-volume movers at the selection gate is the #1 lever for
    turning triggers into FILLS (spread sweep: 06-08 5m liquid ~100bps = +$12,818
    vs wide ~200bps = +$634). The snapshot carries no reliable ask, so dollar-volume
    is the cleanest available proxy. Fail-open: a symbol missing / with no price or
    volume is simply absent from the map (the caller treats absent as 0.0)."""
    want = {str(s).strip().upper() for s in (symbols or []) if str(s or "").strip()}
    if not want:
        return {}
    if snapshot is None:
        try:
            from ...massive_client import get_full_market_snapshot

            snapshot = get_full_market_snapshot(max_age_seconds=max_age_seconds) or []
        except Exception:
            logger.debug("[universe] dollar-volume snapshot fetch failed", exc_info=True)
            snapshot = []
    out: dict[str, float] = {}
    for s in snapshot or []:
        try:
            if not isinstance(s, dict):
                continue
            t = str(s.get("ticker") or "").strip().upper()
            if t not in want:
                continue
            px = _snapshot_price(s)
            vol = _snapshot_today_shares(s) or 0.0
            if px and px > 0 and vol > 0:
                out[t] = float(px) * float(vol)
        except Exception:
            continue
    return out
