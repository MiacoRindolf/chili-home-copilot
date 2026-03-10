"""AI context building: rich context assembly for the trading AI."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, Trade
from ..yf_session import get_fundamentals
from .market_data import (
    compute_indicators, fetch_quote,
    DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights
from .journal import get_journal
from .scanner import _score_ticker

logger = logging.getLogger(__name__)


def _format_fundamentals(ticker_up: str, fund: dict) -> str | None:
    """Format fundamental data dict into a context string."""
    if not fund or not fund.get("short_name"):
        return None
    lines = [f"## FUNDAMENTALS — {ticker_up} ({fund['short_name']})"]
    if fund.get("sector"):
        lines.append(f"Sector: {fund['sector']} | Industry: {fund.get('industry', 'N/A')}")
    val_parts = []
    if fund.get("pe_trailing") is not None:
        val_parts.append(f"P/E (TTM): {fund['pe_trailing']:.1f}")
    if fund.get("pe_forward") is not None:
        val_parts.append(f"P/E (Fwd): {fund['pe_forward']:.1f}")
    if fund.get("eps_trailing") is not None:
        val_parts.append(f"EPS: ${fund['eps_trailing']:.2f}")
    if fund.get("peg_ratio") is not None:
        val_parts.append(f"PEG: {fund['peg_ratio']:.2f}")
    if val_parts:
        lines.append(" | ".join(val_parts))
    val_parts2 = []
    if fund.get("price_to_sales") is not None:
        val_parts2.append(f"P/S: {fund['price_to_sales']:.1f}")
    if fund.get("price_to_book") is not None:
        val_parts2.append(f"P/B: {fund['price_to_book']:.1f}")
    if fund.get("ev_to_ebitda") is not None:
        val_parts2.append(f"EV/EBITDA: {fund['ev_to_ebitda']:.1f}")
    if val_parts2:
        lines.append(" | ".join(val_parts2))
    if fund.get("revenue_fmt"):
        rev_line = f"Revenue: {fund['revenue_fmt']}"
        if fund.get("revenue_growth") is not None:
            rev_line += f" | Growth: {fund['revenue_growth']:+.1%}"
        lines.append(rev_line)
    margin_parts = []
    if fund.get("gross_margins") is not None:
        margin_parts.append(f"Gross {fund['gross_margins']:.1%}")
    if fund.get("operating_margins") is not None:
        margin_parts.append(f"Operating {fund['operating_margins']:.1%}")
    if fund.get("profit_margins") is not None:
        margin_parts.append(f"Net {fund['profit_margins']:.1%}")
    if margin_parts:
        lines.append("Margins: " + " | ".join(margin_parts))
    health_parts = []
    if fund.get("free_cash_flow_fmt"):
        health_parts.append(f"FCF: {fund['free_cash_flow_fmt']}")
    if fund.get("debt_to_equity") is not None:
        health_parts.append(f"D/E: {fund['debt_to_equity']:.1f}")
    if fund.get("return_on_equity") is not None:
        health_parts.append(f"ROE: {fund['return_on_equity']:.1%}")
    if health_parts:
        lines.append(" | ".join(health_parts))
    if fund.get("dividend_yield") is not None and fund["dividend_yield"] > 0:
        lines.append(f"Dividend yield: {fund['dividend_yield']:.2%}")
    return "\n".join(lines)


def build_ai_context(
    db: Session, user_id: int | None, ticker: str, interval: str = "1d",
) -> str:
    """Assemble rich context for the trading AI.

    External API calls (indicators, quote, fundamentals, scanner) run in parallel
    via ThreadPoolExecutor to avoid serial rate-limiter delays.
    """
    parts: list[str] = []
    ticker_up = ticker.upper()
    _is_crypto = ticker_up.endswith("-USD") or ticker_up in {
        "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD",
        "ADA-USD", "AVAX-USD", "DOT-USD", "MATIC-USD", "LINK-USD",
    }

    def _fetch_indicators():
        return compute_indicators(
            ticker, interval=interval, period="6mo",
            indicators=[
                "rsi", "macd", "sma_20", "sma_50",
                "ema_20", "ema_50", "ema_100", "ema_200",
                "bbands", "stoch", "adx", "atr", "obv", "mfi",
                "vwap", "psar", "cci", "willr",
            ],
        )

    def _fetch_fundamentals():
        if _is_crypto:
            return None
        return get_fundamentals(ticker)

    futures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures["indicators"] = pool.submit(_fetch_indicators)
        futures["quote"] = pool.submit(fetch_quote, ticker)
        futures["fundamentals"] = pool.submit(_fetch_fundamentals)
        futures["score"] = pool.submit(_score_ticker, ticker)

    try:
        full_indicators = futures["indicators"].result(timeout=15)
        latest_vals: dict[str, Any] = {}
        for ind_name, records in full_indicators.items():
            if records:
                latest = records[-1]
                latest_vals[ind_name] = {k: v for k, v in latest.items() if k != "time"}
                if len(records) >= 5 and ind_name in ("rsi", "adx", "obv"):
                    recent_5 = [r.get("value") for r in records[-5:] if r.get("value") is not None]
                    if recent_5:
                        direction = "rising" if recent_5[-1] > recent_5[0] else "falling"
                        latest_vals[ind_name]["5d_direction"] = direction
        parts.append(f"## LIVE INDICATORS — {ticker_up} ({interval})\n{json.dumps(latest_vals, indent=2)}")
    except Exception:
        parts.append(f"## Could not fetch indicators for {ticker_up}")

    try:
        quote = futures["quote"].result(timeout=10)
    except Exception:
        quote = None
    if quote:
        parts.append(
            f"## CURRENT PRICE\n"
            f"Price: ${quote.get('price')} | Day change: {quote.get('change_pct')}% (${quote.get('change')})\n"
            f"Day range: ${quote.get('day_low', 'N/A')} - ${quote.get('day_high', 'N/A')} | "
            f"52wk range: ${quote.get('year_low', 'N/A')} - ${quote.get('year_high', 'N/A')}\n"
            f"Volume: {quote.get('volume', 'N/A')} | Avg volume: {quote.get('avg_volume', 'N/A')}\n"
            f"Market cap: {quote.get('market_cap', 'N/A')}"
        )

    try:
        fund = futures["fundamentals"].result(timeout=10)
        fund_text = _format_fundamentals(ticker_up, fund)
        if fund_text:
            parts.append(fund_text)
    except Exception:
        pass

    try:
        scored = futures["score"].result(timeout=15)
    except Exception:
        scored = None
    if scored:
        parts.append(
            f"## AI SCANNER SCORE\n"
            f"Score: {scored['score']}/10 | Signal: {scored['signal'].upper()}\n"
            f"Entry: ${scored['entry_price']} | Stop: ${scored['stop_loss']} | Target: ${scored['take_profit']}\n"
            f"Risk: {scored['risk_level'].upper()}\n"
            f"Signals: {', '.join(scored['signals']) if scored['signals'] else 'None strong'}"
        )

    backtests = db.query(BacktestResult).filter(
        BacktestResult.ticker == ticker_up,
    ).order_by(BacktestResult.return_pct.desc()).limit(3).all()
    if backtests:
        lines = ["## BACKTEST HISTORY (best strategies for this stock)"]
        for bt in backtests:
            lines.append(
                f"- {bt.strategy_name}: {bt.return_pct:+.1f}% return, "
                f"{bt.win_rate:.0f}% win rate, {bt.trade_count} trades, "
                f"Sharpe {bt.sharpe or 'N/A'}, Max DD {bt.max_drawdown:.1f}%"
            )
        parts.append("\n".join(lines))

    trades = db.query(Trade).filter(
        Trade.user_id == user_id, Trade.ticker == ticker_up,
    ).order_by(Trade.entry_date.desc()).limit(10).all()
    if trades:
        open_trades = [t for t in trades if t.status == "open"]
        closed_trades = [t for t in trades if t.status == "closed"]

        lines = [f"## USER'S TRADES ON {ticker_up}"]
        if open_trades:
            lines.append("OPEN POSITIONS:")
            for tr in open_trades:
                lines.append(
                    f"  - {tr.direction.upper()} {tr.quantity}x @ ${tr.entry_price} (entered {tr.entry_date.strftime('%Y-%m-%d') if tr.entry_date else 'N/A'})"
                )
        if closed_trades:
            lines.append("CLOSED (recent):")
            for tr in closed_trades[:5]:
                result = "WIN" if (tr.pnl or 0) > 0 else "LOSS"
                lines.append(
                    f"  - {tr.direction.upper()} @ ${tr.entry_price} → ${tr.exit_price} | "
                    f"P&L: ${tr.pnl} ({result})"
                )
        parts.append("\n".join(lines))

    stats = get_trade_stats(db, user_id)
    if stats.get("total_trades", 0) > 0:
        parts.append(
            f"## OVERALL TRADING PERFORMANCE\n"
            f"Total trades: {stats['total_trades']} | Win rate: {stats['win_rate']}%\n"
            f"Total P&L: ${stats['total_pnl']} | Best: ${stats['best_trade']} | Worst: ${stats['worst_trade']}\n"
            f"Max drawdown: ${stats['max_drawdown']}"
        )
    else:
        parts.append("## TRADING HISTORY\nThis user has no closed trades yet. They are a beginner — guide them carefully with clear, specific first-trade advice.")

    insights = get_insights(db, user_id, limit=10)
    if insights:
        lines = ["## YOUR LEARNED PATTERNS (use these as your edge)"]
        for ins in insights:
            lines.append(
                f"- [{ins.confidence:.0%} confidence, {ins.evidence_count} evidence] "
                f"{ins.pattern_description}"
            )
        parts.append("\n".join(lines))

    journal = get_journal(db, user_id, limit=5)
    if journal:
        lines = ["## RECENT JOURNAL NOTES"]
        for j in journal:
            lines.append(f"- {j.created_at.strftime('%Y-%m-%d')}: {j.content[:300]}")
        parts.append("\n".join(lines))

    market_ctx = build_market_context(db, user_id)
    if market_ctx:
        parts.insert(0, market_ctx)

    try:
        from .. import broker_service
        portfolio_ctx = broker_service.build_portfolio_context()
        if portfolio_ctx:
            parts.insert(0, portfolio_ctx)
    except Exception:
        pass

    return "\n\n".join(parts)


def build_market_context(db: Session, user_id: int | None) -> str:
    """Build a market-wide sentiment summary."""
    parts: list[str] = []

    spy_quote = fetch_quote("SPY")
    if spy_quote:
        spy_dir = "UP" if (spy_quote.get("change_pct") or 0) >= 0 else "DOWN"
        parts.append(
            f"S&P 500 (SPY): ${spy_quote.get('price')} ({spy_dir} {spy_quote.get('change_pct')}% today)"
        )

    sample_tickers = DEFAULT_SCAN_TICKERS[:15] + DEFAULT_CRYPTO_TICKERS[:5]
    bullish = 0
    bearish = 0
    neutral = 0
    rsi_vals: list[float] = []

    for ticker in sample_tickers:
        scored = _score_ticker(ticker)
        if scored is None:
            continue
        if scored["signal"] == "buy":
            bullish += 1
        elif scored["signal"] == "sell":
            bearish += 1
        else:
            neutral += 1
        rsi_v = scored["indicators"].get("rsi")
        if rsi_v is not None:
            rsi_vals.append(rsi_v)

    total = bullish + bearish + neutral
    if total:
        avg_rsi = sum(rsi_vals) / len(rsi_vals) if rsi_vals else 50
        if bullish > bearish * 1.5:
            sentiment = "RISK-ON (bullish majority)"
        elif bearish > bullish * 1.5:
            sentiment = "RISK-OFF (bearish majority)"
        else:
            sentiment = "MIXED / CHOPPY"

        parts.append(
            f"Market sentiment: {sentiment} — {bullish} bullish, {bearish} bearish, {neutral} neutral out of {total} sampled"
        )
        parts.append(f"Average RSI across sample: {avg_rsi:.0f}")

    btc_quote = fetch_quote("BTC-USD")
    eth_quote = fetch_quote("ETH-USD")
    if btc_quote:
        parts.append(f"BTC: ${btc_quote.get('price')} ({btc_quote.get('change_pct')}%)")
    if eth_quote:
        parts.append(f"ETH: ${eth_quote.get('price')} ({eth_quote.get('change_pct')}%)")

    if not parts:
        return ""
    return "## MARKET PULSE (live)\n" + "\n".join(parts)


def generate_market_thesis(db: Session, user_id: int | None) -> dict[str, Any]:
    """Ask the LLM to summarize its current market thesis."""
    import time as _time
    from .scanner import get_scan_status
    from .learning import get_brain_stats

    _thesis_cache = getattr(generate_market_thesis, "_cache", {})
    _THESIS_TTL = 3600
    cache_key = f"thesis:{user_id}"
    cached = _thesis_cache.get(cache_key)
    if cached and (_time.time() - cached[0]) < _THESIS_TTL:
        return cached[1]

    parts: list[str] = []

    market_ctx = build_market_context(db, user_id)
    if market_ctx:
        parts.append(market_ctx)

    insights = get_insights(db, user_id, limit=15)
    if insights:
        lines = ["LEARNED PATTERNS:"]
        for ins in insights:
            lines.append(
                f"- [{ins.confidence:.0%} conf, {ins.evidence_count} evidence] {ins.pattern_description}"
            )
        parts.append("\n".join(lines))

    from ...models.trading import ScanResult
    recent_scans = db.query(ScanResult).filter(
        (ScanResult.user_id == user_id) | (ScanResult.user_id.is_(None)),
    ).order_by(ScanResult.scanned_at.desc()).limit(20).all()
    if recent_scans:
        buy_count = sum(1 for s in recent_scans if s.signal == "buy")
        sell_count = sum(1 for s in recent_scans if s.signal == "sell")
        hold_count = sum(1 for s in recent_scans if s.signal == "hold")
        top_picks = [s for s in recent_scans if s.signal == "buy"][:5]
        parts.append(
            f"RECENT SCAN: {buy_count} buy, {hold_count} hold, {sell_count} sell signals\n"
            f"Top picks: {', '.join(s.ticker + ' (' + str(round(s.score, 1)) + ')' for s in top_picks)}"
        )

    stats = get_brain_stats(db, user_id)
    parts.append(
        f"BRAIN STATE: {stats['total_patterns']} patterns learned, "
        f"{stats['avg_confidence']}% avg confidence, "
        f"{stats['prediction_accuracy']}% accuracy ({stats['total_predictions']} predictions)"
    )

    context = "\n\n".join(parts) if parts else "No market data available yet."

    thesis_prompt = (
        "Based on the market data, learned patterns, and scan results below, write a concise "
        "market thesis in 3-5 sentences. Include:\n"
        "1. Overall market stance: BULLISH, BEARISH, or NEUTRAL (pick one)\n"
        "2. Top 2-3 highest-conviction trade ideas with specific tickers\n"
        "3. Key risks to watch\n"
        "Format: Start with **STANCE: [BULLISH/BEARISH/NEUTRAL]** on the first line.\n"
        "Keep it actionable and specific. No disclaimers needed here.\n\n"
        f"DATA:\n{context}"
    )

    from ...openai_client import chat
    result = chat(
        messages=[{"role": "user", "content": thesis_prompt}],
        system_prompt="You are CHILI's market strategist. Summarize the current market thesis concisely.",
        trace_id="brain-thesis",
    )

    reply = result.get("reply", "").strip()
    stance = "neutral"
    if "BULLISH" in reply.upper()[:100]:
        stance = "bullish"
    elif "BEARISH" in reply.upper()[:100]:
        stance = "bearish"

    thesis_data = {
        "thesis": reply,
        "stance": stance,
        "patterns_count": stats["total_patterns"],
        "accuracy": stats["prediction_accuracy"],
        "last_scan": stats.get("last_scan"),
    }

    generate_market_thesis._cache = {cache_key: (_time.time(), thesis_data)}
    return thesis_data
