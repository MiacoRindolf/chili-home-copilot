"""Thesis generation: convert technical signals into persuasive English.

Also provides structured pick explainability via ``build_evidence_summary``,
which returns machine-readable reasoning that the UI can render as an
evidence panel (why_ranked, key_contributors, invalidation, state_note).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Evidence / explainability ───────────────────────────────────────────────

def _top_contributors(pick: dict[str, Any], *, max_items: int = 4) -> list[dict[str, Any]]:
    """Extract the strongest contributing signals as labelled evidence items."""
    contributors: list[dict[str, Any]] = []

    indicators = pick.get("indicators") or {}
    signals = pick.get("signals") or []
    score = float(pick.get("score") or 0)

    # Score contribution
    if score >= 7:
        contributors.append({"label": "Composite score", "value": f"{score}/10", "weight": "high"})
    elif score >= 5:
        contributors.append({"label": "Composite score", "value": f"{score}/10", "weight": "medium"})

    # Backtest support
    bt_wr = pick.get("backtest_win_rate")
    bt_ret = pick.get("backtest_return")
    if bt_wr is not None:
        contributors.append({
            "label": "Historical win rate",
            "value": f"{float(bt_wr):.0f}%",
            "weight": "high" if float(bt_wr) > 60 else "medium",
        })
    if bt_ret is not None:
        contributors.append({
            "label": "Avg backtest return",
            "value": f"{float(bt_ret):+.1f}%",
            "weight": "high" if float(bt_ret) > 5 else "medium",
        })

    # Risk/reward
    rr = pick.get("risk_reward")
    if rr and float(rr) >= 1.5:
        contributors.append({
            "label": "Risk/reward",
            "value": f"{float(rr):.1f}:1",
            "weight": "high" if float(rr) >= 2.5 else "medium",
        })

    # Technical signals
    _sig_weights = {
        "breakout": "high",
        "squeeze firing": "high",
        "golden cross": "high",
        "vcp": "high",
        "above vwap": "medium",
        "volume surge": "medium",
        "macd bullish": "medium",
        "ema stacking bullish": "medium",
        "nr7": "medium",
        "rsi oversold": "medium",
        "adx trending": "medium",
    }
    for sig in signals:
        sig_l = sig.lower()
        matched_weight = None
        for key, weight in _sig_weights.items():
            if key in sig_l:
                matched_weight = weight
                break
        if matched_weight:
            contributors.append({"label": sig, "value": None, "weight": matched_weight})

    # Indicator values
    rsi = indicators.get("rsi")
    if rsi is not None:
        contributors.append({
            "label": "RSI",
            "value": f"{float(rsi):.1f}",
            "weight": "high" if float(rsi) < 30 or float(rsi) > 70 else "low",
        })

    adx = indicators.get("adx")
    if adx is not None and float(adx) > 25:
        contributors.append({
            "label": "ADX (trend strength)",
            "value": f"{float(adx):.1f}",
            "weight": "high" if float(adx) > 35 else "medium",
        })

    # Deduplicate and trim
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for c in contributors:
        key = c["label"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique[:max_items]


def _why_ranked(pick: dict[str, Any]) -> str:
    """Produce a 1-sentence "why this ranked" explanation."""
    score = float(pick.get("score") or 0)
    signal = pick.get("signal", "buy")
    ticker = pick.get("ticker", "")
    bt_wr = pick.get("backtest_win_rate")
    bt_ret = pick.get("backtest_return")
    signals = pick.get("signals") or []

    # Find the most prominent signal keyword
    breakout_kws = ["breakout", "squeeze firing", "golden cross", "vcp", "nr7"]
    lead_signal = None
    for sig in signals:
        for kw in breakout_kws:
            if kw in sig.lower():
                lead_signal = sig
                break
        if lead_signal:
            break

    parts: list[str] = []
    if score >= 8:
        parts.append(f"{ticker} scored {score:.0f}/10")
    elif score >= 6:
        parts.append(f"{ticker} ranked {score:.0f}/10")
    else:
        parts.append(f"{ticker} appeared in the scan with a score of {score:.0f}/10")

    direction = "bullish" if signal in ("buy", "long") else "bearish" if signal in ("sell", "short") else "neutral"
    parts.append(f"on a {direction} setup")

    if lead_signal:
        parts.append(f"driven by {lead_signal.lower()}")

    if bt_wr is not None and bt_ret is not None:
        parts.append(f"with {float(bt_wr):.0f}% historical win rate ({float(bt_ret):+.1f}% avg return)")
    elif bt_wr is not None:
        parts.append(f"with {float(bt_wr):.0f}% historical win rate")

    return " ".join(parts) + "."


def _invalidation_triggers(pick: dict[str, Any]) -> list[str]:
    """Return a list of conditions that would invalidate the pick."""
    triggers: list[str] = []
    stop = pick.get("stop_loss")
    entry = pick.get("entry_price") or pick.get("price")
    signal = pick.get("signal", "buy")
    indicators = pick.get("indicators") or {}

    if stop and entry:
        triggers.append(f"Price closes below ${float(stop):.2f} (stop loss)")

    if signal in ("buy", "long"):
        rsi = indicators.get("rsi")
        if rsi is not None and float(rsi) > 70:
            triggers.append("RSI crosses into extreme overbought (>80)")
        triggers.append("MACD crosses bearish (histogram turns negative)")
        triggers.append("Volume drops sharply below 20-day average on any up day")
    elif signal in ("sell", "short"):
        triggers.append("Price reclaims the breakdown level on volume")
        triggers.append("MACD crosses bullish (histogram turns positive)")

    triggers.append("Market regime shifts to risk-off (VIX spike)")
    return triggers[:4]


def build_evidence_summary(pick: dict[str, Any]) -> dict[str, Any]:
    """Build a structured evidence panel for a pick or proposal.

    Returns:
        {
            "why_ranked": str,             # 1-sentence explanation
            "key_contributors": list,      # top signals/indicators with weights
            "invalidation": list[str],     # conditions that kill the thesis
            "state_note": str | None,      # freshness / provenance note
        }

    The returned dict is safe to attach as ``evidence_summary`` on any
    pick/proposal response without changing existing fields.
    """
    freshness = pick.get("freshness") or pick.get("as_of")
    pattern_id = pick.get("scan_pattern_id") or pick.get("pattern_id")
    bt_strategy = pick.get("best_strategy") or pick.get("strategy_name")

    state_parts: list[str] = []
    if freshness:
        state_parts.append(f"Data as of {freshness}")
    if pattern_id:
        state_parts.append(f"Pattern #{pattern_id}")
    if bt_strategy:
        state_parts.append(f"Strategy: {bt_strategy}")
    if pick.get("is_stale"):
        state_parts.append("WARNING: data may be stale")

    return {
        "why_ranked": _why_ranked(pick),
        "key_contributors": _top_contributors(pick),
        "invalidation": _invalidation_triggers(pick),
        "state_note": " | ".join(state_parts) if state_parts else None,
    }


def enrich_picks_with_evidence(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach evidence_summary to each pick dict in-place. Returns the same list."""
    for pick in picks:
        try:
            pick["evidence_summary"] = build_evidence_summary(pick)
        except Exception as e:
            logger.debug("[thesis] evidence build failed for %s: %s", pick.get("ticker"), e)
    return picks

