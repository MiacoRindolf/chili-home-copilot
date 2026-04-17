"""Per-position Autopilot overrides for AutoTrader v1 (monitor pause, synergy exclude, close-now).

Reuses ``trading_brain_runtime_modes`` with ``slice_name =
"autotrader_v1_position:{kind}:{trade_id}"`` — no new migration. ``payload_json``
stores ``{monitor_paused: bool, synergy_excluded: bool}`` plus a ``kind`` of
``"trade"`` (live) or ``"paper"``.

Desk wiring calls into these helpers from:

* ``auto_trader_monitor`` — reads ``monitor_paused`` to hold a position past its
  stop/target.
* ``auto_trader_synergy`` — reads ``synergy_excluded`` to refuse a scale-in on
  the existing trade.
* ``/api/trading/autotrader/positions/{trade_id}/close`` — calls
  ``close_position_now`` for an immediate market-sell (live) or a best-effort
  close (paper) at the current quote.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Literal, Optional

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import AutoTraderRun, BrainRuntimeMode, PaperTrade, Trade

logger = logging.getLogger(__name__)

PositionKind = Literal["trade", "paper"]

_DEFAULT = {"monitor_paused": False, "synergy_excluded": False}


def _slice_name(kind: PositionKind, trade_id: int) -> str:
    return f"autotrader_v1_position:{kind}:{int(trade_id)}"


def _get_row(db: Session, kind: PositionKind, trade_id: int) -> BrainRuntimeMode | None:
    return (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == _slice_name(kind, trade_id))
        .first()
    )


def get_position_overrides(
    db: Session, kind: PositionKind, trade_id: int
) -> dict[str, bool]:
    row = _get_row(db, kind, trade_id)
    if row is None:
        return dict(_DEFAULT)
    pj = dict(row.payload_json or {})
    return {
        "monitor_paused": bool(pj.get("monitor_paused", False)),
        "synergy_excluded": bool(pj.get("synergy_excluded", False)),
    }


def list_position_overrides(
    db: Session, pairs: Iterable[tuple[PositionKind, int]]
) -> dict[tuple[PositionKind, int], dict[str, bool]]:
    """Bulk-fetch overrides for a set of (kind, trade_id) pairs. Missing -> defaults."""
    pairs = list(pairs)
    if not pairs:
        return {}
    slices = [_slice_name(k, tid) for (k, tid) in pairs]
    rows = (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name.in_(slices))
        .all()
    )
    out: dict[tuple[PositionKind, int], dict[str, bool]] = {p: dict(_DEFAULT) for p in pairs}
    by_slice = {r.slice_name: r for r in rows}
    for (kind, tid) in pairs:
        r = by_slice.get(_slice_name(kind, tid))
        if r is None:
            continue
        pj = dict(r.payload_json or {})
        out[(kind, tid)] = {
            "monitor_paused": bool(pj.get("monitor_paused", False)),
            "synergy_excluded": bool(pj.get("synergy_excluded", False)),
        }
    return out


def set_position_override(
    db: Session,
    kind: PositionKind,
    trade_id: int,
    field: str,
    value: bool,
    *,
    updated_by: str = "autopilot_ui",
) -> dict[str, bool]:
    if field not in ("monitor_paused", "synergy_excluded"):
        raise ValueError(f"Unsupported override field: {field}")

    row = _get_row(db, kind, trade_id)
    if row is None:
        row = BrainRuntimeMode(
            slice_name=_slice_name(kind, trade_id),
            mode="active",
            updated_by=updated_by,
            reason=f"autotrader_position_{field}",
            payload_json={"kind": kind, field: bool(value)},
        )
        db.add(row)
    else:
        pj = dict(row.payload_json or {})
        pj["kind"] = kind
        pj[field] = bool(value)
        row.payload_json = pj
        row.updated_by = updated_by
        row.reason = f"autotrader_position_{field}"
    db.commit()
    logger.info(
        "[autotrader_pos_override] kind=%s trade_id=%s %s=%s by=%s",
        kind,
        trade_id,
        field,
        value,
        updated_by,
    )
    return get_position_overrides(db, kind, trade_id)


def clear_position_overrides(db: Session, kind: PositionKind, trade_id: int) -> None:
    """Call after a position is closed so overrides don't pile up."""
    row = _get_row(db, kind, trade_id)
    if row is not None:
        db.delete(row)
        db.commit()


