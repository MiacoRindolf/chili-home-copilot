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

from .venue_binance import (
    fetch_funding_history,
    fetch_open_interest,
    fetch_premium_index,
)

logger = logging.getLogger(__name__)


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
    db: Session, contracts: list[dict[str, Any]]
) -> dict[str, int]:
    """One bulk premium-index call -> per-contract row insert."""
    by_symbol = {c["symbol"]: c for c in contracts}
    venue = "binance"
    quotes_inserted = 0
    basis_inserted = 0

    rows = fetch_premium_index()
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
    db: Session, contracts: list[dict[str, Any]]
) -> int:
    """Per-contract OI snapshot."""
    venue = "binance"
    inserted = 0
    for c in contracts:
        sym = c["symbol"]
        oi = fetch_open_interest(sym)
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
                    # premium-index doesn't give USD; OI in contracts +
                    # mark recently inserted gives a usable proxy. Skip
                    # the USD column for now (NULL); features.py uses
                    # the contracts column.
                    "oi_usd": None,
                },
            )
            inserted += 1
        except Exception as e:
            logger.debug("[perps.ingest] perp_oi insert %s failed: %s", sym, e)
    db.commit()
    return inserted


def _ingest_funding(
    db: Session, contracts: list[dict[str, Any]]
) -> int:
    """Per-contract funding history (last 3 periods, idempotent upsert)."""
    venue = "binance"
    inserted = 0
    for c in contracts:
        sym = c["symbol"]
        rows = fetch_funding_history(sym, limit=3)
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
    """Single hourly pass over all tradable Binance perps.

    Returns counts for the scheduler log line. Caller is responsible for
    flag-gating; this function runs unconditionally if reached.
    """
    try:
        rows = db.execute(
            text(
                "SELECT symbol, venue FROM perp_contracts "
                "WHERE venue = 'binance' AND tradable = TRUE "
                "ORDER BY symbol"
            )
        ).fetchall()
    except Exception as e:
        logger.warning("[perps.ingest] enumerate contracts failed: %s", e)
        return {"error": str(e)[:200]}

    contracts = [{"symbol": r[0], "venue": r[1]} for r in rows or []]
    if not contracts:
        return {"contracts": 0, "skipped": "no_tradable_contracts"}

    quotes_basis = _ingest_quotes_and_basis(db, contracts)
    oi_count = _ingest_open_interest(db, contracts)
    funding_count = _ingest_funding(db, contracts)

    summary = {
        "contracts": len(contracts),
        "quotes_inserted": quotes_basis["quotes"],
        "basis_inserted": quotes_basis["basis"],
        "oi_inserted": oi_count,
        "funding_inserted": funding_count,
    }
    logger.info("[perps.ingest] pass complete: %s", summary)
    return summary
