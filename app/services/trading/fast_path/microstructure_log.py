"""Phase 1 (1a): LOG-ONLY L2 microstructure signal layer.

Computes microstructure signals (OFI, micro-price, ask-eaten, hidden-seller,
spoof, book imbalance) from the IN-PROCESS Coinbase book ring (zero DB reads in
the hot path) and persists them to ``trading_microstructure_log`` for a later
forward-return calibration phase. The forward-return columns are filled by a
SEPARATE matured-only backfill (Phase 1b) — they are NULL at insert.

DISCIPLINE: this touches NO decision path. Nothing here is read by entry /
viability / replay. It exists ONLY to accumulate labeled data so calibration can
prove which signals predict BEFORE any is wired (the -1.58pp falsified sub-bar
lesson). Crypto-only for now; the equity branch is gated off
(``chili_micro_log_equity_enabled``) until iqfeed coverage is aligned.

Partitioning is self-contained (ensure today's daily partition before insert;
prune drops partitions older than the retention window) so it does not depend on
the broader fast-path retention sweep — the Phase-0 lesson was that crypto rows
piled into the DEFAULT partition when the sweep didn't pre-create named ones.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from ..microstructure import get_book_buffer, get_features
from .crypto_l2_drain import _norm_imbalance, eligible_crypto_symbols

logger = logging.getLogger(__name__)

_INSERT_SQL = text(
    "INSERT INTO trading_microstructure_log ("
    "symbol, asset_class, observed_at, ingest_at, source, eligibility_state, "
    "viability_score, mid_price, micro_price, best_bid, best_ask, spread_bps, "
    "depth_bid_total, depth_ask_total, book_depth_levels, book_imbalance, "
    "microprice_edge_bps, ofi, ofi_window_s, snapshot_count, ask_eaten_events, "
    "ask_eaten_notional_usd, hidden_seller_score, spoof_score, sample_window_secs"
    ") VALUES ("
    ":symbol, :asset_class, :observed_at, :ingest_at, :source, :eligibility_state, "
    ":viability_score, :mid_price, :micro_price, :best_bid, :best_ask, :spread_bps, "
    ":depth_bid_total, :depth_ask_total, :book_depth_levels, :book_imbalance, "
    ":microprice_edge_bps, :ofi, :ofi_window_s, :snapshot_count, :ask_eaten_events, "
    ":ask_eaten_notional_usd, :hidden_seller_score, :spoof_score, :sample_window_secs"
    ")"
)

# Cache of daily partition keys already ensured this process-lifetime, so the
# hot drain path issues CREATE TABLE at most once per day (it is IF NOT EXISTS,
# but skipping the catalog round-trip keeps the 5s loop lean).
_ensured_days: set[str] = set()


def _snap_ts(snap) -> float:
    """Exchange event time when present, else local arrival."""
    return snap.event_ts if getattr(snap, "event_ts", None) else snap.ts


def _ofi(window) -> float:
    """Cont top-of-book order-flow imbalance summed over the window."""
    ofi = 0.0
    for prev, cur in zip(window, window[1:]):
        if not (prev.bids and prev.asks and cur.bids and cur.asks):
            continue
        pb, cb, pa, ca = prev.bids[0], cur.bids[0], prev.asks[0], cur.asks[0]
        if cb.price > pb.price:
            e_b = cb.size
        elif cb.price < pb.price:
            e_b = -pb.size
        else:
            e_b = cb.size - pb.size
        if ca.price < pa.price:
            e_a = ca.size
        elif ca.price > pa.price:
            e_a = -pa.size
        else:
            e_a = ca.size - pa.size
        ofi += e_b - e_a
    return ofi


def _ask_eaten(window) -> tuple[int, float]:
    """Count + notional of best-ask lifts (ask consumed -> best ask moves up)."""
    events = 0
    notional = 0.0
    for prev, cur in zip(window, window[1:]):
        if not (prev.asks and cur.asks):
            continue
        if cur.asks[0].price > prev.asks[0].price:
            events += 1
            notional += prev.asks[0].size * prev.asks[0].price
    return events, notional


def _hidden_seller(window) -> float:
    """Ask refills at an unchanged touch vs price advance — high = hidden supply
    (seller keeps replenishing the offer, capping the move)."""
    refill = 0.0
    price_adv_bps = 0.0
    for prev, cur in zip(window, window[1:]):
        if not (prev.asks and cur.asks):
            continue
        if cur.asks[0].price == prev.asks[0].price and cur.asks[0].size > prev.asks[0].size:
            refill += cur.asks[0].size - prev.asks[0].size
        elif cur.asks[0].price > prev.asks[0].price and prev.asks[0].price > 0:
            price_adv_bps += (cur.asks[0].price - prev.asks[0].price) / prev.asks[0].price * 1e4
    return refill / (price_adv_bps + 1.0)


def _spoof(window) -> float:
    """Transient oversized away-from-touch ask level pulled with no touch move
    (proxy for spoofing/layering). Returns the largest such pulled size."""
    score = 0.0
    for prev, cur in zip(window, window[1:]):
        if len(prev.asks) < 2 or not cur.asks:
            continue
        if cur.asks[0].price != prev.asks[0].price:
            continue  # touch moved -> not a clean pull
        cur_away = {round(l.price, 8): l.size for l in cur.asks[1:5]}
        for l in prev.asks[1:5]:
            if cur_away.get(round(l.price, 8), 0.0) < l.size * 0.5:  # >50% pulled
                score = max(score, l.size)
    return score


def _compute_signals_crypto(pid: str, ofi_window_s: float) -> dict | None:
    """Compute the log-only signal row for one crypto product from the ring."""
    feats = get_features(pid)
    if feats.spread_bps is None:
        return None
    window = get_book_buffer().recent(pid, window_secs=ofi_window_s)
    if len(window) < 2:
        return None
    last = window[-1]
    if not (last.bids and last.asks):
        return None
    b = float(feats.depth_bid_total or 0.0)
    a = float(feats.depth_ask_total or 0.0)
    best_bid = float(last.bids[0].price)
    best_ask = float(last.asks[0].price)
    mid = (best_bid + best_ask) / 2.0
    micro = (best_ask * b + best_bid * a) / (b + a) if (b + a) > 0 else mid
    eaten_n, eaten_notional = _ask_eaten(window)
    span = _snap_ts(last) - _snap_ts(window[0])
    span = span if span > 0 else 0.0
    return {
        "observed_at": datetime.utcfromtimestamp(_snap_ts(last)),
        "mid_price": mid,
        "micro_price": micro,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": float(feats.spread_bps),
        "depth_bid_total": b,
        "depth_ask_total": a,
        "book_depth_levels": int(feats.book_depth_levels or 0),
        "book_imbalance": _norm_imbalance(b, a),
        "microprice_edge_bps": (micro - mid) / mid * 1e4 if mid else 0.0,
        "ofi": _ofi(window),
        "ofi_window_s": span,
        "snapshot_count": len(window),
        "ask_eaten_events": eaten_n,
        "ask_eaten_notional_usd": eaten_notional,
        "hidden_seller_score": _hidden_seller(window),
        "spoof_score": _spoof(window),
        "sample_window_secs": span,
    }


def _ensure_log_partition(conn, day_dt: datetime) -> None:
    """Create the daily partition covering ``day_dt`` if missing (cached/day)."""
    day = day_dt.date()
    key = day.strftime("%Y%m%d")
    if key in _ensured_days:
        return
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS trading_microstructure_log_{key} "
        f"PARTITION OF trading_microstructure_log "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    ))
    _ensured_days.add(key)


def run_microstructure_log_drain_job() -> None:
    """Compute + persist log-only L2 signals for eligible crypto. Fail-open."""
    from ....config import settings as _s
    from ....db import SessionLocal, engine

    ofi_window = float(getattr(_s, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    db = SessionLocal()
    try:
        eligible = set(eligible_crypto_symbols(db))
    except Exception:
        logger.warning("[micro_log] eligibility query failed; skip cycle", exc_info=True)
        eligible = set()
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
    if not eligible:
        return

    targets = eligible & set(get_book_buffer().product_ids())
    if not targets:
        return

    ingest = datetime.utcnow()
    rows: list[dict] = []
    for pid in targets:
        try:
            sig = _compute_signals_crypto(pid, ofi_window)
        except Exception:
            continue
        if not sig:
            continue
        sig.update({
            "symbol": pid,
            "asset_class": "crypto",
            "source": "coinbase_ws",
            "ingest_at": ingest,
            "eligibility_state": "eligible",
            "viability_score": None,  # 1b: stratification backfill
        })
        rows.append(sig)
    if not rows:
        return

    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            _ensure_log_partition(conn, ingest)
            conn.execute(_INSERT_SQL, rows)
        logger.debug("[micro_log] logged %d crypto signal rows", len(rows))
    except Exception:
        logger.warning("[micro_log] insert failed; dropped cycle", exc_info=True)


def run_microstructure_log_prune_job() -> None:
    """Ensure upcoming daily partitions + drop partitions older than the
    retention window (partition-drop, not row-DELETE). Fail-open."""
    from ....config import settings as _s
    from ....db import engine

    retain = int(getattr(_s, "chili_micro_log_retain_days", 21) or 21)
    cutoff_key = (datetime.utcnow().date() - timedelta(days=retain)).strftime("%Y%m%d")
    try:
        with engine.begin() as conn:
            for off in range(0, 3):  # today + next 2 days
                _ensure_log_partition(conn, datetime.utcnow() + timedelta(days=off))
            parts = conn.execute(text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'trading_microstructure_log' "
                "AND c.relname ~ '^trading_microstructure_log_[0-9]{8}$'"
            )).fetchall()
            dropped = 0
            for (relname,) in parts:
                if relname[-8:] < cutoff_key:
                    conn.execute(text(f"DROP TABLE IF EXISTS {relname}"))
                    dropped += 1
        if dropped:
            logger.info("[micro_log] pruned %d expired partitions (retain=%dd)", dropped, retain)
    except Exception:
        logger.warning("[micro_log] prune failed", exc_info=True)
