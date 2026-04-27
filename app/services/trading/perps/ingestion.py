"""Q2 Task L — perps ingestion pass.

Wraps the venue adapter into a single hourly pass that:

  1. Iterates over tradable rows in ``perp_contracts``.
  2. Calls ``fetch_premium_index`` once (returns ALL contracts in one
     request — Binance lets you bulk it). Writes one row per contract
     into ``perp_quotes`` with mark/index price + spread.
  3. For each contract, calls ``fetch_open_interest`` and writes a row
     into ``perp_oi``.
  4. For each contract, calls ``fetch_funding_history(limit=3)`` and
     upserts the latest funding rows into ``perp_funding`` (idempotent
     on the (symbol, venue, funding_time) unique key).
  5. Computes basis vs spot using the existing ``trading_snapshots`` /
     market_data quote pipeline; writes one row per contract into
     ``perp_basis``.

Network: ~1 + 2N requests per pass for N contracts (currently 9 seeded).
At hourly cadence that's ~19 req/h, well under Binance public limits
(1200 req/min unauthenticated). Funding rates only update every 8 hours
on Binance — the upsert keeps the table tidy regardless of poll cadence.

Best-effort across the board: any single fetch failure logs and
continues; the function returns a counts dict for the scheduler log line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import (
    venue_binance,
    venue_dydx_v4,
    venue_hyperliquid,
    venue_kraken_futures,
)

logger = logging.getLogger(__name__)


# Map perp_contracts.venue -> adapter module. Each module exposes the
# same surface: fetch_premium_index, fetch_open_interest,
# fetch_funding_history. The ingestion pass dispatches by venue so a
# new venue is added by appending one row here.
_VENUE_ADAPTERS = {
    "binance": venue_binance,
    "dydx_v4": venue_dydx_v4,
    "hyperliquid": venue_hyperliquid,
    "kraken_futures": venue_kraken_futures,
}


def _iso_to_dt(ms: Any) -> datetime | None:
    """Binance returns ms-epoch ints. Convert to UTC datetime."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fetch_spot_price(symbol: str) -> float | None:
    """Best-effort spot-price fetch for basis calc.

    Maps perp symbol -> spot ticker. BTCUSDT -> BTC-USD via the existing
    market_data layer. Falls back to None and skips basis calc for that
    contract on any failure.
    """
    base = symbol.upper().replace("USDT", "")
    spot_ticker = f"{base}-USD"
    try:
        from ..market_data import fetch_quote

        q = fetch_quote(spot_ticker)
        if q and q.get("price"):
            return float(q["price"])
    except Exception as e:
        logger.debug("[perps.ingest] spot fetch failed for %s: %s", spot_ticker, e)
    return None


def _ingest_quotes_and_basis(
    db: Session, venue: str, contracts: list[dict[str, Any]]
) -> dict[str, int]:
    """One bulk premium-index call per venue -> per-contract row insert."""
    by_symbol = {c["symbol"]: c for c in contracts}
    quotes_inserted = 0
    basis_inserted = 0

    adapter = _VENUE_ADAPTERS.get(venue)
    if adapter is None:
        return {"quotes": 0, "basis": 0}

    rows = adapter.fetch_premium_index()
    if not rows:
        return {"quotes": 0, "basis": 0}

    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if sym not in by_symbol:
            continue
        mark = r.get("mark_price") or 0
        index = r.get("index_price") or 0
        if mark <= 0 or index <= 0:
            continue

        # Quotes row
        spread_bps = ((mark - index) / index * 10000.0) if index > 0 else None
        try:
            db.execute(
                text(
                    """
                    INSERT INTO perp_quotes
                        (symbol, venue, ts, mark_price, index_price,
                         spread_bps)
                    VALUES (:s, :v, NOW(), :m, :i, :sp)
                    """
                ),
                {"s": sym, "v": venue, "m": mark, "i": index, "sp": spread_bps},
            )
            quotes_inserted += 1
        except Exception as e:
            logger.debug("[perps.ingest] perp_quotes insert %s failed: %s", sym, e)
            continue

        # Basis row vs spot
        spot = _fetch_spot_price(sym)
        if spot is None or spot <= 0:
            continue
        basis_bps = (mark - spot) / spot * 10000.0
        try:
            db.execute(
                text(
                    """
                    INSERT INTO perp_basis
                        (symbol, venue, ts, perp_price, spot_price, basis_bps)
                    VALUES (:s, :v, NOW(), :p, :sp, :b)
                    ON CONFLICT (symbol, venue, ts) DO NOTHING
                    """
                ),
                {"s": sym, "v": venue, "p": mark, "sp": spot, "b": basis_bps},
            )
            basis_inserted += 1
        except Exception as e:
            logger.debug("[perps.ingest] perp_basis insert %s failed: %s", sym, e)

    db.commit()
    return {"quotes": quotes_inserted, "basis": basis_inserted}