def _current_quote_price(ticker: str, *, prefer_rh: bool = False) -> float | None:
    """Generic (Massive / Polygon / yfinance) quote via ``fetch_quote``.

    Paper trades use this directly. For live (Robinhood) trades the callers
    pass ``prefer_rh=True`` so exits / close-now compare against the same
    venue that fills the order — avoids Massive / Polygon / RH drift.
    """
    if prefer_rh:
        try:
            from .venue.robinhood_spot import RobinhoodSpotAdapter

            adapter = RobinhoodSpotAdapter()
            if adapter.is_enabled():
                px = adapter.get_quote_price(ticker)
                if px is not None:
                    return float(px)
        except Exception:
            logger.debug("[autotrader_pos_override] RH quote failed; falling back", exc_info=True)

    from .market_data import fetch_quote

    q = fetch_quote(ticker)
    if not q:
        return None
    p = q.get("price") or q.get("last_price")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def close_position_now(
    db: Session,
    *,
    kind: PositionKind,
    trade_id: int,
    updated_by: str = "autopilot_ui",
) -> dict[str, Any]:
    """Market-sell (live) or best-effort close (paper) immediately.

    Returns ``{"ok": bool, "error"?: str, "exit_price"?: float, "pnl"?: float}``.
    """
    if kind == "trade":
        return _close_trade_now(db, trade_id=trade_id, updated_by=updated_by)
    if kind == "paper":
        return _close_paper_now(db, trade_id=trade_id, updated_by=updated_by)
    return {"ok": False, "error": f"unknown_kind:{kind}"}


def _close_trade_now(db: Session, *, trade_id: int, updated_by: str) -> dict[str, Any]:
    t = db.get(Trade, int(trade_id))
    if t is None:
        return {"ok": False, "error": "trade_not_found"}
    if t.status != "open":
        return {"ok": False, "error": f"not_open:{t.status}"}
    # Close-now is available on v1-adopted rows AND on any pattern-linked open
    # row. The desk only surfaces positions meeting one of these, so this still
    # excludes unrelated broker holdings.
    is_v1 = (t.auto_trader_version or "") == "v1"
    is_linked = bool(t.scan_pattern_id or t.related_alert_id)
    if not (is_v1 or is_linked):
        return {"ok": False, "error": "not_pattern_linked"}

    from .venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    if not adapter.is_enabled():
        return {"ok": False, "error": "rh_adapter_off"}

    qty = float(t.quantity or 0)
    if qty <= 0:
        return {"ok": False, "error": "bad_qty"}

    client_oid = f"atv1-{t.id}-desk-close"
    try:
        res = adapter.place_market_order(
            product_id=t.ticker,
            side="sell",
            base_size=str(qty),
            client_order_id=client_oid,
        )
    except Exception as e:
        logger.exception("[autotrader_pos_override] close live trade=%s failed", t.id)
        return {"ok": False, "error": f"adapter_exc:{e}"}

    if not res.get("ok"):
        return {"ok": False, "error": f"broker:{res.get('error')}"}

    raw = res.get("raw") or {}
    try:
        exit_px = (
            float(raw.get("average_price") or raw.get("price") or 0)
            or _current_quote_price(t.ticker, prefer_rh=True)
            or float(t.entry_price)
        )
    except (TypeError, ValueError):
        exit_px = _current_quote_price(t.ticker, prefer_rh=True) or float(t.entry_price)

    entry = float(t.entry_price)
    pnl = (exit_px - entry) * qty
    t.status = "closed"
    t.exit_price = exit_px
    t.exit_date = datetime.utcnow()
    t.pnl = round(pnl, 4)
    t.exit_reason = "desk_close_now"
    t.broker_order_id = str(res.get("order_id") or "") or t.broker_order_id
    db.add(t)
    db.commit()

    opened_today = _opened_today_et(t.entry_date) if t.entry_date else False
    audit = AutoTraderRun(
        user_id=t.user_id,
        breakout_alert_id=t.related_alert_id,
        scan_pattern_id=t.scan_pattern_id,
        ticker=(t.ticker or "").upper(),
        decision="desk_close_now",
        reason=f"close_by={updated_by}",
        trade_id=t.id,
        rule_snapshot={
            "opened_today_et": opened_today,
            "would_be_day_trade": opened_today and (t.direction or "long") == "long",
        },
    )
    db.add(audit)
    db.commit()

    clear_position_overrides(db, "trade", t.id)
    logger.info(
        "[autotrader_pos_override] live close trade=%s ticker=%s exit=%.4f pnl=%.2f",
        t.id,
        t.ticker,
        exit_px,
        pnl,
    )
    return {"ok": True, "exit_price": exit_px, "pnl": round(pnl, 4)}


