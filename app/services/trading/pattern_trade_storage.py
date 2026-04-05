"""Persist PatternTradeRow from backtest results."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

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
    """Insert up to _MAX_TRADES_PER_SAVE PatternTradeRow for each simulated trade."""
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
    n = 0
    for trade in trades[:_MAX_TRADES_PER_SAVE]:
        try:
            entry_ts = trade.get("entry_time")
            if not entry_ts:
                continue
            as_of = datetime.utcfromtimestamp(int(entry_ts))
            feats = build_features_v1(trade=trade, result_summary=summary, indicators=indicators)
            ret = trade.get("return_pct")
            label_win = bool(ret > 0) if ret is not None else None
            row = PatternTradeRow(
                user_id=user_id,
                scan_pattern_id=int(scan_pattern_id),
                related_insight_id=related_insight_id,
                backtest_result_id=backtest_row.id,
                ticker=result.get("ticker", backtest_row.ticker)[:20],
                as_of_ts=as_of,
                timeframe=str(result.get("period", "1d") or "1d")[:10],
                asset_class=_infer_asset_class(result.get("ticker", backtest_row.ticker) or ""),
                outcome_return_pct=float(ret) if ret is not None else None,
                label_win=label_win,
                features_json=feats,
                source=source[:40],
                feature_schema_version=FEATURE_SCHEMA_V1,
                code_version=code_ver,
            )
            db.add(row)
            n += 1
        except Exception as e:
            logger.debug("[pattern_trade_storage] skip trade row: %s", e)
    if n:
        try:
            db.commit()
        except Exception as e:
            logger.warning("[pattern_trade_storage] commit failed: %s", e)
            db.rollback()
            return 0
    return n
