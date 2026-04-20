"""AutoTrader v1 orchestrator: pattern-imminent alerts → gates → paper or RH live."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session, aliased

from ...config import settings
from ...models.trading import AutoTraderRun, BreakoutAlert, ScanPattern, Trade
from .auto_trader_llm import run_revalidation_llm
from .auto_trader_rules import (
    RuleGateContext,
    autotrader_paper_realized_pnl_today_et,
    autotrader_realized_pnl_today_et,
    breakout_alert_already_processed,
    count_autotrader_v1_open,
    passes_rule_gate,
)
from .autotrader_desk import effective_autotrader_runtime
from .autopilot_scope import (
    AUTOPILOT_AUTO_TRADER_V1,
    check_autopilot_entry_gate,
)
from .auto_trader_synergy import (
    find_open_autotrader_paper,
    find_open_autotrader_trade,
    maybe_scale_in,
)
from .management_scope import MANAGEMENT_SCOPE_AUTO_TRADER_V1

logger = logging.getLogger(__name__)

AUTOTRADER_VERSION = "v1"

# Namespace byte for advisory locks so we can't collide with other
# subsystems that also use pg_advisory_lock on alert-shaped ints. The
# lock key is (NAMESPACE << 32) | breakout_alert_id — fits a signed
# bigint and is deterministic per alert.
_ALERT_CLAIM_LOCK_NAMESPACE = 0x4154  # "AT"


def _alert_claim_lock_key(alert_id: int) -> int:
    return (_ALERT_CLAIM_LOCK_NAMESPACE << 32) | (int(alert_id) & 0xFFFFFFFF)


def _try_claim_alert(db: Session, alert_id: int) -> bool:
    """Acquire a Postgres advisory lock on this alert so only one worker
    processes it. Released when the session closes (or explicitly via
    :func:`_release_alert_claim`). Returns False if another session holds
    the lock — caller should skip.

    Safer than TOCTOU around ``breakout_alert_already_processed``: that
    check reads the AutoTraderRun audit table, but two workers can both
    pass it concurrently before either writes. SQLite (tests) doesn't
    have advisory locks, so we fail open on non-Postgres dialects — the
    existing audit-row dedupe still covers the single-process case the
    test fixtures use.
    """
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return True
    try:
        got = db.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        ).scalar()
        return bool(got)
    except Exception:
        # DB-level failure must not block the loop; treat as 'claimed'
        # and rely on the audit-row check + idempotency_store as fallback.
        logger.warning(
            "[autotrader] advisory lock acquire failed for alert=%s; falling back",
            alert_id, exc_info=True,
        )
        return True


def _release_alert_claim(db: Session, alert_id: int) -> None:
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return
    try:
        db.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        )
    except Exception:
        logger.debug(
            "[autotrader] advisory unlock failed for alert=%s; will release on session close",
            alert_id, exc_info=True,
        )


def _resolve_user_id() -> Optional[int]:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _audit(
    db: Session,
    *,
    user_id: Optional[int],
    alert: BreakoutAlert,
    decision: str,
    reason: str,
    rule_snapshot: dict[str, Any] | None = None,
    llm_snapshot: dict[str, Any] | None = None,
    trade_id: Optional[int] = None,
) -> None:
    row = AutoTraderRun(
        user_id=user_id,
        breakout_alert_id=alert.id,
        scan_pattern_id=alert.scan_pattern_id,
        ticker=(alert.ticker or "").upper(),
        decision=decision,
        reason=reason[:2000] if reason else None,
        rule_snapshot=rule_snapshot,
        llm_snapshot=llm_snapshot,
        management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
        trade_id=trade_id,
    )
    db.add(row)
    db.commit()


def _pattern_name(db: Session, scan_pattern_id: Optional[int]) -> str | None:
    if not scan_pattern_id:
        return None
    p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()
    return p.name if p else None


def _ohlcv_summary(ticker: str) -> str | None:
    try:
        from .market_data import fetch_ohlcv_df

        df = fetch_ohlcv_df(ticker, "5m", period="5d")
        if df is None or df.empty:
            return None
        tail = df.tail(15)
        if "Close" in tail.columns:
            return tail[["Close"]].to_string(max_rows=20)[:3500]
        return tail.to_string(max_rows=10)[:3500]
    except Exception:
        return None


def _current_price(ticker: str) -> float | None:
    from .market_data import fetch_quote

    q = fetch_quote(ticker)
    if not q:
        return None
    p = q.get("price") or q.get("last_price")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def run_auto_trader_tick(db: Session) -> dict[str, Any]:
    """Process a small batch of unprocessed pattern-imminent BreakoutAlerts."""
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    from .governance import is_kill_switch_active

    if is_kill_switch_active():
        return {"ok": True, "skipped": True, "reason": "kill_switch"}

    rt = effective_autotrader_runtime(db)
    if not rt.get("tick_allowed"):
        return {"ok": True, "skipped": True, "reason": "paused_or_disabled", "runtime": rt}

    uid = _resolve_user_id()
    if uid is None:
        logger.debug("[autotrader] No user id (chili_autotrader_user_id / brain_default_user_id)")
        return {"ok": False, "error": "no_user_id"}

    # Match alerts scoped to this autotrader user AND system-generated
    # (``user_id IS NULL``) pattern-imminent alerts. The imminent generator
    # writes alerts without a specific owner; treating them as processable by
    # the configured autotrader user is the intended behavior (single-tenant
    # deployment). Use ``OR`` so explicit-user alerts are still honored.
    ar = aliased(AutoTraderRun)
    candidates = (
        db.query(BreakoutAlert)
        .outerjoin(ar, ar.breakout_alert_id == BreakoutAlert.id)
        .filter(
            BreakoutAlert.alert_tier == "pattern_imminent",
            or_(BreakoutAlert.user_id == uid, BreakoutAlert.user_id.is_(None)),
            ar.id.is_(None),
        )
        .order_by(BreakoutAlert.id.asc())
        .limit(5)
        .all()
    )

    out: dict[str, Any] = {"processed": 0, "placed": 0, "scaled_in": 0, "skipped": 0}

    for alert in candidates:
        # P0.2 — acquire advisory lock before the TOCTOU window. Without
        # this, two ticks (different scheduler replicas) can both pass the
        # audit-row check and both call place_market_order. The lock is
        # held until we explicitly unlock or the session closes.
        if not _try_claim_alert(db, int(alert.id)):
            continue

        try:
            # Re-check race (another worker may have inserted between the
            # outer candidate query and our claim).
            db.expire_all()
            if breakout_alert_already_processed(db, int(alert.id)):
                continue

            try:
                _process_one_alert(db, uid, alert, out, rt)
            except Exception as e:
                logger.exception("[autotrader] alert %s failed: %s", alert.id, e)
                _audit(db, user_id=uid, alert=alert, decision="error", reason=str(e)[:500])
            out["processed"] += 1
        finally:
            _release_alert_claim(db, int(alert.id))

    return {"ok": True, **out}


def _process_one_alert(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    out: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    px = _current_price(alert.ticker)
    if px is None:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason="no_quote")
        out["skipped"] += 1
        return

    live = bool(runtime.get("live_orders_effective"))
    open_n = count_autotrader_v1_open(db, uid, paper_mode=not live)
    loss_today = (
        autotrader_paper_realized_pnl_today_et(db, uid)
        if not live
        else autotrader_realized_pnl_today_et(db, uid)
    )
    ctx = RuleGateContext(
        current_price=px,
        autotrader_open_count=open_n,
        realized_loss_today_usd=loss_today,
    )

    existing_trade = None
    existing_paper = None
    if live:
        existing_trade = find_open_autotrader_trade(db, user_id=uid, ticker=alert.ticker)
    else:
        existing_paper = find_open_autotrader_paper(db, user_id=uid, ticker=alert.ticker)

    scale_plan = None
    if live and existing_trade is not None:
        scale_plan = maybe_scale_in(
            db,
            user_id=uid,
            ticker=alert.ticker,
            new_scan_pattern_id=alert.scan_pattern_id,
            new_stop=float(alert.stop_loss) if alert.stop_loss is not None else None,
            new_target=float(alert.target_price) if alert.target_price is not None else None,
            current_price=px,
            settings=settings,
        )

    if existing_trade is not None:
        if int(existing_trade.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_already_open")
            out["skipped"] += 1
            return
        if scale_plan is None:
            reason = (
                "synergy_disabled_second_signal"
                if not getattr(settings, "chili_autotrader_synergy_enabled", False)
                else "synergy_not_applicable"
            )
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason=reason)
            out["skipped"] += 1
            return

    if not live and existing_paper is not None:
        if int(existing_paper.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_paper_open")
            out["skipped"] += 1
            return
        if getattr(settings, "chili_autotrader_synergy_enabled", False):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="paper_synergy_not_supported")
        else:
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="synergy_disabled_second_signal")
        out["skipped"] += 1
        return

    for_new = scale_plan is None

    # P1.2 — venue health circuit breaker. Cheaper than autopilot_mutex
    # (one rolling-window aggregate) and the more fundamental signal: if
    # the venue is observably sick we shouldn't fire ANY new orders there
    # regardless of ownership. Fails open: if the feature flag is off or
    # we can't compute a summary, treat as healthy. Only gates live paths
    # — paper writes nothing to the event stream so would always show
    # insufficient_data anyway.
    if live:
        try:
            from .venue.venue_health import is_venue_degraded, venue_degraded_reason
            if is_venue_degraded(db, venue="robinhood"):
                reason_detail = venue_degraded_reason(db, venue="robinhood") or "unknown"
                _audit(
                    db,
                    user_id=uid,
                    alert=alert,
                    decision="blocked",
                    reason=f"venue_degraded:robinhood:{reason_detail}"[:255],
                )
                out["skipped"] += 1
                return
        except Exception:
            # Defensive: venue-health module failure must never block the
            # autotrader loop. Fall through to the remaining gates, but
            # log loudly — a silent check failure defeats the circuit breaker.
            logger.warning(
                "[autotrader] venue_health check failed for alert=%s ticker=%s; failing open",
                alert.id, alert.ticker, exc_info=True,
            )

    # P0.4 — autopilot mutual exclusion. Only gate LIVE orders: the lease
    # signal for momentum_neural is a mode="live" TradingAutomationSession,
    # so paper v1 can't contend on the schema level. For live v1:
    #   * scale-in (scale_plan != None) → our own existing Trade is the lease,
    #     gate returns owner_self → allowed.
    #   * new entry → gate blocks if momentum_neural already owns the symbol.
    if live:
        gate = check_autopilot_entry_gate(
            db,
            candidate=AUTOPILOT_AUTO_TRADER_V1,
            symbol=alert.ticker,
            user_id=uid,
        )
        if not gate.get("allowed"):
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=f"autopilot_mutex:{gate.get('reason')}:owner={gate.get('owner') or 'none'}",
            )
            out["skipped"] += 1
            return

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=for_new, fallback_user_id=uid,
    )
    if not ok:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason=reason, rule_snapshot=snap)
        out["skipped"] += 1
        return

    llm_snap: dict[str, Any] | None = None
    if getattr(settings, "chili_autotrader_llm_revalidation_enabled", True):
        ohlcv = _ohlcv_summary(alert.ticker)
        viable, llm_snap = run_revalidation_llm(
            alert,
            current_price=px,
            ohlcv_summary=ohlcv,
            pattern_name=_pattern_name(db, alert.scan_pattern_id),
            trace_id=f"autotrader-{alert.id}",
        )
        if not viable:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="llm_not_viable",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return

    # P1.4 — runtime feature-parity assertion at entry. Fetches a fresh
    # OHLCV frame, computes the live indicator snapshot, and verifies it
    # matches the canonical compute_all_from_df output on the same frame.
    # Fails open (flag off / no frame / compute exception) so an unwired
    # environment behaves unchanged. In ``soft`` mode always allows through
    # — just records a TradingExecutionEvent with event_type='feature_parity_drift'
    # and emits an alert. In ``hard`` mode blocks entry on critical drift.
    # Only gates live paths: paper skip is cheap and paper drift still
    # records for auditing below.
    if live:
        _parity_blocked = _maybe_check_feature_parity(
            db,
            ticker=alert.ticker,
            scan_pattern_id=alert.scan_pattern_id,
            venue="robinhood",
            source="auto_trader_v1",
        )
        if _parity_blocked is not None:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=_parity_blocked[:255],
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return

    if scale_plan is not None:
        _execute_scale_in(db, uid, alert, scale_plan, px, snap, llm_snap, live, out)
        return

    _execute_new_entry(db, uid, alert, px, snap, llm_snap, live, out)


def _maybe_check_feature_parity(
    db: Session,
    *,
    ticker: str,
    scan_pattern_id: int | None,
    venue: str,
    source: str,
) -> str | None:
    """Run the P1.4 parity check. Returns a ``reason`` string when hard-mode
    blocks entry, ``None`` otherwise (including soft-mode drift, disabled,
    compute failures). Never raises.

    **Short-circuits on the feature flag BEFORE any OHLCV fetch / compute
    work.** The flag is off by default, so this function must be near-zero
    cost when unwired — otherwise every live alert pays a network fetch for
    nothing, which in a Windows test environment has been observed to exhaust
    the ephemeral socket pool (WinError 10055).
    """
    if not bool(getattr(settings, "chili_feature_parity_enabled", False)):
        return None

    try:
        from .feature_parity import (
            DEFAULT_FEATURES,
            check_entry_feature_parity,
        )
        from .indicator_core import compute_all_from_df
        from .market_data import fetch_ohlcv_df
    except Exception:
        logger.warning(
            "[autotrader] feature_parity imports unavailable; failing open for %s",
            ticker, exc_info=True,
        )
        return None

    try:
        df = fetch_ohlcv_df(ticker, "1d", "6mo")
    except Exception:
        logger.warning(
            "[autotrader] feature_parity OHLCV fetch failed for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    if df is None or df.empty:
        return None

    try:
        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
    except Exception:
        logger.warning(
            "[autotrader] feature_parity indicator compute failed for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    live_snap: dict[str, Any] = {}
    for key, vec in arrays.items():
        if not isinstance(vec, list) or not vec:
            continue
        v = vec[-1]
        if v is None:
            continue
        live_snap[key] = v

    try:
        result = check_entry_feature_parity(
            db,
            ticker=ticker,
            live_snap=live_snap,
            reference_df=df,
            features=DEFAULT_FEATURES,
            source=source,
            scan_pattern_id=scan_pattern_id,
            venue=venue,
        )
    except Exception:
        logger.warning(
            "[autotrader] feature_parity check raised for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    if result.ok:
        return None
    # Hard-mode critical block.
    return f"feature_parity:{result.reason or result.severity}"


def _execute_scale_in(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    plan: Any,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    t = plan.trade
    add_q = float(plan.added_quantity)
    if live:
        from .governance import is_kill_switch_active
        from .venue.factory import get_adapter

        # P0.5 — re-check kill switch immediately before submitting. The
        # initial check at tick entry can go stale if an operator flips the
        # switch while gates are evaluating (feature_parity / LLM can take
        # seconds). Cheap (in-memory lock + bool), so no reason not to.
        if is_kill_switch_active():
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="kill_switch_activated_mid_flight",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return

        ad = get_adapter("robinhood")
        if ad is None or not ad.is_enabled():
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="rh_adapter_off",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return
        res = ad.place_market_order(
            product_id=alert.ticker,
            side="buy",
            base_size=str(add_q),
            client_order_id=f"atv1-{alert.id}-scale",
        )
        if not res.get("ok"):
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=f"broker:{res.get('error')}",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return

    t.entry_price = float(plan.new_avg_entry)
    t.quantity = float(t.quantity) + add_q
    t.stop_loss = float(plan.new_stop)
    t.take_profit = float(plan.new_target)
    t.scale_in_count = int(t.scale_in_count or 0) + 1
    if t.indicator_snapshot is None:
        t.indicator_snapshot = {}
    if isinstance(t.indicator_snapshot, dict):
        t.indicator_snapshot = {
            **t.indicator_snapshot,
            "autotrader_scale_in_alert_ids": (t.indicator_snapshot.get("autotrader_scale_in_alert_ids") or [])
            + [alert.id],
        }
    db.add(t)
    db.commit()
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="scaled_in",
        reason="ok",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=t.id,
    )
    out["scaled_in"] += 1


def _execute_new_entry(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    if px <= 0:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason="bad_px", rule_snapshot=snap)
        out["skipped"] += 1
        return

    # Phase 3: notional = min(dial-scaled equity slice, env fallback).
    # The flat ``chili_autotrader_per_trade_notional_usd=300`` was blind to
    # equity and to pattern quality. Prefer a percent-of-equity sizing that
    # scales with the risk dial, falling back to the env dollar amount only
    # when live equity is unreachable.
    from .auto_trader_rules import (
        resolve_brain_risk_context,
        resolve_effective_capital,
    )

    env_notional = float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 300.0))
    per_trade_pct = float(getattr(settings, "chili_autotrader_per_trade_risk_pct", 1.0))
    equity, cap_source = resolve_effective_capital(db, settings)
    brain_ctx = resolve_brain_risk_context(db, user_id=uid)
    dial = float(brain_ctx.get("dial_value", 1.0))

    if equity > 0 and per_trade_pct > 0:
        dyn_notional = equity * (per_trade_pct / 100.0) * dial
        notional = dyn_notional
        snap["notional_source"] = "equity_pct_dial"
    else:
        notional = env_notional * dial
        snap["notional_source"] = "env_dollar_dial"
    snap["notional_env"] = env_notional
    snap["notional_effective"] = round(notional, 2)
    snap["notional_dial"] = dial
    snap["notional_capital_source"] = cap_source

    qty = notional / px

    if live:
        from .governance import is_kill_switch_active
        from .venue.factory import get_adapter

        # P0.5 — re-check kill switch immediately before submitting (see
        # note in _execute_scale_in).
        if is_kill_switch_active():
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="kill_switch_activated_mid_flight",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return

        ad = get_adapter("robinhood")
        if ad is None or not ad.is_enabled():
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="rh_adapter_off",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return
        res = ad.place_market_order(
            product_id=alert.ticker,
            side="buy",
            base_size=str(qty),
            client_order_id=f"atv1-{alert.id}-buy",
        )
        if not res.get("ok"):
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=f"broker:{res.get('error')}",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            return
        raw = res.get("raw") or {}
        try:
            fill = float(raw.get("average_price") or raw.get("price") or px)
        except (TypeError, ValueError):
            fill = px

        tr = Trade(
            user_id=uid,
            ticker=alert.ticker.upper(),
            direction="long",
            entry_price=fill,
            quantity=float(qty),
            entry_date=datetime.utcnow(),
            status="open",
            stop_loss=float(alert.stop_loss) if alert.stop_loss is not None else None,
            take_profit=float(alert.target_price) if alert.target_price is not None else None,
            scan_pattern_id=alert.scan_pattern_id,
            related_alert_id=alert.id,
            broker_source="robinhood",
            management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
            broker_order_id=str(res.get("order_id") or ""),
            indicator_snapshot={
                "breakout_alert": alert.indicator_snapshot,
                "signals": alert.signals_snapshot,
            },
            tags="autotrader_v1",
            auto_trader_version=AUTOTRADER_VERSION,
            scale_in_count=0,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        # Phase 2C: emit trade_lifecycle entry event and save correlation_id
        # on the Trade. On close, plasticity uses this to look up the path log
        # and reinforce/attenuate the edges that carried the signal.
        try:
            from .brain_neural_mesh.publisher import publish_trade_lifecycle

            entry_corr = publish_trade_lifecycle(
                db,
                trade_id=int(tr.id),
                ticker=tr.ticker,
                transition="entry",
                broker_source="robinhood",
                quantity=float(tr.quantity),
                price=float(fill),
            )
            if entry_corr:
                tr.mesh_entry_correlation_id = entry_corr
                db.commit()
        except Exception:
            pass
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="placed",
            reason="ok",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
            trade_id=tr.id,
        )
        out["placed"] += 1
        return

    # Paper
    from .paper_trading import open_paper_trade

    iq = max(1, int(qty))
    sig = {
        "auto_trader_v1": True,
        "breakout_alert_id": alert.id,
        "projected": snap.get("projected_profit_pct"),
    }
    pt = open_paper_trade(
        db,
        uid,
        alert.ticker,
        px,
        scan_pattern_id=alert.scan_pattern_id,
        stop_price=float(alert.stop_loss) if alert.stop_loss is not None else None,
        target_price=float(alert.target_price) if alert.target_price is not None else None,
        direction="long",
        quantity=iq,
        signal_json=sig,
    )
    if pt is None:
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="paper_open_failed",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        return

    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="placed",
        reason="paper",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=None,
    )
    out["placed"] += 1
