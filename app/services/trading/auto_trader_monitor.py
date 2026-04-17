"""Monitor open pattern-linked live trades: stop/target → RH market sell; daily loss → kill switch.

Scope (as of D1 — no-adopt model): any open ``Trade`` with a CHILI pattern link
(``scan_pattern_id`` or ``related_alert_id``) is managed by this monitor, not
only AutoTrader-v1-originated rows. Users opt a specific position out via the
desk's **Pause monitor** per-row control (stored in ``trading_brain_runtime_modes``).
If stop/target are missing on a linked row the monitor seeds them on first
encounter from the linked ``ScanPattern``'s ``rules_json.exits`` hints so the
monitor never trades blind — a position with no pattern exits defined and no
manual levels is skipped and logged.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models.trading import AutoTraderRun, ScanPattern, Trade
from ...config import settings

logger = logging.getLogger(__name__)


def _quote_price(ticker: str) -> float | None:
    from .market_data import fetch_quote

    q = fetch_quote(ticker)
    if not q:
        return None
    p = q.get("price") or q.get("last_price")
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def _seed_missing_levels(db: Session, rows: list[Trade]) -> None:
    """Populate ``stop_loss`` / ``take_profit`` in-place from the linked
    ``ScanPattern`` exit hints when the row has none.

    Pattern hints live in ``ScanPattern.rules_json.exits`` as percents
    (``stop_pct`` / ``target_pct``) and are applied relative to ``entry_price``
    (long-aware: stop below entry, target above). Rows that still have no stop
    AND no target after seeding are skipped by the caller, never traded blindly.
    """
    dirty = False
    for t in rows:
        if (t.stop_loss or 0) > 0 and (t.take_profit or 0) > 0:
            continue
        if not t.scan_pattern_id:
            continue
        try:
            p = db.get(ScanPattern, int(t.scan_pattern_id))
        except Exception:
            continue
        if p is None:
            continue
        rj = dict(p.rules_json or {})
        exits = rj.get("exits") if isinstance(rj, dict) else None
        if not isinstance(exits, dict):
            continue
        entry = float(t.entry_price or 0.0)
        if entry <= 0:
            continue
        side = (t.direction or "long").lower()

        if not (t.stop_loss or 0) > 0:
            stop_pct = exits.get("stop_pct") or exits.get("stop_loss_pct")
            try:
                sp = float(stop_pct) if stop_pct is not None else None
            except (TypeError, ValueError):
                sp = None
            if sp is not None and sp > 0:
                t.stop_loss = round(
                    entry * (1.0 - sp / 100.0) if side != "short" else entry * (1.0 + sp / 100.0),
                    4,
                )
                dirty = True

        if not (t.take_profit or 0) > 0:
            target_pct = exits.get("target_pct") or exits.get("take_profit_pct")
            try:
                tp = float(target_pct) if target_pct is not None else None
            except (TypeError, ValueError):
                tp = None
            if tp is not None and tp > 0:
                t.take_profit = round(
                    entry * (1.0 + tp / 100.0) if side != "short" else entry * (1.0 - tp / 100.0),
                    4,
                )
                dirty = True

        if dirty:
            db.add(t)
            logger.info(
                "[autotrader_monitor] seeded levels trade=%s ticker=%s stop=%s target=%s from pattern=%s",
                t.id,
                t.ticker,
                t.stop_loss,
                t.take_profit,
                t.scan_pattern_id,
            )
    if dirty:
        db.commit()


def tick_auto_trader_monitor(db: Session) -> dict[str, Any]:
    """Poll open autotrader v1 trades; market-sell on stop/target when live enabled."""
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": "autotrader_disabled"}

    from .governance import is_kill_switch_active

    if is_kill_switch_active():
        return {"ok": True, "skipped": "kill_switch"}

    if getattr(settings, "chili_autotrader_rth_only", True):
        from .pattern_imminent_alerts import us_stock_session_open

        if not us_stock_session_open():
            return {"ok": True, "skipped": "outside_rth"}

    summary: dict[str, Any] = {"checked": 0, "closed": 0, "errors": []}

    from .autotrader_desk import effective_autotrader_runtime

    rt = effective_autotrader_runtime(db)
    live_effective = bool(rt.get("live_orders_effective"))

    # Paper path: accelerate exit checks for autotrader-tagged paper rows
    if not live_effective:
        uid = getattr(settings, "chili_autotrader_user_id", None) or getattr(
            settings, "brain_default_user_id", None
        )
        if uid is not None:
            try:
                from .auto_trader_position_overrides import (
                    paused_paper_trade_ids_for_user,
                )
                from .paper_trading import check_paper_exits

                skip = paused_paper_trade_ids_for_user(db, uid)
                if skip:
                    summary["paper_monitor_paused_ids"] = sorted(skip)
                summary["paper_exits"] = check_paper_exits(
                    db, uid, skip_trade_ids=skip
                )
            except Exception as e:
                logger.warning("[autotrader_monitor] check_paper_exits failed: %s", e)
                summary["errors"].append(str(e))
            try:
                from .auto_trader_rules import autotrader_paper_realized_pnl_today_et
                from .governance import activate_kill_switch

                cap = float(getattr(settings, "chili_autotrader_daily_loss_cap_usd", 150.0))
                if cap > 0:
                    total_p = autotrader_paper_realized_pnl_today_et(db, uid)
                    if total_p <= -cap:
                        activate_kill_switch("autotrader_daily_loss_cap_paper")
                        logger.critical(
                            "[autotrader_monitor] Paper daily loss cap hit pnl_today=%.2f — kill switch",
                            total_p,
                        )
            except Exception as e:
                logger.warning("[autotrader_monitor] paper daily loss check failed: %s", e)
        return summary

    if not live_effective:
        return summary

    from .venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    if not adapter.is_enabled():
        logger.debug("[autotrader_monitor] RH adapter not enabled/connected — skip live monitor")
        return {**summary, "skipped": "rh_adapter_off"}

    # D1: manage any open pattern-linked Trade, not just v1-originated rows.
    # Users opt specific positions out via the desk's per-row "Pause monitor".
    open_rows = (
        db.query(Trade)
        .filter(
            Trade.status == "open",
            or_(
                Trade.auto_trader_version == "v1",
                Trade.scan_pattern_id.isnot(None),
                Trade.related_alert_id.isnot(None),
            ),
        )
        .all()
    )

    from .auto_trader_position_overrides import (
        clear_position_overrides,
        list_position_overrides,
    )

    # Seed missing stop/target from the linked ScanPattern's exit hints so the
    # monitor never trades blind on a CHILI-tagged row. If a row truly has no
    # levels (no manual setup and no pattern exits), log + skip below.
    _seed_missing_levels(db, open_rows)

    overrides = list_position_overrides(db, [("trade", int(t.id)) for t in open_rows])
    paused_ids = {
        tid for (kind, tid), ov in overrides.items()
        if kind == "trade" and ov.get("monitor_paused")
    }
    if paused_ids:
        summary["live_monitor_paused_ids"] = sorted(paused_ids)

    for t in open_rows:
        summary["checked"] += 1
        if t.id in paused_ids:
            continue

        # Prefer Robinhood's own feed for live stock exits (same venue as fills).
        # Fall back to generic market_data only if RH momentarily fails (halt,
        # transient auth blip). Crypto rows shouldn't reach here — RH adapter
        # returns None for non-equity tickers.
        px = adapter.get_quote_price(t.ticker)
        quote_src = "robinhood"
        if px is None:
            px = _quote_price(t.ticker)
            quote_src = "market_data" if px is not None else "none"
        if px is None:
            summary["errors"].append(f"no_quote:{t.ticker}")
            continue
        summary.setdefault("quote_sources", {})[t.ticker] = quote_src

        stop = float(t.stop_loss or 0)
        tgt = float(t.take_profit or 0)
        if stop <= 0 and tgt <= 0:
            # No levels after seed attempt — refuse to manage blindly.
            summary.setdefault("skipped_no_levels", []).append(int(t.id))
            logger.info(
                "[autotrader_monitor] skip trade=%s ticker=%s: no stop/target after pattern seed",
                t.id,
                t.ticker,
            )
            continue
        hit_stop = stop > 0 and px <= stop
        hit_target = tgt > 0 and px >= tgt
        if not hit_stop and not hit_target:
            continue

        qty = float(t.quantity or 0)
        if qty <= 0:
            summary["errors"].append(f"bad_qty:{t.id}")
            continue

        reason = "stop" if hit_stop else "target"
        client_oid = f"atv1-{t.id}-exit-{reason}"
        try:
            res = adapter.place_market_order(
                product_id=t.ticker,
                side="sell",
                base_size=str(qty),
                client_order_id=client_oid,
            )
        except Exception as e:
            logger.warning("[autotrader_monitor] sell failed trade=%s: %s", t.id, e)
            summary["errors"].append(f"sell_exc:{t.id}")
            continue

        if not res.get("ok"):
            summary["errors"].append(f"sell_fail:{t.id}:{res.get('error')}")
            continue

        raw = res.get("raw") or {}
        exit_px = None
        try:
            exit_px = float(raw.get("average_price") or raw.get("price") or px)
        except (TypeError, ValueError):
            exit_px = px

        entry = float(t.entry_price)
        pnl = (exit_px - entry) * qty
        t.status = "closed"
        t.exit_price = exit_px
        t.exit_date = datetime.utcnow()
        t.pnl = round(pnl, 4)
        t.exit_reason = reason
        t.broker_order_id = str(res.get("order_id") or "") or t.broker_order_id
        db.add(t)
        db.commit()
        summary["closed"] += 1
        logger.info(
            "[autotrader_monitor] Closed trade id=%s ticker=%s reason=%s pnl=%.2f",
            t.id,
            t.ticker,
            reason,
            pnl,
        )

        # PDT soft-warn: stamp audit row when the exit would be a same-day
        # round trip (long only, stocks only). We don't block — Robinhood
        # itself rejects if the account is PDT flagged.
        try:
            from .auto_trader_position_overrides import _opened_today_et

            opened_today = bool(t.entry_date) and _opened_today_et(t.entry_date)
            would_be_day_trade = (
                opened_today
                and (t.direction or "long") == "long"
                and (t.broker_source or "") != "crypto"
            )
            audit = AutoTraderRun(
                user_id=t.user_id,
                breakout_alert_id=t.related_alert_id,
                scan_pattern_id=t.scan_pattern_id,
                ticker=(t.ticker or "").upper(),
                decision="monitor_exit",
                reason=reason,
                trade_id=int(t.id),
                rule_snapshot={
                    "exit_reason": reason,
                    "pnl": round(pnl, 4),
                    "opened_today_et": opened_today,
                    "would_be_day_trade": would_be_day_trade,
                },
            )
            db.add(audit)
            db.commit()
            if would_be_day_trade:
                summary.setdefault("would_be_day_trade_exits", []).append(int(t.id))
        except Exception:
            logger.debug("[autotrader_monitor] PDT audit stamp failed", exc_info=True)

        try:
            clear_position_overrides(db, "trade", int(t.id))
        except Exception:
            logger.debug("[autotrader_monitor] clear_position_overrides failed", exc_info=True)

        _maybe_trip_daily_loss_kill_switch(db, t.user_id)

    return summary


def _maybe_trip_daily_loss_kill_switch(db: Session, user_id: int | None) -> None:
    from .auto_trader_rules import autotrader_realized_pnl_today_et
    from .governance import activate_kill_switch

    cap = float(getattr(settings, "chili_autotrader_daily_loss_cap_usd", 150.0))
    if cap <= 0:
        return
    total = autotrader_realized_pnl_today_et(db, user_id)
    if total <= -cap:
        activate_kill_switch("autotrader_daily_loss_cap")
        logger.critical(
            "[autotrader_monitor] Daily loss cap hit (pnl_today=%.2f cap=%.2f) — kill switch",
            total,
            cap,
        )
