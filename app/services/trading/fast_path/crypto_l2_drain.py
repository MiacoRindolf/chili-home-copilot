"""Phase 0 crypto L2 writer: drain the warmed Coinbase WS full-book ring into
``fast_orderbook``.

The Coinbase WebSocket (``coinbase_spot._handle_l2``) maintains an authoritative
per-product full order book in the scheduler process and pushes top-of-book
snapshots into the in-memory ``microstructure`` ring. ``_presubscribe_crypto_l2``
(trading_scheduler) warms that ring for the live-eligible crypto candidates every
viability cycle. This module's job, registered on a fast interval, snapshots that
ring to the ``fast_orderbook`` table so the L2 history is persisted for the
Phase-1 log-only signal layer + the Phase-2 forward-return backfill (the table is
otherwise empty — nothing else writes crypto L2).

Design constraints (from the L2 design + red-team):
* CRYPTO ONLY (``-USD`` set). Never touches equity: equity L2 comes from
  ``iqfeed_depth_snapshots`` and the equity path never reads ``fast_orderbook``.
* No NEW subscriptions (so no added 429 exposure) — drains only books already
  warmed by ``_presubscribe_crypto_l2``, intersected with the live-eligible set.
* Normalized ``imbalance`` = (b-a)/(b+a) in [-1, 1] to MATCH the existing
  ``fast_orderbook`` convention (the ring's ``bid_ask_imbalance`` is a RATIO; the
  table is normalized — writing the ratio would poison the table).
* Short-lived write txn with ``statement_timeout`` < cadence; fail-open (a DB or
  ring hiccup drops the cycle, never crashes the scheduler).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from ..microstructure import get_book_buffer, get_features

logger = logging.getLogger(__name__)

# Exact column/cast contract copied from fast_path/db_writer.py (do NOT route
# through the FastPathDBWriter asyncio writer — it is loop-affine; this job runs
# on the APScheduler thread).
_INSERT_SQL = text(
    "INSERT INTO fast_orderbook (ticker, snapshot_at, bid_levels, ask_levels, "
    "bid_total_size, ask_total_size, imbalance, spread_bps, source) VALUES "
    "(:ticker, :snapshot_at, CAST(:bid_levels AS JSONB), CAST(:ask_levels AS JSONB), "
    ":bid_total_size, :ask_total_size, :imbalance, :spread_bps, :source)"
)


def _norm_imbalance(bid_total: float, ask_total: float) -> float:
    """Normalized book imbalance in [-1, 1] (>0 bid-heavy). Matches the
    ``fast_orderbook.imbalance`` + ``iqfeed_depth_snapshots.imbalance5`` convention."""
    denom = bid_total + ask_total
    return (bid_total - ask_total) / denom if denom > 0 else 0.0


def eligible_crypto_symbols(db) -> list[str]:
    """Live-eligible, fresh crypto (-USD) symbols — the EXACT filter used by
    ``_presubscribe_crypto_l2`` so the warmed (subscribed) set and the drained
    (written-to-fast_orderbook) set match.

    Unions in the symbols of ACTIVE live crypto sessions (watching-to-enter or
    holding), unconditionally — a name we are actually trading must keep its L2
    captured even after it drops out of the fresh-eligible universe. JASMY-USD was
    a real +2.3R winner with 0 fast_orderbook rows because it was never a fresh
    candidate, so its OFI/micro read None and the exit lock could never fire."""
    from ....config import settings as _settings
    from ....models.trading import MomentumSymbolViability, TradingAutomationSession

    max_age = float(
        getattr(_settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0
    )
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    out: set[str] = {
        str(s).upper()
        for (s,) in (
            db.query(MomentumSymbolViability.symbol)
            .filter(
                MomentumSymbolViability.scope == "symbol",
                MomentumSymbolViability.live_eligible.is_(True),
                MomentumSymbolViability.symbol.like("%-USD%"),
                MomentumSymbolViability.freshness_ts >= cutoff,
            )
            .distinct()
            .all()
        )
        if s
    }
    # Active live crypto sessions: capital-at-risk OR watching to enter. These need
    # L2 for the exit lock / entry tilt regardless of candidate freshness.
    try:
        active = (
            db.query(TradingAutomationSession.symbol)
            .filter(
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.symbol.like("%-USD%"),
                TradingAutomationSession.state.in_((
                    "watching_live", "live_entry_candidate", "live_pending_entry",
                    "live_entered", "live_scaling_out", "live_trailing", "live_bailout",
                )),
            )
            .distinct()
            .all()
        )
        for (s,) in active:
            if s:
                out.add(str(s).upper())
    except Exception:
        pass
    return sorted(out)


def _book_item_for(pid: str) -> dict | None:
    """Build a fast_orderbook insert row from the ring's latest full-book snapshot.
    Returns None when the ring has no usable two-sided book for ``pid``."""
    buf = get_book_buffer()
    snap = buf.latest(pid)
    if snap is None or not snap.bids or not snap.asks:
        return None
    bid_levels = [(float(l.price), float(l.size)) for l in snap.bids[:20] if l.size > 0]
    ask_levels = [(float(l.price), float(l.size)) for l in snap.asks[:20] if l.size > 0]
    if not bid_levels or not ask_levels:
        return None
    feats = get_features(pid)
    b = float(feats.depth_bid_total or 0.0)
    a = float(feats.depth_ask_total or 0.0)
    return {
        "ticker": pid,
        # naive UTC = the exchange event time stamped by _handle_l2 (RT-2/RT-3);
        # fall back to local arrival ts when the event time was absent.
        "snapshot_at": datetime.utcfromtimestamp(snap.event_ts or snap.ts),
        "bid_levels": json.dumps(bid_levels),
        "ask_levels": json.dumps(ask_levels),
        "bid_total_size": b,
        "ask_total_size": a,
        "imbalance": _norm_imbalance(b, a),
        "spread_bps": float(feats.spread_bps) if feats.spread_bps is not None else 0.0,
        "source": "coinbase",
    }


def run_crypto_l2_drain_job() -> None:
    """Drain the warmed crypto book ring -> fast_orderbook. Fail-open."""
    from ....db import SessionLocal, engine

    db = SessionLocal()
    try:
        eligible = set(eligible_crypto_symbols(db))
    except Exception:
        logger.warning("[crypto_l2_drain] eligibility query failed; skip cycle", exc_info=True)
        eligible = set()
    finally:
        # FIX-46: end the implicit read txn so the conn returns to pool clean.
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

    params: list[dict] = []
    for pid in targets:
        try:
            item = _book_item_for(pid)
        except Exception:
            continue
        if item:
            params.append(item)
    if not params:
        return

    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 1500"))
            conn.execute(_INSERT_SQL, params)
        logger.debug("[crypto_l2_drain] wrote %d crypto book snapshots", len(params))
    except Exception:
        logger.warning("[crypto_l2_drain] insert failed; dropped cycle", exc_info=True)
