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
    BreakoutAlert,
    PatternMonitorDecision,
    ScanPattern,
    Trade,
)
from ...config import settings
from .autopilot_scope import live_autopilot_trade_filter

logger = logging.getLogger(__name__)
# f-options-exit-monitor-pattern-exit-now-audit (2026-05-06):
# the freshness window + the two helpers below have moved to the shared
# `_exit_monitor_common` module. Local re-exports preserved for
# backwards compatibility (any external caller / test that imported
# `_MONITOR_EXIT_NOW_MAX_AGE_HOURS` keeps working).
from ._exit_monitor_common import (
    MONITOR_EXIT_NOW_MAX_AGE_HOURS as _MONITOR_EXIT_NOW_MAX_AGE_HOURS,
    is_implausible_quote,
    latest_monitor_decisions_by_trade as _latest_monitor_decisions_by_trade,
    fresh_monitor_exit_meta as _fresh_monitor_exit_meta,
    resolve_monitor_exit_action,
    apply_monitor_exit_reroute_tighten,
)


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


_TARGET_TOUCH_MAX_SLIPPAGE_BPS = 25.0


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _freshness_age_seconds(fresh: Any) -> float | None:
    age_fn = getattr(fresh, "age_seconds", None)
    if not callable(age_fn):
        return None
    try:
        age = age_fn()
    except Exception:
        return None
    return _safe_float(age)


def _compact_quote_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": snapshot.get("source"),
        "feed": snapshot.get("feed"),
    }
    for key in (
        "price",
        "bid",
        "ask",
        "mid",
        "last_price",
        "regular_last_price",
        "extended_last_price",
        "spread_bps",
        "age_seconds",
    ):
        value = _safe_float(snapshot.get(key))
        if value is not None:
            out[key] = round(value, 6)
    if snapshot.get("error"):
        out["error"] = str(snapshot.get("error"))
    return out


def _quote_snapshot_from_adapter(
    ticker: str,
    adapter: Any,
    *,
    broker_source: str | None,
) -> dict[str, Any]:
    """Return a broker-aware quote snapshot for live exit decisions.

    The monitor used to collapse the broker feed into one scalar midpoint.
    That is fine for display, but exits need microstructure: a long can sell
    at bid, not at ask; extended-hours prints matter, but only when the
    executable side is still close enough to the target. The source is keyed
    from the trade's ``broker_source`` so a Coinbase-opened position is not
    evaluated with a Robinhood quote, or vice versa.
    """
    broker_key = (broker_source or "broker").strip().lower() or "broker"
    bbo_candidates: list[tuple[str, Any]] = []
    get_ticker = getattr(adapter, "get_ticker", None)
    if callable(get_ticker):
        bbo_candidates.append(("ticker", get_ticker))
    get_bbo = getattr(adapter, "get_best_bid_ask", None)
    if callable(get_bbo):
        bbo_candidates.append(("bbo", get_bbo))
    for feed_kind, quote_fn in bbo_candidates:
        try:
            raw_bbo = quote_fn(ticker)
        except Exception as exc:
            raw_bbo = None
            bbo_error = f"{type(exc).__name__}:{exc}"
        else:
            bbo_error = None
        if isinstance(raw_bbo, tuple) and len(raw_bbo) == 2:
            tick, fresh = raw_bbo
            if tick is not None:
                age_seconds = _freshness_age_seconds(fresh)
                max_age_seconds = _safe_float(getattr(fresh, "max_age_seconds", None))
                if (
                    age_seconds is not None
                    and max_age_seconds is not None
                    and age_seconds > max_age_seconds
                ):
                    logger.debug(
                        "[autotrader_monitor] stale broker quote skipped "
                        "broker=%s ticker=%s feed=%s age=%.3fs max=%.3fs",
                        broker_key,
                        ticker,
                        feed_kind,
                        age_seconds,
                        max_age_seconds,
                    )
                    continue
                raw = getattr(tick, "raw", None) or {}
                regular_last = _safe_float(raw.get("last_trade_price"))
                extended_last = _safe_float(raw.get("last_extended_hours_trade_price"))
                last_price = (
                    _safe_float(getattr(tick, "last_price", None))
                    or extended_last
                    or regular_last
                )
                mid = _safe_float(getattr(tick, "mid", None))
                bid = _safe_float(getattr(tick, "bid", None))
                ask = _safe_float(getattr(tick, "ask", None))
                price = mid or last_price or bid or ask
                if price is not None:
                    return {
                        "source": broker_key,
                        "feed": f"{broker_key}_{feed_kind}",
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "last_price": last_price,
                        "regular_last_price": regular_last,
                        "extended_last_price": extended_last,
                        "spread_bps": _safe_float(getattr(tick, "spread_bps", None)),
                        "age_seconds": age_seconds,
                    }
        elif bbo_error:
            logger.debug(
                "[autotrader_monitor] BBO quote failed ticker=%s err=%s",
                ticker,
                bbo_error,
            )

    try:
        px = adapter.get_quote_price(ticker)
    except Exception as exc:
        logger.debug(
            "[autotrader_monitor] scalar broker quote failed "
            "broker=%s ticker=%s err=%s",
            broker_key,
            ticker,
            exc,
        )
        px = None
    px = _safe_float(px)
    if px is not None:
        return {
            "source": broker_key,
            "feed": f"{broker_key}_scalar",
            "price": px,
            "bid": None,
            "ask": None,
            "mid": px,
            "last_price": None,
            "regular_last_price": None,
            "extended_last_price": None,
            "spread_bps": None,
            "age_seconds": None,
        }

    if broker_key in ("", "broker", "manual", "market_data"):
        px = _quote_price(ticker)
        px = _safe_float(px)
        if px is not None:
            return {
                "source": "market_data",
                "feed": "market_data_scalar",
                "price": px,
                "bid": None,
                "ask": None,
                "mid": px,
                "last_price": None,
                "regular_last_price": None,
                "extended_last_price": None,
                "spread_bps": None,
                "age_seconds": None,
            }

    return {
        "source": broker_key,
        "feed": f"{broker_key}_unavailable",
        "price": None,
        "error": "no_fresh_broker_quote",
    }


