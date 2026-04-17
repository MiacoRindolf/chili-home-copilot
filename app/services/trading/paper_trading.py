"""Paper trading simulation for promoted patterns (LEGACY system).

Auto-enters paper trades when a promoted pattern fires a signal,
auto-exits on stop/target/expiry, and tracks simulated P&L.

Supports ATR-based adaptive stops/targets, trailing stops, spread/slippage
modeling, and pattern-specific exit_config.

NOTE: This is the **legacy** paper trade system using ``PaperTrade`` rows
(table ``trading_paper_trades``).  The **momentum autopilot** system uses
``TradingAutomationSession`` rows instead (see ``momentum_neural/`` package).
The two systems are **independent**:

- Legacy: ``auto_enter_from_signals()`` + ``check_paper_exits()`` + scheduler
  ``paper_trade_check`` job.  Simpler, pattern-driven.
- Autopilot: ``paper_runner`` / ``live_runner`` FSMs with operator controls,
  decision ledger, viability pipeline, and venue adapters.

Both can run simultaneously.  Ensure you check the correct table when
inspecting P&L or open positions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import PaperTrade, ScanPattern

logger = logging.getLogger(__name__)

DEFAULT_PAPER_CAPITAL = 100_000.0
MAX_OPEN_PAPER_TRADES = 20
PAPER_TRADE_EXPIRY_DAYS = 5
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_ATR_STOP_MULT = 2.0
DEFAULT_ATR_TARGET_MULT = 3.0
TRAILING_STOP_ACTIVATION_R = 1.0  # activate trailing after 1R move


def _get_pattern_exit_config(db: Session, scan_pattern_id: int | None) -> dict:
    """Load exit_config from the pattern's ScanPattern row, with defaults."""
    defaults = {
        "atr_stop_mult": DEFAULT_ATR_STOP_MULT,
        "atr_target_mult": DEFAULT_ATR_TARGET_MULT,
        "trailing_enabled": True,
        "trailing_atr_mult": 1.5,
        "max_bars": None,
        "timeframe": "1d",
    }
    if not scan_pattern_id:
        return defaults
    try:
        pat = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
        if pat and pat.exit_config:
            cfg = pat.exit_config if isinstance(pat.exit_config, dict) else json.loads(pat.exit_config)
            defaults.update({k: v for k, v in cfg.items() if v is not None})
        if pat and pat.timeframe:
            defaults["timeframe"] = pat.timeframe
    except Exception:
        pass
    return defaults


def _compute_atr_levels(ticker: str, entry_price: float, exit_cfg: dict) -> tuple[float | None, float | None, float | None]:
    """Compute ATR-based stop, target, and ATR value for a ticker."""
    try:
        from .market_data import fetch_ohlcv_df
        from .indicator_core import compute_atr

        df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
        if df is None or len(df) < 14:
            return None, None, None

        atr_arr = compute_atr(df["High"].values, df["Low"].values, df["Close"].values, period=14)
        atr_val = float(atr_arr[-1]) if len(atr_arr) > 0 else None
        if not atr_val or atr_val <= 0:
            return None, None, None

        stop_dist = atr_val * exit_cfg.get("atr_stop_mult", DEFAULT_ATR_STOP_MULT)
        target_dist = atr_val * exit_cfg.get("atr_target_mult", DEFAULT_ATR_TARGET_MULT)
        return round(entry_price - stop_dist, 4), round(entry_price + target_dist, 4), atr_val
    except Exception:
        return None, None, None


def _apply_slippage(price: float, direction: str, is_entry: bool) -> float:
    """Apply simulated slippage to a fill price."""
    spread_pct = float(getattr(settings, "backtest_spread", DEFAULT_SLIPPAGE_PCT / 100) or 0.0) * 100
    slip = price * spread_pct / 100
    if is_entry:
        return price + slip if direction == "long" else price - slip
    else:
        return price - slip if direction == "long" else price + slip


def _expiry_days_for_timeframe(timeframe: str) -> int:
    """Adaptive expiry based on pattern timeframe."""
    tf_map = {"5m": 1, "15m": 2, "1h": 3, "4h": 5, "1d": 10, "1wk": 30}
    return tf_map.get(timeframe, PAPER_TRADE_EXPIRY_DAYS)


