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

TICKER_TO_SECTOR: dict[str, str] = {}
for _sect, _tlist in SECTOR_TICKERS.items():
    for _t in _tlist:
        TICKER_TO_SECTOR[_t] = _sect

_CRYPTO_HINTS = {
    "btc", "eth", "sol", "bnb", "xrp", "crypto", "-usd", "coin",
    "defi", "token", "blockchain", "doge", "altcoin",
}
_STOCK_ONLY_HINTS = {"earnings", "sector", "dividend", "pe ratio", "eps"}

_GENERIC_STRATEGY_NAMES: set[str] = {
    "SMA Crossover", "EMA Crossover", "RSI Reversal", "Bollinger Bounce",
    "MACD Crossover", "Trend Following", "Momentum Breakout",
}

# ---------------------------------------------------------------------------
# Description → structured conditions parser
# ---------------------------------------------------------------------------

_DESC_CONDITION_RULES: list[tuple[re.Pattern, list[dict[str, Any]]]] = [
    # RSI thresholds
    (re.compile(r"rsi\s*(?:overbought\s*)?\(?[>]\s*(\d+)\)?", re.I),
     [{"indicator": "rsi_14", "op": ">", "_val_group": 1}]),
    (re.compile(r"rsi\s*>\s*(\d+)", re.I),
     [{"indicator": "rsi_14", "op": ">", "_val_group": 1}]),
    (re.compile(r"rsi\s*<\s*(\d+)", re.I),
     [{"indicator": "rsi_14", "op": "<", "_val_group": 1}]),
    (re.compile(r"rsi\s*(?:near-)?oversold\s*\((\d+)[–\-](\d+)\)", re.I),
     [{"indicator": "rsi_14", "op": "between", "_val_groups": (1, 2)}]),
    (re.compile(r"deep\s+oversold\s+rsi\s*<\s*(\d+)", re.I),
     [{"indicator": "rsi_14", "op": "<", "_val_group": 1}]),
    (re.compile(r"oversold\s+rsi", re.I),
     [{"indicator": "rsi_14", "op": "<", "value": 35}]),
    (re.compile(r"overbought\s+rsi", re.I),
     [{"indicator": "rsi_14", "op": ">", "value": 65}]),
    (re.compile(r"rsi\s+not\s+overbought", re.I),
     [{"indicator": "rsi_14", "op": "<", "value": 70}]),

    # ADX
    (re.compile(r"adx\s*>\s*(\d+)", re.I),
     [{"indicator": "adx", "op": ">", "_val_group": 1}]),
    (re.compile(r"adx\s*<\s*(\d+)", re.I),
     [{"indicator": "adx", "op": "<", "_val_group": 1}]),
    (re.compile(r"strong\s+trend", re.I),
     [{"indicator": "adx", "op": ">", "value": 25}]),
    (re.compile(r"no\s+trend", re.I),
     [{"indicator": "adx", "op": "<", "value": 15}]),

    # MACD
    (re.compile(r"macd\s+bullish\s+crossover|macd\s+turning\s+positive|macd\s+positive|macd\s+bullish", re.I),
     [{"indicator": "macd_hist", "op": ">", "value": 0}]),
    (re.compile(r"macd\s+(?:turning\s+)?negative|macd\s+flipped\s+negative|macd\s+bearish", re.I),
     [{"indicator": "macd_hist", "op": "<", "value": 0}]),
    (re.compile(r"macd\s+histogram\s+positive\s+while\s+macd\s+negative", re.I),
     [{"indicator": "macd_hist", "op": ">", "value": 0}]),

    # Bollinger Bands
    (re.compile(r"(?:price\s+)?above\s+upper\s+bollinger|above\s+upper\s+bb|upper\s+bb", re.I),
     [{"indicator": "price", "op": ">", "value": "bb_upper"}]),
    (re.compile(r"(?:price\s+)?below\s+lower\s+bollinger|below\s+lower\s+bb|lower\s+bb|near\s+lower\s+bb", re.I),
     [{"indicator": "price", "op": "<", "value": "bb_lower"}]),
    (re.compile(r"mid[- ]?bb\s+range|bb\s+squeeze|bollinger\s+squeeze", re.I),
     [{"indicator": "bb_squeeze", "op": "==", "value": 1}]),

    # EMA / SMA stacking and price relationships
    (re.compile(r"ema\s+stack(?:ing)?\s+bullish|price\s*>\s*ema\s*20\s*>\s*ema\s*50\s*>\s*ema\s*100", re.I),
     [{"indicator": "price", "op": ">", "value": "ema_20"},
      {"indicator": "ema_20", "op": ">", "value": "ema_50"},
      {"indicator": "ema_50", "op": ">", "value": "ema_100"}]),
    (re.compile(r"above\s+sma\s*(\d+)", re.I),
     [{"indicator": "price", "op": ">", "_val_fmt": "sma_{1}"}]),
    (re.compile(r"above\s+ema\s*(\d+)", re.I),
     [{"indicator": "price", "op": ">", "_val_fmt": "ema_{1}"}]),

    # Volume
    (re.compile(r"volume\s+surge\s+(\d+)x", re.I),
     [{"indicator": "rel_vol", "op": ">", "_val_group": 1}]),
    (re.compile(r"volume\s+surge|high\s+volume|volume\s+spike", re.I),
     [{"indicator": "rel_vol", "op": ">", "value": 3}]),

    # Gap
    (re.compile(r"gap\s+up\s*>?\s*(\d+)%", re.I),
     [{"indicator": "gap_pct", "op": ">", "_val_group": 1}]),
    (re.compile(r"gap\s+down\s*>?\s*(\d+)%", re.I),
     [{"indicator": "gap_pct", "op": "<", "_val_group_neg": 1}]),
    (re.compile(r"(\d+)%\+?\s*gapper", re.I),
     [{"indicator": "gap_pct", "op": ">", "_val_group": 1}]),

    # Stochastic
    (re.compile(r"stochastic\s+oversold\s*\(?\s*k?\s*<\s*(\d+)\)?", re.I),
     [{"indicator": "stoch_k", "op": "<", "_val_group": 1}]),
    (re.compile(r"stochastic\s+overbought", re.I),
     [{"indicator": "stoch_k", "op": ">", "value": 80}]),
    (re.compile(r"stochastic\s+oversold", re.I),
     [{"indicator": "stoch_k", "op": "<", "value": 20}]),
]


