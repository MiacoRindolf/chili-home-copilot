"""Persist PatternTradeRow from backtest results.

Round-17 FIX (2026-04-30): switched from ORM ``db.add()`` + bulk commit to
PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` against the partial
unique index ``trading_pattern_trades_natural_key_uniq`` (added in
mig 208). Old behavior: a single duplicate row in the batch caused the
whole commit to roll back and the function returned 0 -- losing 49
valid rows because of one collision. New behavior: duplicates skip
silently (DO NOTHING), valid rows insert atomically, and the writer
returns the real inserted count from ``rowcount``. R16 audit noted
recurring constraint violations on ADA-USD pattern 875; this is the
fix for that bug class.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, PatternTradeRow
from .pattern_trade_features import FEATURE_SCHEMA_V1, build_features_v1

logger = logging.getLogger(__name__)

_MAX_TRADES_PER_SAVE = 50

_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH", "USDT", "BUSD")
_CRYPTO_BASES = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "AVAX", "MATIC",
    "DOT", "LINK", "SHIB", "BNB",
})


def _infer_asset_class(ticker: str) -> str:
    t = ticker.upper()
    base = t.split("-")[0] if "-" in t else t
    if any(t.endswith(s) for s in _CRYPTO_SUFFIXES) or base in _CRYPTO_BASES:
        return "crypto"
    return "stock"


def persist_rows_from_backtest_result(
    db: Session,
    *,
    user_id: int | None,
    scan_pattern_id: int | None,
    related_insight_id: int | None,
    backtest_row: BacktestResult,
    result: dict[str, Any],
    source: str = "queue_backtest",
) -> int:
    """Insert up to _MAX_TRADES_PER_SAVE PatternTradeRow for each simulated trade.

    Uses ON CONFLICT DO NOTHING against the partial unique index on
    (scan_pattern_id, ticker, as_of_ts, timeframe) WHERE scan_pattern_id
    IS NOT NULL. Returns the count of rows actually inserted (skipping
    duplicates) -- not the count of trades attempted.
    """
    if not scan_pattern_id:
        return 0
    trades = result.get("trades") or []
    if not trades:
        return 0
    code_ver = os.environ.get("CHILI_VERSION", "")[:40] or None
    summary = {
        "return_pct": result.get("return_pct"),
        "win_rate": result.get("win_rate"),
        "trade_count": result.get("trade_count"),
    }
    indicators = result.get("indicators") or {}
    rows: list[dict[str, Any]] = []
    for trade in trades[:_MAX_TRADES_PER_SAVE]:
        try:
            entry_ts = trade.get("entry_time")
            if not entry_ts:
                continue
            as_of = datetime.utcfromtimestamp(int(entry_ts))
            feats = build_features_v1(trade=trade, result_summary=summary, indicators=indicators)
            ret = trade.get("return_pct")
            label_win = bool(ret > 0) if ret is not None else None
            ticker = (result.get("ticker", backtest_row.ticker) or "")[:20]
            rows.append({
                "user_id": user_id,
                "scan_pattern_id": int(scan_pattern_id),
                "related_insight_id": related_insight_id,
                "backtest_result_id": backtest_row.id,
                "ticker": ticker,
                "as_of_ts": as_of,
                "timeframe": str(result.get("period", "1d") or "1d")[:10],
                "asset_class": _infer_asset_class(ticker),
                "outcome_return_pct": float(ret) if ret is not None else None,
                "label_win": label_win,
                "features_json": feats,
                "source": source[:40],
                "feature_schema_version": FEATURE_SCHEMA_V1,
                "code_version": code_ver,
                "created_at": datetime.utcnow(),
            })
        except Exception as e:
            logger.debug("[pattern_trade_storage] skip trade row: %s", e)
    if not rows:
        return 0
    try:
        stmt = (
            pg_insert(PatternTradeRow.__table__)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["scan_pattern_id", "ticker", "as_of_ts", "timeframe"],
                index_where=text("scan_pattern_id IS NOT NULL"),
            )
        )
        result_proxy = db.execute(stmt)
        db.commit()
        inserted = int(result_proxy.rowcount or 0)
        skipped = len(rows) - inserted
        if skipped:
            logger.debug(
                "[pattern_trade_storage] inserted=%d skipped_dup=%d pattern=%s",
                inserted, skipped, scan_pattern_id,
            )
        return inserted
    except Exception as e:
        # Real failure (not a dup) -- log + roll back the batch. The cycle
        # continues; we return 0 so the caller knows nothing landed.
        logger.warning("[pattern_trade_storage] insert failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
