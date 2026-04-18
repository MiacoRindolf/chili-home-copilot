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

from sqlalchemy.orm import Session

from ...models.trading import (
    AutoTraderRun,
    BreakoutAlert,
    PatternMonitorDecision,
    ScanPattern,
    Trade,
)
from ...config import settings
from .autopilot_scope import live_autopilot_trade_filter

logger = logging.getLogger(__name__)
# Carry fresh EXIT_NOW recommendations across a normal weekend / long-holiday
# gap so Friday evening decisions can still execute on the next regular session.
_MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0


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
    """Populate missing ``stop_loss`` / ``take_profit`` in-place from the most
    authoritative source available for each CHILI-tagged row.

    Lookup order (first populated value wins per field, per row):
      1. Linked ``BreakoutAlert`` — concrete numeric ``stop_loss`` /
         ``target_price`` stamped when the alert fired. This is the source of
         truth for pattern-imminent entries in production (data-first).
      2. Linked ``ScanPattern.rules_json.exits`` percents (``stop_pct`` /
         ``target_pct``), applied long/short-aware to ``entry_price``. Fallback
         for rows linked to a pattern but no alert, e.g. ad-hoc mirrors.

    Rows that still have **no** stop AND **no** target after both lookups are
    skipped by the caller (``skipped_no_levels`` on the summary) so the monitor
    never trades blind.
    """
    dirty_rows: list[Trade] = []
    for t in rows:
        if (t.stop_loss or 0) > 0 and (t.take_profit or 0) > 0:
            continue
        entry = float(t.entry_price or 0.0)
        side = (t.direction or "long").lower()
        seeded_from: list[str] = []

        # 1) Breakout alert — canonical numeric levels for pattern-imminent entries.
        if t.related_alert_id:
            try:
                a = db.get(BreakoutAlert, int(t.related_alert_id))
            except Exception:
                a = None
            if a is not None:
                alert_stop = float(a.stop_loss) if a.stop_loss is not None else 0.0
                alert_tgt = float(a.target_price) if a.target_price is not None else 0.0
                if not (t.stop_loss or 0) > 0 and alert_stop > 0:
                    t.stop_loss = round(alert_stop, 4)
                    seeded_from.append("alert.stop_loss")
                if not (t.take_profit or 0) > 0 and alert_tgt > 0:
                    t.take_profit = round(alert_tgt, 4)
                    seeded_from.append("alert.target_price")

        # 2) Pattern rules_json.exits percents — fallback for any side still empty.
        if t.scan_pattern_id and (
            not (t.stop_loss or 0) > 0 or not (t.take_profit or 0) > 0
        ) and entry > 0:
            try:
                p = db.get(ScanPattern, int(t.scan_pattern_id))
            except Exception:
                p = None
            if p is not None:
                rj = dict(p.rules_json or {})
                exits = rj.get("exits") if isinstance(rj, dict) else None
                if isinstance(exits, dict):
                    if not (t.stop_loss or 0) > 0:
                        sp = _coerce_pct(
                            exits.get("stop_pct") or exits.get("stop_loss_pct")
                        )
                        if sp is not None and sp > 0:
                            t.stop_loss = round(
                                entry * (1.0 - sp / 100.0)
                                if side != "short"
                                else entry * (1.0 + sp / 100.0),
                                4,
                            )
                            seeded_from.append("pattern.stop_pct")
                    if not (t.take_profit or 0) > 0:
                        tp = _coerce_pct(
                            exits.get("target_pct") or exits.get("take_profit_pct")
                        )
                        if tp is not None and tp > 0:
                            t.take_profit = round(
                                entry * (1.0 + tp / 100.0)
                                if side != "short"
                                else entry * (1.0 - tp / 100.0),
                                4,
                            )
                            seeded_from.append("pattern.target_pct")

        if seeded_from:
            db.add(t)
            dirty_rows.append(t)
            logger.info(
                "[autotrader_monitor] seeded levels trade=%s ticker=%s stop=%s target=%s sources=%s",
                t.id,
                t.ticker,
                t.stop_loss,
                t.take_profit,
                ",".join(seeded_from),
            )
    if dirty_rows:
        db.commit()