def _latest_trade_print(snapshot: dict[str, Any]) -> tuple[float | None, str | None]:
    for key, source in (
        ("extended_last_price", "extended_last"),
        ("last_price", "last"),
        ("regular_last_price", "regular_last"),
    ):
        value = _safe_float(snapshot.get(key))
        if value is not None:
            return value, source
    return None, None


def _target_touch_is_actionable(
    *,
    target: float,
    executable_price: float | None,
    is_long: bool,
) -> bool:
    if target <= 0 or executable_price is None:
        return False
    tol = _TARGET_TOUCH_MAX_SLIPPAGE_BPS / 10_000.0
    if is_long:
        return executable_price >= target * (1.0 - tol)
    return executable_price <= target * (1.0 + tol)


def _evaluate_exit_trigger(
    snapshot: dict[str, Any],
    *,
    stop: float,
    target: float,
    is_long: bool,
) -> dict[str, Any]:
    price = _safe_float(snapshot.get("price"))
    bid = _safe_float(snapshot.get("bid"))
    ask = _safe_float(snapshot.get("ask"))
    trade_print, trade_source = _latest_trade_print(snapshot)

    if is_long:
        executable, executable_source = (bid, "bid") if bid is not None else (None, None)
        if executable is None:
            executable, executable_source = (
                (trade_print, trade_source) if trade_print is not None else (price, "price")
            )
        stop_price, stop_source = executable, executable_source
        target_price, target_source = executable, executable_source
        hit_stop = stop > 0 and stop_price is not None and stop_price <= stop
        hit_target = target > 0 and target_price is not None and target_price >= target
        if (
            not hit_target
            and target > 0
            and trade_print is not None
            and trade_print >= target
            and _target_touch_is_actionable(
                target=target,
                executable_price=executable,
                is_long=True,
            )
        ):
            hit_target = True
            target_price, target_source = trade_print, trade_source
    else:
        executable, executable_source = (ask, "ask") if ask is not None else (None, None)
        if executable is None:
            executable, executable_source = (
                (trade_print, trade_source) if trade_print is not None else (price, "price")
            )
        stop_price, stop_source = executable, executable_source
        target_price, target_source = executable, executable_source
        hit_stop = stop > 0 and stop_price is not None and stop_price >= stop
        hit_target = target > 0 and target_price is not None and target_price <= target
        if (
            not hit_target
            and target > 0
            and trade_print is not None
            and trade_print <= target
            and _target_touch_is_actionable(
                target=target,
                executable_price=executable,
                is_long=False,
            )
        ):
            hit_target = True
            target_price, target_source = trade_print, trade_source

    return {
        "hit_stop": bool(hit_stop),
        "hit_target": bool(hit_target),
        "stop_price": stop_price,
        "stop_source": stop_source,
        "target_price": target_price,
        "target_source": target_source,
        "executable_price": executable,
        "executable_source": executable_source,
        "trade_print": trade_print,
        "trade_print_source": trade_source,
    }