_SIGNAL_TRANSLATIONS: dict[str, str] = {
    "rsi oversold": "RSI has dipped into oversold territory, suggesting a potential bounce.",
    "rsi overbought": "RSI is elevated in overbought territory -- momentum may be fading.",
    "macd bullish cross": "MACD just crossed bullish, signalling rising momentum.",
    "macd bearish cross": "MACD has crossed bearish, hinting at weakening momentum.",
    "macd positive": "MACD histogram is positive, confirming upward momentum.",
    "ema stacking bullish": "Moving averages are stacking upward -- a classic bullish alignment.",
    "ema stacking bearish": "Moving averages are stacking downward -- bearish alignment.",
    "golden cross": "A golden cross (50-day crossing above 200-day MA) has formed.",
    "death cross": "A death cross has formed, a longer-term bearish signal.",
    "volume surge": "Volume is surging well above average, showing strong participation.",
    "above vwap": "Price is trading above VWAP, indicating intraday bullish control.",
    "below vwap": "Price has slipped below VWAP, suggesting intraday selling pressure.",
    "breakout": "Price is breaking out of a consolidation range.",
    "gap up": "The stock gapped up at open, showing overnight demand.",
    "bollinger squeeze": "Bollinger Bands are squeezing -- a big move may be imminent.",
    "adx trending": "ADX is elevated, confirming a strong directional trend.",
}