def _coerce_pct(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _latest_monitor_decisions_by_trade(
    db: Session,
    trade_ids: list[int],
) -> dict[int, PatternMonitorDecision]:
    """Latest PatternMonitorDecision per trade.

    Execution should follow the newest advisory state only. If a prior
    ``exit_now`` has since been superseded by ``hold``, the live monitor must
    not keep selling from the stale recommendation.
    """
    if not trade_ids:
        return {}
    rows = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
        .order_by(PatternMonitorDecision.created_at.desc())
        .all()
    )
    latest: dict[int, PatternMonitorDecision] = {}
    for row in rows:
        latest.setdefault(int(row.trade_id), row)
    return latest


def _fresh_monitor_exit_meta(
    decision: PatternMonitorDecision | None,
) -> dict[str, Any] | None:
    """Audit metadata when the latest monitor decision still means exit."""
    if decision is None or (decision.action or "").lower() != "exit_now":
        return None
    age_h = (datetime.utcnow() - decision.created_at).total_seconds() / 3600.0
    if age_h > _MONITOR_EXIT_NOW_MAX_AGE_HOURS:
        return None
    return {
        "decision_id": int(decision.id),
        "decision_source": decision.decision_source,
        "decision_age_hours": round(age_h, 3),
        "decision_price": (
            float(decision.price_at_decision)
            if decision.price_at_decision is not None
            else None
        ),
    }


def tick_auto_trader_monitor(db: Session) -> dict[str, Any]:
    """Poll open autotrader v1 trades; market-sell on stop/target when live enabled."""
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": "autotrader_disabled"}

    from .governance import is_kill_switch_active

    if is_kill_switch_active():
        return {"ok": True, "skipped": "kill_switch"}

    if getattr(settings, "chili_autotrader_rth_only", True):
        from .pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        allow_ext = bool(getattr(settings, "chili_autotrader_allow_extended_hours", False))
        session_open = (
            us_stock_extended_session_open() if allow_ext else us_stock_session_open()
        )
        if not session_open:
            return {
                "ok": True,
                "skipped": "outside_extended_hours" if allow_ext else "outside_rth",
            }

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

    # Manage any Autopilot-surfaced live Trade: AutoTrader v1, pattern-linked,
    # or AI/manual plan-level rows with a saved stop/target. Users opt
    # specific positions out via the desk's per-row "Pause monitor".
    #
    # SAFETY: scope to the configured autotrader user (``chili_autotrader_user_id``
    # or ``brain_default_user_id``). Without this scope the monitor would sweep
    # pattern-linked trades for every user in the DB and market-sell positions
    # held in other brokerage accounts. Enforce a resolved uid before any live
    # exits can fire.
    uid = getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )
    if uid is None:
        logger.warning(
            "[autotrader_monitor] live monitor aborted: no chili_autotrader_user_id/"
            "brain_default_user_id configured — cannot scope to owner"
        )
        return {**summary, "skipped": "no_user_scope"}

    open_rows = (
        db.query(Trade)
        .filter(
            Trade.user_id == int(uid),
            Trade.status == "open",
            live_autopilot_trade_filter(),
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
    latest_monitor_decisions = _latest_monitor_decisions_by_trade(
        db,
        [int(t.id) for t in open_rows],
    )
    if paused_ids:
        summary["live_monitor_paused_ids"] = sorted(paused_ids)

    for t in open_rows:
        summary["checked"] += 1
        if t.id in paused_ids:
            continue
        broker_source = (t.broker_source or "").strip().lower()
        if broker_source and broker_source != "robinhood":
            summary.setdefault("skipped_broker_source", []).append(
                {"trade_id": int(t.id), "ticker": t.ticker, "broker_source": broker_source}
            )
            continue
        if broker_source == "robinhood" and (t.ticker or "").upper().endswith("-USD"):
            summary.setdefault("skipped_unsupported_ticker", []).append(
                {"trade_id": int(t.id), "ticker": t.ticker, "broker_source": broker_source}
            )
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
        monitor_exit_meta = _fresh_monitor_exit_meta(
            latest_monitor_decisions.get(int(t.id))
        )
        if not hit_stop and not hit_target and monitor_exit_meta is None:
            continue

        qty = float(t.quantity or 0)
        if qty <= 0:
            summary["errors"].append(f"bad_qty:{t.id}")
            continue

        if hit_stop:
            reason = "stop"
        elif hit_target:
            reason = "target"
        else:
            reason = "pattern_exit_now"
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
                    **(
                        {"monitor_decision": monitor_exit_meta}
                        if monitor_exit_meta is not None
                        else {}
                    ),
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