def open_paper_trade(
    db: Session,
    user_id: int | None,
    ticker: str,
    entry_price: float,
    *,
    scan_pattern_id: int | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    direction: str = "long",
    quantity: int = 100,
    signal_json: dict | None = None,
) -> PaperTrade | None:
    """Open a simulated paper trade with ATR-based adaptive levels."""
    open_count = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    ).count()
    if open_count >= MAX_OPEN_PAPER_TRADES:
        logger.debug("[paper] Max open paper trades (%d) reached", MAX_OPEN_PAPER_TRADES)
        return None

    existing = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.ticker == ticker.upper(),
        PaperTrade.status == "open",
        PaperTrade.scan_pattern_id == scan_pattern_id,
    ).first()
    if existing:
        logger.debug("[paper] Already have open paper trade for %s pattern %s", ticker, scan_pattern_id)
        return None

    exit_cfg = _get_pattern_exit_config(db, scan_pattern_id)
    atr_val = None

    if stop_price is None or target_price is None:
        atr_stop, atr_target, atr_val = _compute_atr_levels(ticker, entry_price, exit_cfg)
        if stop_price is None:
            stop_price = atr_stop if atr_stop else entry_price * 0.97
        if target_price is None:
            target_price = atr_target if atr_target else entry_price + abs(entry_price - stop_price) * 2

    fill_price = _apply_slippage(entry_price, direction, is_entry=True)

    meta = dict(signal_json or {})
    meta["_paper_meta"] = {
        "original_entry": entry_price,
        "fill_price": fill_price,
        "slippage_applied": round(abs(fill_price - entry_price), 4),
        "atr_value": atr_val,
        "exit_config": exit_cfg,
        "trailing_enabled": exit_cfg.get("trailing_enabled", True),
        "trailing_atr_mult": exit_cfg.get("trailing_atr_mult", 1.5),
        "trailing_stop": None,
        "highest_price": fill_price if direction == "long" else None,
        "lowest_price": fill_price if direction == "short" else None,
        "expiry_days": _expiry_days_for_timeframe(exit_cfg.get("timeframe", "1d")),
    }

    pt = PaperTrade(
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker.upper(),
        direction=direction,
        entry_price=round(fill_price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        quantity=quantity,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json=meta,
    )
    db.add(pt)
    db.flush()

    # Phase A shadow hook: record entry fill in the canonical economic-truth
    # ledger. Legacy PaperTrade.pnl remains authoritative. Any failure is
    # swallowed so the paper path never breaks due to ledger bugs.
    try:
        from . import economic_ledger as _ledger
        if _ledger.mode_is_active():
            _ledger.record_entry_fill(
                db,
                source="paper",
                paper_trade_id=int(pt.id),
                user_id=user_id,
                scan_pattern_id=scan_pattern_id,
                ticker=pt.ticker,
                direction=direction,
                quantity=float(quantity),
                fill_price=float(fill_price),
                fee=0.0,
                event_ts=pt.entry_date,
                provenance={"legacy_path": "open_paper_trade", "atr_value": atr_val},
            )
    except Exception:
        logger.debug("[paper] economic_ledger entry hook failed", exc_info=True)

    logger.info("[paper] Opened paper trade: %s %s @ %.4f (fill=%.4f, stop=%.4f, target=%.4f, atr=%.4f)",
                direction, ticker, entry_price, fill_price, stop_price, target_price, atr_val or 0)
    return pt


def check_paper_exits(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Check all open paper trades for stop/target/trailing-stop/expiry exits.

    Supports ATR trailing stops: once price moves >= 1R in profit, trail the
    stop at trailing_atr_mult * ATR behind the best price seen. The trailing
    stop only tightens, never loosens.
    """
    from .market_data import fetch_quote

    open_trades = db.query(PaperTrade).filter(
        PaperTrade.status == "open",
    )
    if user_id is not None:
        open_trades = open_trades.filter(PaperTrade.user_id == user_id)
    open_trades = open_trades.all()

    if not open_trades:
        return {"checked": 0, "closed": 0, "trailing_updated": 0}

    def _paper_close_ledger(db_sess: Session, ptx: PaperTrade) -> None:
        try:
            from .brain_work.execution_hooks import on_paper_trade_closed
            on_paper_trade_closed(db_sess, ptx)
        except Exception:
            pass

    closed = 0
    trailing_updated = 0
    for pt in open_trades:
        try:
            meta = (pt.signal_json or {}).get("_paper_meta", {})
            expiry = meta.get("expiry_days", PAPER_TRADE_EXPIRY_DAYS)

            quote = fetch_quote(pt.ticker)
            if not quote or not quote.get("price"):
                if pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= expiry:
                    exit_p = _apply_slippage(pt.entry_price, pt.direction, is_entry=False)
                    _close_paper_trade(pt, exit_p, "expired")
                    _paper_close_ledger(db, pt)
                    closed += 1
                continue

            price = float(quote["price"])
            is_long = pt.direction == "long"

            # --- Trailing stop logic ---
            trail_enabled = meta.get("trailing_enabled", False)
            atr_val = meta.get("atr_value")
            trail_mult = meta.get("trailing_atr_mult", 1.5)
            if trail_enabled and atr_val and atr_val > 0:
                risk = abs(pt.entry_price - (pt.stop_price or pt.entry_price * 0.97))
                if is_long:
                    best = max(meta.get("highest_price") or pt.entry_price, price)
                    meta["highest_price"] = best
                    profit_r = (best - pt.entry_price) / risk if risk > 0 else 0
                    if profit_r >= TRAILING_STOP_ACTIVATION_R:
                        new_trail = best - (atr_val * trail_mult)
                        old_trail = meta.get("trailing_stop")
                        if old_trail is None or new_trail > old_trail:
                            meta["trailing_stop"] = round(new_trail, 4)
                            trailing_updated += 1
                else:
                    best = min(meta.get("lowest_price") or pt.entry_price, price)
                    meta["lowest_price"] = best
                    profit_r = (pt.entry_price - best) / risk if risk > 0 else 0
                    if profit_r >= TRAILING_STOP_ACTIVATION_R:
                        new_trail = best + (atr_val * trail_mult)
                        old_trail = meta.get("trailing_stop")
                        if old_trail is None or new_trail < old_trail:
                            meta["trailing_stop"] = round(new_trail, 4)
                            trailing_updated += 1

                sj = dict(pt.signal_json or {})
                sj["_paper_meta"] = meta
                pt.signal_json = sj

            effective_stop = pt.stop_price
            trail_stop = meta.get("trailing_stop")
            if trail_stop is not None:
                if is_long:
                    effective_stop = max(effective_stop or 0, trail_stop)
                else:
                    effective_stop = min(effective_stop or float("inf"), trail_stop)

            exit_price_with_slip = _apply_slippage(price, pt.direction, is_entry=False)

            # Stop hit (includes trailing)
            if is_long and effective_stop and price <= effective_stop:
                reason = "trailing_stop" if trail_stop and trail_stop >= (pt.stop_price or 0) else "stop"
                _close_paper_trade(pt, _apply_slippage(effective_stop, pt.direction, is_entry=False), reason)
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and effective_stop and price >= effective_stop:
                reason = "trailing_stop" if trail_stop and trail_stop <= (pt.stop_price or float("inf")) else "stop"
                _close_paper_trade(pt, _apply_slippage(effective_stop, pt.direction, is_entry=False), reason)
                _paper_close_ledger(db, pt)
                closed += 1
            # Target hit
            elif is_long and pt.target_price and price >= pt.target_price:
                _close_paper_trade(pt, _apply_slippage(pt.target_price, pt.direction, is_entry=False), "target")
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and pt.target_price and price <= pt.target_price:
                _close_paper_trade(pt, _apply_slippage(pt.target_price, pt.direction, is_entry=False), "target")
                _paper_close_ledger(db, pt)
                closed += 1
            # Time-based forced exit for day trades / scalps
            elif pt.entry_date and meta.get("trade_type") in ("scalp", "daytrade", "breakout", "momentum"):
                from .scanner import _MAX_HOLD_HOURS
                _max_h = _MAX_HOLD_HOURS.get(meta["trade_type"])
                if _max_h is not None:
                    _held_h = (datetime.utcnow() - pt.entry_date).total_seconds() / 3600
                    if _held_h >= _max_h:
                        _close_paper_trade(pt, exit_price_with_slip, f"time_exit_{meta['trade_type']}")
                        _paper_close_ledger(db, pt)
                        closed += 1
                        continue
            # Expiry
            elif pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= expiry:
                _close_paper_trade(pt, exit_price_with_slip, "expired")
                _paper_close_ledger(db, pt)
                closed += 1
        except Exception as e:
            logger.debug("[paper] Error checking %s: %s", pt.ticker, e)

    if closed > 0 or trailing_updated > 0:
        db.commit()

    return {"checked": len(open_trades), "closed": closed, "trailing_updated": trailing_updated}


def _close_paper_trade(pt: PaperTrade, exit_price: float, reason: str) -> None:
    """Close a paper trade with P&L calculation."""
    pt.status = "closed"
    pt.exit_date = datetime.utcnow()
    pt.exit_price = exit_price
    pt.exit_reason = reason

    if pt.direction == "long":
        gross_pnl = (exit_price - pt.entry_price) * pt.quantity
        gross_pct = (exit_price - pt.entry_price) / pt.entry_price * 100
    else:
        gross_pnl = (pt.entry_price - exit_price) * pt.quantity
        gross_pct = (pt.entry_price - exit_price) / pt.entry_price * 100

    commission_rate = float(getattr(settings, "backtest_commission", 0.0) or 0.0)
    commission_cost = (pt.entry_price + exit_price) * pt.quantity * commission_rate
    net_pnl = gross_pnl - commission_cost
    notional = max(pt.entry_price * pt.quantity, 1e-9)
    net_pct = (net_pnl / notional) * 100
    pt.pnl = round(net_pnl, 2)
    pt.pnl_pct = round(net_pct, 2)

    logger.info("[paper] Closed %s %s @ %.2f (%s) P&L: $%.2f (%.2f%%)",
                pt.direction, pt.ticker, exit_price, reason, pt.pnl, pt.pnl_pct)

    # Phase A shadow hook: record exit fill + reconcile legacy pnl against
    # the canonical ledger's realized_pnl_delta sum. Shadow-only; legacy
    # pt.pnl remains authoritative. Any failure is swallowed.
    try:
        from sqlalchemy import inspect as _sa_inspect
        from . import economic_ledger as _ledger

        if _ledger.mode_is_active():
            sess = _sa_inspect(pt).session
            if sess is not None:
                # Entry-leg + exit-leg fees are both folded into commission_cost
                # at legacy close time; ledger rows express them on the exit leg
                # only so the sum of realized_pnl_delta matches pt.pnl exactly.
                fee_total = float(commission_cost)
                _ledger.record_exit_fill(
                    sess,
                    source="paper",
                    paper_trade_id=int(pt.id),
                    user_id=pt.user_id,
                    scan_pattern_id=pt.scan_pattern_id,
                    ticker=pt.ticker or "",
                    direction=pt.direction or "long",
                    quantity=float(pt.quantity),
                    fill_price=float(exit_price),
                    entry_price=float(pt.entry_price),
                    fee=fee_total,
                    event_ts=pt.exit_date,
                    provenance={"legacy_path": "_close_paper_trade", "reason": reason},
                )
                _ledger.reconcile_trade(
                    sess,
                    source="paper",
                    paper_trade_id=int(pt.id),
                    user_id=pt.user_id,
                    scan_pattern_id=pt.scan_pattern_id,
                    ticker=pt.ticker or "",
                    legacy_pnl=float(pt.pnl) if pt.pnl is not None else None,
                    provenance={"legacy_path": "_close_paper_trade", "reason": reason},
                )
    except Exception:
        logger.debug("[paper] economic_ledger exit/reconcile hook failed", exc_info=True)


def get_paper_dashboard(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Get paper trading performance summary."""
    open_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    ).all()

    closed_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "closed",
    ).all()

    total_pnl = sum(t.pnl or 0 for t in closed_trades)
    wins = [t for t in closed_trades if (t.pnl or 0) > 0]
    losses = [t for t in closed_trades if (t.pnl or 0) <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    stops_hit = sum(1 for t in closed_trades if t.exit_reason == "stop")
    targets_hit = sum(1 for t in closed_trades if t.exit_reason == "target")
    expired = sum(1 for t in closed_trades if t.exit_reason == "expired")

    # Per-pattern attribution
    sp_pnl: dict[int, list[float]] = {}
    for t in closed_trades:
        if t.scan_pattern_id:
            sp_pnl.setdefault(t.scan_pattern_id, []).append(t.pnl or 0)

    sp_ids = list(sp_pnl.keys())
    sp_names = {}
    if sp_ids:
        for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
            sp_names[sp.id] = sp.name

    pattern_stats = sorted([
        {
            "pattern_id": sp_id,
            "pattern_name": sp_names.get(sp_id, f"#{sp_id}"),
            "trades": len(pnls),
            "pnl": round(sum(pnls), 2),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
        }
        for sp_id, pnls in sp_pnl.items()
    ], key=lambda x: x["pnl"], reverse=True)

    return {
        "ok": True,
        "open_trades": len(open_trades),
        "closed_trades": len(closed_trades),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "stops_hit": stops_hit,
        "targets_hit": targets_hit,
        "expired": expired,
        "pattern_stats": pattern_stats[:10],
        "open": [
            {
                "id": t.id, "ticker": t.ticker, "direction": t.direction,
                "entry": t.entry_price, "stop": t.stop_price, "target": t.target_price,
                "pattern_id": t.scan_pattern_id,
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            }
            for t in open_trades
        ],
    }


def auto_enter_from_signals(
    db: Session,
    user_id: int | None,
    signals: list[dict[str, Any]],
    capital: float = DEFAULT_PAPER_CAPITAL,
) -> int:
    """Automatically open paper trades from high-confidence signals.

    Each signal dict should have: ticker, entry_price, stop_price, target_price,
    scan_pattern_id, confidence.

    Checks portfolio risk gate before each entry to enforce position limits,
    heat caps, sector concentration, correlation risk, and circuit breakers.
    """
    from .portfolio_risk import size_position, check_new_trade_allowed

    # NetEdgeRanker (Phase E, shadow-only). Imported lazily so the paper path
    # never breaks if the ranker module has an issue; the heuristic sizing
    # below is the single source of truth until Phase E becomes authoritative.
    try:
        from . import net_edge_ranker as _net_edge
    except Exception as _exc:  # pragma: no cover - defensive
        _net_edge = None  # type: ignore[assignment]
        logger.debug("[paper] net_edge_ranker unavailable: %s", _exc)

    entered = 0
    blocked = 0
    for sig in signals:
        conf = sig.get("confidence", 0)
        if conf < 0.6:
            continue

        ticker = sig.get("ticker", "")
        entry = sig.get("entry_price") or sig.get("price")
        stop = sig.get("stop_price") or sig.get("stop")
        target = sig.get("target_price") or sig.get("target")
        if not entry or entry <= 0:
            continue

        allowed, reason = check_new_trade_allowed(db, user_id, ticker, capital)
        if not allowed:
            logger.info("[paper] Trade blocked for %s: %s", ticker, reason)
            blocked += 1
            continue

        if not stop:
            stop = entry * 0.97

        qty = size_position(capital, entry, stop, risk_pct=0.5)
        if qty <= 0:
            qty = 10

        # SHADOW HOOK: Compute NetEdgeRanker score for measurement only. The
        # qty above is and remains the authoritative sizing decision. The
        # ranker is not allowed to skip or resize entries until
        # ``brain_net_edge_ranker_mode == "authoritative"`` (future phase).
        _net_edge_score = None
        if _net_edge is not None and _net_edge.mode_is_active():
            try:
                _ctx = _net_edge.NetEdgeSignalContext(
                    ticker=ticker,
                    asset_class="crypto" if str(ticker).upper().endswith("-USD") else "stock",
                    scan_pattern_id=sig.get("scan_pattern_id"),
                    raw_prob=float(conf),
                    entry_price=float(entry),
                    stop_price=float(stop),
                    target_price=float(target) if target else None,
                    regime=sig.get("regime"),
                    timeframe=sig.get("timeframe"),
                    heuristic_score=sig.get("heuristic_score"),
                )
                _net_edge_score = _net_edge.score(db, _ctx)  # logged + persisted
            except Exception as _exc:  # pragma: no cover - defensive
                logger.debug("[paper] net_edge shadow score failed: %s", _exc)

        # Phase H shadow hook: log a canonical sizing proposal so the
        # paper path's ``qty`` can be compared against the Kelly +
        # portfolio-capped size. Shadow-only; qty above is unchanged.
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = float(qty) * float(entry) if qty > 0 and entry > 0 else None
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="paper_trading.auto_open",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry),
                        stop_price=float(stop),
                        capital=float(capital or 0.0),
                        target_price=float(target) if target else None,
                        asset_class=(
                            "crypto" if str(ticker).upper().endswith("-USD") else "equity"
                        ),
                        user_id=user_id,
                        pattern_id=sig.get("scan_pattern_id"),
                        regime=sig.get("regime"),
                        confidence=float(conf) if conf is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(qty) if qty else None,
                        source="paper_trading.size_position",
                    ),
                    net_edge_score=_net_edge_score,
                )
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug("[paper] phase H shadow emit failed: %s", _exc)

        pt = open_paper_trade(
            db, user_id,
            ticker=ticker,
            entry_price=entry,
            scan_pattern_id=sig.get("scan_pattern_id"),
            stop_price=stop,
            target_price=target,
            quantity=qty,
            signal_json=sig,
        )
        if pt:
            entered += 1

    if entered > 0:
        db.commit()

    if blocked > 0:
        logger.info("[paper] %d signals blocked by risk gate, %d entered", blocked, entered)

    return entered