def _parse_conditions_from_description(desc: str) -> list[dict[str, Any]]:
    """Extract structured backtest conditions from a natural-language description.

    Returns a list of condition dicts compatible with ``DynamicPatternStrategy``.
    """
    conditions: list[dict[str, Any]] = []
    seen_indicators: set[str] = set()

    for pattern, templates in _DESC_CONDITION_RULES:
        m = pattern.search(desc)
        if not m:
            continue
        for tmpl in templates:
            ind = tmpl["indicator"]
            # Avoid duplicate conditions for the same indicator+op
            key = f"{ind}_{tmpl['op']}"
            if key in seen_indicators:
                continue

            cond: dict[str, Any] = {"indicator": ind, "op": tmpl["op"]}

            if "value" in tmpl:
                cond["value"] = tmpl["value"]
            elif "_val_group" in tmpl:
                try:
                    cond["value"] = float(m.group(tmpl["_val_group"]))
                except (IndexError, ValueError, TypeError):
                    continue
            elif "_val_group_neg" in tmpl:
                try:
                    cond["value"] = -float(m.group(tmpl["_val_group_neg"]))
                except (IndexError, ValueError, TypeError):
                    continue
            elif "_val_groups" in tmpl:
                try:
                    g1, g2 = tmpl["_val_groups"]
                    cond["value"] = [float(m.group(g1)), float(m.group(g2))]
                except (IndexError, ValueError, TypeError):
                    continue
            elif "_val_fmt" in tmpl:
                try:
                    cond["value"] = tmpl["_val_fmt"].replace("{1}", m.group(1))
                except (IndexError, TypeError):
                    continue
            elif "_val_group_or_default" in tmpl:
                grp, default = tmpl["_val_group_or_default"]
                try:
                    cond["value"] = float(m.group(grp))
                except (IndexError, ValueError, TypeError):
                    cond["value"] = default
            else:
                continue

            seen_indicators.add(key)
            conditions.append(cond)

    # Handle CHILI refinement descriptions: "rsi gt 70", "adx gt 30", etc.
    chili_m = re.search(r"(?:chili\s+refinement:\s*)(\w+)\s+(gt|lt|gte|lte|eq)\s+(\d+(?:\.\d+)?)", desc, re.I)
    if chili_m:
        ind_raw = chili_m.group(1).lower()
        op_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "=="}
        op = op_map.get(chili_m.group(2).lower(), ">")
        val = float(chili_m.group(3))
        ind_name = {"rsi": "rsi_14", "adx": "adx", "ema": "ema_20", "macd": "macd_hist"}.get(ind_raw, ind_raw)
        key = f"{ind_name}_{op}"
        if key not in seen_indicators:
            conditions.append({"indicator": ind_name, "op": op, "value": val})

    return conditions


