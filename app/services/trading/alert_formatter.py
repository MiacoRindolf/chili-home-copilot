"""Centralized Telegram / HTML alert message formatter.

Pure functions -- no DB, no service imports. Every public ``format_*``
function returns a ready-to-send HTML string safe for Telegram's
``parse_mode: "HTML"``.
"""

from __future__ import annotations

from html import escape as _esc

# ── Emoji map (one per alert semantic) ─────────────────────────────

ALERT_EMOJI: dict[str, str] = {
    "target_hit":                "\U0001f3af",   # 🎯
    "stop_hit":                  "\U0001f6d1",   # 🛑
    "time_exit":                 "\u23f0",        # ⏰
    "stop_approaching":          "\u26a0\ufe0f",  # ⚠️
    "breakeven_reached":         "\U0001f504",   # 🔄
    "stop_tightened":            "\U0001f527",   # 🔧
    "position_opened":           "\U0001f4e5",   # 📥
    "position_closed":           "\U0001f4e4",   # 📤
    "breakout_triggered":        "\U0001f680",   # 🚀
    "crypto_breakout":           "\U0001f680",   # 🚀
    "crypto_squeeze_firing":     "\U0001f4a5",   # 💥
    "stock_breakout":            "\U0001f4ca",   # 📊
    "momentum_immaculate":       "\u26a1",        # ⚡
    "strategy_proposed":         "\U0001f4a1",   # 💡
    "pattern_breakout_imminent": "\U0001f514",   # 🔔
    "test":                      "\U0001f9ea",   # 🧪
    "order_filled":              "\u2705",        # ✅
    "order_placed":              "\u23f3",        # ⏳
    "order_failed":              "\u274c",        # ❌
    "order_blocked":             "\U0001f6ab",   # 🚫
}

_SEP = "\u2500" * 16  # ────────────────


# ── Helpers ────────────────────────────────────────────────────────

def _h(text: object) -> str:
    """HTML-escape arbitrary dynamic content."""
    if text is None or text == "":
        return ""
    return _esc(str(text))


def _price(val: float | None, *, crypto: bool = False) -> str:
    """Format a price wrapped in ``<code>``."""
    if val is None:
        return "n/a"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return f"<code>{_h(val)}</code>"
    if crypto and v < 1:
        return f"<code>${v:,.6f}</code>"
    return f"<code>${v:,.2f}</code>"


def _ctx(strategy_tag: str, lifecycle_tag: str, regime: str = "") -> str:
    """Build an italic context line from strategy/lifecycle/regime."""
    parts: list[str] = []
    if strategy_tag:
        parts.append(f"strategy: {_h(strategy_tag)}")
    if lifecycle_tag:
        parts.append(f"lifecycle: {_h(lifecycle_tag)}")
    if regime:
        parts.append(f"regime: {_h(regime)}")
    if not parts:
        return ""
    return "<i>" + ", ".join(parts) + "</i>"


def _ticker_code(ticker: str) -> str:
    return f"<code>{_h(ticker)}</code>"


def _is_crypto(ticker: str) -> bool:
    return str(ticker).endswith("-USD")


# ── Stop engine alerts ─────────────────────────────────────────────