def _close_paper_now(db: Session, *, trade_id: int, updated_by: str) -> dict[str, Any]:
    from .paper_trading import _apply_slippage, _close_paper_trade

    pt = db.get(PaperTrade, int(trade_id))
    if pt is None:
        return {"ok": False, "error": "paper_not_found"}
    if pt.status != "open":
        return {"ok": False, "error": f"not_open:{pt.status}"}
    sj = pt.signal_json or {}
    # Close-now works for v1-adopted paper rows AND for any pattern-linked open
    # paper row (the desk only surfaces rows with scan_pattern_id).
    is_v1 = bool(sj.get("auto_trader_v1"))
    is_linked = bool(pt.scan_pattern_id)
    if not (is_v1 or is_linked):
        return {"ok": False, "error": "not_pattern_linked"}

    px = _current_quote_price(pt.ticker)
    if px is None:
        px = float(pt.entry_price)

    exit_px = _apply_slippage(px, pt.direction or "long", is_entry=False)
    _close_paper_trade(pt, exit_px, "desk_close_now")
    db.add(pt)
    db.commit()

    opened_today = _opened_today_et(pt.entry_date) if pt.entry_date else False
    raw_alert_id = sj.get("breakout_alert_id") if isinstance(sj, dict) else None
    try:
        alert_fk = int(raw_alert_id) if raw_alert_id else None
    except (TypeError, ValueError):
        alert_fk = None
    audit = AutoTraderRun(
        user_id=pt.user_id,
        breakout_alert_id=alert_fk,
        scan_pattern_id=pt.scan_pattern_id,
        ticker=(pt.ticker or "").upper(),
        decision="desk_close_now",
        reason=f"paper_close_by={updated_by}",
        rule_snapshot={
            "opened_today_et": opened_today,
            "would_be_day_trade": opened_today and (pt.direction or "long") == "long",
            "paper": True,
        },
    )
    db.add(audit)
    db.commit()

    clear_position_overrides(db, "paper", pt.id)
    logger.info(
        "[autotrader_pos_override] paper close paper=%s ticker=%s exit=%.4f pnl=%s",
        pt.id,
        pt.ticker,
        exit_px,
        pt.pnl,
    )
    return {"ok": True, "exit_price": exit_px, "pnl": float(pt.pnl or 0.0)}