_TICKER_RE = re.compile(r"\b([A-Z]{2,5}(?:-USD)?)\b")
_TICKER_STOPWORDS = {
    "RSI", "MACD", "EMA", "SMA", "ADX", "ATR", "AND", "THE", "FOR",
    "OBV", "MFI", "CCI", "SAR", "USD", "AVG", "NET", "LOW", "HIGH",
    "CHILI", "NR4", "NR7",
}

# Max workers for parallel backtest execution
def _bt_workers() -> int:
    """Threads per insight for parallel ticker backtests (bounded when many patterns run in parallel)."""
    base = max(8, (os.cpu_count() or 4) * 2)
    try:
        from ...config import settings
        cap = getattr(settings, "brain_smart_bt_max_workers", None)
        if cap is not None:
            return max(4, min(base, int(cap)))
    except Exception:
        pass
    return base




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

    return {
        "mentioned_tickers": mentioned_tickers[:10],
        "wants_crypto": wants_crypto,
        "crypto_only": crypto_only,
        "stock_only": stock_only,
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
    ticker_scope: str = "universal",
    scope_tickers: list[str] | None = None,
) -> list[str]:
    """Build a ticker pool for backtesting, respecting the pattern's scope.

    * ``ticker_specific`` -- 60 % from scope tickers, 40 % exploration
    * ``sector`` -- 60 % from scope sectors, 40 % exploration
    * ``universal`` -- broad diversification across all sectors + crypto
    """
    pool: list[str] = []
    pool_set: set[str] = set()

    def _add(ticker: str) -> None:
        if ticker not in pool_set:
            pool.append(ticker)
            pool_set.add(ticker)

    for t in ctx["mentioned_tickers"]:
        _add(t)

    if ticker_scope == "ticker_specific" and scope_tickers:
        for t in scope_tickers:
            _add(t)
        bias_count = max(1, int(target_count * 0.6)) - len(pool)
        if bias_count > 0 and scope_tickers:
            extras = scope_tickers * ((bias_count // len(scope_tickers)) + 1)
            random.shuffle(extras)
            for t in extras[:bias_count]:
                _add(t)
    elif ticker_scope == "sector" and scope_tickers:
        bias_count = max(1, int(target_count * 0.6)) - len(pool)
        for sector_name in scope_tickers:
            sector_list = SECTOR_TICKERS.get(sector_name, [])
            avail = [t for t in sector_list if t not in pool_set]
            per = max(1, bias_count // max(1, len(scope_tickers)))
            for t in random.sample(avail, min(per, len(avail))):
                _add(t)

    crypto_list = SECTOR_TICKERS.get("crypto", [])
    stock_sectors = [k for k in SECTOR_TICKERS if k != "crypto"]

    remaining = target_count - len(pool)
    n_crypto = int(remaining * _DEFAULT_CRYPTO_RATIO)
    n_stocks = remaining - n_crypto

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
        hot = get_prescreened_candidates(include_crypto=True, max_total=800)
        if hot:
            for t in random.sample(hot, min(5, len(hot))):
                _add(t)
    except Exception:
        pass

    return pool[:target_count]


# ---------------------------------------------------------------------------
# Link insight → ScanPattern (for pattern-aware backtesting)
# ---------------------------------------------------------------------------

def _rules_tuple_from_scan_pattern(
    pattern,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, int] | None:
    """Parse ``ScanPattern`` into (conditions, name, exit_config, pattern.id) or None."""
    import json as _json

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
            return conditions, pattern.name, exit_cfg, int(pattern.id)
    except (_json.JSONDecodeError, TypeError):
        pass
    return None


def _find_linked_pattern(
    db: Session, insight,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, int] | None:
    """Find ScanPattern conditions for a TradingInsight.

    Returns ``(conditions, pattern_name, exit_config, scan_pattern_id)`` or ``None``.

    When insight.scan_pattern_id is set, uses a direct lookup (one query). Otherwise
    uses resolve_to_scan_pattern (FK + backtests) and falls back to description-based
    matching for legacy rows.
    """
    try:
        from ...models.trading import ScanPattern
    except ImportError:
        return None

    sp_id = getattr(insight, "scan_pattern_id", None)
    if sp_id is not None:
        pattern = db.get(ScanPattern, int(sp_id))
        if pattern:
            tup = _rules_tuple_from_scan_pattern(pattern)
            if tup:
                return tup

    from .pattern_resolution import resolve_to_scan_pattern

    pattern = resolve_to_scan_pattern(db, int(insight.id))
    if pattern:
        tup = _rules_tuple_from_scan_pattern(pattern)
        if tup:
            return tup

    desc = (insight.pattern_description or "").strip()
    if not desc:
        return None

    name_part = desc.split("\u2014")[0].split(" - ")[0].strip()
    pattern = db.query(ScanPattern).filter(ScanPattern.name == name_part).first()

    if not pattern:
        all_patterns = (
            db.query(ScanPattern)
            .filter(ScanPattern.active.is_(True), ScanPattern.rules_json.isnot(None))
            .all()
        )
        desc_lower = desc.lower()
        for p in all_patterns:
            if p.name and p.name.lower() in desc_lower:
                pattern = p
                break

        if not pattern and all_patterns:
            _stop = {
                "the", "and", "for", "from", "with", "avg", "win",
                "sell", "buy", "signal", "refined", "chili", "sam",
                "refinement", "pattern", "above", "below",
            }
            desc_words = {
                w for w in desc_lower.replace("(", " ").replace(")", " ")
                .replace(",", " ").replace(":", " ").split()
                if len(w) >= 3 and w not in _stop
            }
            best, best_score = None, 0
            for p in all_patterns:
                pname_lower = (p.name or "").lower()
                p_words = {
                    w for w in pname_lower.replace("+", " ").split()
                    if len(w) >= 3 and w not in _stop
                }
                if not p_words:
                    continue
                overlap = len(desc_words & p_words)
                score = overlap / len(p_words)
                if score > best_score and overlap >= 2:
                    best_score = score
                    best = p
            if best and best_score >= 0.4:
                pattern = best

    if pattern and hasattr(insight, "scan_pattern_id"):
        try:
            insight.scan_pattern_id = pattern.id
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    return _rules_tuple_from_scan_pattern(pattern)


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
    period: str | None = None,
    target_tickers: int = 40,
    update_confidence: bool = True,
) -> dict[str, Any]:
    """Run diversified backtests for a single TradingInsight.

    Always uses ``DynamicPatternStrategy`` with actual conditions — either
    from a linked ``ScanPattern.rules_json`` or parsed from the insight
    description.  Strategy names are auto-generated from the conditions.

    Automatically selects the correct interval/period based on the linked
    ScanPattern's ``timeframe`` field (intraday patterns use shorter
    candles and lookback periods).

    Returns ``{"wins": int, "losses": int, "total": int, "backtests_run": int}``.
    """
    from ..backtest_service import (
        run_pattern_backtest, save_backtest, get_backtest_params,
        infer_pattern_timeframe,
    )

    shutdown = _get_shutdown_event()
    desc = insight.pattern_description or ""
    ctx = _extract_context(desc, db=db, insight_id=insight.id)

    linked = _find_linked_pattern(db, insight)
    exit_config: dict[str, Any] | None = None
    timeframe = "1d"
    _scope = "universal"
    _scope_tickers: list[str] | None = None
    linked_scan_pattern_id: int | None = None

    if linked:
        conditions, pattern_name, exit_config, linked_scan_pattern_id = linked
        sp_id = getattr(insight, "scan_pattern_id", None) or linked_scan_pattern_id
        if sp_id:
            try:
                from ...models.trading import ScanPattern
                import json as _json
                sp = db.query(ScanPattern).get(sp_id)
                if sp:
                    timeframe = getattr(sp, "timeframe", "1d") or "1d"
                    _scope = getattr(sp, "ticker_scope", "universal") or "universal"
                    _raw_st = getattr(sp, "scope_tickers", None)
                    if _raw_st:
                        try:
                            _scope_tickers = _json.loads(_raw_st)
                        except Exception:
                            pass
            except Exception:
                pass
    else:
        conditions = _parse_conditions_from_description(desc)
        pattern_name = None
        if not conditions:
            logger.info(
                "[backtest_engine] Skipping insight %d — no conditions extracted from: %s",
                insight.id, desc[:100],
            )
            return {"wins": 0, "losses": 0, "total": 0, "backtests_run": 0}
        timeframe = infer_pattern_timeframe(conditions, name=desc[:60])

    tickers = _select_tickers(
        ctx, db=db, insight_id=insight.id, target_count=target_tickers,
        ticker_scope=_scope, scope_tickers=_scope_tickers,
    )

    if linked:
        logger.info(
            "[backtest_engine] Pattern-aware BT for '%s' — %d tickers (exit_config=%s, tf=%s, scope=%s)",
            pattern_name, len(tickers), "custom" if exit_config else "auto", timeframe, _scope,
        )
    else:
        logger.info(
            "[backtest_engine] Parsed %d conditions from description for insight %d — %d tickers (tf=%s)",
            len(conditions), insight.id, len(tickers), timeframe,
        )

    bt_params = get_backtest_params(timeframe)
    bt_interval = bt_params["interval"]
    bt_period = period or bt_params["period"]

    jobs_count = len(tickers)

    def _run_one_pattern(ticker: str) -> dict[str, Any] | None:
        if shutdown.is_set():
            return None
        try:
            result = run_pattern_backtest(
                ticker, conditions, pattern_name=pattern_name,
                period=bt_period, interval=bt_interval,
                exit_config=exit_config,
            )
            if result.get("ok"):
                return result
        except Exception:
            pass
        return None

    wins, losses, total = 0, 0, 0

    with ThreadPoolExecutor(max_workers=_bt_workers()) as pool:
        futures = pool.map(_run_one_pattern, tickers)

        for result in futures:
            if result is None:
                continue
            try:
                save_backtest(
                    db,
                    insight.user_id,
                    result,
                    insight_id=insight.id,
                    scan_pattern_id=linked_scan_pattern_id,
                )
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
        # Recompute win/loss from ALL linked BacktestResult records so
        # card and detail views always agree.
        try:
            from ...models.trading import BacktestResult as _BT2
            all_linked = (
                db.query(_BT2.return_pct, _BT2.trade_count)
                .filter(
                    _BT2.related_insight_id == insight.id,
                    _BT2.trade_count > 0,
                )
                .all()
            )
            all_wins = sum(1 for r in all_linked if (r[0] or 0) > 0)
            all_losses = len(all_linked) - all_wins
            insight.win_count = all_wins
            insight.loss_count = all_losses
        except Exception:
            insight.win_count = wins
            insight.loss_count = losses
        insight.evidence_count = (insight.evidence_count or 0) + total

        if update_confidence and total >= 3:
            bt_win_rate = wins / total
            old_conf = insight.confidence
            new_conf = old_conf * 0.7 + bt_win_rate * 0.3
            insight.confidence = round(min(0.95, max(0.1, new_conf)), 3)

        db.commit()
    elif jobs_count > 0:
        try:
            from ..backtest_service import generate_strategy_name as _gsn
            display_name = pattern_name or _gsn(conditions)
            from ...models.trading import LearningEvent
            evt = LearningEvent(
                user_id=insight.user_id,
                event_type="review",
                description=(
                    f"Pattern \"{display_name}\" produced 0 trades across "
                    f"{len(tickers)} tickers over {period}. Conditions may be "
                    f"too restrictive — consider relaxing "
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
                display_name, len(tickers),
            )
        except Exception:
            logger.exception("[backtest_engine] Failed to log zero-trade event")

    return {
        "wins": wins, "losses": losses, "total": total,
        "backtests_run": jobs_count,
    }
