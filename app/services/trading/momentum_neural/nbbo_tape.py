"""NBBO spread tape — persist the CLEAN consolidated bid/ask (Massive snapshot
``lastQuote``) for the Ross momentum universe each cycle, so the spread-sensitive
replay can use REAL spreads instead of a proxy.

Why this exists (project_momentum_zero_fills_root_cause): OHLCV bars carry no
bid/ask, so the day-replay proxied spread from dollar-volume — which was 6-17x too
tight for explosive low-float names (PAVS proxy 53bps vs real 317bps), making the
replay PnL meaningless. Reconstructing the NBBO from raw per-exchange ``/v3/quotes``
proved unreliable too (crossed/locked/stale quotes -> a PAVS rebuild read 60bps vs
the 317bps the live lane actually saw). BUT the live lane already receives the clean
consolidated NBBO from Massive's snapshot ``lastQuote`` (P=ask, p=bid) every cycle —
the exact 317bps it recorded. So we simply PERSIST what we already see: every future
day then has a real intraday spread per name, and the replay reads it back.

Design notes:
  * Source = Massive snapshot ``lastQuote`` (already consolidated by the vendor),
    NOT raw quotes (no fragile NBBO reconstruction).
  * Sampled only for the Ross universe (price 1-20, $vol>=1M, |change|>=5%) — the
    names a replay actually considers — to bound row volume.
  * Extended-session-gated via market_profile.is_tradeable_now (#562): the tape
    covers premarket -> afterhours, every minute the lane can trade.
  * Retention-bounded (the exit_parity_log bloat lesson): prune older than the
    window; both indexes keep the read and the prune cheap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from .market_profile import is_data_session_now

logger = logging.getLogger(__name__)

# Ross small-cap profile bounds (mirror universe.EQUITY_ROSS_SMALLCAP defaults; kept
# local so the sampler is self-contained and cannot break the trade path on import).
_PRICE_MIN = 1.0
_PRICE_MAX = 20.0
_MIN_DOLLAR_VOLUME = 1_000_000.0
_MIN_ABS_CHANGE_PCT = 5.0
_MAX_SANE_SPREAD_BPS = 5_000.0  # >50% round-trip = stale/garbage quote, not a real market


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _in_sampling_window(now_utc: datetime) -> bool:
    # Sample the full US DATA session (04:00-20:00 ET) — wider than the lane's
    # ENTRY window (premarket_start 07:00): the movers Ross trades at 7:00 develop
    # from 4:00, so the tape/selection must already be warm by the first allowed
    # entry (operator 2026-06-11: preparation time before the open). Holidays
    # aren't excluded — stale quotes fail the per-row sanity filters.
    return is_data_session_now("EQUITY", now=now_utc)


def _ross_row(s: dict[str, Any]) -> Optional[dict[str, Any]]:
    """If a snapshot entry is a Ross-universe mover with a usable clean NBBO, return
    its tape row dict; else None. Pure + side-effect-free."""
    if not isinstance(s, dict):
        return None
    sym = str(s.get("ticker") or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return None
    day = s.get("day") if isinstance(s.get("day"), dict) else {}
    mn = s.get("min") if isinstance(s.get("min"), dict) else {}
    lt = s.get("lastTrade") if isinstance(s.get("lastTrade"), dict) else {}
    prev = s.get("prevDay") if isinstance(s.get("prevDay"), dict) else {}
    # PRE-MARKET truth: the snapshot 'day' aggregate stays zeroed until the RTH open,
    # so a pre-market mover (the Ross gap-and-go) needs the live tick (lastTrade /
    # latest minute bar) and the minute bar's ACCUMULATED volume ('av', which counts
    # extended-hours prints). The old day-or-prevDay fallback graded pre-market names
    # by YESTERDAY's move — which is why the tape had zero pre-market rows.
    px = _f(day.get("c")) or _f(day.get("vw")) or _f(lt.get("p")) or _f(mn.get("c"))
    vol = max(_f(day.get("v")) or 0.0, _f(mn.get("av")) or 0.0)
    if not px or px <= 0:
        return None
    if not (_PRICE_MIN <= px <= _PRICE_MAX):
        return None
    if px * vol < _MIN_DOLLAR_VOLUME:
        return None
    chg = _f(s.get("todaysChangePerc"))  # vendor change vs prev close — valid pre-market
    if chg is None:
        base = _f(day.get("o")) or _f(prev.get("c"))
        chg = ((px - base) / base * 100.0) if (base and base > 0) else 0.0
    if abs(chg) < _MIN_ABS_CHANGE_PCT:
        return None
    lq = s.get("lastQuote") if isinstance(s.get("lastQuote"), dict) else (s.get("last_quote") or {})
    bid = _f(lq.get("p")) or _f(lq.get("bid_price"))
    ask = _f(lq.get("P")) or _f(lq.get("ask_price"))
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else None
    if spread_bps is None or spread_bps > _MAX_SANE_SPREAD_BPS:
        return None
    return {
        "symbol": sym, "bid": bid, "ask": ask, "mid": mid,
        "spread_bps": spread_bps, "day_volume": vol,
    }


def sample_universe_nbbo_spreads(db: Session, *, now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """Read the current Massive snapshot, keep the Ross-universe movers with a clean
    NBBO, and batch-insert their spreads into the tape. Extended-session-gated; best-effort (never
    raises — a sampler failure must not affect anything else). Returns a small summary."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if not _in_sampling_window(now_utc):
        return {"ok": True, "skipped": "outside_session", "inserted": 0}
    try:
        from ...massive_client import get_full_market_snapshot
        snap = get_full_market_snapshot(max_age_seconds=120) or []
    except Exception as exc:  # pragma: no cover - network/vendor
        logger.debug("[nbbo_tape] snapshot fetch failed: %s", exc)
        return {"ok": False, "error": "snapshot_unavailable", "inserted": 0}

    rows = []
    for s in snap:
        r = _ross_row(s)
        if r is not None:
            rows.append(r)
    if not rows:
        return {"ok": True, "inserted": 0, "universe": 0}

    stamped = now_utc.replace(tzinfo=None)
    for r in rows:
        r["observed_at"] = stamped
    try:
        db.execute(
            text(
                "INSERT INTO momentum_nbbo_spread_tape "
                "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
                "VALUES (:symbol, :observed_at, :bid, :ask, :mid, :spread_bps, :day_volume, 'massive_snapshot')"
            ),
            rows,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("[nbbo_tape] insert failed: %s", exc)
        return {"ok": False, "error": "insert_failed", "inserted": 0}
    logger.info("[nbbo_tape] sampled %d Ross-universe NBBO spreads", len(rows))
    return {"ok": True, "inserted": len(rows)}


def prune_nbbo_tape(db: Session, *, retention_days: Optional[int] = None) -> dict[str, Any]:
    """Trim rows older than the retention window (the exit_parity_log bloat lesson).
    The observed_at index makes this cheap. Best-effort."""
    days = int(retention_days if retention_days is not None
               else getattr(settings, "chili_momentum_nbbo_tape_retention_days", 30) or 30)
    # Densified universe ticks (source='massive_ws_universe' for equity, +
    # 'coinbase_ws_universe' for the crypto L2-drain twin, #2026-06-15) are a
    # HIGHER-volume, SHORTER-lived class than the 1-min snapshot tape — prune them
    # on their own (shorter) window so the densification can't regrow the table
    # (the exit_parity_log bloat lesson: bound every high-cardinality write path).
    uni_days = int(
        getattr(settings, "chili_momentum_universe_tick_retention_days", 5) or 5
    )
    try:
        res = db.execute(
            text("DELETE FROM momentum_nbbo_spread_tape WHERE observed_at < (now() - make_interval(days => :d))"),
            {"d": days},
        )
        db.commit()
        n = int(getattr(res, "rowcount", 0) or 0)
        # Second bulk DELETE: trim densified universe ticks on their shorter window.
        res_u = db.execute(
            text(
                "DELETE FROM momentum_nbbo_spread_tape "
                "WHERE source IN ('massive_ws_universe', 'coinbase_ws_universe') "
                "AND observed_at < (now() - make_interval(days => :n))"
            ),
            {"n": uni_days},
        )
        db.commit()
        n_u = int(getattr(res_u, "rowcount", 0) or 0)
        if n or n_u:
            logger.info(
                "[nbbo_tape] pruned %d rows older than %dd + %d universe ticks older than %dd",
                n, days, n_u, uni_days,
            )
        return {"ok": True, "pruned": n, "pruned_universe": n_u,
                "retention_days": days, "universe_retention_days": uni_days}
    except Exception as exc:
        db.rollback()
        logger.warning("[nbbo_tape] prune failed: %s", exc)
        return {"ok": False, "error": str(exc)[:120]}


def tape_running_up_symbols(db: Session, *, now_utc: Optional[datetime] = None) -> list[str]:
    """Ross's "Running Up" scanner, rebuilt from our own NBBO tape: symbols whose
    MID price burst over the last few minutes — regardless of day change.

    Why (2026-06-11, the SKYQ gap): the viability refresh batch ranks DAY-change
    movers (a Top Gainers clone), so a name spiking NOW from a flat day (SKYQ:
    +2% on the day yet 2,163% 5-min RVOL, firing a textbook pullback break on
    Ross's Running Up scanner) never earns a fresh viability row and can never
    arm. The tape already samples every Ross-universe name each minute — this
    reads the burst straight off it. Selection-batch feeder ONLY: every real
    gate (viability, probes, spread, belts) still runs downstream.

    Returns burst symbols, fastest first, bounded by the max-symbols knob.
    Best-effort: returns [] on any failure.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    lookback_min = float(getattr(settings, "chili_momentum_running_up_lookback_min", 5.0) or 5.0)
    min_pct = float(getattr(settings, "chili_momentum_running_up_min_pct", 3.0) or 3.0)
    max_symbols = int(getattr(settings, "chili_momentum_running_up_max_symbols", 6) or 6)
    _MIN_SAMPLES = 3  # one stale print must not read as a burst
    try:
        rows = db.execute(
            text(
                "WITH recent AS ("
                "  SELECT symbol, mid,"
                "         row_number() OVER (PARTITION BY symbol ORDER BY observed_at ASC) rn_a,"
                "         row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) rn_d,"
                "         count(*) OVER (PARTITION BY symbol) n"
                "  FROM momentum_nbbo_spread_tape"
                "  WHERE observed_at >= :since AND mid > 0 AND symbol NOT LIKE '%-USD'"
                ") "
                "SELECT a.symbol, a.mid AS first_mid, d.mid AS last_mid "
                "FROM recent a JOIN recent d ON d.symbol = a.symbol AND d.rn_d = 1 "
                "WHERE a.rn_a = 1 AND a.n >= :min_n"
            ),
            {
                "since": (now_utc.replace(tzinfo=None) - timedelta(minutes=lookback_min)),
                "min_n": _MIN_SAMPLES,
            },
        ).fetchall()
    except Exception as exc:
        logger.debug("[nbbo_tape] running-up read failed: %s", exc)
        return []
    bursts: list[tuple[float, str]] = []
    for sym, first_mid, last_mid in rows:
        try:
            f, l = float(first_mid), float(last_mid)
        except (TypeError, ValueError):
            continue
        if f <= 0:
            continue
        pct = (l - f) / f * 100.0
        if pct >= min_pct:
            bursts.append((pct, str(sym).upper()))
    bursts.sort(reverse=True)
    return [s for _, s in bursts[:max_symbols]]


def recent_spread_median_bps(
    db: Session, symbol: str, *, window_s: float, now_utc: Optional[datetime] = None,
) -> tuple[float, int] | None:
    """Median spread (bps) and sample count over the last ``window_s`` seconds of
    tape for ``symbol`` — the spread-STABILITY read (2026-06-11 INDP: a single
    clean BBO instant passed the gate inside an otherwise-hostile flickering
    spread regime; one snapshot is an opinion, the median is the market).
    Returns None on no data / failure (caller decides fail-open semantics)."""
    sym = str(symbol or "").strip().upper()
    if not sym or window_s <= 0:
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        row = db.execute(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps), count(*) "
                "FROM momentum_nbbo_spread_tape "
                "WHERE symbol = :s AND spread_bps IS NOT NULL AND observed_at >= :since"
            ),
            {"s": sym, "since": now_utc.replace(tzinfo=None) - timedelta(seconds=float(window_s))},
        ).fetchone()
    except Exception as exc:
        logger.debug("[nbbo_tape] spread stability read failed: %s", exc)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0]), int(row[1] or 0)


def read_spread_profile(
    db: Session, symbol: str, *, day: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Replay reader: a name's recorded intraday spread samples (optionally for one
    UTC date, 'YYYY-MM-DD'), oldest first. Empty when the tape has no data yet."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    sql = ("SELECT observed_at, bid, ask, mid, spread_bps, day_volume "
           "FROM momentum_nbbo_spread_tape WHERE symbol = :s")
    params: dict[str, Any] = {"s": sym}
    if day:
        sql += " AND observed_at::date = :d"
        params["d"] = day
    sql += " ORDER BY observed_at ASC"
    try:
        rows = db.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("[nbbo_tape] read_spread_profile failed: %s", exc)
        return []