def _exit_quote_meta(
    snapshot: dict[str, Any],
    trigger: dict[str, Any],
    *,
    reason: str,
    side: str,
    stop: float,
    target: float,
) -> dict[str, Any]:
    if reason == "stop":
        trigger_price = trigger.get("stop_price")
        trigger_source = trigger.get("stop_source")
    elif reason == "target":
        trigger_price = trigger.get("target_price")
        trigger_source = trigger.get("target_source")
    else:
        trigger_price = trigger.get("executable_price")
        trigger_source = trigger.get("executable_source")
    return {
        "decision_source": "venue_quote",
        "decision_reason": reason,
        "decision_price": round(float(trigger_price), 6)
        if _safe_float(trigger_price) is not None
        else None,
        "decision_age_hours": 0,
        "side": side,
        "stop_loss": stop,
        "take_profit": target,
        "trigger_source": trigger_source,
        "quote": _compact_quote_snapshot(snapshot),
    }


def _merge_exit_quote_meta(
    monitor_exit_meta: dict[str, Any] | None,
    quote_meta: dict[str, Any],
) -> dict[str, Any]:
    if monitor_exit_meta is None:
        return quote_meta
    merged = dict(monitor_exit_meta)
    merged["venue_quote"] = quote_meta
    return merged


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


def _rollback_monitor_session(db: Session, reason: str) -> None:
    try:
        db.rollback()
    except Exception:
        logger.debug(
            "[autotrader_monitor] rollback after %s failed",
            reason,
            exc_info=True,
        )


# f-options-exit-monitor-pattern-exit-now-audit (2026-05-06):
# the previous local definitions of _latest_monitor_decisions_by_trade
# and _fresh_monitor_exit_meta moved to ._exit_monitor_common. The
# public names are re-exported above for backwards compatibility with
# existing tests and any external imports.


