"""AI context building: rich context assembly for the trading AI."""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, Trade, StrategyProposal
from ..yf_session import get_fundamentals
from .market_data import (
    compute_indicators, fetch_quote, _use_massive, _use_polygon,
    DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights
from .journal import get_journal
from .scanner import _score_ticker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Market-context cache (avoids re-scoring 20 tickers on every analyze call)
# ---------------------------------------------------------------------------
_market_ctx_cache: dict[str, Any] = {"text": "", "ts": 0.0}
_MARKET_CTX_TTL = 300          # 5 min fresh
_MARKET_CTX_STALE_TTL = 600    # 10 min — serve stale while refreshing
_market_ctx_lock = threading.Lock()
_market_ctx_refreshing = False


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


def _build_candle_snapshots(tickers: list[str]) -> list[str]:
    """Fetch 15m and 5m candle snapshots for the given tickers.

    Returns formatted lines ready to append to the AI context.
    Data is already cached from the scanner run so this is fast.
    """
    from .market_data import fetch_ohlcv_df

    lines: list[str] = []
    for ticker in tickers:
        for interval, period, n_bars, label in [
            ("15m", "5d", 8, "15m"),
            ("5m", "1d", 10, "5m"),
        ]:
            try:
                df = fetch_ohlcv_df(ticker, period=period, interval=interval)
                if df.empty or len(df) < 2:
                    continue
                tail = df.tail(n_bars)
                lines.append(f"\n{ticker} — last {len(tail)} candles ({label}):")
                lines.append("  Time            | Open      | High      | Low       | Close     | Volume")
                for ts, row in tail.iterrows():
                    t_str = ts.strftime("%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[-11:]
                    o = f"${row['Open']:.4f}" if row["Open"] < 1 else f"${row['Open']:,.2f}"
                    h = f"${row['High']:.4f}" if row["High"] < 1 else f"${row['High']:,.2f}"
                    lo = f"${row['Low']:.4f}" if row["Low"] < 1 else f"${row['Low']:,.2f}"
                    c = f"${row['Close']:.4f}" if row["Close"] < 1 else f"${row['Close']:,.2f}"
                    v = row.get("Volume", 0)
                    if v >= 1_000_000:
                        v_s = f"{v / 1_000_000:.1f}M"
                    elif v >= 1_000:
                        v_s = f"{v / 1_000:.1f}K"
                    else:
                        v_s = str(int(v))
                    lines.append(f"  {t_str:17}| {o:>9} | {h:>9} | {lo:>9} | {c:>9} | {v_s}")
            except Exception:
                continue
    return lines


def build_ai_context(
    db: Session, user_id: int | None, ticker: str, interval: str = "1d",
) -> str:
    """Assemble rich context for the trading AI.

    External API calls (indicators, quote, fundamentals, scanner, market context)
    all run in parallel via ThreadPoolExecutor.  ``build_market_context`` is also
    cached for 5 minutes so back-to-back Analyze calls don't re-score 20 tickers.
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

    def _fetch_market_ctx():
        return build_market_context(db, user_id)

    def _fetch_portfolio_ctx():
        try:
            from .. import broker_service
            return broker_service.build_portfolio_context()
        except Exception:
            return None

    futures = {}
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures["indicators"] = pool.submit(_fetch_indicators)
        futures["quote"] = pool.submit(fetch_quote, ticker)
        futures["fundamentals"] = pool.submit(_fetch_fundamentals)
        futures["score"] = pool.submit(_score_ticker, ticker)
        futures["market_ctx"] = pool.submit(_fetch_market_ctx)
        futures["portfolio_ctx"] = pool.submit(_fetch_portfolio_ctx)

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

    # Per-ticker crypto intraday context: if the ticker being analyzed is in
    # the breakout cache, surface its full 15m indicator data + candle snapshot
    if _is_crypto:
        try:
            from .scanner import get_crypto_breakout_cache
            _bo_cache = get_crypto_breakout_cache()
            _bo_match = None
            for _r in _bo_cache.get("results", []):
                if _r["ticker"].upper() == ticker_up:
                    _bo_match = _r
                    break
            if _bo_match:
                ind = _bo_match.get("indicators", {})
                _cr_lines = [f"## INTRADAY BREAKOUT ANALYSIS — {ticker_up} (15m candles)"]
                _cr_lines.append(
                    f"Breakout Score: {_bo_match['score']}/10 | Signal: {_bo_match['signal'].upper()} | "
                    f"RVOL: {_bo_match.get('rvol', 0):.1f}x"
                )
                _flags = []
                if _bo_match.get("breakout_confirmed"):
                    _flags.append(f"BREAKOUT CONFIRMED ({_bo_match.get('breakout_dir', '').upper()})")
                if _bo_match.get("bb_squeeze_firing"):
                    _flags.append("BB SQUEEZE FIRING")
                elif _bo_match.get("bb_squeeze"):
                    _flags.append("BB squeeze active")
                if _bo_match.get("atr_state") != "normal":
                    _flags.append(f"ATR {_bo_match['atr_state']}")
                _flags.append(f"EMA: {_bo_match.get('ema_alignment', 'neutral').replace('_', ' ')}")
                _cr_lines.append("  " + " | ".join(_flags))

                ind_parts = []
                if ind.get("rsi") is not None:
                    ind_parts.append(f"RSI {ind['rsi']}")
                if ind.get("macd_hist") is not None:
                    ind_parts.append(f"MACD {ind['macd_hist']:+.4f}")
                if ind.get("adx") is not None:
                    ind_parts.append(f"ADX {ind['adx']:.0f}")
                if ind.get("atr") is not None:
                    ind_parts.append(f"ATR ${ind['atr']:.4f}")
                if ind.get("bb_width") is not None:
                    ind_parts.append(f"BB width {ind['bb_width']:.4f}")
                if ind_parts:
                    _cr_lines.append("  " + " | ".join(ind_parts))

                ema_vals = []
                for _ek in ("ema_9", "ema_21", "ema_50"):
                    if ind.get(_ek) is not None:
                        ema_vals.append(f"{_ek.upper().replace('_','')} ${ind[_ek]}")
                if ema_vals:
                    _cr_lines.append("  " + " > ".join(ema_vals))

                stoch_parts = []
                if ind.get("stoch_k") is not None:
                    stoch_parts.append(f"Stoch %K {ind['stoch_k']:.0f}")
                if ind.get("stoch_d") is not None:
                    stoch_parts.append(f"%D {ind['stoch_d']:.0f}")
                if _bo_match.get("vwap") is not None:
                    stoch_parts.append(f"VWAP ${_bo_match['vwap']} ({_bo_match.get('vwap_pct', 0):+.1f}%)")
                if stoch_parts:
                    _cr_lines.append("  " + " | ".join(stoch_parts))

                _cr_lines.append(
                    f"  Entry ${_bo_match.get('entry_price')} | Stop ${_bo_match.get('stop_loss')} | "
                    f"Target ${_bo_match.get('take_profit')} | R:R {_bo_match.get('risk_reward', 'n/a')}"
                )
                sigs = ", ".join(_bo_match.get("signals", [])[:5])
                _cr_lines.append(f"  Signals: {sigs}")

                # Candle snapshots for this specific ticker
                _cr_lines.append("")
                _cr_lines.extend(_build_candle_snapshots([ticker_up]))
                parts.append("\n".join(_cr_lines))
        except Exception:
            pass

    # Active / pending strategy proposals — prevent contradictions
    from datetime import datetime, timedelta
    recent_cutoff = datetime.utcnow() - timedelta(hours=24)
    proposals = db.query(StrategyProposal).filter(
        StrategyProposal.ticker == ticker_up,
        StrategyProposal.status.in_(["pending", "approved", "working", "executed"]),
        StrategyProposal.proposed_at >= recent_cutoff,
    ).order_by(StrategyProposal.proposed_at.desc()).limit(3).all()
    if proposals:
        lines = [f"## CHILI'S OWN STRATEGY PROPOSALS FOR {ticker_up} (active)"]
        lines.append(
            "IMPORTANT: These are recommendations CHILI already made to the user. "
            "If your analysis disagrees, you MUST explicitly acknowledge the proposal "
            "and explain WHY your view differs. Do NOT silently contradict yourself."
        )
        for p in proposals:
            score_parts = []
            if p.scan_score is not None:
                score_parts.append(f"Scanner: {p.scan_score:.1f}/10")
            if p.brain_score is not None:
                score_parts.append(f"Brain: {p.brain_score:.1f}")
            if p.ml_probability is not None:
                score_parts.append(f"ML: {p.ml_probability:.1%}")
            score_str = f" | Scores: {', '.join(score_parts)}" if score_parts else ""
            lines.append(
                f"- [{p.status.upper()}] {p.direction.upper()} @ ${p.entry_price:.2f} | "
                f"Stop: ${p.stop_loss:.2f} | Target: ${p.take_profit:.2f} | "
                f"R:R {p.risk_reward_ratio:.1f}:1 | Confidence: {p.confidence:.0f}%{score_str} | "
                f"Timeframe: {p.timeframe}"
            )
            if p.signals_json:
                try:
                    _sigs = json.loads(p.signals_json) if isinstance(p.signals_json, str) else p.signals_json
                    if isinstance(_sigs, list) and _sigs:
                        lines.append(f"  Signals: {'; '.join(str(s) for s in _sigs[:6])}")
                except Exception:
                    pass
            if p.thesis:
                lines.append(f"  Thesis: {p.thesis[:300]}")
        parts.append("\n".join(lines))

    # Brain prediction for this ticker
    try:
        from .pattern_ml import get_meta_learner, extract_pattern_features
        from .pattern_engine import get_active_patterns, evaluate_patterns_with_strength
        from .learning_predictions import _indicator_data_to_flat_snapshot
        full_indicators_for_pred = futures["indicators"].result(timeout=1) if "indicators" in futures else {}
        ind_flat = {}
        for ind_name, records in full_indicators_for_pred.items():
            if records:
                latest = records[-1]
                for k, v in latest.items():
                    if k != "time":
                        ind_flat[f"{ind_name}_{k}" if k != "value" else ind_name] = v
        if ind_flat:
            flat_snap = _indicator_data_to_flat_snapshot(full_indicators_for_pred, None)
            meta = get_meta_learner()
            brain_lines = [f"## CHILI BRAIN PREDICTION — {ticker_up}"]
            meta_prob = None
            try:
                from ...db import SessionLocal as _SL
                _ctx_db = _SL()
                try:
                    _pats = get_active_patterns(_ctx_db)
                finally:
                    _ctx_db.close()
            except Exception:
                _pats = []
            if _pats and meta.is_ready():
                pat_feats = extract_pattern_features(_pats, flat_snap)
                meta_prob = meta.predict(pat_feats)
            matches = evaluate_patterns_with_strength(flat_snap, _pats) if _pats else []
            if meta_prob is not None:
                score = round((meta_prob - 0.5) * 20, 2)
            elif matches:
                score = sum(m.get("score_boost", 1.0) * max(0.5, m.get("win_rate") or 0.5) * m.get("match_quality", 1.0) for m in matches)
                score = max(-10.0, min(10.0, score))
            else:
                score = 0.0
            direction = "BULLISH" if score > 1 else "BEARISH" if score < -1 else "NEUTRAL"
            brain_lines.append(f"Pattern ML score: {score:+.1f}/10 ({direction})")
            if meta_prob is not None:
                brain_lines.append(f"Meta-learner probability (5-day up): {meta_prob:.1%}")
            if matches:
                for m in matches[:5]:
                    wr = m.get("win_rate")
                    wr_s = f" ({round(wr*100)}% WR)" if wr else ""
                    brain_lines.append(f"  Matched: {m['name']}{wr_s}")

            # Reconciliation: compare brain vs scanner
            if scored:
                scanner_signal = scored["signal"].upper()
                scanner_score_val = scored["score"]
                brain_dir_upper = direction
                if brain_dir_upper == scanner_signal or (brain_dir_upper == "BULLISH" and scanner_signal == "BUY"):
                    brain_lines.append(
                        f"CONFLUENCE: Brain ({direction}) and Scanner ({scanner_signal}, {scanner_score_val}/10) AGREE. "
                        "Give this strong weight in your analysis."
                    )
                elif brain_dir_upper == "NEUTRAL":
                    brain_lines.append(
                        f"Brain is NEUTRAL while Scanner says {scanner_signal} ({scanner_score_val}/10). "
                        "The scanner has stronger conviction — weigh technical signals more heavily."
                    )
                else:
                    brain_lines.append(
                        f"DIVERGENCE: Brain says {direction} but Scanner says {scanner_signal} ({scanner_score_val}/10). "
                        "Explain this conflict and which view you trust more based on the indicators."
                    )
            brain_lines.append(
                "Factor the Brain's view into your analysis. If you disagree with it, explain why."
            )
            parts.append("\n".join(brain_lines))
    except Exception:
        pass

    backtests = db.query(BacktestResult).filter(
        BacktestResult.ticker == ticker_up,
    ).order_by(BacktestResult.return_pct.desc()).limit(3).all()
    if backtests:
        lines = ["## BACKTEST HISTORY (best strategies for this stock)"]
        for bt in backtests:
            kpi_bits = ""
            try:
                from .research_kpis import parse_kpis_from_backtest_params

                k = parse_kpis_from_backtest_params(bt.params) or {}
                so = k.get("sortino_ratio")
                ir = k.get("information_ratio")
                ca = k.get("calmar_ratio")
                if so is not None or ir is not None or ca is not None:
                    kpi_bits = " | KPIs:" + "".join(
                        x
                        for x in (
                            f" Sortino {so}" if so is not None else "",
                            f" IR {ir}" if ir is not None else "",
                            f" Calmar {ca}" if ca is not None else "",
                        )
                        if x
                    )
            except Exception:
                kpi_bits = ""
            from .backtest_metrics import backtest_win_rate_db_to_display_pct

            _wr_llm = backtest_win_rate_db_to_display_pct(bt.win_rate)
            _wr_s = f"{_wr_llm:.0f}%" if _wr_llm is not None else "N/A"
            lines.append(
                f"- {bt.strategy_name}: {bt.return_pct:+.1f}% return, "
                f"{_wr_s} win rate, {bt.trade_count} trades, "
                f"Sharpe {bt.sharpe or 'N/A'}, Max DD {bt.max_drawdown:.1f}%{kpi_bits}"
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

    # Crypto breakout scan context (from cached 15m scan)
    try:
        from .scanner import get_crypto_breakout_cache
        crypto_bo = get_crypto_breakout_cache()
        bo_results = crypto_bo.get("results", [])
        bo_age = crypto_bo.get("age_seconds")
        if bo_results and bo_age is not None:
            age_str = f"{bo_age // 60}m ago" if bo_age < 3600 else f"{bo_age // 3600}h ago"
            lines = [
                f"## CRYPTO BREAKOUT SCAN (15m candles, updated {age_str}, "
                f"{crypto_bo.get('total_scanned', 0)} pairs scanned)\n"
                "You have LIVE 15m OHLCV + indicators for all pairs below. "
                "Use this data to make precise entry/stop/target calls."
            ]
            for r in bo_results[:10]:
                ind = r.get("indicators", {})
                flags = []
                if r.get("breakout_confirmed"):
                    flags.append(f"BREAKOUT {r.get('breakout_dir', '').upper()}")
                if r.get("bb_squeeze_firing"):
                    flags.append("BB SQUEEZE FIRING")
                elif r.get("bb_squeeze"):
                    flags.append("BB squeeze")
                if r.get("atr_state") == "expanding":
                    flags.append("ATR expanding")
                elif r.get("atr_state") == "compressed":
                    flags.append("ATR compressed")
                if r.get("ema_alignment") in ("bullish_stack", "bearish_stack"):
                    flags.append(f"EMA {r['ema_alignment'].replace('_', ' ')}")
                flag_str = " | ".join(flags) if flags else "—"

                rsi_s = f"RSI {ind['rsi']}" if ind.get("rsi") is not None else "RSI n/a"
                macd_s = f"MACD {ind['macd_hist']:+.4f}" if ind.get("macd_hist") is not None else "MACD n/a"
                adx_s = f"ADX {ind['adx']:.0f}" if ind.get("adx") is not None else "ADX n/a"
                atr_s = f"ATR ${ind['atr']:.4f}" if ind.get("atr") is not None else "ATR n/a"
                bb_s = f"BB {ind['bb_width']:.4f}" if ind.get("bb_width") is not None else "BB n/a"
                stoch_s = ""
                if ind.get("stoch_k") is not None:
                    stoch_s = f"Stoch %K {ind['stoch_k']:.0f}"
                    if ind.get("stoch_d") is not None:
                        stoch_s += f" / %D {ind['stoch_d']:.0f}"

                ema_parts = []
                for ema_key in ("ema_9", "ema_21", "ema_50"):
                    if ind.get(ema_key) is not None:
                        ema_parts.append(f"{ema_key.upper().replace('_','')} ${ind[ema_key]}")
                ema_s = " > ".join(ema_parts) if ema_parts else ""
                ema_label = f"({r.get('ema_alignment', 'neutral').replace('_', ' ')})"

                vwap_s = ""
                if r.get("vwap") is not None:
                    vwap_s = f"VWAP ${r['vwap']} ({r.get('vwap_pct', 0):+.1f}%)"

                sigs = ", ".join(r.get("signals", [])[:5])

                lines.append(
                    f"\n### {r['ticker']} | Score {r['score']}/10 | ${r['price']} ({r.get('change_24h', 0):+.1f}% 24h) | RVOL {r.get('rvol', 0):.1f}x | {r['signal'].upper()}"
                )
                lines.append(f"  {flag_str}")
                lines.append(f"  {rsi_s} | {macd_s} | {adx_s} | {atr_s} | {bb_s}")
                if ema_s:
                    lines.append(f"  {ema_s} {ema_label}")
                if stoch_s:
                    line_extra = f"  {stoch_s}"
                    if vwap_s:
                        line_extra += f" | {vwap_s}"
                    lines.append(line_extra)
                elif vwap_s:
                    lines.append(f"  {vwap_s}")
                lines.append(
                    f"  Entry ${r.get('entry_price')} | Stop ${r.get('stop_loss')} | "
                    f"Target ${r.get('take_profit')} | R:R {r.get('risk_reward', 'n/a')}"
                )
                lines.append(f"  Signals: {sigs}")

            # Candle snapshots for top 3 setups (15m + 5m)
            _top_tickers_for_candles = [r["ticker"] for r in bo_results[:3]]
            if _top_tickers_for_candles:
                lines.append("\n## CANDLE SNAPSHOTS (top breakout candidates)")
                lines.extend(
                    _build_candle_snapshots(_top_tickers_for_candles)
                )

            parts.append("\n".join(lines))
    except Exception as exc:
        logger.debug(f"Crypto breakout context failed: {exc}")

    try:
        market_ctx = futures["market_ctx"].result(timeout=20)
        if market_ctx:
            parts.insert(0, market_ctx)
    except Exception:
        pass

    try:
        portfolio_ctx = futures["portfolio_ctx"].result(timeout=10)
        if portfolio_ctx:
            parts.insert(0, portfolio_ctx)
    except Exception:
        pass

    return "\n\n".join(parts)


def _build_market_context_fresh(db: Session, user_id: int | None) -> str:
    """Heavy-lift: score sample tickers + fetch macro quotes.

    All network I/O is parallelized:
    - Batch pre-warm downloads 20 tickers in a single HTTP call
    - Individual _score_ticker calls then hit the warm cache
    - SPY / BTC / ETH quotes fetched in parallel with scoring
    """
    sample_tickers = DEFAULT_SCAN_TICKERS[:15] + DEFAULT_CRYPTO_TICKERS[:5]

    # Pre-warm OHLCV cache (Massive/Polygon handle their own caching; yfinance needs batch_download)
    if not (_use_massive() or _use_polygon()):
        try:
            from ..yf_session import batch_download
            batch_download(sample_tickers + ["SPY"], period="6mo", interval="1d")
        except Exception:
            pass

    parts: list[str] = []
    bullish = 0
    bearish = 0
    neutral = 0
    rsi_vals: list[float] = []

    with ThreadPoolExecutor(max_workers=32) as pool:
        # Kick off all scoring + quote fetches concurrently
        score_futures = {pool.submit(_score_ticker, t): t for t in sample_tickers}
        spy_fut = pool.submit(fetch_quote, "SPY")
        btc_fut = pool.submit(fetch_quote, "BTC-USD")
        eth_fut = pool.submit(fetch_quote, "ETH-USD")

        for fut in as_completed(score_futures):
            scored = None
            try:
                scored = fut.result(timeout=10)
            except Exception:
                pass
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

        try:
            spy_quote = spy_fut.result(timeout=10)
        except Exception:
            spy_quote = None
        try:
            btc_quote = btc_fut.result(timeout=10)
        except Exception:
            btc_quote = None
        try:
            eth_quote = eth_fut.result(timeout=10)
        except Exception:
            eth_quote = None

    if spy_quote:
        spy_dir = "UP" if (spy_quote.get("change_pct") or 0) >= 0 else "DOWN"
        parts.append(
            f"S&P 500 (SPY): ${spy_quote.get('price')} ({spy_dir} {spy_quote.get('change_pct')}% today)"
        )

    try:
        from .market_data import get_market_regime
        regime = get_market_regime()
        if regime:
            parts.append(
                f"VIX: {regime.get('vix', 'N/A')} ({regime.get('vix_regime', 'N/A')}) | "
                f"Regime: {regime.get('regime', 'N/A').upper()} | "
                f"SPY 5d momentum: {regime.get('spy_momentum_5d', 0):+.1f}%"
            )
    except Exception:
        pass

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

    if btc_quote:
        parts.append(f"BTC: ${btc_quote.get('price')} ({btc_quote.get('change_pct')}%)")
    if eth_quote:
        parts.append(f"ETH: ${eth_quote.get('price')} ({eth_quote.get('change_pct')}%)")

    if not parts:
        return ""
    return "## MARKET PULSE (live)\n" + "\n".join(parts)


def build_market_context(db: Session, user_id: int | None) -> str:
    """Cached market-wide sentiment summary.

    Returns a cached string for up to 5 minutes.  Between 5-10 minutes, serves
    stale data and triggers a background refresh so the caller isn't blocked.
    """
    global _market_ctx_refreshing

    now = _time.time()
    age = now - _market_ctx_cache["ts"]

    # Fresh cache hit
    if _market_ctx_cache["text"] and age < _MARKET_CTX_TTL:
        return _market_ctx_cache["text"]

    # Stale-while-revalidate: serve stale, refresh in background
    if _market_ctx_cache["text"] and age < _MARKET_CTX_STALE_TTL:
        if not _market_ctx_refreshing:
            _market_ctx_refreshing = True
            def _bg_refresh():
                global _market_ctx_refreshing
                try:
                    text = _build_market_context_fresh(db, user_id)
                    with _market_ctx_lock:
                        _market_ctx_cache["text"] = text
                        _market_ctx_cache["ts"] = _time.time()
                finally:
                    _market_ctx_refreshing = False
            threading.Thread(target=_bg_refresh, daemon=True).start()
        return _market_ctx_cache["text"]

    # Cold or expired — must compute synchronously
    text = _build_market_context_fresh(db, user_id)
    with _market_ctx_lock:
        _market_ctx_cache["text"] = text
        _market_ctx_cache["ts"] = _time.time()
    return text


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
        f"{stats['prediction_accuracy']}% overall accuracy ({stats['total_predictions']} predictions), "
        f"{stats.get('strong_accuracy', 0)}% strong-signal accuracy ({stats.get('strong_predictions', 0)} high-conviction)"
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
        max_tokens=512,
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
        "strong_accuracy": stats.get("strong_accuracy", 0),
        "total_predictions": stats["total_predictions"],
        "strong_predictions": stats.get("strong_predictions", 0),
        "last_scan": stats.get("last_scan"),
    }

    generate_market_thesis._cache = {cache_key: (_time.time(), thesis_data)}
    return thesis_data