def _opened_today_et(entry_date: datetime) -> bool:
    """True if ``entry_date`` (UTC) falls on the current US/Eastern calendar day."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    if entry_date.tzinfo is None:
        entry_et = entry_date.replace(tzinfo=ZoneInfo("UTC")).astimezone(et)
    else:
        entry_et = entry_date.astimezone(et)
    return entry_et.date() == now_et.date()


def adopt_position_into_v1(
    db: Session,
    *,
    kind: PositionKind,
    trade_id: int,
    stop: float | None = None,
    target: float | None = None,
    updated_by: str = "autopilot_ui",
) -> dict[str, Any]:
    """Hand a pattern-linked open position over to AutoTrader v1.

    Live (``kind="trade"``): sets ``Trade.auto_trader_version = "v1"``. Seeds
    ``stop_loss`` / ``take_profit`` from the provided ``stop`` / ``target`` when
    the trade has none, else from the linked ``ScanPattern`` exit hints when
    available.

    Paper (``kind="paper"``): sets ``signal_json.auto_trader_v1 = True``. Same
    stop / target seeding rules apply to ``PaperTrade.stop_price`` /
    ``target_price``.

    Writes an ``AutoTraderRun(decision="adopt_manual")`` audit row so the
    handover is traceable. Returns ``{"ok": bool, "error"?: str, "stop"?: float,
    "target"?: float}``.
    """
    if kind == "trade":
        return _adopt_trade(db, trade_id=trade_id, stop=stop, target=target, updated_by=updated_by)
    if kind == "paper":
        return _adopt_paper(db, trade_id=trade_id, stop=stop, target=target, updated_by=updated_by)
    return {"ok": False, "error": f"unknown_kind:{kind}"}


def unadopt_position_from_v1(
    db: Session,
    *,
    kind: PositionKind,
    trade_id: int,
    updated_by: str = "autopilot_ui",
) -> dict[str, Any]:
    """Revert an adopted position back to "linked only" (CHILI stops managing exits).

    Clears ``auto_trader_version`` (live) or ``signal_json.auto_trader_v1``
    (paper), clears any per-position overrides, and writes an
    ``AutoTraderRun(decision="unadopt_manual")`` audit row. The broker position
    itself stays open — only CHILI's monitor relinquishes control.
    """
    if kind == "trade":
        return _unadopt_trade(db, trade_id=trade_id, updated_by=updated_by)
    if kind == "paper":
        return _unadopt_paper(db, trade_id=trade_id, updated_by=updated_by)
    return {"ok": False, "error": f"unknown_kind:{kind}"}


def _pattern_exit_hints(db: Session, scan_pattern_id: int | None) -> dict[str, float | None]:
    """Best-effort stop/target fallback from the linked ScanPattern's rules_json."""
    if not scan_pattern_id:
        return {"stop": None, "target": None}
    try:
        from ...models.trading import ScanPattern

        p = db.get(ScanPattern, int(scan_pattern_id))
        if p is None:
            return {"stop": None, "target": None}
        rj = dict(p.rules_json or {})
        exits = rj.get("exits") if isinstance(rj, dict) else None
        if not isinstance(exits, dict):
            return {"stop": None, "target": None}
        stop = exits.get("stop_pct") or exits.get("stop_loss_pct")
        target = exits.get("target_pct") or exits.get("take_profit_pct")
        return {
            "stop": float(stop) if stop not in (None, "") else None,
            "target": float(target) if target not in (None, "") else None,
        }
    except Exception:
        logger.debug("[autotrader_pos_override] pattern exit hints failed", exc_info=True)
        return {"stop": None, "target": None}


