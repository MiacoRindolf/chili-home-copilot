"""Activation hook: refresh momentum intelligence into BrainNodeState."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainActivationEvent
from ..brain_neural_mesh.repository import get_or_create_state
from ..brain_neural_mesh.schema import mesh_enabled

from .context import build_momentum_regime_context


from .evolution import record_evolution_trace
from .features import ExecutionReadinessFeatures
from .telemetry import log_tick
from .variants import iter_momentum_families
from .viability import score_viability
from .viability_scope import VIABILITY_SCOPE_AGGREGATE, VIABILITY_SCOPE_SYMBOL

HUB_NODE_ID = "nm_momentum_crypto_intel"
VIABILITY_NODE_ID = "nm_momentum_viability_pool"

_log = logging.getLogger(__name__)


def _symbol_country(symbol: str) -> str | None:
    """HQ country from the yfinance fundamentals CACHE — cache-only read (the
    viability tick must never block on network); a miss fires a background
    fetch so the NEXT tick has it. Crypto/aggregate/blank -> None."""
    s = (symbol or "").strip().upper()
    if not s or s.endswith("-USD") or s == "__AGGREGATE__":
        return None
    try:
        from ...yf_session import _cache_get as _yf_cache_get

        f = _yf_cache_get(f"fund:{s}")
        if isinstance(f, dict):
            c = f.get("country")
            return str(c) if c else None
    except Exception:
        return None
    try:
        import threading

        from ...yf_session import get_fundamentals

        threading.Thread(target=get_fundamentals, args=(s,), daemon=True).start()
    except Exception:
        pass
    return None


def _live_book_imbalance(symbol: str, db: Any = None) -> float | None:
    """Signed order-book imbalance in [-1, 1] from the LIVE venue feeds.

    Crypto: Coinbase Advanced WS ``level2`` ring buffer (true book depth — top
    bid total vs ask total). Equities, best-first: a FRESH multi-venue Level 2
    snapshot from the IQFeed depth bridge (host-side daemon writing
    ``iqfeed_depth_snapshots``; scripts/iqfeed_depth_bridge.py), else the
    Massive WS NBBO displayed sizes (the L1 depth the RH rail can see).
    Positive = bid-heavy (supportive for the long-only lane) — the sign
    convention viability's Phase 4a microstructure rules already score
    (>0.12 boost, <-0.18 penalty + warning). None when no live feed covers
    the symbol (behavior unchanged).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None
    try:
        if s.endswith("-USD"):
            from ..microstructure import get_features

            r = get_features(s).bid_ask_imbalance  # ratio: >1 = bid-heavy
            if r is None or float(r) <= 0:
                return None
            r = float(r)
            return round((r - 1.0) / (r + 1.0), 4)
        if db is not None:
            # True 5-level multi-venue depth (fail-open: missing table / stale
            # bridge -> fall through to the L1 sizes below)
            try:
                from sqlalchemy import text as _sql

                row = db.execute(
                    _sql(
                        "SELECT imbalance5 FROM iqfeed_depth_snapshots "
                        "WHERE symbol = :s AND observed_at > (now() at time zone 'utc') - interval '15 seconds' "
                        "ORDER BY observed_at DESC LIMIT 1"
                    ),
                    {"s": s},
                ).fetchone()
                if row is not None and row[0] is not None:
                    return float(row[0])
            except Exception:
                pass
        from ...massive_client import get_ws_quote

        q = get_ws_quote(s)
        if q is None or not q.bid_size or not q.ask_size:
            return None
        tot = float(q.bid_size) + float(q.ask_size)
        if tot <= 0:
            return None
        return round((float(q.bid_size) - float(q.ask_size)) / tot, 4)
    except Exception:
        return None


def _compute_ofi_micro(
    seq: list[tuple[float, float, float, float]],
) -> tuple[float | None, float | None]:
    """OFI (normalized net directional flow in [-1, 1]) + micro-price edge (bps)
    from a time-ordered sequence of ``(bid_px, bid_sz, ask_px, ask_sz)`` best
    quotes (oldest first).

    OFI per Cont/Kukanov/Stoikov: at each step the bid contributes +new size if
    the bid price rose, -old size if it fell, the size delta if unchanged; the
    ask mirrors with the demand sign (ask retreating up = buying pressure).
    Normalized by gross flow so it is bounded and scale-free (no magic depth
    constant). Micro-price (Stoikov) edge from the latest quote.
    """
    if not seq:
        return None, None
    pb, qb, pa, qa = seq[-1]
    micro_edge: float | None = None
    mid = (pb + pa) / 2.0
    denom = qb + qa
    if mid > 0 and denom > 0:
        micro = (pa * qb + pb * qa) / denom
        micro_edge = round((micro - mid) / mid * 10000.0, 2)
    if len(seq) < 2:
        return None, micro_edge
    ofi = 0.0
    gross = 0.0
    for (pb0, qb0, pa0, qa0), (pb1, qb1, pa1, qa1) in zip(seq, seq[1:]):
        if pb1 > pb0:
            eb = qb1
        elif pb1 < pb0:
            eb = -qb0
        else:
            eb = qb1 - qb0
        if pa1 < pa0:
            ea = qa1
        elif pa1 > pa0:
            ea = -qa0
        else:
            ea = qa1 - qa0
        ofi += eb - ea
        gross += abs(eb) + abs(ea)
    if gross <= 0:
        return 0.0, micro_edge
    return round(max(-1.0, min(1.0, ofi / gross)), 4), micro_edge