def _bt_wr_pct_for_display(bt_wr: float | None) -> float | None:
    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    return backtest_win_rate_db_to_display_pct(bt_wr)


def build_conversational_thesis(pick: dict[str, Any]) -> str:
    """Produce a 2-4 sentence plain-English thesis that 'sells' the pick."""
    ticker = pick.get("ticker", "")
    direction = pick.get("signal", "buy")
    signals = pick.get("signals") or []
    indicators = pick.get("indicators") or {}
    rr = pick.get("risk_reward")
    bt_strategy = pick.get("best_strategy")
    bt_return = pick.get("backtest_return")
    bt_wr = pick.get("backtest_win_rate")

    parts: list[str] = []

    if direction == "buy":
        rsi = indicators.get("rsi")
        if rsi and rsi < 35:
            parts.append(f"{ticker} is flashing a bullish setup with RSI pulling back to {rsi:.0f}.")
        else:
            parts.append(f"{ticker} is showing a strong bullish setup.")
    elif direction == "sell":
        parts.append(f"{ticker} is displaying bearish signals that warrant caution.")
    else:
        parts.append(f"{ticker} has a developing setup worth watching.")

    translated: list[str] = []
    for sig in signals[:4]:
        sig_lower = sig.lower().strip()
        matched = False
        for key, sentence in _SIGNAL_TRANSLATIONS.items():
            if key in sig_lower:
                translated.append(sentence)
                matched = True
                break
        if not matched and len(sig) > 10:
            translated.append(sig.rstrip(".") + ".")
    if translated:
        parts.append(" ".join(translated[:2]))

    if rr and rr > 1:
        parts.append(
            f"With a {rr:.1f}:1 risk-to-reward ratio, "
            f"the potential upside meaningfully outweighs the downside."
        )

    if bt_strategy and bt_wr is not None:
        ret_str = f" returning {bt_return:+.1f}%" if bt_return else ""
        parts.append(
            f"Historical backtesting of the {bt_strategy} strategy shows "
            f"a {float(bt_wr):.0f}% win rate{ret_str}."
        )

    return " ".join(parts)