def _ingest_open_interest(
    db: Session, venue: str, contracts: list[dict[str, Any]]
) -> int:
    """Per-contract OI snapshot."""
    adapter = _VENUE_ADAPTERS.get(venue)
    if adapter is None:
        return 0
    inserted = 0
    for c in contracts:
        sym = c["symbol"]
        oi = adapter.fetch_open_interest(sym)
        if not oi or oi.get("open_interest") is None:
            continue
        try:
            db.execute(
                text(
                    """
                    INSERT INTO perp_oi
                        (symbol, venue, ts, open_interest, open_interest_usd)
                    VALUES (:s, :v, NOW(), :oi, :oi_usd)
                    ON CONFLICT (symbol, venue, ts) DO NOTHING
                    """
                ),
                {
                    "s": sym, "v": venue,
                    "oi": oi.get("open_interest"),
                    # Hyperliquid returns USD notional via the bulk
                    # call; Binance per-symbol endpoint does not, so we
                    # accept None for the binance branch.
                    "oi_usd": oi.get("open_interest_usd"),
                },
            )
            inserted += 1
        except Exception as e:
            logger.debug("[perps.ingest] perp_oi insert %s failed: %s", sym, e)
    db.commit()
    return inserted


def _ingest_funding(
    db: Session, venue: str, contracts: list[dict[str, Any]]
) -> int:
    """Per-contract funding history (last 3 periods, idempotent upsert)."""
    adapter = _VENUE_ADAPTERS.get(venue)
    if adapter is None:
        return 0
    inserted = 0
    for c in contracts:
        sym = c["symbol"]
        rows = adapter.fetch_funding_history(sym, limit=3)
        for r in rows:
            ft = _iso_to_dt(r.get("funding_time"))
            if ft is None or r.get("funding_rate") is None:
                continue
            try:
                res = db.execute(
                    text(
                        """
                        INSERT INTO perp_funding
                            (symbol, venue, funding_time, funding_rate,
                             mark_at_funding)
                        VALUES (:s, :v, :ft, :fr, :mp)
                        ON CONFLICT (symbol, venue, funding_time)
                        DO NOTHING
                        """
                    ),
                    {
                        "s": sym, "v": venue, "ft": ft,
                        "fr": r.get("funding_rate"),
                        "mp": r.get("mark_at_funding"),
                    },
                )
                if res.rowcount:
                    inserted += 1
            except Exception as e:
                logger.debug(
                    "[perps.ingest] perp_funding insert %s @ %s failed: %s",
                    sym, ft, e,
                )
    db.commit()
    return inserted


def run_perps_ingestion_pass(db: Session) -> dict[str, Any]:
    """Single pass over all tradable perps, grouped by venue.

    Returns per-venue counts for the scheduler log line. Caller is
    responsible for flag-gating; this function runs unconditionally
    if reached.
    """
    try:
        rows = db.execute(
            text(
                "SELECT symbol, venue FROM perp_contracts "
                "WHERE tradable = TRUE "
                "ORDER BY venue, symbol"
            )
        ).fetchall()
    except Exception as e:
        logger.warning("[perps.ingest] enumerate contracts failed: %s", e)
        return {"error": str(e)[:200]}

    by_venue: dict[str, list[dict[str, Any]]] = {}
    for r in rows or []:
        by_venue.setdefault(r[1], []).append({"symbol": r[0], "venue": r[1]})
    if not by_venue:
        return {"contracts": 0, "skipped": "no_tradable_contracts"}

    summary: dict[str, Any] = {
        "contracts": sum(len(v) for v in by_venue.values()),
        "venues": {},
    }
    for venue, contracts in by_venue.items():
        if venue not in _VENUE_ADAPTERS:
            summary["venues"][venue] = {"skipped": "no_adapter"}
            continue
        qb = _ingest_quotes_and_basis(db, venue, contracts)
        oi_count = _ingest_open_interest(db, venue, contracts)
        funding_count = _ingest_funding(db, venue, contracts)
        summary["venues"][venue] = {
            "contracts": len(contracts),
            "quotes_inserted": qb["quotes"],
            "basis_inserted": qb["basis"],
            "oi_inserted": oi_count,
            "funding_inserted": funding_count,
        }

    logger.info("[perps.ingest] pass complete: %s", summary)
    return summary