def _coerce_level(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _adopt_trade(
    db: Session,
    *,
    trade_id: int,
    stop: float | None,
    target: float | None,
    updated_by: str,
) -> dict[str, Any]:
    t = db.get(Trade, int(trade_id))
    if t is None:
        return {"ok": False, "error": "trade_not_found"}
    if t.status != "open":
        return {"ok": False, "error": f"not_open:{t.status}"}
    if not (t.scan_pattern_id or t.related_alert_id):
        return {"ok": False, "error": "not_pattern_linked"}
    if (t.auto_trader_version or "") == "v1":
        return {"ok": False, "error": "already_v1"}

    seeded_stop = _coerce_level(stop)
    seeded_target = _coerce_level(target)

    if t.stop_loss is None and seeded_stop is None:
        hints = _pattern_exit_hints(db, t.scan_pattern_id)
        entry = float(t.entry_price or 0.0)
        if entry > 0 and hints.get("stop") is not None:
            seeded_stop = entry * (1.0 - float(hints["stop"]) / 100.0)
    if t.take_profit is None and seeded_target is None:
        hints = _pattern_exit_hints(db, t.scan_pattern_id)
        entry = float(t.entry_price or 0.0)
        if entry > 0 and hints.get("target") is not None:
            seeded_target = entry * (1.0 + float(hints["target"]) / 100.0)

    previous_version = t.auto_trader_version
    t.auto_trader_version = "v1"
    if t.stop_loss is None and seeded_stop is not None:
        t.stop_loss = float(seeded_stop)
    if t.take_profit is None and seeded_target is not None:
        t.take_profit = float(seeded_target)
    db.add(t)
    db.commit()

    opened_today = _opened_today_et(t.entry_date) if t.entry_date else False
    audit = AutoTraderRun(
        user_id=t.user_id,
        breakout_alert_id=t.related_alert_id,
        scan_pattern_id=t.scan_pattern_id,
        ticker=(t.ticker or "").upper(),
        decision="adopt_manual",
        reason=f"adopt_by={updated_by}",
        trade_id=t.id,
        rule_snapshot={
            "previous_version": previous_version,
            "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
            "take_profit": float(t.take_profit) if t.take_profit is not None else None,
            "opened_today_et": opened_today,
            "would_be_day_trade": opened_today and (t.direction or "long") == "long",
        },
    )
    db.add(audit)
    db.commit()
    logger.info(
        "[autotrader_pos_override] adopt trade=%s ticker=%s stop=%s target=%s by=%s",
        t.id,
        t.ticker,
        t.stop_loss,
        t.take_profit,
        updated_by,
    )
    return {
        "ok": True,
        "kind": "trade",
        "trade_id": t.id,
        "stop": float(t.stop_loss) if t.stop_loss is not None else None,
        "target": float(t.take_profit) if t.take_profit is not None else None,
    }


def _adopt_paper(
    db: Session,
    *,
    trade_id: int,
    stop: float | None,
    target: float | None,
    updated_by: str,
) -> dict[str, Any]:
    pt = db.get(PaperTrade, int(trade_id))
    if pt is None:
        return {"ok": False, "error": "paper_not_found"}
    if pt.status != "open":
        return {"ok": False, "error": f"not_open:{pt.status}"}
    if not pt.scan_pattern_id:
        return {"ok": False, "error": "not_pattern_linked"}
    sj = dict(pt.signal_json or {})
    if sj.get("auto_trader_v1"):
        return {"ok": False, "error": "already_v1"}

    seeded_stop = _coerce_level(stop)
    seeded_target = _coerce_level(target)
    if pt.stop_price is None and seeded_stop is None:
        hints = _pattern_exit_hints(db, pt.scan_pattern_id)
        entry = float(pt.entry_price or 0.0)
        if entry > 0 and hints.get("stop") is not None:
            seeded_stop = entry * (1.0 - float(hints["stop"]) / 100.0)
    if pt.target_price is None and seeded_target is None:
        hints = _pattern_exit_hints(db, pt.scan_pattern_id)
        entry = float(pt.entry_price or 0.0)
        if entry > 0 and hints.get("target") is not None:
            seeded_target = entry * (1.0 + float(hints["target"]) / 100.0)

    sj["auto_trader_v1"] = True
    sj.setdefault("adopted_at", datetime.utcnow().isoformat())
    pt.signal_json = sj
    if pt.stop_price is None and seeded_stop is not None:
        pt.stop_price = float(seeded_stop)
    if pt.target_price is None and seeded_target is not None:
        pt.target_price = float(seeded_target)
    db.add(pt)
    db.commit()

    opened_today = _opened_today_et(pt.entry_date) if pt.entry_date else False
    raw_alert_id = sj.get("breakout_alert_id")
    try:
        alert_fk = int(raw_alert_id) if raw_alert_id else None
    except (TypeError, ValueError):
        alert_fk = None
    audit = AutoTraderRun(
        user_id=pt.user_id,
        breakout_alert_id=alert_fk,
        scan_pattern_id=pt.scan_pattern_id,
        ticker=(pt.ticker or "").upper(),
        decision="adopt_manual",
        reason=f"paper_adopt_by={updated_by}",
        rule_snapshot={
            "stop_price": float(pt.stop_price) if pt.stop_price is not None else None,
            "target_price": float(pt.target_price) if pt.target_price is not None else None,
            "opened_today_et": opened_today,
            "paper": True,
        },
    )
    db.add(audit)
    db.commit()
    logger.info(
        "[autotrader_pos_override] adopt paper=%s ticker=%s stop=%s target=%s by=%s",
        pt.id,
        pt.ticker,
        pt.stop_price,
        pt.target_price,
        updated_by,
    )
    return {
        "ok": True,
        "kind": "paper",
        "trade_id": pt.id,
        "stop": float(pt.stop_price) if pt.stop_price is not None else None,
        "target": float(pt.target_price) if pt.target_price is not None else None,
    }


def _unadopt_trade(db: Session, *, trade_id: int, updated_by: str) -> dict[str, Any]:
    t = db.get(Trade, int(trade_id))
    if t is None:
        return {"ok": False, "error": "trade_not_found"}
    if t.status != "open":
        return {"ok": False, "error": f"not_open:{t.status}"}
    if (t.auto_trader_version or "") != "v1":
        return {"ok": False, "error": "not_v1"}

    t.auto_trader_version = None
    db.add(t)
    db.commit()
    clear_position_overrides(db, "trade", t.id)

    audit = AutoTraderRun(
        user_id=t.user_id,
        breakout_alert_id=t.related_alert_id,
        scan_pattern_id=t.scan_pattern_id,
        ticker=(t.ticker or "").upper(),
        decision="unadopt_manual",
        reason=f"unadopt_by={updated_by}",
        trade_id=t.id,
        rule_snapshot={"released_to_user": True},
    )
    db.add(audit)
    db.commit()
    logger.info("[autotrader_pos_override] unadopt trade=%s by=%s", t.id, updated_by)
    return {"ok": True, "kind": "trade", "trade_id": t.id}


def _unadopt_paper(db: Session, *, trade_id: int, updated_by: str) -> dict[str, Any]:
    pt = db.get(PaperTrade, int(trade_id))
    if pt is None:
        return {"ok": False, "error": "paper_not_found"}
    if pt.status != "open":
        return {"ok": False, "error": f"not_open:{pt.status}"}
    sj = dict(pt.signal_json or {})
    if not sj.get("auto_trader_v1"):
        return {"ok": False, "error": "not_v1"}

    sj["auto_trader_v1"] = False
    sj["unadopted_at"] = datetime.utcnow().isoformat()
    pt.signal_json = sj
    db.add(pt)
    db.commit()
    clear_position_overrides(db, "paper", pt.id)

    audit = AutoTraderRun(
        user_id=pt.user_id,
        breakout_alert_id=None,
        scan_pattern_id=pt.scan_pattern_id,
        ticker=(pt.ticker or "").upper(),
        decision="unadopt_manual",
        reason=f"paper_unadopt_by={updated_by}",
        rule_snapshot={"released_to_user": True, "paper": True},
    )
    db.add(audit)
    db.commit()
    logger.info("[autotrader_pos_override] unadopt paper=%s by=%s", pt.id, updated_by)
    return {"ok": True, "kind": "paper", "trade_id": pt.id}


def paused_paper_trade_ids_for_user(db: Session, user_id: Optional[int]) -> set[int]:
    """Return AutoTrader v1 paper trade ids whose monitor is currently paused.

    Used by ``auto_trader_monitor`` to pass ``skip_trade_ids`` to
    ``check_paper_exits``.
    """
    q = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        q = q.filter(PaperTrade.user_id == user_id)
    rows = q.all()
    autotrader_ids: list[int] = []
    for pt in rows:
        sj = pt.signal_json or {}
        if sj.get("auto_trader_v1"):
            autotrader_ids.append(int(pt.id))
    if not autotrader_ids:
        return set()
    overrides = list_position_overrides(db, [("paper", tid) for tid in autotrader_ids])
    return {
        tid for (kind, tid), ov in overrides.items()
        if kind == "paper" and ov.get("monitor_paused")
    }