def format_stop_hit(
    ticker: str,
    price: float,
    reason: str,
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    emoji = ALERT_EMOJI["stop_hit"]
    lines = [
        f"{emoji} <b>STOP HIT</b>  {_ticker_code(ticker)}",
        f"Price {_price(price, crypto=_is_crypto(ticker))}",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


def format_time_exit(
    ticker: str,
    price: float,
    reason: str,
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    emoji = ALERT_EMOJI["time_exit"]
    lines = [
        f"{emoji} <b>TIME EXIT</b>  {_ticker_code(ticker)}",
        f"Price {_price(price, crypto=_is_crypto(ticker))}",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


def format_target_hit(
    ticker: str,
    price: float,
    reason: str = "",
    *,
    pnl_pct: float | None = None,
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    emoji = ALERT_EMOJI["target_hit"]
    price_str = _price(price, crypto=_is_crypto(ticker))
    pnl = f"  |  <b>+{pnl_pct:.1f}%</b>" if pnl_pct is not None else ""
    lines = [
        f"{emoji} <b>TARGET HIT</b>  {_ticker_code(ticker)}",
        f"Price {price_str}{pnl}",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


def format_stop_approaching(
    ticker: str,
    price: float,
    reason: str = "",
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    emoji = ALERT_EMOJI["stop_approaching"]
    lines = [
        f"{emoji} <b>STOP APPROACHING</b>  {_ticker_code(ticker)}",
        f"Price {_price(price, crypto=_is_crypto(ticker))}",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


def format_breakeven(
    ticker: str,
    reason: str = "",
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    emoji = ALERT_EMOJI["breakeven_reached"]
    lines = [
        f"{emoji} <b>BREAKEVEN</b>  {_ticker_code(ticker)}",
        "Stop moved to entry",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


def format_stop_tightened(
    ticker: str,
    old_stop: float,
    new_stop: float,
    reason: str = "",
    strategy_tag: str = "",
    lifecycle_tag: str = "",
    regime: str = "",
) -> str:
    crypto = _is_crypto(ticker)
    emoji = ALERT_EMOJI["stop_tightened"]
    lines = [
        f"{emoji} <b>STOP TIGHTENED</b>  {_ticker_code(ticker)}",
        f"{_price(old_stop, crypto=crypto)} \u2192 {_price(new_stop, crypto=crypto)}",
    ]
    ctx = _ctx(strategy_tag, lifecycle_tag, regime)
    if ctx:
        lines.append(ctx)
    if reason:
        lines.append(_SEP)
        lines.append(_h(reason))
    return "\n".join(lines)


# ── Legacy position-check alerts (alerts.py) ──────────────────────

def format_legacy_target_hit(
    ticker: str,
    price: float,
    target: float,
    pnl_pct: float,
    trade_type_label: str = "",
) -> str:
    emoji = ALERT_EMOJI["target_hit"]
    crypto = _is_crypto(ticker)
    label_part = f"  <i>{_h(trade_type_label)}</i>" if trade_type_label else ""
    return "\n".join([
        f"{emoji} <b>TARGET HIT</b>{label_part}  {_ticker_code(ticker)}",
        f"Price {_price(price, crypto=crypto)}  (target {_price(target, crypto=crypto)})",
        f"PnL <b>+{pnl_pct:.1f}%</b>",
        _SEP,
        "Consider taking profits",
    ])


def format_legacy_stop_hit(
    ticker: str,
    price: float,
    stop: float,
    pnl_pct: float,
    trade_type_label: str = "",
) -> str:
    emoji = ALERT_EMOJI["stop_hit"]
    crypto = _is_crypto(ticker)
    label_part = f"  <i>{_h(trade_type_label)}</i>" if trade_type_label else ""
    return "\n".join([
        f"{emoji} <b>STOP HIT</b>{label_part}  {_ticker_code(ticker)}",
        f"Price {_price(price, crypto=crypto)}  (stop {_price(stop, crypto=crypto)})",
        f"PnL <b>{pnl_pct:.1f}%</b>",
        _SEP,
        "Consider cutting losses",
    ])


# ── Breakout (price monitor) ──────────────────────────────────────

def format_breakout(
    ticker: str,
    price: float,
    resistance: float,
    score: float,
    rationale: str = "",
    dur_label: str = "",
    l2_note: str = "",
) -> str:
    emoji = ALERT_EMOJI["breakout_triggered"]
    crypto = _is_crypto(ticker)
    lines = [
        f"{emoji} <b>BREAKOUT</b>  {_ticker_code(ticker)}",
        f"Broke {_price(resistance, crypto=crypto)}  \u2192  now {_price(price, crypto=crypto)}",
        f"Score <b>{score:.1f}</b>/10",
    ]
    if dur_label:
        lines.append(_h(dur_label))
    if l2_note:
        lines.append(_h(l2_note))
    if rationale:
        lines.append(_SEP)
        lines.append(f"<i>{_h(rationale[:80])}</i>")
    return "\n".join(lines)


# ── Strategy proposed ─────────────────────────────────────────────

def format_strategy_proposed(
    ticker: str,
    price: float,
    stop: float,
    target: float,
    rr_ratio: float,
    projected_profit_pct: float,
    confidence: float,
    trade_type_label: str = "",
    duration_label: str = "",
) -> str:
    emoji = ALERT_EMOJI["strategy_proposed"]
    crypto = _is_crypto(ticker)
    lines = [
        f"{emoji} <b>{_h(trade_type_label or 'TRADE')}</b>  BUY {_ticker_code(ticker)}",
        (
            f"Entry {_price(price, crypto=crypto)}  |  "
            f"Stop {_price(stop, crypto=crypto)}  |  "
            f"Target {_price(target, crypto=crypto)}"
        ),
        (
            f"R:R <b>{rr_ratio:.1f}:1</b>  |  "
            f"+{projected_profit_pct:.1f}%  |  "
            f"Conf {confidence:.0f}%"
        ),
    ]
    if duration_label:
        lines.append(f"Est. Hold: {_h(duration_label)}")
    lines.append(_SEP)
    lines.append("Review in app")
    return "\n".join(lines)


# ── Order / position alerts ───────────────────────────────────────

def format_order_filled(
    ticker: str,
    quantity: object,
    entry_price: float,
    broker: str,
    proposal_id: int | None = None,
) -> str:
    emoji = ALERT_EMOJI["order_filled"]
    crypto = _is_crypto(ticker)
    lines = [
        f"{emoji} <b>ORDER FILLED</b>  {_ticker_code(ticker)}",
        f"BUY {_h(quantity)} @ {_price(entry_price, crypto=crypto)} via {_h(broker)}",
    ]
    if proposal_id is not None:
        lines.append(f"Proposal #{proposal_id}")
    return "\n".join(lines)


def format_order_placed(
    ticker: str,
    quantity: object,
    entry_price: float,
    broker: str,
    proposal_id: int | None = None,
) -> str:
    emoji = ALERT_EMOJI["order_placed"]
    crypto = _is_crypto(ticker)
    lines = [
        f"{emoji} <b>ORDER PLACED</b>  {_ticker_code(ticker)}",
        f"BUY {_h(quantity)} @ {_price(entry_price, crypto=crypto)} (limit, waiting)",
        f"via {_h(broker)}",
    ]
    if proposal_id is not None:
        lines.append(f"Proposal #{proposal_id}")
    return "\n".join(lines)


def format_order_failed(ticker: str, error: str) -> str:
    emoji = ALERT_EMOJI["order_failed"]
    return f"{emoji} <b>ORDER FAILED</b>  {_ticker_code(ticker)}\n{_h(error)}"


def format_order_blocked(ticker: str, reason: str) -> str:
    emoji = ALERT_EMOJI["order_blocked"]
    return f"{emoji} <b>ORDER BLOCKED</b>  {_ticker_code(ticker)}\n{_h(reason)}"


# ── Pattern imminent ──────────────────────────────────────────────

def format_pattern_imminent(
    ticker: str,
    pattern_name: str,
    pattern_id: int,
    price: object,
    readiness: float,
    composite_score: float,
    eta_txt: str,
    hold_line: str = "",
    entry_price: object = None,
    stop_loss: object = None,
    take_profit: object = None,
    description: str = "",
    signals: str = "",
) -> str:
    emoji = ALERT_EMOJI["pattern_breakout_imminent"]
    crypto = _is_crypto(ticker)
    lines = [
        f"{emoji} <b>IMMINENT PATTERN</b>  {_ticker_code(ticker)}  #{pattern_id}",
        f"<i>{_h(pattern_name)}</i>",
        f"Readiness <b>{readiness:.0%}</b>  |  Score <code>{composite_score:.2f}</code>",
        f"\u23f3 Breakout ETA: {_h(eta_txt)} (heuristic)",
    ]
    if hold_line:
        lines.append(f"\U0001f4cf Est. Hold: {_h(hold_line)}")
    lines.append(_SEP)
    lines.append(
        f"Entry {_price(entry_price, crypto=crypto)}  |  "
        f"Stop {_price(stop_loss, crypto=crypto)}  |  "
        f"Target {_price(take_profit, crypto=crypto)}"
    )
    if description:
        lines.append(f"<i>{_h(description)}</i>")
    if signals:
        lines.append(_h(signals))
    return "\n".join(lines)


# ── Scanner: crypto breakout ──────────────────────────────────────

def format_crypto_breakout(
    ticker: str,
    trade_label: str,
    score: float,
    price: object,
    change_24h: float = 0.0,
    rvol: float = 0.0,
    ema_alignment: str = "n/a",
    flag_line: str = "",
    entry_price: object = None,
    stop_loss: object = None,
    take_profit: object = None,
    duration: str = "",
    sig_text: str = "",
) -> str:
    emoji = ALERT_EMOJI["crypto_breakout"]
    lines = [
        f"{emoji} <b>{_h(trade_label)}</b>  {_ticker_code(ticker)}",
        (
            f"Score <b>{score}</b>/10  |  "
            f"{_price(price, crypto=True)}  "
            f"({change_24h:+.1f}% 24h)"
        ),
        f"RVOL {rvol:.1f}x  |  EMA: {_h(ema_alignment.replace('_', ' '))}",
    ]
    if flag_line:
        lines.append(_h(flag_line))
    lines.append(
        f"Entry {_price(entry_price, crypto=True)}  |  "
        f"Stop {_price(stop_loss, crypto=True)}  |  "
        f"Target {_price(take_profit, crypto=True)}"
    )
    if duration:
        lines.append(f"Est. Hold: {_h(duration)}")
    if sig_text:
        lines.append(f"<i>{_h(sig_text)}</i>")
    lines.append(_SEP)
    lines.append("<i>Heuristic scan, not a Brain ScanPattern</i>")
    return "\n".join(lines)


# ── Scanner: stock breakout ───────────────────────────────────────

def format_stock_breakout(
    ticker: str,
    trade_label: str,
    score: float,
    price: object,
    dist_to_breakout: float = 0.0,
    flag_line: str = "",
    entry_price: object = None,
    stop_loss: object = None,
    take_profit: object = None,
    duration: str = "",
    sig_text: str = "",
) -> str:
    emoji = ALERT_EMOJI["stock_breakout"]
    lines = [
        f"{emoji} <b>{_h(trade_label)}</b>  {_ticker_code(ticker)}",
        f"Score <b>{score}</b>/10  |  {_price(price)}",
        f"Dist to breakout: {dist_to_breakout:.1f}%",
    ]
    if flag_line:
        lines.append(_h(flag_line))
    lines.append(
        f"Entry {_price(entry_price)}  |  "
        f"Stop {_price(stop_loss)}  |  "
        f"Target {_price(take_profit)}"
    )
    if duration:
        lines.append(f"Est. Hold: {_h(duration)}")
    if sig_text:
        lines.append(f"<i>{_h(sig_text)}</i>")
    lines.append(_SEP)
    lines.append("<i>Heuristic scan, not a Brain ScanPattern</i>")
    return "\n".join(lines)


# ── Scanner: momentum ─────────────────────────────────────────────

def format_momentum(
    ticker: str,
    trade_label: str,
    score: float,
    price: object,
    vol_ratio: float = 0.0,
    risk_reward: float = 0.0,
    duration: str = "",
    signals: str = "",
) -> str:
    emoji = ALERT_EMOJI["momentum_immaculate"]
    dur_part = f"  |  ETA {_h(duration)}" if duration else ""
    lines = [
        f"{emoji} <b>MOMENTUM {_h(trade_label)}</b>  {_ticker_code(ticker)}",
        (
            f"Score <b>{score}</b>/10  |  {_price(price)}  |  "
            f"Vol {vol_ratio:.1f}x  |  R:R {risk_reward:.1f}{dur_part}"
        ),
    ]
    if signals:
        lines.append(f"<i>{_h(signals)}</i>")
    return "\n".join(lines)


# ── Test alert ────────────────────────────────────────────────────

def format_test_alert() -> str:
    emoji = ALERT_EMOJI["test"]
    return "\n".join([
        f"{emoji} <b>CHILI Test Alert</b>",
        "Telegram notifications are working.",
        "You will receive alerts for breakouts, targets, stops, and strategy proposals.",
    ])