def make_plain_english(scored: dict, insights: str) -> str:
    """Convert technical signals into beginner-friendly language."""
    parts = []
    signal = scored["signal"]
    is_crypto = scored.get("ticker", "").endswith("-USD") or scored.get("is_crypto")
    asset = "coin" if is_crypto else "stock"

    if signal in ("buy", "long"):
        parts.append(f"This {asset} looks like a good buying opportunity right now.")
    elif signal in ("sell", "short"):
        parts.append(f"This {asset} might be overpriced. Consider taking profits.")
    elif signal == "watch":
        parts.append(f"This {asset} is worth watching closely -- a big move may be building.")
    else:
        parts.append(f"No strong signal either way. Best to wait for a clearer setup.")

    for s in scored.get("signals", [])[:5]:
        sl = s.lower()
        if "oversold" in sl:
            parts.append("The price has dropped a lot and may be due for a bounce.")
        elif "overbought" in sl or "overextended" in sl:
            parts.append("The price has risen sharply and may pull back soon.")
        elif "uptrend" in sl:
            parts.append("The overall direction has been up, which is a good sign.")
        elif "downtrend" in sl:
            parts.append("The overall direction has been down, so be cautious.")
        elif "volume explosion" in sl or "massive volume" in sl:
            parts.append("Trading volume just exploded -- a sign that something major is happening.")
        elif "volume surge" in sl or "strong volume" in sl:
            parts.append("Trading activity just spiked, which often signals a big move.")
        elif "low volume" in sl:
            parts.append("Volume is low, so any price move could be a fakeout -- wait for confirmation.")
        elif "squeeze firing" in sl:
            parts.append("The price has been coiling in a tight range and is now breaking out -- this is a high-probability setup.")
        elif "squeeze" in sl and "bollinger" in sl:
            parts.append("The price has been trading in a very tight range (consolidation). This often leads to a big move soon.")
        elif "confirmed breakout" in sl:
            parts.append("The price just broke out of its normal range on strong volume -- this is a confirmed breakout.")
        elif "atr expanding" in sl:
            parts.append("Price swings are getting larger, which usually means a breakout is in progress.")
        elif "atr compressed" in sl or "coiled spring" in sl:
            parts.append("Price movement has gotten very quiet -- like a coiled spring, it often explodes after this.")
        elif "bullish ema stack" in sl or "ema stack" in sl:
            parts.append("Short, medium, and long-term trends are all aligned upward -- strong bullish momentum.")
        elif "bearish ema" in sl:
            parts.append("The trend is pointing down across multiple timeframes.")
        elif "macd bullish" in sl:
            parts.append("Momentum indicators suggest buyers are stepping in.")
        elif "macd" in sl and ("negative" in sl or "bearish" in sl):
            parts.append("Momentum has turned negative -- sellers are in control.")
        elif "above vwap" in sl:
            parts.append("The price is above the average trading price today -- institutions are buying.")
        elif "below vwap" in sl:
            parts.append("The price is below the average trading price today -- watch for support.")
        elif "bollinger" in sl:
            parts.append("The price is near a statistical extreme and often bounces from here.")
        elif "hot mover" in sl or "top gainer" in sl or "strong gainer" in sl:
            parts.append(f"This {asset} is one of the biggest movers right now -- high activity.")
        elif "volume awakening" in sl:
            parts.append("Volume is picking up inside the squeeze -- like a car revving before the light turns green.")
        elif "stochastic curl" in sl:
            parts.append("Momentum is starting to build inside the tight range -- early buyers are stepping in.")
        elif "higher lows" in sl and "resistance" in sl:
            parts.append("Buyers are pushing the floor higher while hitting the same ceiling -- pressure is building for a breakout.")
        elif "vcp" in sl and "3" in sl:
            parts.append("The price has been pulling back in tighter and tighter waves with less volume each time -- a classic 'coiled spring' that often explodes upward.")
        elif "vcp" in sl:
            parts.append("Price pullbacks are getting tighter -- the selling pressure is drying up.")
        elif "nr7" in sl:
            parts.append("Today's price range is the tightest in 7 bars -- like a rubber band pulled tight, a big move is likely very soon.")
        elif "nr4" in sl:
            parts.append("Price range is unusually narrow -- the calm before the storm.")
        elif "multi-tf" in sl or "multi timeframe" in sl.replace("-", " "):
            parts.append("The trend on the bigger picture (higher timeframe) confirms what the short-term chart is showing -- strong alignment.")
        elif "vwap reclaim" in sl:
            parts.append("The price just jumped back above the average institutional trading price -- big money is likely buying here.")
        elif "opening range breakout" in sl or "orb" in sl:
            parts.append("The price broke above the first 30 minutes' high -- this often sets the direction for the rest of the day.")
        elif "accumulation" in sl and "obv" in sl:
            parts.append("Smart money appears to be quietly buying while the price stays flat -- a breakout often follows.")
        elif "divergence" in sl and "rsi" in sl:
            parts.append("Warning: the momentum indicator disagrees with the price -- the breakout may be a fake-out.")
        elif "divergence" in sl and "macd" in sl:
            parts.append("Warning: underlying momentum is weakening even as price moves higher -- proceed with caution.")
        elif "engulfing" in sl:
            parts.append("A strong bullish candle just swallowed the previous bearish one -- buyers are taking over.")
        elif "hammer" in sl:
            parts.append("A hammer candle appeared -- sellers pushed the price down but buyers snapped it right back up.")

    risk = scored.get("risk_level", "medium")
    if risk == "high":
        parts.append("Risk is HIGH -- only use money you're comfortable losing.")
    elif risk == "low":
        parts.append(f"This is a relatively stable {asset} with lower risk.")

    return " ".join(parts)