def _live_ofi_microprice(
    symbol: str, db: Any = None, as_of: "datetime | None" = None
) -> tuple[float | None, float | None]:
    """Live OFI (normalized [-1, 1]) + micro-price edge (bps) for a symbol.

    Crypto: from the in-process Coinbase full-book ring (``recent()`` window of
    top-of-book snapshots). Equity: from the IQFeed depth-bridge time series
    (``iqfeed_depth_snapshots`` best bid/ask price+size). Returns ``(None, None)``
    when no live sequence covers the symbol (then no tilt — behavior unchanged).

    OFI is the strongest L2 short-horizon predictor in the literature
    (Cont/Kukanov/Stoikov 2014; portable across cryptos), micro-price the
    confirmer (Stoikov). Wired as a SMALL agreement-guarded viability tilt and
    validated by live A/B (the literature edge is contemporaneous + may sit near
    Coinbase fees, so net incremental alpha is proven LIVE, not assumed).

    ``as_of`` (UTC-naive) reads L2 AS-OF a historical instant for the replay
    instrument — the window becomes ``(as_of - w, as_of]`` instead of trailing
    ``now()``, and the in-process ring (which has no history) is skipped so the
    durable table is the sole source. ``as_of=None`` is the LIVE default and emits
    the EXACT original SQL (literal ``now()``, no upper bound) → byte-identical.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None, None
    if as_of is not None and getattr(as_of, "tzinfo", None) is not None:
        as_of = as_of.replace(tzinfo=None)  # naive UTC for the naive snapshot columns
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    try:
        if s.endswith("-USD"):
            # Crypto L2: prefer the in-process Coinbase book ring (sub-second), and
            # FALL BACK to the durable, cross-process ``fast_orderbook`` table when the
            # ring is empty in THIS process. The ring is per-process: a held name not in
            # this process's subscription set has 0 ring snapshots → ofi=None → the exit
            # lock can never fire (JASMY-USD logged 89 armed ticks at None as a real
            # +2.3R winner). The table is fed by the crypto L2 drain and is always
            # populated for any subscribed name, mirroring how the equity branch reads
            # iqfeed_depth_snapshots. Crypto-GATED: the equity path NEVER reads
            # fast_orderbook (equity decisions stay isolated from crypto rows).
            seq: list[tuple[float, float, float, float]] = []
            if as_of is None:
                # in-process ring has no replay history → table-only when as_of is set
                try:
                    from ..microstructure import get_book_buffer

                    snaps = get_book_buffer().recent(s, window_secs=window)
                    seq = [
                        (snap.bids[0].price, snap.bids[0].size, snap.asks[0].price, snap.asks[0].size)
                        for snap in snaps
                        if snap.bids and snap.asks
                    ]
                except Exception:
                    seq = []
                if seq:
                    return _compute_ofi_micro(seq)
            # ring empty in this process (or replaying) → durable table fallback (top-of-book)
            if db is None:
                return None, None
            from sqlalchemy import text as _sql

            if as_of is None:
                _q = (
                    "SELECT bid_levels, ask_levels FROM fast_orderbook "
                    "WHERE ticker = :s AND snapshot_at > "
                    "(now() at time zone 'utc') - make_interval(secs => :w) "
                    "ORDER BY snapshot_at ASC"
                )
                _p = {"s": s, "w": window}
            else:
                _q = (
                    "SELECT bid_levels, ask_levels FROM fast_orderbook "
                    "WHERE ticker = :s AND snapshot_at > :as_of - make_interval(secs => :w) "
                    "AND snapshot_at <= :as_of ORDER BY snapshot_at ASC"
                )
                _p = {"s": s, "w": window, "as_of": as_of}
            rows = db.execute(_sql(_q), _p).fetchall()
            for r in rows:
                bl, al = r[0], r[1]
                try:
                    if bl and al:
                        seq.append((float(bl[0][0]), float(bl[0][1]), float(al[0][0]), float(al[0][1])))
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            return _compute_ofi_micro(seq)
        if db is not None:
            from sqlalchemy import text as _sql

            if as_of is None:
                _q = (
                    "SELECT bid_top, bid_top_size, ask_top, ask_top_size "
                    "FROM iqfeed_depth_snapshots "
                    "WHERE symbol = :s AND observed_at > "
                    "(now() at time zone 'utc') - make_interval(secs => :w) "
                    "ORDER BY observed_at ASC"
                )
                _p = {"s": s, "w": window}
            else:
                _q = (
                    "SELECT bid_top, bid_top_size, ask_top, ask_top_size "
                    "FROM iqfeed_depth_snapshots "
                    "WHERE symbol = :s AND observed_at > :as_of - make_interval(secs => :w) "
                    "AND observed_at <= :as_of ORDER BY observed_at ASC"
                )
                _p = {"s": s, "w": window, "as_of": as_of}
            rows = db.execute(_sql(_q), _p).fetchall()
            seq = [
                (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                for r in rows
                if None not in (r[0], r[1], r[2], r[3])
            ]
            return _compute_ofi_micro(seq)
    except Exception:
        return None, None
    return None, None


def _live_trade_flow(
    symbol: str, db: Any = None, as_of: "datetime | None" = None
) -> float | None:
    """Live TRADE-FLOW: signed-volume AGGRESSOR imbalance in [-1, 1] over the short OFI window.

    The research's #2 micro signal (Ross's "ask getting eaten" = aggressive buying = real thrust),
    distinct from OFI (book flow) and book_imbalance (book state) — this is the TAPE.
      Equity: from ``iqfeed_trade_ticks`` (the IQFeed L1 trade-tape bridge). Each trade is aggressor-
        classified by the QUOTE RULE (Lee-Ready: px>=ask -> buy +1, px<=bid -> sell -1, else mid
        split) when the prevailing bid/ask is present, else the TICK RULE (px vs the prior trade;
        zero-tick carries the prior sign). imbalance = Σ(sign·size) / Σ(size).
      Crypto: from the in-process microstructure tape (``trade_aggression`` = buy_vol/total in [0,1])
        re-centered to 2·aggr-1 in [-1, 1].
    Returns ``None`` when no live tape covers the symbol (then the feature is simply absent). ``as_of``
    reads the tape AS-OF a historical instant for the replay instrument (window ``(as_of-w, as_of]``);
    ``as_of=None`` is the LIVE default (trailing ``now()``)."""
    s = (symbol or "").strip().upper()
    if not s:
        return None
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    # crypto: in-process microstructure tape (no historical as_of -> live ring only)
    if s.endswith("-USD"):
        if as_of is not None:
            return None
        try:
            from ..microstructure import get_features

            aggr = getattr(get_features(s), "trade_aggression", None)
            if aggr is not None:
                return float(max(-1.0, min(1.0, 2.0 * float(aggr) - 1.0)))
        except Exception:
            return None
        return None
    # equity: the IQFeed L1 trade-tape table (defensive — absent table/rows -> None)
    if db is None:
        return None
    try:
        from sqlalchemy import text as _sql

        if as_of is None:
            q = ("SELECT price, size, bid, ask FROM iqfeed_trade_ticks WHERE symbol = :s AND "
                 "observed_at > (now() at time zone 'utc') - make_interval(secs => :w) ORDER BY observed_at ASC")
            p = {"s": s, "w": window}
        else:
            _ao = as_of.replace(tzinfo=None) if getattr(as_of, "tzinfo", None) is not None else as_of
            q = ("SELECT price, size, bid, ask FROM iqfeed_trade_ticks WHERE symbol = :s AND "
                 "observed_at > :as_of - make_interval(secs => :w) AND observed_at <= :as_of ORDER BY observed_at ASC")
            p = {"s": s, "w": window, "as_of": _ao}
        rows = db.execute(_sql(q), p).fetchall()
    except Exception:
        return None
    return _aggressor_imbalance(rows)


def _aggressor_imbalance(rows) -> float | None:
    """Pure: signed-volume aggressor imbalance in [-1,1] from oldest-first ``(price, size, bid, ask)``
    trades. QUOTE RULE (Lee-Ready) when bid/ask present, TICK RULE fallback (zero-tick carries prior
    sign). ``None`` on empty/zero-volume. Separated for lookahead-free unit testing (no DB)."""
    if not rows:
        return None
    signed = 0.0
    total = 0.0
    prev_px = None
    last_sign = 0
    for r in rows:
        try:
            px = float(r[0])
            sz = float(r[1])
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        bid, ask = r[2], r[3]
        sign = 0
        if bid is not None and ask is not None:
            try:
                fb, fa = float(bid), float(ask)
            except (TypeError, ValueError):
                fb = fa = 0.0
            if fa > fb > 0:
                mid = (fa + fb) / 2.0
                if px >= fa:
                    sign = 1
                elif px <= fb:
                    sign = -1
                elif px > mid:
                    sign = 1
                elif px < mid:
                    sign = -1
        if sign == 0:                                   # tick-rule fallback
            if prev_px is not None and px != prev_px:
                sign = 1 if px > prev_px else -1
            else:
                sign = last_sign                        # zero-tick / first trade carries prior sign
        prev_px = px
        if sign != 0:
            last_sign = sign
        signed += sign * sz
        total += sz
    if total <= 0:
        return None
    return float(max(-1.0, min(1.0, signed / total)))


@dataclass(frozen=True)
class LadderRead:
    """Multi-level L2 distribution read for the proactive sell-into-strength exit.

    Every field fail-safe to ``None``; a ``None`` in any REQUIRED slot (depth_imbal,
    depth_imbal_pctile, ofi, micro_edge) keeps the exit state machine in HOLD — it
    never sells on missing/stale data. Crypto-only (``fast_orderbook`` is ``-USD``)."""

    depth_imbal: float | None          # (Σbid5 − Σask5)/(Σbid5 + Σask5) ∈ [-1,1], NEWEST snap
    depth_imbal_pctile: float | None   # where NEWEST imbalance sits in the K-window [0,1]
    ofi: float | None                  # order-flow imbalance (Cont), in-process/table
    micro_edge: float | None           # micro-price edge (bps, Stoikov)
    bid_refill: float | None           # Δ(best-bid size)/prior across the window
    ask_build: float | None            # Δ(Σask5)/prior across the window (wall building)
    spread_bps: float | None
    snapshot_age_s: float | None       # now − newest snapshot_at (staleness gate)
    n_snaps: int                       # rows actually parsed


def _depth_imbal5(bl: Any, al: Any) -> float | None:
    """5-level depth imbalance from [[price,size],…] tuple ladders. None if empty."""
    try:
        b = sum(float(l[1]) for l in bl[:5])
        a = sum(float(l[1]) for l in al[:5])
    except (TypeError, ValueError, IndexError):
        return None
    return (b - a) / (b + a) if (b + a) > 0 else None


def read_ladder_distribution(
    symbol: str, db: Any = None, *, k: int = 6, as_of: "datetime | None" = None
) -> LadderRead:
    """Multi-level L2 distribution read for the proactive sell-into-strength exit.
    CLASS-AWARE: crypto (``-USD``) reads the per-level ``fast_orderbook`` table; equities
    read the aggregate 5-level ``iqfeed_depth_snapshots`` (bid5_size/ask5_size/imbalance5
    — no per-level arrays, but the aggregate is enough for the distribution read). Both
    pair with the OFI/micro read. K newest snapshots in a 30s window, newest-first.
    Fail-open: any miss → ``None`` fields → the caller HOLDs (never sells on bad data).

    ``as_of`` (UTC-naive) reads AS-OF a historical instant for the replay instrument
    (window ``(as_of - 30s, as_of]``, age relative to ``as_of``); ``as_of=None`` is the
    LIVE default and emits the EXACT original SQL → byte-identical."""
    _NULL = LadderRead(None, None, None, None, None, None, None, None, 0)
    s = (symbol or "").strip().upper()
    if not s or db is None:
        return _NULL
    if as_of is not None and getattr(as_of, "tzinfo", None) is not None:
        as_of = as_of.replace(tzinfo=None)
    ofi, micro = None, None
    try:
        ofi, micro = _live_ofi_microprice(s, db=db, as_of=as_of)
    except Exception:
        pass
    if s.endswith("-USD"):
        return _ladder_crypto(s, db, int(k), ofi, micro, as_of=as_of)
    return _ladder_equity(s, db, int(k), ofi, micro, as_of=as_of)


def _ladder_crypto(
    s: str, db: Any, k: int, ofi: float | None, micro: float | None,
    as_of: "datetime | None" = None,
) -> LadderRead:
    """Crypto ladder from ``fast_orderbook`` — per-level JSONB ``[[price,size],…]``."""
    try:
        from sqlalchemy import text as _sql

        if as_of is None:
            _q = (
                "SELECT snapshot_at, bid_levels, ask_levels, spread_bps "
                "FROM fast_orderbook WHERE ticker = :s AND snapshot_at > "
                "(now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY snapshot_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k)}
        else:
            _q = (
                "SELECT snapshot_at, bid_levels, ask_levels, spread_bps "
                "FROM fast_orderbook WHERE ticker = :s "
                "AND snapshot_at > :as_of - make_interval(secs => :w) "
                "AND snapshot_at <= :as_of ORDER BY snapshot_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k), "as_of": as_of}
        rows = db.execute(_sql(_q), _p).fetchall()
    except Exception:
        rows = []
    if not rows:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    import json as _json

    series = []  # newest-first: (snapshot_at, bid_levels, ask_levels, spread_bps)
    for r in rows:
        try:
            bl = _json.loads(r[1]) if isinstance(r[1], str) else r[1]
            al = _json.loads(r[2]) if isinstance(r[2], str) else r[2]
            if isinstance(bl, list) and isinstance(al, list) and bl and al:
                series.append((r[0], bl, al, r[3]))
        except Exception:
            continue
    if not series:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    newest, oldest = series[0], series[-1]
    imb_now = _depth_imbal5(newest[1], newest[2])
    imbs = [v for v in (_depth_imbal5(b, a) for _, b, a, _ in series) if v is not None]
    pctile = None
    if imb_now is not None and len(imbs) >= 3:
        # Low percentile ⇒ NEWEST book is ask-heavy relative to its own recent window
        # (a TREND of distribution, not an absolute threshold a single spoof can trip).
        pctile = sum(1 for v in imbs if v <= imb_now) / float(len(imbs))

    def _bb(lv: Any) -> float:
        try:
            return float(lv[0][1])
        except (TypeError, ValueError, IndexError):
            return 0.0

    def _sa5(lv: Any) -> float:
        try:
            return sum(float(x[1]) for x in lv[:5])
        except (TypeError, ValueError, IndexError):
            return 0.0

    bb_old = _bb(oldest[1])
    bid_refill = (_bb(newest[1]) - bb_old) / bb_old if bb_old > 0 else None
    a_old = _sa5(oldest[2])
    ask_build = (_sa5(newest[2]) - a_old) / a_old if a_old > 0 else None
    try:
        age = max(0.0, ((as_of or datetime.utcnow()) - newest[0]).total_seconds())
    except Exception:
        age = None
    try:
        spread = float(newest[3]) if newest[3] is not None else None
    except (TypeError, ValueError):
        spread = None
    return LadderRead(imb_now, pctile, ofi, micro, bid_refill, ask_build, spread, age, len(series))


def _eq_imbalance5(row: Any) -> float | None:
    """5-level imbalance for an iqfeed row: prefer the precomputed ``imbalance5``;
    else derive from the aggregate 5-level sizes (bid5_size − ask5_size)/(sum)."""
    try:
        if row[7] is not None:
            return float(row[7])
    except (TypeError, ValueError, IndexError):
        pass
    try:
        b = float(row[5] or 0.0)
        a = float(row[6] or 0.0)
    except (TypeError, ValueError, IndexError):
        return None
    return (b - a) / (b + a) if (b + a) > 0 else None


def _ladder_equity(
    s: str, db: Any, k: int, ofi: float | None, micro: float | None,
    as_of: "datetime | None" = None,
) -> LadderRead:
    """Equity ladder from ``iqfeed_depth_snapshots`` — aggregate 5-level (bid5_size,
    ask5_size, imbalance5) + top-of-book (bid_top/ask_top + sizes). No per-level arrays,
    but the aggregate carries the distribution signal. Populated market-hours by the
    iqfeed depth bridge → ``None`` (HOLD) overnight / pre-RTH before data flows."""
    try:
        from sqlalchemy import text as _sql

        if as_of is None:
            _q = (
                "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
                "bid5_size, ask5_size, imbalance5 FROM iqfeed_depth_snapshots "
                "WHERE symbol = :s AND observed_at > "
                "(now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY observed_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k)}
        else:
            _q = (
                "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
                "bid5_size, ask5_size, imbalance5 FROM iqfeed_depth_snapshots "
                "WHERE symbol = :s AND observed_at > :as_of - make_interval(secs => :w) "
                "AND observed_at <= :as_of ORDER BY observed_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k), "as_of": as_of}
        rows = db.execute(_sql(_q), _p).fetchall()
    except Exception:
        rows = []
    if not rows:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    series = list(rows)  # newest-first
    newest, oldest = series[0], series[-1]
    imb_now = _eq_imbalance5(newest)
    imbs = [v for v in (_eq_imbalance5(r) for r in series) if v is not None]
    pctile = None
    if imb_now is not None and len(imbs) >= 3:
        pctile = sum(1 for v in imbs if v <= imb_now) / float(len(imbs))

    def _f(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    bbo = _f(oldest[3])  # oldest bid_top_size — best-bid refill across the window
    bbn = _f(newest[3])
    bid_refill = (bbn - bbo) / bbo if (bbo and bbo > 0 and bbn is not None) else None
    a_old = _f(oldest[6])  # oldest ask5_size — ask-side build across the window
    a_new = _f(newest[6])
    ask_build = (a_new - a_old) / a_old if (a_old and a_old > 0 and a_new is not None) else None
    bt, at = _f(newest[1]), _f(newest[2])  # top-of-book spread (no spread col in iqfeed)
    spread = (at - bt) / ((at + bt) / 2.0) * 10_000.0 if (bt and at and (bt + at) > 0) else None
    try:
        age = max(0.0, ((as_of or datetime.utcnow()) - newest[0]).total_seconds())
    except Exception:
        age = None
    return LadderRead(imb_now, pctile, ofi, micro, bid_refill, ask_build, spread, age, len(series))


def maybe_run_momentum_neural_tick(
    db: Session,
    ev: BrainActivationEvent,
    *,
    graph_version: int = 1,
) -> None:
    """Run tick when activation event is a momentum context refresh."""
    if not settings.chili_momentum_neural_enabled:
        return
    if not mesh_enabled():
        return
    pl = ev.payload if isinstance(ev.payload, dict) else {}
    if ev.cause != "momentum_context_refresh" and pl.get("signal_type") != "momentum_context_refresh":
        return
    meta = pl.get("meta") if isinstance(pl.get("meta"), dict) else {}
    run_momentum_neural_tick(
        db,
        meta=meta,
        correlation_id=ev.correlation_id,
        graph_version=graph_version,
    )


def run_momentum_neural_tick(
    db: Session,
    *,
    meta: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    graph_version: int = 1,
) -> dict[str, Any]:
    """Compute regime + family viability; persist on hub and viability pool nodes."""
    _ = graph_version
    meta = dict(meta or {})
    tickers = meta.get("tickers")
    if isinstance(tickers, list) and tickers:
        symbols = [str(t).strip().upper() for t in tickers if t][:32]
        scope = VIABILITY_SCOPE_SYMBOL
    else:
        symbols = ["__aggregate__"]
        scope = VIABILITY_SCOPE_AGGREGATE

    # Phase 6c: optional Hurst proxy from first symbol's recent closes (feeds regime context).
    if symbols and symbols[0].upper() != "__AGGREGATE__":
        try:
            from ..market_data import fetch_ohlcv_df

            from .entry_gates import hurst_proxy_from_closes

            df_h = fetch_ohlcv_df(symbols[0], interval="15m", period="5d")
            if df_h is not None and not df_h.empty and "Close" in df_h.columns:
                meta["hurst_proxy"] = hurst_proxy_from_closes(df_h["Close"])
        except Exception:
            pass

    # Ross momentum-quality (M2): the scanner bridge forwards the RVOL/gap/
    # daily-change/float signals it computed as meta["ross_signals"] instead of
    # discarding them. Rank the batch once here and pass each symbol's [0,1]
    # quality through ctx_meta below so score_viability prefers EXPLOSIVE
    # instruments. Strict no-op when absent.
    _ross_signals = meta.get("ross_signals")
    if isinstance(_ross_signals, dict) and _ross_signals:
        try:
            from .ross_momentum import ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
            from .ross_momentum import score_universe as _ross_score_universe

            # Liquidity-BIASED weights: prefer movers the lane can actually FILL
            # (dollar turnover -> tighter spread), not only the most explosive
            # names that get spread-gated and only ever watched. Validated on the
            # 11-day previous-days A/B replay: +6 fills, +$914 PnL vs baseline
            # (scripts/_sim_liquidity_selection.py, 2026-06-10).
            _weights = ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
            # REAL-FLOAT enrichment (GAP 1, off => byte-identical): the low-float pillar
            # wants a SHARE COUNT but without a producer it falls back to market_cap ($,
            # wrong units, price-contaminated). Inject the real float (share count) from
            # the reference endpoint; for names it can't resolve, use a CONSISTENT
            # share-count estimate (market_cap / price) so the pillar never MIXES
            # share-count and $ across names (which would corrupt the -log10 ranking).
            # Cached + fail-open. Kill-switch chili_momentum_use_real_float=False.
            if bool(getattr(settings, "chili_momentum_use_real_float", True)):
                try:
                    from ...massive_client import get_ticker_float

                    def _pick_num(_d, _keys):
                        for _k in _keys:
                            _v = _d.get(_k)
                            if _v is not None:
                                try:
                                    return float(_v)
                                except (TypeError, ValueError):
                                    continue
                        return None

                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or "-USD" in str(_sym):
                            continue  # equities only
                        try:
                            _f = get_ticker_float(_sym)
                            if _f and _f > 0:
                                _sig["float_shares"] = float(_f)
                                continue
                            _mc = _pick_num(_sig, ("market_cap", "marketcap"))
                            _px = _pick_num(_sig, ("price", "last", "close", "last_price"))
                            if _mc and _px and _px > 0:
                                _sig["float_shares"] = _mc / _px
                        except Exception:
                            continue
                except Exception:
                    pass
            # Daily-chart context TILT (off => byte-identical): enrich each candidate
            # with daily_structure_pct (break ABOVE a major daily level + room to the
            # next level + soft trend) and switch to the 5-pillar weights. The daily
            # fetch is cached (600s) and runs on the viability-refresh pass, NOT the
            # live tick path. A breaking-spike scores HIGH (the CUPR guarantee), so the
            # tilt PREFERS clean daily breakouts; it can never block a fill.
            if bool(getattr(settings, "chili_momentum_daily_context_enabled", True)):
                try:
                    from .ross_momentum import ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT
                    from .daily_levels import compute_daily_context
                    from ..market_data import fetch_ohlcv_df as _fetch_daily

                    _lb = int(getattr(settings, "chili_momentum_daily_lookback_days", 20) or 20)
                    _n_daily = 0
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or "-USD" in str(_sym):
                            continue  # equities only; crypto has no daily-S&R regime here
                        try:
                            _px = None
                            for _k in ("price", "last", "close", "last_price"):
                                _v = _sig.get(_k)
                                if _v is not None:
                                    _px = float(_v)
                                    break
                            _ddf = _fetch_daily(_sym, interval="1d", period="1y")
                            _dctx = compute_daily_context(_ddf, lookback=_lb, price=_px)
                            if _dctx.daily_structure_pct is not None:
                                _sig["daily_structure_pct"] = _dctx.daily_structure_pct
                                _sig["daily_breaking_major"] = bool(_dctx.breaking_major_level)
                                _n_daily += 1
                        except Exception:
                            continue
                    if _n_daily > 0:
                        _weights = ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT
                except Exception:
                    pass
            meta["ross_scores"] = {
                s: rs.score
                for s, rs in _ross_score_universe(_ross_signals, weights=_weights).items()
            }
        except Exception:
            pass
        # Ross gap #3: absolute RVOL/change FLOOR on top of the within-batch percentile.
        # On a dull tape the best-of-a-dull-batch still percentile-ranks #1 and would arm
        # a non-explosive name; Ross's rule is that <~5x RVOL or <~10% up is simply not a
        # setup. Mark the EQUITY symbols (crypto 24h semantics differ) that fail the floor
        # so score_viability can drop them from LIVE eligibility (pool membership +
        # paper/scoring untouched). Compact symbol list — does not bloat the persisted
        # ctx_meta the way the full signal dict would.
        try:
            from .ross_momentum import below_explosive_floor as _below_floor

            meta["ross_below_floor"] = sorted(
                s
                for s, sig in _ross_signals.items()
                if isinstance(sig, dict)
                and not str(s).upper().endswith("-USD")
                and _below_floor(sig)
            )
        except Exception:
            pass

        # Ross gap #4: sympathy/theme cluster. The day's EQUITY movers cluster by SIC
        # sector; a sector whose LEADER is a big % gainer drags its peers (the "hot potato"
        # sympathy run = the STI/ASTC-class moves). Resolve sectors for the strongest
        # in-play equity movers (cached process-lifetime; bounded so a cold start can't
        # flood the rate limiter), cluster, and forward the peer set so viability tilts the
        # sympathy longs. Equity-only (crypto has no SIC sector) -> a no-op on the
        # crypto-only lane (no equity movers -> no fetches). Fail-open.
        try:
            from ...massive_client import get_ticker_sector
            from .catalyst import sympathy_peer_symbols
            from .ross_momentum import _extract_pillars

            _eq: list[tuple[str, float]] = []
            for s, sig in _ross_signals.items():
                su = str(s).upper()
                if su.endswith("-USD") or not isinstance(sig, dict):
                    continue
                _, _mom, _, _ = _extract_pillars(sig)
                if _mom is not None:
                    _eq.append((su, float(_mom)))
            _eq.sort(key=lambda x: x[1], reverse=True)
            # Ross gap #6: market-wide leading-gainer tilt — only the top few % gainers get
            # the broker hot-lists / eyes that make a pattern resolve; a perfect pattern on
            # a non-obvious name is a tree falling in an empty forest. The top-N equity
            # movers by change get a small additive viability boost (Ross's "top 3-5"). The
            # eligibility floor (#3) gates membership; this orders WITHIN it. Equity-only.
            if _eq:
                meta["top_market_gainers"] = sorted(su for su, _ in _eq[:5])
            _eq = _eq[:40]  # only the strongest movers cluster; bounds cold-start fetches
            _movers = {su: mom for su, mom in _eq}
            _peers = sympathy_peer_symbols(_movers, {su: get_ticker_sector(su) for su in _movers})
            if _peers:
                meta["sympathy_symbols"] = sorted(_peers)
        except Exception:
            pass

        # Ross gap #16: dilution-risk penalty. A recent S-1/424B* (registration / offering)
        # filing means the low-float will ISSUE SHARES and fade despite good news (CTNT vs
        # SNTI). Flag the equity movers with a recent dilution filing (SEC EDGAR, free;
        # per-ticker cached, bounded per pass) so viability PENALIZES them. Equity-only;
        # a no-op on the crypto-only lane (no equity movers -> no lookups). Fail-open.
        try:
            from .edgar import dilution_risk_symbols

            _eq_syms = [
                str(s).upper()
                for s, sig in _ross_signals.items()
                if isinstance(sig, dict) and not str(s).upper().endswith("-USD")
            ]
            _dil = dilution_risk_symbols(_eq_syms)
            if _dil:
                meta["dilution_symbols"] = sorted(_dil)
        except Exception:
            pass

        # Re-analysis survivor S1: cross-day close-strength prior. A name that closed near
        # its HOD (and green) into the power hour gap-continues the next day — get warm on
        # it BEFORE the tape forms (the proven "right name early" bottleneck). Compute the
        # prior for the equity movers (bounded daily-bar reads, cached) -> viability tilt.
        # Equity-only -> a no-op on the crypto-only lane. Fail-open.
        try:
            from .catalyst import close_strength_priors

            _csp = close_strength_priors(_eq_syms)
            if _csp:
                meta["close_strength_priors"] = _csp
        except Exception:
            pass

    # E5: news-catalyst set (EARNINGS + fresh general NEWS headlines) for the catalyst
    # viability tilt. The fresh-news union is what catches Ross's explosive sympathy/
    # theme movers (a low-float small-cap that just printed a hot headline), not just
    # scheduled earnings. Best-effort + cached; empty -> no-op (degrades gracefully
    # without the news/Benzinga feed). (catalyst.py)
    # Hot-tape regime + HQ countries for the REGIME-AWARE catalyst tilt (Ross
    # 06-10: in a hot tape the no-news foreign small caps run; news names fade).
    # Both best-effort: regime from the bridge's own signals (no fetch); country
    # from the yfinance fundamentals CACHE only (a miss fires a background fill
    # so the next tick has it — the viability tick never blocks on network).
    try:
        from .catalyst import hot_tape_regime

        meta["hot_tape"] = bool(hot_tape_regime(meta.get("ross_signals")))
    except Exception:
        pass
    try:
        _countries = {}
        for _s in symbols:
            _c = _symbol_country(_s)
            if _c:
                _countries[_s] = _c
        if _countries:
            meta["symbol_countries"] = _countries
    except Exception:
        pass

    try:
        from .catalyst import theme_catalyst_symbols

        _theme = theme_catalyst_symbols()
        if _theme:
            meta["theme_symbols"] = sorted(_theme)  # JSON-safe (regression guard #528)
    except Exception:
        pass

    try:
        from .catalyst import all_catalyst_symbols

        _cat = all_catalyst_symbols()
        if _cat:
            # MUST be a list, not a set: meta flows into the brain_node_states
            # local_state JSONB and a set is not JSON-serializable ("Object of type
            # set is not JSON serializable"), which would fail the ENTIRE viability
            # write and leave every symbol stale. (regression guard for #528)
            meta["catalyst_symbols"] = sorted(_cat)
    except Exception:
        pass

    # Gap #12: WEAK-catalyst de-boost set (dilution/compliance/legal headlines Ross
    # distrusts). Computed once per pass like catalyst_symbols; JSON-safe sorted list.
    try:
        from .catalyst import weak_catalyst_symbols

        _weak = weak_catalyst_symbols()
        if _weak:
            meta["weak_catalyst_symbols"] = sorted(_weak)
    except Exception:
        pass

    ctx_meta = {
        k: meta[k]
        for k in (
            "spread_regime",
            "fee_burden_regime",
            "liquidity_regime",
            "exhaustion_cooldown",
            "rolling_range_state",
            "breakout_continuity",
            "realized_vol_rank",
            "atr_pct",
            "hurst_proxy",
            "adx",
            "adx_14",
            "ross_scores",
            "ross_below_floor",
            "catalyst_symbols",
            "weak_catalyst_symbols",
            "sympathy_symbols",
            "top_market_gainers",
            "dilution_symbols",
            "close_strength_priors",
            "hot_tape",
            "symbol_countries",
            "theme_symbols",
        )
        if k in meta
    }
    ctx = build_momentum_regime_context(
        realized_vol_rank=meta.get("realized_vol_rank"),
        atr_pct=meta.get("atr_pct"),
        meta=ctx_meta,
    )
    feats = ExecutionReadinessFeatures.from_meta(meta)

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        # L2/order-flow utilization: viability's microstructure rules (book_imbalance
        # boost/penalty) were designed in Phase 4a but no producer ever filled the
        # field. Fill it per-symbol from the LIVE feeds now running in this process
        # (#596): Coinbase level2 ring buffer for crypto, Massive WS NBBO sizes for
        # equities. None (no feed for the symbol) -> unchanged behavior.
        _imb = _live_book_imbalance(sym, db=db)
        # Order-flow imbalance (Cont/Kukanov/Stoikov) + micro-price (Stoikov) edge
        # from the SAME live feeds, fed in as a small agreement-guarded viability
        # tilt (research: OFI is the strongest L2 short-horizon predictor).
        _ofi, _mpe = _live_ofi_microprice(sym, db=db)
        _overrides: dict[str, Any] = {}
        if _imb is not None:
            _overrides["book_imbalance"] = _imb
        if _ofi is not None:
            _overrides["ofi"] = _ofi
        if _mpe is not None:
            _overrides["micro_price_edge"] = _mpe
        sym_feats = replace(feats, **_overrides) if _overrides else feats
        # 10s-candle breakout tilt (Ross's 10s chart, LIVE + ON): a fresh ABCD/flat-top
        # BREAKOUT on a crypto name nudges its viability UP (small, bounded, kill-switch
        # chili_tenbeat_entry_tilt_weight). A fired breakout is bullish ⇒ naturally
        # agreement-guarded for the long-only lane. The detections keep accruing forward-
        # returns so the live A/B (realized-with-tilt vs counterfactual) keeps measuring;
        # revert with the kill-switch if the A/B turns negative.
        _tb_w = float(getattr(settings, "chili_tenbeat_entry_tilt_weight", 0.0) or 0.0)
        _tbk = None
        if _tb_w > 0:
            try:
                from ..fast_path.tenbeat_candle_log import latest_tenbeat_breakout
                _tbk = latest_tenbeat_breakout(sym, db)
            except Exception:
                _tbk = None
        for family in iter_momentum_families():
            vr = score_viability(sym, family, ctx, sym_feats, db=db)
            d = vr.to_public_dict()
            if _tb_w > 0 and _tbk is not None:
                d["viability"] = min(1.0, float(d.get("viability") or 0.0) + _tb_w * _tbk)
                d["tenbeat_breakout_tilt"] = round(_tb_w * _tbk, 4)
            d["scope"] = scope
            d["label"] = family.label
            d["entry_style"] = family.entry_style
            d["default_stop_logic"] = family.default_stop_logic
            d["default_exit_logic"] = family.default_exit_logic
            rows.append(d)

    rows.sort(key=lambda r: r["viability"], reverse=True)
    top = rows[0] if rows else {}

    now = datetime.utcnow().isoformat()
    hub_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "correlation_id": correlation_id,
        "regime": ctx.to_public_dict(),
        "symbols_evaluated": symbols,
        "top_preview": rows[:8],
    }
    viability_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "viability_rows": rows[:64],
        "correlation_id": correlation_id,
    }

    hub = get_or_create_state(db, HUB_NODE_ID)
    hub.local_state = hub_payload
    hub.last_activated_at = datetime.utcnow()
    hub.updated_at = datetime.utcnow()

    pool = get_or_create_state(db, VIABILITY_NODE_ID)
    pool.local_state = viability_payload
    pool.last_activated_at = datetime.utcnow()
    pool.updated_at = datetime.utcnow()

    record_evolution_trace(
        db,
        snapshot={
            "top_family_id": top.get("family_id"),
            "top_viability": top.get("viability"),
            "session_label": ctx.session_label,
        },
    )

    persistence_ok = True
    try:
        from .persistence import persist_neural_momentum_tick

        n = persist_neural_momentum_tick(
            db,
            row_dicts=rows,
            regime_snapshot=ctx.to_public_dict(),
            features=feats,
            correlation_id=correlation_id,
            source_node_id=HUB_NODE_ID,
        )
        if n:
            log_tick("persisted viability rows=%s", n)
    except Exception as e:
        _log.warning("[momentum_neural] viability persistence failed: %s", e)
        persistence_ok = False

    log_tick(
        "tick symbols=%s families=%s top=%s corr=%s",
        len(symbols),
        len(rows) // max(len(symbols), 1),
        top.get("family_id"),
        correlation_id,
    )
    return {"ok": True, "rows": len(rows), "top_family": top.get("family_id"), "persistence_ok": persistence_ok}
