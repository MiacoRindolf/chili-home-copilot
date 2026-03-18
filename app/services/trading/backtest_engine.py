"""Smart dynamic backtesting engine.

Provides ``smart_backtest_insight`` — the single entry point used by both
the startup backfill (main.py) and the periodic learning cycle
(learning.py _auto_backtest_patterns).  Instead of testing every pattern
against the same handful of mega-caps, it:

1. Extracts context from the pattern description (tickers, asset class,
   indicator keywords).
2. Builds a diversified ticker pool (sector-diverse stocks, crypto when
   relevant, prescreened hot movers, previous winners).
3. Maps the pattern to *multiple* relevant backtest strategies.
4. Runs backtests in parallel and records results with direct insight
   linkage.
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sector-diverse ticker groups (mirrors comment groups in market_data.py)
# ---------------------------------------------------------------------------

SECTOR_TICKERS: dict[str, list[str]] = {
    "mega_tech": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
        "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN", "NFLX",
    ],
    "cloud_saas": [
        "DDOG", "NET", "SNOW", "PLTR", "SHOP", "SQ", "PYPL", "COIN",
        "UBER", "ABNB", "MDB", "HUBS", "TEAM", "WDAY",
    ],
    "finance": [
        "JPM", "V", "MA", "BAC", "GS", "MS", "AXP", "BLK", "SCHW",
        "CME", "HOOD", "SOFI",
    ],
    "healthcare": [
        "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "AMGN",
        "GILD", "VRTX", "REGN", "ISRG", "MRNA",
    ],
    "consumer": [
        "WMT", "COST", "HD", "LOW", "TGT", "PG", "KO", "PEP", "MCD",
        "SBUX", "NKE", "LULU", "CMG",
    ],
    "industrial": [
        "CAT", "DE", "HON", "UPS", "BA", "LMT", "RTX", "GE", "EMR",
        "ETN", "AXON",
    ],
    "energy": [
        "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "OXY", "HAL",
    ],
    "reits_telecom_util": [
        "PLD", "AMT", "EQIX", "SPG", "O", "DIS", "CMCSA", "T", "VZ",
        "TMUS", "NEE",
    ],
    "materials": [
        "LIN", "APD", "SHW", "FCX", "NEM", "NUE",
    ],
    "etfs": [
        "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK", "XLF", "XLE",
        "XLK", "XLV",
    ],
    "growth_momentum": [
        "SMCI", "ARM", "CELH", "DUOL", "ENPH", "FSLR", "DKNG", "BKNG",
        "RIVN", "NIO", "IONQ", "AFRM", "UPST", "CAVA",
    ],
    "crypto": [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
        "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD",
        "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "NEAR-USD",
        "ARB-USD", "OP-USD", "FET-USD", "INJ-USD", "RENDER-USD",
    ],
}

_ALL_STOCK_TICKERS: set[str] = set()
for _sect, _tlist in SECTOR_TICKERS.items():
    if _sect != "crypto":
        _ALL_STOCK_TICKERS.update(_tlist)

_CRYPTO_HINTS = {
    "btc", "eth", "sol", "bnb", "xrp", "crypto", "-usd", "coin",
    "defi", "token", "blockchain", "doge", "altcoin",
}
_STOCK_ONLY_HINTS = {"earnings", "sector", "dividend", "pe ratio", "eps"}

_STRATEGY_KEYWORD_MAP: dict[str, str] = {
    "rsi": "rsi_reversal",
    "momentum": "momentum_breakout",
    "breakout": "momentum_breakout",
    "ema": "ema_cross",
    "sma": "sma_cross",
    "bollinger": "bb_bounce",
    "squeeze": "bb_bounce",
    "macd": "macd",
    "trend": "trend_follow",
    "vwap": "trend_follow",
}

_TICKER_RE = re.compile(r"\b([A-Z]{2,5}(?:-USD)?)\b")
_TICKER_STOPWORDS = {
    "RSI", "MACD", "EMA", "SMA", "ADX", "ATR", "AND", "THE", "FOR",
    "OBV", "MFI", "CCI", "SAR", "USD", "AVG", "NET", "LOW", "HIGH",
    "CHILI", "NR4", "NR7",
}

# Max workers for parallel backtest execution
_BT_WORKERS = max(8, (os.cpu_count() or 4) * 2)


# ---------------------------------------------------------------------------
# Pattern context extraction
# ---------------------------------------------------------------------------

def _extract_context(
    description: str,
    db: Session | None = None,
    insight_id: int | None = None,
) -> dict[str, Any]:
    """Parse a pattern description to extract actionable context.

    Also checks ``LearningEvent`` records associated with the insight to
    detect the original asset class the pattern was discovered from.
    """
    desc_lower = description.lower()

    mentioned_tickers = [
        t for t in _TICKER_RE.findall(description)
        if t not in _TICKER_STOPWORDS
    ]

    has_crypto_tickers = any(t.endswith("-USD") for t in mentioned_tickers)
    has_stock_tickers = any(
        not t.endswith("-USD") and t in _ALL_STOCK_TICKERS
        for t in mentioned_tickers
    )
    keyword_crypto = any(h in desc_lower for h in _CRYPTO_HINTS)

    # Check learning events for original discovery context (crypto tickers etc.)
    event_crypto = False
    if db and insight_id and not keyword_crypto and not has_crypto_tickers:
        try:
            from ...models.trading import LearningEvent
            events = (
                db.query(LearningEvent.description)
                .filter(LearningEvent.related_insight_id == insight_id)
                .limit(20)
                .all()
            )
            for (det,) in events:
                if not det:
                    continue
                det_lower = det.lower()
                if any(h in det_lower for h in _CRYPTO_HINTS):
                    event_crypto = True
                    break
                evt_tickers = _TICKER_RE.findall(det)
                if any(t.endswith("-USD") for t in evt_tickers):
                    event_crypto = True
                    break
        except Exception:
            pass

    wants_crypto = keyword_crypto or has_crypto_tickers or event_crypto
    crypto_only = wants_crypto and not has_stock_tickers and not any(
        h in desc_lower for h in _STOCK_ONLY_HINTS
    )
    stock_only = (
        any(h in desc_lower for h in _STOCK_ONLY_HINTS) and not wants_crypto
    )

    strategies: list[str] = []
    seen: set[str] = set()
    for keyword, strat in _STRATEGY_KEYWORD_MAP.items():
        if keyword in desc_lower and strat not in seen:
            strategies.append(strat)
            seen.add(strat)
    if not strategies:
        strategies = ["trend_follow"]
    strategies = strategies[:3]

    return {
        "mentioned_tickers": mentioned_tickers[:10],
        "wants_crypto": wants_crypto,
        "crypto_only": crypto_only,
        "stock_only": stock_only,
        "strategies": strategies,
    }


# ---------------------------------------------------------------------------
# Dynamic ticker selection
# ---------------------------------------------------------------------------

_DEFAULT_CRYPTO_RATIO = 0.30  # 30 % crypto, 70 % stocks — always broad


def _select_tickers(
    ctx: dict[str, Any],
    db: Session | None = None,
    insight_id: int | None = None,
    target_count: int = 40,
) -> list[str]:
    """Build a **broad, balanced** ticker pool across all sectors + crypto.

    Every pattern is tested against both stocks and crypto so the brain can
    *learn* which asset classes work best from actual results instead of
    guessing up front.  The allocation is fixed at ~30 % crypto / ~70 %
    stocks (distributed evenly across all stock sectors).

    Mentioned tickers and previous winners are still prioritised.
    """
    pool: list[str] = []
    pool_set: set[str] = set()

    def _add(ticker: str) -> None:
        if ticker not in pool_set:
            pool.append(ticker)
            pool_set.add(ticker)

    for t in ctx["mentioned_tickers"]:
        _add(t)

    crypto_list = SECTOR_TICKERS.get("crypto", [])
    stock_sectors = [k for k in SECTOR_TICKERS if k != "crypto"]

    n_crypto = int(target_count * _DEFAULT_CRYPTO_RATIO)
    n_stocks = target_count - n_crypto

    already_crypto = sum(1 for t in pool if t.endswith("-USD"))
    already_stocks = len(pool) - already_crypto
    need_crypto = max(0, n_crypto - already_crypto)
    need_stocks = max(0, n_stocks - already_stocks)

    if need_crypto > 0 and crypto_list:
        available = [t for t in crypto_list if t not in pool_set]
        for t in random.sample(available, min(need_crypto, len(available))):
            _add(t)

    if need_stocks > 0 and stock_sectors:
        per_sector = max(1, need_stocks // len(stock_sectors))
        for sector in stock_sectors:
            tickers = [t for t in SECTOR_TICKERS[sector] if t not in pool_set]
            for t in random.sample(tickers, min(per_sector, len(tickers))):
                _add(t)

    if db and insight_id:
        try:
            from ...models.trading import BacktestResult
            prev_winners = (
                db.query(BacktestResult.ticker)
                .filter(
                    BacktestResult.related_insight_id == insight_id,
                    BacktestResult.return_pct > 0,
                )
                .distinct()
                .limit(5)
                .all()
            )
            for (t,) in prev_winners:
                _add(t)
        except Exception:
            pass

    try:
        from .prescreener import get_prescreened_candidates
        hot = get_prescreened_candidates(include_crypto=True, max_total=200)
        if hot:
            for t in random.sample(hot, min(5, len(hot))):
                _add(t)
    except Exception:
        pass

    return pool[:target_count]


# ---------------------------------------------------------------------------
# Link insight → ScanPattern (for pattern-aware backtesting)
# ---------------------------------------------------------------------------

def _find_linked_pattern(
    db: Session, insight,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None] | None:
    """Find the ScanPattern conditions linked to a TradingInsight.

    Returns ``(conditions_list, pattern_name, exit_config_dict_or_None)``
    or ``None`` when no matching pattern with valid ``rules_json`` is found.
    """
    import json as _json
    try:
        from ...models.trading import ScanPattern
    except ImportError:
        return None

    desc = insight.pattern_description or ""
    if not desc:
        return None

    name_part = desc.split("\u2014")[0].split(" - ")[0].strip()

    pattern = db.query(ScanPattern).filter(ScanPattern.name == name_part).first()

    if not pattern:
        all_patterns = db.query(ScanPattern).all()
        for p in all_patterns:
            if p.name and p.name.lower() in desc.lower():
                pattern = p
                break

    if not pattern or not pattern.rules_json:
        return None

    exit_cfg = None
    if pattern.exit_config:
        try:
            exit_cfg = _json.loads(pattern.exit_config)
        except (_json.JSONDecodeError, TypeError):
            pass

    try:
        rules = _json.loads(pattern.rules_json)
        conditions = rules.get("conditions", [])
        if conditions:
            return conditions, pattern.name, exit_cfg
    except (_json.JSONDecodeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Core: smart_backtest_insight
# ---------------------------------------------------------------------------

_shutting_down: threading.Event | None = None


def _get_shutdown_event() -> threading.Event:
    global _shutting_down
    if _shutting_down is None:
        try:
            from . import learning
            _shutting_down = learning._shutting_down
        except Exception:
            _shutting_down = threading.Event()
    return _shutting_down


def smart_backtest_insight(
    db: Session,
    insight,
    *,
    period: str = "1y",
    target_tickers: int = 40,
    update_confidence: bool = True,
) -> dict[str, Any]:
    """Run diversified backtests for a single TradingInsight.

    When the insight is linked to a ``ScanPattern`` with valid ``rules_json``,
    uses **pattern-aware backtesting** (``run_pattern_backtest``) which
    evaluates the actual composite conditions bar-by-bar.  Otherwise falls
    back to keyword-mapped generic strategies.

    Returns ``{"wins": int, "losses": int, "total": int, "backtests_run": int}``.
    """
    from ..backtest_service import run_backtest, run_pattern_backtest, save_backtest

    shutdown = _get_shutdown_event()
    desc = insight.pattern_description or ""
    ctx = _extract_context(desc, db=db, insight_id=insight.id)
    tickers = _select_tickers(
        ctx, db=db, insight_id=insight.id, target_count=target_tickers,
    )

    linked = _find_linked_pattern(db, insight)
    use_pattern_bt = linked is not None
    exit_config: dict[str, Any] | None = None

    if use_pattern_bt:
        conditions, pattern_name, exit_config = linked
        jobs_count = len(tickers)
        logger.info(
            "[backtest_engine] Pattern-aware BT for '%s' — %d tickers (exit_config=%s)",
            pattern_name, jobs_count, "custom" if exit_config else "auto",
        )
    else:
        conditions, pattern_name = [], ""
        strategies = ctx["strategies"]
        jobs_count = len(tickers) * len(strategies)

    def _run_one_pattern(ticker: str) -> dict[str, Any] | None:
        if shutdown.is_set():
            return None
        try:
            result = run_pattern_backtest(
                ticker, conditions, pattern_name=pattern_name, period=period,
                exit_config=exit_config,
            )
            if result.get("ok"):
                return result
        except Exception:
            pass
        return None

    def _run_one_generic(args: tuple[str, str]) -> dict[str, Any] | None:
        if shutdown.is_set():
            return None
        ticker, strategy_id = args
        try:
            result = run_backtest(ticker, strategy_id=strategy_id, period=period)
            if result.get("ok") and result.get("trade_count", 0) > 0:
                return result
        except Exception:
            pass
        return None

    wins, losses, total = 0, 0, 0

    with ThreadPoolExecutor(max_workers=_BT_WORKERS) as pool:
        if use_pattern_bt:
            futures = pool.map(_run_one_pattern, tickers)
        else:
            generic_jobs = [
                (t, s) for t in tickers for s in ctx["strategies"]
            ]
            futures = pool.map(_run_one_generic, generic_jobs)

        for result in futures:
            if result is None:
                continue
            try:
                save_backtest(db, insight.user_id, result, insight_id=insight.id)
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                continue
            trade_count = result.get("trade_count", 0)
            if trade_count > 0:
                total += 1
                if result.get("return_pct", 0) > 0:
                    wins += 1
                else:
                    losses += 1

    if total > 0:
        insight.win_count = wins
        insight.loss_count = losses
        insight.evidence_count = (insight.evidence_count or 0) + total

        if update_confidence and total >= 3:
            bt_win_rate = wins / total
            old_conf = insight.confidence
            new_conf = old_conf * 0.7 + bt_win_rate * 0.3
            insight.confidence = round(min(0.95, max(0.1, new_conf)), 3)

        db.commit()
    elif jobs_count > 0 and use_pattern_bt:
        try:
            from ...models.trading import LearningEvent
            evt = LearningEvent(
                user_id=insight.user_id,
                event_type="review",
                description=(
                    f"Pattern \"{pattern_name}\" produced 0 trades across "
                    f"{len(tickers)} tickers over {period}. Conditions may be "
                    f"too restrictive for {interval} bars — consider relaxing "
                    f"thresholds or testing on lower timeframes."
                ),
                confidence_before=insight.confidence,
                confidence_after=insight.confidence,
                related_insight_id=insight.id,
            )
            db.add(evt)
            db.commit()
            logger.warning(
                "[backtest_engine] 0 trades for '%s' across %d tickers — "
                "logged LearningEvent",
                pattern_name, len(tickers),
            )
        except Exception:
            logger.exception("[backtest_engine] Failed to log zero-trade event")

    return {
        "wins": wins, "losses": losses, "total": total,
        "backtests_run": jobs_count,
    }
