"""AI context building: rich context assembly for the trading AI."""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import (
    BacktestResult,
    BreakoutAlert,
    PatternMonitorDecision,
    StrategyProposal,
    Trade,
)
from ..yf_session import get_fundamentals
from .market_data import (
    compute_indicators, fetch_quote, _use_massive, _use_polygon,
    DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights
from .journal import get_journal
from .scanner import _score_ticker

logger = logging.getLogger(__name__)


def _build_pattern_monitor_alignment_block(
    db: Session,
    open_trades: list,
    current_price: float | None,
) -> str | None:
    """Inject recent pattern-monitor decisions + reconciled verdict for AI Analysis."""
    if not open_trades:
        return None

    from .pattern_position_monitor import resolve_position_verdict

    trade_ids = [t.id for t in open_trades]
    cutoff = datetime.utcnow() - timedelta(hours=4)
    recent = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.trade_id.in_(trade_ids),
            PatternMonitorDecision.created_at >= cutoff,
        )
        .order_by(PatternMonitorDecision.created_at.desc())
        .limit(20)
        .all()
    )

    lines = ["## PATTERN MONITOR ALIGNMENT (same brain as Telegram alerts)"]
    sig = [d for d in recent if d.action != "hold"]
    for d in sig[:5]:
        age_m = (datetime.utcnow() - d.created_at).total_seconds() / 60.0
        lines.append(
            f"- Trade #{d.trade_id}: **{d.action.upper()}** {age_m:.0f}m ago @ "
            f"${d.price_at_decision or 0:.4f} (pattern health {d.health_score:.0%}, "
            f"source={d.decision_source or 'n/a'})"
        )
        if d.action == "exit_now":
            lines.append(
                "  The live position monitor **recommended EXIT** for this DB-linked trade. "
                "You must address this in your verdict — do not ignore it."
            )

    for tr in open_trades:
        if not tr.related_alert_id:
            continue
        v = resolve_position_verdict(db, tr, current_price=current_price)
        if not v:
            continue
        sl = f"${v.stop_level:.4f}" if v.stop_level is not None else "n/a"
        lines.append(
            f"- **Reconciled verdict** (trade #{tr.id}): `{v.action}` | urgency={v.urgency} | "
            f"reference level: {sl}"
        )
        lines.append(f"  _{v.reasoning}_")

    if len(lines) <= 1:
        return None

    lines.append("")
    lines.append(
        "If the pattern monitor fired **EXIT NOW** and you recommend **HOLD**, you MUST: "
        "(1) state the monitor recommends EXIT, (2) cite a structural level (price) that justifies holding, "
        "(3) give an exact hard-stop price where HOLD becomes EXIT, "
        "(4) acknowledge the risk of holding against a dead pattern."
    )
    return "\n".join(lines)


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

    # Breakout lookup first — needed to annotate the scanner score section.
    _bo_match = None
    try:
        from .scanner import get_breakout_cache
        _bo_all = get_breakout_cache()
        for _r in (_bo_all.get("results") or []):
            if _r.get("ticker", "").upper() == ticker_up:
                _bo_match = _r
                break
    except Exception:
        pass

    try:
        scored = futures["score"].result(timeout=15)
    except Exception:
        scored = None
    if scored:
        _scanner_header = "## AI SCANNER SCORE"
        if _bo_match:
            _scanner_header += " (general swing strategy — see BREAKOUT ANALYSIS below for breakout-specific levels)"
        parts.append(
            f"{_scanner_header}\n"
            f"Score: {scored['score']}/10 | Signal: {scored['signal'].upper()}\n"
            f"Entry: ${scored['entry_price']} | Stop: ${scored['stop_loss']} | Target: ${scored['take_profit']}\n"
            f"Risk: {scored['risk_level'].upper()}\n"
            f"Signals: {', '.join(scored['signals']) if scored['signals'] else 'None strong'}"
        )

    # Breakout context: inject breakout-specific entry/stop/target so the LLM
    # uses the correct levels (at resistance, not current price).
    if _bo_match:
        ind = _bo_match.get("indicators", {})
        _bo_is_crypto = _is_crypto or _bo_match.get("rvol") is not None
        _bo_note = (
            "IMPORTANT: The entry/stop/target in this BREAKOUT ANALYSIS section are the "
            "authoritative levels for this breakout setup. The AI Scanner Score above "
            "uses a different (general swing) strategy with wider levels based on current "
            "price — when the user is evaluating a breakout, use THESE breakout levels "
            "for your recommendation."
        )

        if _bo_is_crypto:
            _cr_lines = [f"## BREAKOUT ANALYSIS — {ticker_up} (15m candles, breakout strategy)"]
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
            _cr_lines.append(_bo_note)
            _cr_lines.append("")
            _cr_lines.extend(_build_candle_snapshots([ticker_up]))
            parts.append("\n".join(_cr_lines))
        else:
            _st_lines = [f"## BREAKOUT ANALYSIS — {ticker_up} (daily, breakout strategy)"]
            _status = _bo_match.get("status", _bo_match.get("signal", "watch")).upper()
            _cr_flag = "crypto" if _is_crypto else "stock"
            _st_lines.append(
                f"Breakout Score: {_bo_match['score']}/10 | Status: {_status} | Asset: {_cr_flag}"
            )
            _st_lines.append(
                f"  Resistance: ${_bo_match.get('resistance')} | "
                f"Distance to breakout: {_bo_match.get('dist_to_breakout', 'N/A')}%"
            )
            _st_lines.append(
                f"  Entry: ${_bo_match.get('entry_price')} (at resistance) | "
                f"Stop: ${_bo_match.get('stop_loss')} | Target: ${_bo_match.get('take_profit')}"
            )
            _st_lines.append(_bo_note)
            _bo_flags = []
            if _bo_match.get("bb_squeeze"):
                _bo_flags.append("BB SQUEEZE active")
            bb_pctile = _bo_match.get("bb_width_pctile")
            if bb_pctile is not None:
                _bo_flags.append(f"BB width pctile: {bb_pctile}%")
            if ind.get("adx") is not None:
                _bo_flags.append(f"ADX {ind['adx']:.0f}")
            vol_trend = _bo_match.get("vol_trend_pct")
            if vol_trend is not None:
                _bo_flags.append(f"Vol trend: {vol_trend:+.0f}%")
            tight_days = _bo_match.get("tight_days")
            if tight_days:
                _bo_flags.append(f"Tight range: {tight_days} days")
            if _bo_flags:
                _st_lines.append("  " + " | ".join(_bo_flags))

            ind_parts = []
            if ind.get("rsi") is not None:
                ind_parts.append(f"RSI {ind['rsi']}")
            if ind.get("macd_hist") is not None:
                ind_parts.append(f"MACD {ind['macd_hist']:+.4f}")
            if ind.get("atr") is not None:
                ind_parts.append(f"ATR ${ind['atr']}")
            for _ek in ("ema_20", "ema_50", "ema_100"):
                if ind.get(_ek) is not None:
                    ind_parts.append(f"{_ek.upper().replace('_','')} ${ind[_ek]}")
            if ind_parts:
                _st_lines.append("  " + " | ".join(ind_parts))

            sigs = ", ".join(_bo_match.get("signals", [])[:6])
            _st_lines.append(f"  Signals: {sigs}")
            hold_est = _bo_match.get("hold_estimate")
            if hold_est:
                _st_lines.append(f"  Hold estimate: {hold_est}")
            parts.append("\n".join(_st_lines))

    # Recent pattern-imminent alerts for this ticker (from the brain's learned
    # patterns, with entry/stop/target and the triggering pattern name).
    try:
        from datetime import datetime, timedelta
        _alerts = (
            db.query(BreakoutAlert)
            .filter(
                BreakoutAlert.ticker == ticker_up,
                BreakoutAlert.alert_tier == "pattern_imminent",
            )
            .order_by(BreakoutAlert.alerted_at.desc())
            .limit(3)
            .all()
        )
        if _alerts:
            _newest_age_h = (
                (datetime.utcnow() - _alerts[0].alerted_at).total_seconds() / 3600
                if _alerts[0].alerted_at else 999
            )
            _freshness = (
                "FRESH" if _newest_age_h < 24
                else "RECENT" if _newest_age_h < 72
                else "OLDER (prices may have moved)"
            )
            _al = []
            _al.append(f"## PATTERN-IMMINENT ALERTS — {ticker_up} ({_freshness})")
            _al.append(
                "These alerts are from CHILI's pattern-recognition engine (learned patterns). "
                "The entry/stop/target below come from the specific pattern that triggered. "
                "Use these levels as the primary reference for this setup, adjusting for "
                "any price movement since the alert was generated."
            )
            _seen_pattern_ids: set[int] = set()
            for _a in _alerts:
                _pat_name = ""
                _pat_obj = None
                if _a.scan_pattern_id:
                    from ...models.trading import ScanPattern as _SP
                    _pat_obj = db.get(_SP, _a.scan_pattern_id)
                    if _pat_obj:
                        _pat_name = _pat_obj.name or f"Pattern #{_pat_obj.id}"
                _age_h = (datetime.utcnow() - _a.alerted_at).total_seconds() / 3600 if _a.alerted_at else 0
                _snap = _a.indicator_snapshot or {}
                _sc = _snap.get("imminent_scorecard", {})
                _sigs = (_a.signals_snapshot or {}).get("signals", [])

                _al.append(f"  Pattern: {_pat_name or 'Unknown'} | Alert age: {_age_h:.1f}h ago")
                _al.append(
                    f"  Price at alert: ${_a.price_at_alert} | "
                    f"Entry: ${_a.entry_price} | Stop: ${_a.stop_loss} | "
                    f"Target: ${_a.target_price}"
                )
                if _sc.get("readiness") is not None:
                    _eta = _sc.get("eta_hours", [None, None])
                    _eta_lo = f"{_eta[0]:.1f}" if isinstance(_eta[0], (int, float)) else "?"
                    _eta_hi = f"{_eta[1]:.1f}" if isinstance(_eta[1], (int, float)) else "?"
                    _al.append(
                        f"  Readiness: {_sc['readiness']:.0%} | "
                        f"Composite: {_sc.get('composite', 0):.2f} | "
                        f"ETA: {_eta_lo}-{_eta_hi}h"
                    )
                if _sigs:
                    _al.append(f"  Signals: {', '.join(_sigs[:5])}")
                _al.append(f"  Outcome: {_a.outcome or 'pending'}")

                # Full pattern conditions (once per pattern)
                if _pat_obj and _a.scan_pattern_id not in _seen_pattern_ids:
                    _seen_pattern_ids.add(_a.scan_pattern_id)
                    _al.append(f"\n  --- Pattern Detail: {_pat_name} ---")
                    if _pat_obj.description:
                        _al.append(f"  Description: {_pat_obj.description}")
                    _al.append(
                        f"  Timeframe: {_pat_obj.timeframe or '?'} | "
                        f"Win rate: {(_pat_obj.win_rate or 0) * 100:.1f}% | "
                        f"Avg return: {_pat_obj.avg_return_pct or 0:.1f}%"
                    )
                    _rj = _pat_obj.rules_json
                    if isinstance(_rj, str):
                        try:
                            _rj = json.loads(_rj)
                        except Exception:
                            _rj = {}
                    _conditions = (_rj or {}).get("conditions", [])
                    if _conditions:
                        _al.append("  CONDITIONS (all must be true for pattern to fire):")
                        for _c in _conditions:
                            _ind = _c.get("indicator", "?")
                            _op = _c.get("op", "?")
                            _val = _c.get("value") if "value" in _c else _c.get("ref", "?")
                            _al.append(f"    - {_ind} {_op} {_val}")
                        _al.append(
                            "  In your response, explain each condition above in plain English "
                            "so the user understands WHY this pattern works and what market "
                            "state it captures."
                        )
            parts.append("\n".join(_al))
    except Exception as e:
        logger.debug("[ai_context] pattern-imminent alert lookup failed: %s", e)

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
                from .backtest_param_sets import materialize_backtest_params
                from .research_kpis import parse_kpis_from_backtest_params

                k = parse_kpis_from_backtest_params(materialize_backtest_params(db, bt)) or {}
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

    # Live broker position for this ticker (independent of DB Trade rows).
    _live_pos = None
    try:
        from .. import broker_service
        _live_pos = broker_service.get_position_for_ticker(ticker_up)
    except Exception:
        pass

    if trades or _live_pos:
        open_trades = [t for t in trades if t.status == "open"] if trades else []
        closed_trades = [t for t in trades if t.status == "closed"] if trades else []

        lines = [f"## USER'S POSITION & TRADES — {ticker_up}"]

        if _live_pos:
            _lq = _live_pos.get("quantity", 0)
            _la = _live_pos.get("average_buy_price", 0)
            _lc = _live_pos.get("current_price", _la)
            _lpnl = (_lc - _la) * _lq if _la and _lc and _lq else 0
            _lpct = ((_lc / _la - 1) * 100) if _la and _lc else 0
            _pnl_sign = "+" if _lpnl >= 0 else ""
            lines.append(
                f">>> LIVE BROKER POSITION (Robinhood): "
                f"HOLDING {_lq} shares @ ${_la:,.4f} avg cost | "
                f"Current ${_lc:,.4f} | P&L {_pnl_sign}${_lpnl:,.2f} ({_pnl_sign}{_lpct:.1f}%)"
            )
            lines.append(
                "The user OWNS this stock right now. Your analysis MUST acknowledge "
                "this position, reference their avg cost, and advise on hold/add/trim/exit."
            )

        if open_trades:
            lines.append("OPEN DB POSITIONS:")
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

        try:
            _qpx = None
            if quote:
                _qpx = float(quote.get("price") or quote.get("last") or 0) or None
        except (TypeError, ValueError):
            _qpx = None
        _align = _build_pattern_monitor_alignment_block(db, open_trades, _qpx)
        if _align:
            parts.append(_align)

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
    """Deterministic market thesis from scan votes + brain stats (no LLM)."""
    import time as _time

    from .learning import get_brain_stats
    from .portfolio import get_insights
    from ...models.trading import ScanResult

    _thesis_cache: dict = getattr(generate_market_thesis, "_cache", {})
    _THESIS_TTL = 3600
    cache_key = f"thesis:{user_id}"
    cached = _thesis_cache.get(cache_key)
    if cached and (_time.time() - cached[0]) < _THESIS_TTL:
        return cached[1]

    stats = get_brain_stats(db, user_id)
    market_ctx = build_market_context(db, user_id) or ""

    recent_scans = (
        db.query(ScanResult)
        .filter((ScanResult.user_id == user_id) | (ScanResult.user_id.is_(None)))
        .order_by(ScanResult.scanned_at.desc())
        .limit(24)
        .all()
    )
    buy_count = sum(1 for s in recent_scans if s.signal == "buy")
    sell_count = sum(1 for s in recent_scans if s.signal == "sell")
    hold_count = sum(1 for s in recent_scans if s.signal == "hold")
    denom = buy_count + sell_count + hold_count
    if denom == 0:
        stance = "neutral"
    else:
        bull_ratio = buy_count / denom
        bear_ratio = sell_count / denom
        if bull_ratio >= 0.6:
            stance = "bullish"
        elif bear_ratio >= 0.4 or bull_ratio <= 0.25:
            stance = "bearish"
        else:
            stance = "neutral"

    buys_sorted = sorted(
        (s for s in recent_scans if s.signal == "buy"),
        key=lambda s: float(s.score or 0),
        reverse=True,
    )[:3]
    idea_lines: list[str] = []
    for s in buys_sorted:
        r = (s.rationale or "").strip().replace("\n", " ")
        tail = f" — {r[:100]}…" if len(r) > 100 else (f" — {r}" if r else "")
        idea_lines.append(f"- {s.ticker} (score {float(s.score):.1f}){tail}")

    risks: list[str] = []
    mc_low = market_ctx.lower()
    if "risk_off" in mc_low or "caution" in mc_low or "bearish" in mc_low[:800]:
        risks.append("Market pulse skews cautious — favor tighter risk.")
    try:
        acc = float(stats.get("prediction_accuracy") or 0)
        if acc > 0 and acc < 48:
            risks.append("Brain prediction accuracy is soft — size down on scanner-only ideas.")
    except (TypeError, ValueError):
        pass
    if sell_count > buy_count and denom > 3:
        risks.append("Recent scan sample has more sell than buy rows.")
    if not risks:
        risks.append("No major flags from automated counters; still use stops and plan risk.")

    insights = get_insights(db, user_id, limit=8)
    ins_line = ""
    if insights:
        top_i = max(insights, key=lambda x: float(x.confidence or 0))
        ins_line = (
            f"\nStrongest learned pattern signal: [{top_i.confidence:.0%}] "
            f"{(top_i.pattern_description or '')[:160]}"
        )

    stance_tag = stance.upper()
    reply_parts: list[str] = [
        f"**STANCE: {stance_tag}**",
        "",
    ]
    if market_ctx.strip():
        reply_parts.extend(
            [
                "Market pulse (cached):",
                market_ctx[:1200] + ("…" if len(market_ctx) > 1200 else ""),
                "",
            ]
        )
    reply_parts.extend(
        [
            f"Scan mix (last {len(recent_scans)} rows): {buy_count} buy, {hold_count} hold, {sell_count} sell.",
            "",
            "Top buy-scored ideas:",
            *(idea_lines if idea_lines else ["- (No buy signals in recent sample)"]),
            "",
            "Risks:",
            *[f"- {r}" for r in risks[:5]],
            "",
            (
                f"Brain: {stats['total_patterns']} patterns, "
                f"{stats['avg_confidence']}% avg confidence, "
                f"{stats['prediction_accuracy']}% accuracy "
                f"({stats['total_predictions']} predictions)."
            ),
        ]
    )
    if ins_line.strip():
        reply_parts.append(ins_line)
    reply_parts.extend(["", "_Template thesis (no LLM)._"])

    reply = "\n".join(reply_parts).strip()

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

    _thesis_cache[cache_key] = (_time.time(), thesis_data)
    generate_market_thesis._cache = _thesis_cache
    return thesis_data