def build_smart_pick_context_strings(db: Session, ctx: dict[str, Any]) -> str:
    """Render the human-readable context string for the LLM from structured ctx."""
    from ...models.trading import BacktestResult

    top_picks: list[dict[str, Any]] = ctx["top_picks"]
    total_scanned: int = ctx["total_scanned"]
    stats: dict[str, Any] = ctx.get("stats") or {}
    insights = ctx.get("insights") or []
    budget = ctx.get("budget")
    risk_tolerance: str = ctx.get("risk_tolerance", "medium")
    portfolio_ctx: str | None = ctx.get("portfolio_ctx")

    pick_details: list[str] = []
    for p in top_picks:
        detail = (
            f"**{p['ticker']}** — Score: {p['score']}/10, Signal: {p['signal'].upper()}\n"
            f"  Price: ${p['price']} | Entry: ${p['entry_price']} | Stop: ${p['stop_loss']} | Target: ${p['take_profit']}\n"
            f"  Risk: {p['risk_level'].upper()} | Signals: {', '.join(p['signals'])}\n"
            f"  Indicators: RSI={p['indicators'].get('rsi', 'N/A')}, "
            f"MACD={p['indicators'].get('macd', 'N/A')}, "
            f"ADX={p['indicators'].get('adx', 'N/A')}"
        )

        best_bt = db.query(BacktestResult).filter(
            BacktestResult.ticker == p["ticker"],
        ).order_by(BacktestResult.return_pct.desc()).first()
        if best_bt:
            _wrp = _bt_wr_pct_for_display(best_bt.win_rate)
            _wr_txt = f"{_wrp:.0f}%" if _wrp is not None else "N/A"
            detail += (
                f"\n  Best backtest: {best_bt.strategy_name} → "
                f"{best_bt.return_pct:+.1f}% return, {_wr_txt} win rate"
            )

        pick_details.append(detail)

    context_parts: list[str] = [
        f"## MARKET SCAN RESULTS — Top {len(top_picks)} candidates from {total_scanned:,} stocks & crypto scanned",
        "\n\n".join(pick_details),
    ]

    if stats.get("total_trades", 0) > 0:
        context_parts.append(
            f"## USER PROFILE\n"
            f"Experience: {stats['total_trades']} trades, {stats['win_rate']}% win rate, "
            f"Total P&L: ${stats['total_pnl']}"
        )
    else:
        context_parts.append(
            "## USER PROFILE\nBeginner trader with no closed trades yet. "
            "Recommend safer, high-confidence setups with clear instructions."
        )

    if insights:
        lines = ["## LEARNED PATTERNS (your edge)"]
        for ins in insights:
            lines.append(f"- [{ins.confidence:.0%}] {ins.pattern_description}")
        context_parts.append("\n".join(lines))

    if budget:
        context_parts.append(f"## BUDGET\nUser has ${budget:,.2f} available to invest.")

    context_parts.append(f"## RISK TOLERANCE: {risk_tolerance.upper()}")

    if portfolio_ctx:
        context_parts.insert(0, portfolio_ctx)

    return "\n\n".join(context_parts)