def tick_auto_trader_monitor(db: Session) -> dict[str, Any]:
    """Poll open autotrader v1 trades; market-sell on stop/target when live enabled."""
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": "autotrader_disabled"}

    from .governance import is_kill_switch_active

    if is_kill_switch_active():
        return {"ok": True, "skipped": "kill_switch"}

    summary: dict[str, Any] = {
        "checked": 0,
        "closed": 0,
        "working": 0,
        "deferred": 0,
        "cancelled": 0,
        "errors": [],
    }

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
                _rollback_monitor_session(db, "paper_exits")
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
                _rollback_monitor_session(db, "paper_daily_loss")
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
    from .broker_position_truth import (
        filter_broker_stale_open_trades,
        reconcile_stale_robinhood_open_trade,
    )

    _open_by_id = {int(t.id): t for t in open_rows if getattr(t, "id", None)}
    open_rows, stale_broker_rows = filter_broker_stale_open_trades(db, open_rows)
    if stale_broker_rows:
        summary["skipped_stale_broker_positions"] = stale_broker_rows
        reconciled_rows = []
        for snap in stale_broker_rows:
            stale_trade = _open_by_id.get(int(snap.get("id") or 0))
            if stale_trade is None:
                continue
            reconciled = reconcile_stale_robinhood_open_trade(
                db,
                stale_trade,
                snapshot=snap,
                source="auto_trader_monitor_broker_truth_gate",
            )
            if reconciled:
                reconciled_rows.append(reconciled)
        if reconciled_rows:
            summary["reconciled_stale_broker_positions"] = reconciled_rows
            db.commit()

    from .auto_trader_position_overrides import list_position_overrides
    from .robinhood_exit_execution import (
        _opened_today_et,
        cancel_pending_exit_order,
        clear_pending_exit_fields,
        describe_robinhood_equity_execution_window,
        has_active_pending_exit,
        has_stranded_pending_exit,
        submit_robinhood_trade_exit,
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

    # DDD -- partition out option trades. The equity monitor manages
    # share-based positions via submit_robinhood_trade_exit (which
    # calls the spot adapter against the underlying ticker). For an
    # option trade the position is a CONTRACT, not shares -- Robinhood
    # rejects every sell with "Not enough shares to sell." Phase 5
    # run_options_exit_pass (already wired into trading_scheduler at
    # the same cadence) handles option exits via place_option_sell
    # on the contract symbol; we just skip them here.
    from .autopilot_scope import is_option_trade
    option_trade_ids = {int(t.id) for t in open_rows if is_option_trade(t)}
    if option_trade_ids:
        summary["delegated_to_options_exit_monitor"] = sorted(option_trade_ids)
    open_rows = [t for t in open_rows if int(t.id) not in option_trade_ids]

    # HHH -- partition out crypto trades. Equity monitor skips RH -USD
    # tickers; doing it here matches DDD design and lets
    # run_crypto_exit_pass own crypto exits via place_crypto_sell_order.
    crypto_trade_ids = {
        int(t.id) for t in open_rows
        if (t.ticker or "").upper().endswith("-USD")
        or (t.broker_source or "").strip().lower() == "coinbase"
    }
    if crypto_trade_ids:
        summary["delegated_to_crypto_exit_monitor"] = sorted(crypto_trade_ids)
        for t in open_rows:
            if int(t.id) not in crypto_trade_ids:
                continue
            broker_source = (t.broker_source or "").strip().lower()
            if broker_source and broker_source != "robinhood":
                summary.setdefault("skipped_broker_source", []).append(
                    {
                        "trade_id": int(t.id),
                        "ticker": t.ticker,
                        "broker_source": broker_source,
                    }
                )
            elif broker_source == "robinhood" and (t.ticker or "").upper().endswith("-USD"):
                summary.setdefault("skipped_unsupported_ticker", []).append(
                    {
                        "trade_id": int(t.id),
                        "ticker": t.ticker,
                        "broker_source": broker_source,
                    }
                )
    open_rows = [t for t in open_rows if int(t.id) not in crypto_trade_ids]

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
        quote_broker_source = broker_source or "robinhood"
        quote_snapshot = _quote_snapshot_from_adapter(
            t.ticker,
            adapter,
            broker_source=quote_broker_source,
        )
        px = _safe_float(quote_snapshot.get("price"))
        quote_src = str(quote_snapshot.get("source") or "none")
        if px is None:
            summary["errors"].append(f"no_quote:{t.ticker}")
            continue
        summary.setdefault("quote_sources", {})[t.ticker] = quote_src
        summary.setdefault("quote_snapshots", {})[t.ticker] = _compact_quote_snapshot(
            quote_snapshot
        )

        # f-exit-monitor-quote-guard-unification (2026-05-06): equity
        # had no implausible-quote guard until now. A bogus $0.50 quote
        # on a $50 entry would force ``hit_stop=True`` and force-sell at
        # the bad price. Per-lane parity with crypto and options. Note
        # this lane does NOT consult an LLM advisory after the trigger
        # check today (the advisory branch is in auto_trader.py, the
        # entry path); ``should_consult_monitor_after_refusal`` is only
        # needed at lanes that do consult. If a future brief adds an
        # exit-side advisory here, route it through that helper.
        if is_implausible_quote(px, t.entry_price):
            try:
                _ratio = (px / float(t.entry_price)) if t.entry_price else float("inf")
            except (TypeError, ZeroDivisionError):
                _ratio = float("inf")
            logger.warning(
                "[autotrader_monitor] implausible quote refused: "
                "ticker=%s trade_id=%s px=%s entry=%s ratio=%.4f",
                t.ticker, t.id, px, t.entry_price, _ratio,
            )
            summary["skipped_implausible_quote"] = (
                summary.get("skipped_implausible_quote", 0) + 1
            )
            continue

        stop = float(t.stop_loss or 0)
        tgt = float(t.take_profit or 0)
        side = (t.direction or "long").lower()
        is_long = side == "long"
        if stop <= 0 and tgt <= 0:
            # No levels after seed attempt — refuse to manage blindly.
            summary.setdefault("skipped_no_levels", []).append(int(t.id))
            logger.info(
                "[autotrader_monitor] skip trade=%s ticker=%s: no stop/target after pattern seed",
                t.id,
                t.ticker,
            )
            continue
        # Self-heal: if the trade is marked ``pending_exit_status=submitted``
        # but the broker's order_id was never captured (e.g. prior JSON-serialize
        # audit crash lost the order_id), clear the pending state so a fresh
        # submission can go through. The deterministic client_order_id would
        # otherwise make retries permanently fail with duplicate_client_order_id.
        if has_stranded_pending_exit(t):
            logger.warning(
                "[autotrader_monitor] stranded pending_exit cleared trade=%s ticker=%s "
                "(status=%s, order_id empty)",
                t.id, t.ticker, t.pending_exit_status,
            )
            clear_pending_exit_fields(t)
            db.add(t)
            db.commit()
            summary.setdefault("stranded_cleared", []).append(int(t.id))

        trigger = _evaluate_exit_trigger(
            quote_snapshot,
            stop=stop,
            target=tgt,
            is_long=is_long,
        )
        hit_stop = bool(trigger.get("hit_stop"))
        hit_target = bool(trigger.get("hit_target"))
        # Fix 5B: gate the pattern-monitor exit_now advisory. An uncorroborated
        # exit_now (price hasn't deteriorated toward the stop) is rerouted to a
        # stop-tighten instead of a premature cut; denylisted (0%-beneficial)
        # sources are dropped. monitor_exit_meta stays non-None (-> pattern_exit_now
        # exit below) only when price corroborates the exit. Stop/target hits are
        # evaluated independently above and always take precedence.
        _md_decision = latest_monitor_decisions.get(int(t.id))
        _px_now = _safe_float(quote_snapshot.get("price")) if quote_snapshot else None
        _verdict, _new_stop, _meta = resolve_monitor_exit_action(
            _md_decision,
            entry=float(t.entry_price or 0.0),
            stop=stop,
            current_px=_px_now,
            is_long=is_long,
        )
        monitor_exit_meta = _meta if _verdict == "exit" else None
        if _verdict == "tighten_stop" and _new_stop is not None:
            if apply_monitor_exit_reroute_tighten(
                db, t, new_stop=_new_stop, decision_meta=_meta
            ):
                summary["rerouted_tighten"] = (
                    int(summary.get("rerouted_tighten", 0) or 0) + 1
                )
        pending_reason = (t.pending_exit_reason or "").strip().lower()
        pending_status = (t.pending_exit_status or "").strip().lower()
        if pending_reason == "pattern_exit_now" and monitor_exit_meta is None and (
            has_active_pending_exit(t) or pending_status == "deferred"
        ):
            cancelled = cancel_pending_exit_order(
                db,
                t,
                reason="superseded_monitor_hold",
                audit_decision_prefix="monitor_exit",
                adapter=adapter,
            )
            if not cancelled.get("ok"):
                summary["errors"].append(
                    f"cancel_pending_exit:{t.id}:{cancelled.get('error')}"
                )
                continue
            summary["cancelled"] += 1
            pending_reason = ""
            pending_status = ""
        if not hit_stop and not hit_target and monitor_exit_meta is None:
            if has_active_pending_exit(t):
                summary["working"] += 1
            elif pending_status == "deferred":
                summary["deferred"] += 1
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
        quote_meta = _exit_quote_meta(
            quote_snapshot,
            trigger,
            reason=reason,
            side=side,
            stop=stop,
            target=tgt,
        )
        submit_monitor_exit_meta = _merge_exit_quote_meta(
            monitor_exit_meta,
            quote_meta,
        )
        if has_active_pending_exit(t) and pending_reason == reason:
            summary["working"] += 1
            continue
        if pending_status == "deferred" and pending_reason == reason:
            window = describe_robinhood_equity_execution_window(
                t.ticker,
                adapter=adapter,
            )
            if not window.get("can_submit_now"):
                summary["deferred"] += 1
                continue

            # Cooldown: when can_submit_now is True but the previous tick's
            # submit failed downstream (wide_spread, whole_shares_required,
            # offhours_quote_rejected), the deferred re-attempt fires every
            # 30s and audit-spams. Only retry deferreds every 5 minutes so
            # the audit log stays signal-rich. The trade still goes live the
            # moment the market opens — the regular session_open transition
            # gives can_submit_now a different value, hence different code
            # path. (LL.9)
            from datetime import datetime, timedelta, timezone
            requested_at = t.pending_exit_requested_at
            if requested_at is not None:
                # pending_exit_requested_at is naive UTC (see _mark_deferred_exit)
                req_aware = requested_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - req_aware < timedelta(minutes=5):
                    summary["deferred"] += 1
                    continue

        client_oid = f"atv1-{t.id}-exit-{reason}"
        res = submit_robinhood_trade_exit(
            db,
            t,
            exit_reason=reason,
            audit_decision_prefix="monitor_exit",
            client_order_id=client_oid,
            adapter=adapter,
            monitor_exit_meta=submit_monitor_exit_meta,
        )

        if not res.get("ok"):
            summary["errors"].append(f"sell_fail:{t.id}:{res.get('error')}")
            continue

        state = str(res.get("state") or "").lower()
        if state == "filled":
            db.refresh(t)
            summary["closed"] += 1
            if _opened_today_et(t.entry_date) and (t.direction or "long").lower() == "long":
                summary.setdefault("would_be_day_trade_exits", []).append(int(t.id))
            logger.info(
                "[autotrader_monitor] Closed trade id=%s ticker=%s reason=%s pnl=%s",
                t.id,
                t.ticker,
                reason,
                t.pnl,
            )
            _maybe_trip_daily_loss_kill_switch(db, t.user_id)
        elif state == "working":
            summary["working"] += 1
        elif state == "deferred":
            summary["deferred"] += 1

    return summary


def _maybe_trip_daily_loss_kill_switch(db: Session, user_id: int | None) -> None:
    from .auto_trader_rules import autotrader_realized_pnl_today_et
    from .governance import activate_kill_switch, check_daily_loss_breach

    # Path-local v1 cap (legacy, autotrader-only, stays as a tighter tripwire).
    cap = float(getattr(settings, "chili_autotrader_daily_loss_cap_usd", 150.0))
    if cap > 0:
        total = autotrader_realized_pnl_today_et(db, user_id)
        if total <= -cap:
            activate_kill_switch("autotrader_daily_loss_cap")
            logger.critical(
                "[autotrader_monitor] Daily loss cap hit (pnl_today=%.2f cap=%.2f) — kill switch",
                total,
                cap,
            )

    # Global cap (P0.2) — spans autotrader + momentum_neural so a mixed-path
    # drawdown can't sneak past either path-local cap. `check_daily_loss_breach`
    # handles the kill-switch activation and no-op's if already active.
    try:
        check_daily_loss_breach(db, user_id=user_id)
    except Exception:
        logger.debug(
            "[autotrader_monitor] Global daily-loss check failed (non-fatal)",
            exc_info=True,
        )
