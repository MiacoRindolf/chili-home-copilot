"""Alert engine: price monitoring, strategy proposals, and alert dispatch.

Monitors open positions, breakout candidates, and AI Brain predictions to
send SMS alerts and generate strategy proposals for user review.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Alert type constants
BREAKOUT_TRIGGERED = "breakout_triggered"
TARGET_HIT = "target_hit"
STOP_HIT = "stop_hit"
NEW_TOP_PICK = "new_top_pick"
POSITION_OPENED = "position_opened"
POSITION_CLOSED = "position_closed"
STRATEGY_PROPOSED = "strategy_proposed"
WEEKLY_REVIEW = "weekly_review"

ALL_ALERT_TYPES = [
    BREAKOUT_TRIGGERED, TARGET_HIT, STOP_HIT, NEW_TOP_PICK,
    POSITION_OPENED, POSITION_CLOSED, STRATEGY_PROPOSED, WEEKLY_REVIEW,
]

_PATTERN_KW = [
    "macd_bullish", "macd_positive", "macd_negative",
    "ema_stack", "ema_stacking", "rsi_oversold", "rsi_overbought",
    "bollinger", "bb_squeeze", "volume_surge", "pullback",
    "breakout", "gap_up", "gap_down", "stoch_oversold", "stoch_overbought",
    "adx_strong", "float_micro", "float_low", "topping_tail",
    "vwap_above", "momentum", "reversal", "divergence",
]


def _extract_pattern_keywords(signals: list[str]) -> list[str]:
    """Extract canonical pattern tags from free-form signal descriptions."""
    found: list[str] = []
    combined = " ".join(s.lower() for s in signals)
    for kw in _PATTERN_KW:
        readable = kw.replace("_", " ")
        if readable in combined or kw in combined:
            found.append(kw)
    if not found and signals:
        found.append("unclassified")
    return found[:10]

_PROPOSAL_EXPIRE_HOURS = 24
_MIN_SCORE_FOR_PROPOSAL = 7.5
_MAX_RISK_PCT = 2.0  # max 2% of portfolio per trade
_MIN_RR_FOR_PROPOSAL = 1.5  # minimum risk:reward ratio
_MIN_RR_FOR_FROM_PICK = 0.8  # lower threshold when user explicitly creates from pick (price may have drifted)
_MIN_PRICE_FOR_PROPOSAL = 1.0  # skip sub-$1 penny stocks

# ── Position sizing caps ───────────────────────────────────────────────
_POS_PCT_HARD_CAP = 10.0
_POS_PCT_RISK_OFF_CAP = 7.0
_POS_PCT_SPECULATIVE_CAP = 5.0

_SPECULATIVE_KEYWORDS = [
    "float_micro", "float_low", "micro", "microcap", "penny",
    "high risk", "high volatility", "leveraged",
]


def _compute_position_size(
    price: float,
    stop: float,
    buying_power: float,
    pick: dict[str, Any],
) -> tuple[int | None, float | None]:
    """Risk-based position sizing with regime, volatility, and instrument overlays.

    Returns (quantity, position_size_pct).  Both are None when buying_power
    is zero or inputs are invalid.
    """
    risk_per_share = abs(price - stop)
    if buying_power <= 0 or risk_per_share <= 0 or price <= 0:
        return None, None

    risk_dollars = buying_power * (_MAX_RISK_PCT / 100)
    raw_shares = risk_dollars / risk_per_share
    raw_pct = (raw_shares * price) / buying_power * 100

    # ── 1. Market-regime overlay ──────────────────────────────────────
    regime_label = "cautious"
    vix_regime = "normal"
    try:
        from .market_data import get_market_regime
        _mr = get_market_regime()
        regime_label = _mr.get("regime", "cautious")
        vix_regime = _mr.get("vix_regime", "normal")
    except Exception:
        pass

    regime_mult = 1.0
    if regime_label == "risk_off":
        regime_mult = 0.50
    elif regime_label == "cautious":
        regime_mult = 0.75

    if vix_regime == "elevated":
        regime_mult *= 0.85
    elif vix_regime == "extreme":
        regime_mult *= 0.70

    # ── 2. Volatility overlay (wide stop → more volatile) ─────────────
    stop_dist_pct = risk_per_share / price * 100
    vol_mult = 1.0
    if stop_dist_pct > 10:
        vol_mult = 0.70
    elif stop_dist_pct > 8:
        vol_mult = 0.80
    elif stop_dist_pct > 5:
        vol_mult = 0.90

    # ── 3. Instrument-quality / speculative overlay ───────────────────
    signals_text = " ".join(s.lower() for s in pick.get("signals", []))
    risk_level = (pick.get("risk_level") or "").lower()

    is_speculative = risk_level in ("high", "very_high") or pick.get("is_crypto", False)
    if not is_speculative:
        for kw in _SPECULATIVE_KEYWORDS:
            if kw in signals_text:
                is_speculative = True
                break

    spec_mult = 0.60 if is_speculative else 1.0

    # ── 4. Apply multiplicative overlays ──────────────────────────────
    adjusted_pct = raw_pct * regime_mult * vol_mult * spec_mult

    # ── 5. Scanner/brain soft cap — stay near their suggestion ────────
    pick_pct = pick.get("position_size_pct")
    if pick_pct and pick_pct > 0:
        adjusted_pct = min(adjusted_pct, pick_pct * 1.25)

    # ── 6. Hard caps (regime-aware) ───────────────────────────────────
    cap = _POS_PCT_HARD_CAP
    if regime_label == "risk_off" or vix_regime in ("elevated", "extreme"):
        cap = min(cap, _POS_PCT_RISK_OFF_CAP)
    if is_speculative:
        cap = min(cap, _POS_PCT_SPECULATIVE_CAP)

    final_pct = round(min(adjusted_pct, cap), 2)

    position_dollars = buying_power * (final_pct / 100)
    quantity = max(1, int(position_dollars / price))

    return quantity, final_pct


def _expire_proposals_on_stop(
    db: Session,
    user_id: int | None,
    ticker: str,
) -> None:
    """Expire any active proposals for *ticker* after a stop-hit alert."""
    from ...models.trading import StrategyProposal

    try:
        active = (
            db.query(StrategyProposal)
            .filter(
                StrategyProposal.ticker == ticker,
                StrategyProposal.status.in_(["pending", "approved"]),
            )
        )
        if user_id is not None:
            active = active.filter(StrategyProposal.user_id == user_id)
        proposals = active.all()
        for p in proposals:
            p.status = "expired"
            p.thesis = (p.thesis or "") + " [Auto-expired: stop-loss hit]"
        if proposals:
            db.commit()
            logger.info(
                f"[alerts] Auto-expired {len(proposals)} proposal(s) for {ticker} on stop hit"
            )
    except Exception as e:
        logger.warning(f"[alerts] Failed to expire proposals for {ticker}: {e}")


def dispatch_alert(
    db: Session | None = None,
    user_id: int | None = None,
    alert_type: str = "",
    ticker: str | None = None,
    message: str = "",
    *,
    price: float | None = None,
) -> bool:
    """Log an alert to the DB and optionally send via SMS.

    Always persists to DB regardless of SMS outcome.  If SMS is not
    configured, the alert is logged with sent_via='log_only' (no
    error-level log — just info).  Only logs a warning when SMS IS
    configured but delivery fails.
    """
    from ..sms_service import is_configured as sms_is_configured, send_sms

    own_session = False
    if db is None:
        from ...db import SessionLocal
        db = SessionLocal()
        own_session = True

    sent = False
    sent_via = "log_only"

    try:
        if sms_is_configured():
            sent = send_sms(message)
            sent_via = ("twilio" if sent else "sms_failed")
            if sent:
                logger.info(f"[alerts] Sent {alert_type} alert for {ticker}: {message[:80]}")
            else:
                logger.warning(f"[alerts] SMS delivery failed for {alert_type}/{ticker}")
        else:
            logger.info(f"[alerts] Logged {alert_type} for {ticker} (SMS not configured)")

        from ...models.trading import AlertHistory
        record = AlertHistory(
            user_id=user_id,
            alert_type=alert_type,
            ticker=ticker,
            message=message,
            sent_via=sent_via,
            success=sent,
        )
        db.add(record)
        db.commit()
    except Exception as e:
        logger.error(f"[alerts] dispatch_alert DB error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if own_session:
            db.close()

    return sent


def get_alert_history(
    db: Session,
    user_id: int | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from ...models.trading import AlertHistory
    rows = (
        db.query(AlertHistory)
        .filter(AlertHistory.user_id == user_id)
        .order_by(AlertHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "alert_type": r.alert_type,
            "ticker": r.ticker,
            "message": r.message,
            "sent_via": r.sent_via,
            "success": r.success,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ── Strategy Proposals ────────────────────────────────────────────────


def _supersede_proposals(db: Session, user_id: int | None, ticker: str) -> int:
    """Reject any existing pending/approved/working proposals for *ticker* so
    only the newest proposal remains active. Returns count of superseded rows."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    user_filter = (
        or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None))
        if user_id is not None
        else StrategyProposal.user_id.is_(None)
    )
    stale = (
        db.query(StrategyProposal)
        .filter(
            user_filter,
            StrategyProposal.ticker == ticker,
            StrategyProposal.status.in_(["pending", "approved", "working"]),
        )
        .all()
    )
    now = datetime.utcnow()
    for p in stale:
        p.status = "rejected"
        p.reviewed_at = now
    return len(stale)


def generate_strategy_proposals(
    db: Session,
    user_id: int | None,
) -> list[dict[str, Any]]:
    """Generate strategy proposals from high-confidence top picks.

    Called after each learning cycle and by the price monitor when
    high-confidence opportunities emerge.
    """
    from ...models.trading import StrategyProposal, ScanResult
    from .scanner import generate_top_picks
    from .market_data import fetch_quote

    picks = generate_top_picks(db, user_id)
    created = []

    buying_power = _get_buying_power()

    for pick in picks:
        combined = pick.get("combined_score", 0)
        if combined < _MIN_SCORE_FOR_PROPOSAL:
            continue
        if pick.get("signal") != "buy":
            continue

        ticker = pick["ticker"]

        # Supersede any existing non-executed proposal for this ticker
        _supersede_proposals(db, user_id, ticker)

        # Use latest Massive-backed price for entry
        quote = fetch_quote(ticker) or {}
        price = quote.get("price") or pick.get("price") or pick.get("entry_price") or 0
        stop = pick.get("stop_loss") or pick.get("brain_stop") or 0
        target = pick.get("take_profit") or pick.get("brain_target") or 0

        if not price or price <= 0 or not stop or not target:
            continue

        risk_per_share = abs(price - stop)
        reward_per_share = abs(target - price)

        if risk_per_share <= 0:
            continue

        rr_ratio = round(reward_per_share / risk_per_share, 2)

        if rr_ratio < _MIN_RR_FOR_PROPOSAL:
            continue
        if price < _MIN_PRICE_FOR_PROPOSAL:
            continue

        projected_profit_pct = round((target - price) / price * 100, 2)
        projected_loss_pct = round((price - stop) / price * 100, 2)

        quantity, position_size_pct = _compute_position_size(
            price, stop, buying_power, pick,
        )

        confidence = pick.get("brain_confidence") or (combined * 10)

        signals = pick.get("signals", [])
        indicators = pick.get("indicators", {})

        # Determine timeframe
        timeframe = pick.get("timeframe", "swing")

        thesis = pick.get("thesis", "")
        if not thesis:
            thesis = f"AI-identified bullish setup for {ticker} with score {combined:.1f}/10."

        proposal = StrategyProposal(
            user_id=user_id,
            ticker=ticker,
            direction="long",
            status="pending",
            entry_price=price,
            stop_loss=stop,
            take_profit=target,
            quantity=quantity,
            position_size_pct=position_size_pct,
            projected_profit_pct=projected_profit_pct,
            projected_loss_pct=projected_loss_pct,
            risk_reward_ratio=rr_ratio,
            confidence=round(confidence, 1),
            timeframe=timeframe,
            thesis=thesis,
            signals_json=json.dumps(signals) if signals else None,
            indicator_json=json.dumps(indicators) if indicators else None,
            brain_score=pick.get("brain_score"),
            ml_probability=pick.get("ml_probability"),
            scan_score=pick.get("score"),
            proposed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=_PROPOSAL_EXPIRE_HOURS),
        )
        db.add(proposal)
        db.flush()

        # Send SMS about the new proposal
        _cr = ticker.endswith("-USD")
        price_fmt = f"${price:,.6f}" if _cr and price < 1 else f"${price:,.2f}"
        sms_msg = (
            f"CHILI Strategy: BUY {ticker} @ {price_fmt} | "
            f"Stop ${stop:,.2f} | Target ${target:,.2f} | "
            f"R:R {rr_ratio:.1f}:1 | "
            f"+{projected_profit_pct:.1f}% profit | "
            f"Conf {confidence:.0f}% | "
            f"Review in app"
        )
        dispatch_alert(db, user_id, STRATEGY_PROPOSED, ticker, sms_msg)

        created.append({
            "id": proposal.id,
            "ticker": ticker,
            "confidence": confidence,
            "rr_ratio": rr_ratio,
        })

    try:
        db.commit()
    except Exception:
        db.rollback()

    logger.info(f"[alerts] Generated {len(created)} strategy proposals")
    return created


def create_proposal_from_pick(
    db: Session,
    user_id: int | None,
    ticker: str,
    pick: dict[str, Any] | None = None,
    override_levels: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Create (or replace) a single strategy proposal for *ticker* using the
    latest Massive-backed price and the pick's or override stop/target levels.

    override_levels may contain entry_price, stop_loss, take_profit (used when
    ticker is not in top picks cache).

    Returns (proposal_dict, None) on success, (None, error_message) on failure.
    """
    from ...models.trading import StrategyProposal
    from .market_data import fetch_quote
    from .scanner import generate_top_picks

    ticker = ticker.upper()
    override_levels = override_levels or {}

    if pick is None:
        from .scanner import _top_picks_cache
        cached = _top_picks_cache.get("picks") or []
        pick = next((p for p in cached if (p.get("ticker") or "").upper() == ticker), None)
        if pick is None:
            picks = generate_top_picks(db, user_id)
            pick = next((p for p in picks if (p.get("ticker") or "").upper() == ticker), None)
    if not pick and override_levels:
        quote = fetch_quote(ticker) or {}
        entry = override_levels.get("entry_price") or quote.get("price")
        stop = override_levels.get("stop_loss")
        target = override_levels.get("take_profit")
        if stop and target and (entry or quote.get("price")):
            entry = entry or quote.get("price")
            pick = {
                "ticker": ticker,
                "price": entry,
                "entry_price": entry,
                "stop_loss": stop,
                "brain_stop": stop,
                "take_profit": target,
                "brain_target": target,
                "combined_score": 5.0,
                "timeframe": "swing",
                "thesis": f"Proposal from pick for {ticker} (levels from request).",
            }
    if not pick:
        return None, f"{ticker} is not in current top picks. Run a Full Scan or refresh, or the pick may have expired."

    combined = pick.get("combined_score", 0)

    quote = fetch_quote(ticker) or {}
    price = quote.get("price") or pick.get("price") or pick.get("entry_price") or 0
    stop = pick.get("stop_loss") or pick.get("brain_stop") or 0
    target = pick.get("take_profit") or pick.get("brain_target") or 0

    if not price or price <= 0:
        return None, "Could not get current price for this ticker."
    if not stop or not target:
        return None, "Missing stop loss or take profit; cannot create proposal."

    risk_per_share = abs(price - stop)
    reward_per_share = abs(target - price)
    if risk_per_share <= 0:
        return None, "Stop loss must be different from entry."

    rr_ratio = round(reward_per_share / risk_per_share, 2)
    min_rr = _MIN_RR_FOR_FROM_PICK  # use lower threshold for explicit "create from pick"
    if rr_ratio < min_rr:
        return None, f"Risk:reward ratio {rr_ratio}:1 is below minimum {min_rr}:1."
    if price < _MIN_PRICE_FOR_PROPOSAL:
        return None, f"Price ${price} is below minimum ${_MIN_PRICE_FOR_PROPOSAL}."

    _supersede_proposals(db, user_id, ticker)

    projected_profit_pct = round((target - price) / price * 100, 2)
    projected_loss_pct = round((price - stop) / price * 100, 2)

    buying_power = _get_buying_power()
    quantity, position_size_pct = _compute_position_size(price, stop, buying_power, pick)
    confidence = pick.get("brain_confidence") or (combined * 10)
    signals = pick.get("signals", [])
    indicators = pick.get("indicators", {})
    timeframe = pick.get("timeframe", "swing")
    thesis = pick.get("thesis", "")
    if not thesis:
        thesis = f"AI-identified bullish setup for {ticker} with score {combined:.1f}/10."

    proposal = StrategyProposal(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        status="pending",
        entry_price=price,
        stop_loss=stop,
        take_profit=target,
        quantity=quantity,
        position_size_pct=position_size_pct,
        projected_profit_pct=projected_profit_pct,
        projected_loss_pct=projected_loss_pct,
        risk_reward_ratio=rr_ratio,
        confidence=round(confidence, 1),
        timeframe=timeframe,
        thesis=thesis,
        signals_json=json.dumps(signals) if signals else None,
        indicator_json=json.dumps(indicators) if indicators else None,
        brain_score=pick.get("brain_score"),
        ml_probability=pick.get("ml_probability"),
        scan_score=pick.get("score"),
        proposed_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=_PROPOSAL_EXPIRE_HOURS),
    )
    db.add(proposal)
    try:
        db.commit()
        db.refresh(proposal)
    except Exception as e:
        db.rollback()
        logger.exception("create_proposal_from_pick commit failed")
        return None, f"Database error: {e!s}"

    logger.info(f"[alerts] Created proposal from pick: {ticker} @ ${price}")
    return _proposal_to_dict(proposal), None


def get_proposals(
    db: Session,
    user_id: int | None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    # Expire old pending proposals
    _expire_proposals(db)

    if user_id is not None:
        q = db.query(StrategyProposal).filter(
            or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None))
        )
    else:
        q = db.query(StrategyProposal).filter(StrategyProposal.user_id.is_(None))
    if status:
        q = q.filter(StrategyProposal.status == status)
    else:
        q = q.filter(
            StrategyProposal.status.in_(["pending", "approved", "working", "executed", "rejected"]),
        )

    rows = q.order_by(StrategyProposal.proposed_at.desc()).limit(limit).all()
    return [_proposal_to_dict(r) for r in rows]


def approve_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
) -> dict[str, Any]:
    """Approve a proposal and execute via Robinhood if connected."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}
    if proposal.status != "pending":
        return {"ok": False, "error": f"Proposal is already {proposal.status}"}
    if proposal.expires_at and proposal.expires_at < datetime.utcnow():
        proposal.status = "expired"
        db.commit()
        return {"ok": False, "error": "Proposal has expired"}

    proposal.status = "approved"
    proposal.reviewed_at = datetime.utcnow()
    db.commit()

    # Attempt order execution
    execution = _execute_proposal(db, proposal, user_id)

    return {
        "ok": True,
        "proposal": _proposal_to_dict(proposal),
        "execution": execution,
    }


def reject_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
) -> dict[str, Any]:
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}

    proposal.status = "rejected"
    proposal.reviewed_at = datetime.utcnow()
    db.commit()

    return {"ok": True, "proposal": _proposal_to_dict(proposal)}


def recheck_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
    *,
    drift_expire_pct: float = 30.0,
) -> dict[str, Any]:
    """Revalidate a proposal with live price. Returns drift info; expires if drifted past threshold."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_
    from .market_data import fetch_quote

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}

    quote = fetch_quote(proposal.ticker)
    live_price = quote.get("price") if quote else None

    if not live_price or live_price <= 0:
        return {
            "ok": True,
            "proposal": _proposal_to_dict(proposal),
            "live_price": None,
            "drift_pct": None,
            "status": "unavailable",
            "message": "Could not fetch current price.",
        }

    drift_pct = abs(live_price - proposal.entry_price) / proposal.entry_price * 100

    if drift_pct > drift_expire_pct and proposal.status in ("pending", "approved"):
        proposal.status = "expired"
        proposal.thesis = (proposal.thesis or "") + f" [Auto-expired: price drifted {drift_pct:.0f}% from entry]"
        db.commit()
        return {
            "ok": True,
            "proposal": _proposal_to_dict(proposal),
            "live_price": live_price,
            "drift_pct": round(drift_pct, 2),
            "status": "invalidated",
            "expired": True,
        }

    status = "valid" if drift_pct <= 5 else ("moved_but_ok" if drift_pct <= 15 else "invalidated")
    return {
        "ok": True,
        "proposal": _proposal_to_dict(proposal),
        "live_price": live_price,
        "drift_pct": round(drift_pct, 2),
        "status": status,
        "expired": False,
    }


# ── Price Monitor ─────────────────────────────────────────────────────


def run_price_monitor(db: Session, user_id: int | None) -> dict[str, Any]:
    """Check positions, breakouts, and picks for alert-worthy events.

    Designed to run every 5 minutes during market hours.
    """
    results: dict[str, Any] = {
        "targets_hit": 0,
        "stops_hit": 0,
        "breakouts": 0,
        "proposals_generated": 0,
    }

    # 1. Check open positions for target/stop hits
    results.update(_check_open_positions(db, user_id))

    # 2. Check breakout candidates
    results["breakouts"] = _check_breakout_candidates(db, user_id)

    # 3. Check for new high-confidence picks → auto-generate proposals
    proposals = _check_top_picks_for_proposals(db, user_id)
    results["proposals_generated"] = len(proposals)

    logger.info(f"[alerts] Price monitor: {results}")
    return results


def _check_open_positions(db: Session, user_id: int | None) -> dict[str, int]:
    """Check open trades against current prices for stop/target alerts."""
    from ...models.trading import Trade
    from .market_data import fetch_quote

    targets_hit = 0
    stops_hit = 0

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    for trade in open_trades:
        try:
            quote = fetch_quote(trade.ticker)
            price = quote.get("price", 0) if quote else 0
            if not price or price <= 0:
                continue

            # Parse stop/target from indicator_snapshot or tags
            snapshot = {}
            if trade.indicator_snapshot:
                try:
                    snapshot = json.loads(trade.indicator_snapshot)
                except Exception:
                    pass

            stop = snapshot.get("stop_loss") or (trade.entry_price * 0.95)
            target = snapshot.get("take_profit") or (trade.entry_price * 1.10)

            if trade.direction == "long":
                if price >= target:
                    _cr = trade.ticker.endswith("-USD")
                    pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)
                    msg = (
                        f"TARGET HIT: {trade.ticker} reached ${price:,.2f} "
                        f"(target ${target:,.2f}) | +{pnl_pct:.1f}% | "
                        f"Consider taking profits"
                    )
                    dispatch_alert(db, user_id, TARGET_HIT, trade.ticker, msg)
                    targets_hit += 1

                elif price <= stop:
                    pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)
                    msg = (
                        f"STOP HIT: {trade.ticker} dropped to ${price:,.2f} "
                        f"(stop ${stop:,.2f}) | {pnl_pct:.1f}% | "
                        f"Consider cutting losses"
                    )
                    dispatch_alert(db, user_id, STOP_HIT, trade.ticker, msg)
                    _expire_proposals_on_stop(db, user_id, trade.ticker)
                    stops_hit += 1

        except Exception as e:
            logger.debug(f"[alerts] Error checking {trade.ticker}: {e}")

    return {"targets_hit": targets_hit, "stops_hit": stops_hit}


def _check_breakout_candidates(db: Session, user_id: int | None) -> int:
    """Check recent scan results with breakout signals."""
    from ...models.trading import ScanResult, AlertHistory
    from .market_data import fetch_quote

    cutoff = datetime.utcnow() - timedelta(hours=6)
    recent_scans = (
        db.query(ScanResult)
        .filter(
            ScanResult.scanned_at >= cutoff,
            ScanResult.score >= 7.0,
        )
        .all()
    )

    breakouts = 0
    for scan in recent_scans:
        try:
            # Only alert if we haven't already alerted for this ticker recently
            recent_alert = (
                db.query(AlertHistory)
                .filter(
                    AlertHistory.ticker == scan.ticker,
                    AlertHistory.alert_type == BREAKOUT_TRIGGERED,
                    AlertHistory.created_at >= datetime.utcnow() - timedelta(hours=4),
                )
                .first()
            )
            if recent_alert:
                continue

            quote = fetch_quote(scan.ticker)
            price = quote.get("price", 0) if quote else 0
            if not price:
                continue

            # Check if price has broken above the resistance (take_profit level as proxy)
            resistance = scan.take_profit or (scan.entry_price * 1.03 if scan.entry_price else 0)
            if resistance and price > resistance and scan.signal == "buy":
                msg = (
                    f"BREAKOUT: {scan.ticker} broke ${resistance:,.2f} "
                    f"now at ${price:,.2f} | Score {scan.score:.1f}/10 | "
                    f"{scan.rationale[:60] if scan.rationale else ''}"
                )
                dispatch_alert(db, user_id, BREAKOUT_TRIGGERED, scan.ticker, msg)
                breakouts += 1

        except Exception as e:
            logger.debug(f"[alerts] Error checking breakout {scan.ticker}: {e}")

    return breakouts


def _check_top_picks_for_proposals(
    db: Session,
    user_id: int | None,
) -> list[dict]:
    """If high-confidence picks exist without proposals, create them."""
    from ...models.trading import StrategyProposal
    from .scanner import generate_top_picks

    picks = generate_top_picks(db, user_id)
    new_proposals = []

    for pick in picks:
        if (pick.get("combined_score", 0) < 8.0 or
                pick.get("signal") != "buy"):
            continue

        ticker = pick["ticker"]
        existing = (
            db.query(StrategyProposal)
            .filter(
                StrategyProposal.ticker == ticker,
                StrategyProposal.status.in_(["pending", "approved", "working", "executed"]),
                StrategyProposal.proposed_at >= datetime.utcnow() - timedelta(hours=12),
            )
            .first()
        )
        if existing:
            continue

        # Delegate to the full proposal generator (one at a time)
        proposals = generate_strategy_proposals(db, user_id)
        new_proposals.extend(proposals)
        break  # generate_strategy_proposals handles all qualifying picks

    return new_proposals


# ── Helpers ───────────────────────────────────────────────────────────


def _execute_proposal(
    db: Session,
    proposal,
    user_id: int | None,
) -> dict[str, Any]:
    """Place a Robinhood order for an approved proposal (or record locally).

    When Robinhood is connected the order is placed and both the proposal
    and trade start in **working** status.  Only the periodic order-sync
    job will flip them to *executed* / *open* once Robinhood confirms a
    fill — this prevents marking trades as "executed" when the limit order
    is still sitting unfilled.
    """
    from ..broker_service import is_connected, place_buy_order, map_rh_status
    from ...models.trading import Trade

    ticker = proposal.ticker
    quantity = proposal.quantity

    # Compute quantity if missing
    if not quantity or quantity <= 0:
        buying_power = _get_buying_power()
        risk_per_share = abs(proposal.entry_price - proposal.stop_loss) if proposal.stop_loss else 0
        if buying_power > 0 and risk_per_share > 0:
            risk_dollars = buying_power * (_MAX_RISK_PCT / 100)
            quantity = max(1, int(risk_dollars / risk_per_share))
            proposal.quantity = quantity
        else:
            quantity = 1
            proposal.quantity = 1

    if not is_connected():
        trade = Trade(
            user_id=user_id,
            ticker=ticker,
            direction="long",
            entry_price=proposal.entry_price,
            quantity=quantity,
            status="open",
            broker_source="manual",
            indicator_snapshot=json.dumps({
                "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit,
                "proposal_id": proposal.id,
            }),
            tags="proposal-approved",
            notes=f"Approved from proposal #{proposal.id} (manual — broker not connected)",
        )
        db.add(trade)
        db.flush()
        proposal.status = "executed"
        proposal.executed_at = datetime.utcnow()
        proposal.trade_id = trade.id
        db.commit()
        return {"status": "recorded", "trade_id": trade.id, "reason": "Broker not connected — trade recorded locally"}

    try:
        result = place_buy_order(
            ticker=ticker,
            quantity=quantity,
            order_type="limit",
            limit_price=proposal.entry_price,
        )

        if result.get("ok"):
            rh_state = (result.get("raw") or {}).get("state", "queued")
            chili_status = map_rh_status(rh_state)
            is_already_filled = rh_state == "filled"

            proposal.broker_order_id = result.get("order_id")
            if is_already_filled:
                proposal.status = "executed"
                proposal.executed_at = datetime.utcnow()
            else:
                proposal.status = "working"

            _ptags = ""
            if proposal.signals_json:
                try:
                    _signals = json.loads(proposal.signals_json) if isinstance(proposal.signals_json, str) else proposal.signals_json
                    if isinstance(_signals, list):
                        _ptags = ",".join(_extract_pattern_keywords(_signals))
                except Exception:
                    pass

            avg_price = _safe_float((result.get("raw") or {}).get("average_price"))

            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price if is_already_filled and avg_price else proposal.entry_price,
                quantity=quantity,
                status="open" if is_already_filled else "working",
                broker_source="robinhood",
                broker_order_id=result.get("order_id"),
                broker_status=rh_state,
                last_broker_sync=datetime.utcnow(),
                filled_at=datetime.utcnow() if is_already_filled else None,
                avg_fill_price=avg_price if is_already_filled else None,
                indicator_snapshot=json.dumps({
                    "stop_loss": proposal.stop_loss,
                    "take_profit": proposal.take_profit,
                    "proposal_id": proposal.id,
                }),
                tags="auto-trade,proposal",
                pattern_tags=_ptags or None,
                notes=f"Order placed from proposal #{proposal.id}: {proposal.thesis[:100]}",
            )
            db.add(trade)
            db.flush()
            proposal.trade_id = trade.id
            db.commit()

            if is_already_filled:
                msg = (
                    f"ORDER FILLED: BUY {quantity} {ticker} @ ${trade.entry_price:,.2f} "
                    f"via Robinhood | Proposal #{proposal.id}"
                )
                dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
                return {"status": "executed", "order_id": result.get("order_id"), "trade_id": trade.id}
            else:
                msg = (
                    f"ORDER PLACED: BUY {quantity} {ticker} @ ${proposal.entry_price:,.2f} "
                    f"(limit, waiting for fill) via Robinhood | Proposal #{proposal.id}"
                )
                dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
                return {"status": "working", "order_id": result.get("order_id"), "trade_id": trade.id}
        else:
            error = result.get("error", "Unknown error")
            msg = f"ORDER FAILED: {ticker} — {error}"
            dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
            return {"status": "failed", "error": error}

    except Exception as e:
        logger.error(f"[alerts] Execution failed for {ticker}: {e}")
        return {"status": "error", "error": str(e)}


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _get_buying_power() -> float:
    """Get current buying power from Robinhood, or a sensible default."""
    try:
        from ..broker_service import is_connected, get_portfolio
        if is_connected():
            portfolio = get_portfolio()
            return portfolio.get("buying_power", 0)
    except Exception:
        pass
    return 10000.0  # default for position sizing when broker is not connected


_PRICE_DRIFT_EXPIRE_PCT = 30  # expire proposals when price drifts >30% from entry


def _expire_proposals(db: Session) -> int:
    """Mark expired pending proposals (time-based + price-drift)."""
    from ...models.trading import StrategyProposal
    from .market_data import fetch_quote

    count = 0

    # 1. Time-based expiry
    time_expired = (
        db.query(StrategyProposal)
        .filter(
            StrategyProposal.status == "pending",
            StrategyProposal.expires_at < datetime.utcnow(),
        )
        .all()
    )
    for p in time_expired:
        p.status = "expired"
        count += 1

    # 2. Price-drift expiry: if price moved far from entry, the setup is invalid
    active = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.status.in_(["pending", "approved"]))
        .all()
    )
    for p in active:
        try:
            quote = fetch_quote(p.ticker)
            price = quote.get("price", 0) if quote else 0
            if price and p.entry_price and p.entry_price > 0:
                drift_pct = abs(price - p.entry_price) / p.entry_price * 100
                if drift_pct > _PRICE_DRIFT_EXPIRE_PCT:
                    p.status = "expired"
                    p.thesis = (p.thesis or "") + f" [Auto-expired: price drifted {drift_pct:.0f}% from entry]"
                    count += 1
                    logger.info(
                        f"[alerts] Price-drift expired {p.ticker}: "
                        f"entry ${p.entry_price:.2f} → ${price:.2f} ({drift_pct:.0f}%)"
                    )
        except Exception:
            pass

    if count:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return count


def _proposal_to_dict(p) -> dict[str, Any]:
    now = datetime.utcnow()
    proposed_at = p.proposed_at
    expires_at = p.expires_at

    age_seconds = (
        round((now - proposed_at).total_seconds()) if proposed_at else None
    )
    expires_in_seconds = (
        round((expires_at - now).total_seconds()) if expires_at else None
    )
    is_expired = p.status == "expired" or (
        expires_at is not None and expires_at < now
    )

    expiry_reason = None
    if p.status == "expired" and p.thesis:
        if "[Auto-expired: price drifted" in p.thesis:
            expiry_reason = "price_drift"
        else:
            expiry_reason = "time"

    return {
        "id": p.id,
        "ticker": p.ticker,
        "direction": p.direction,
        "status": p.status,
        "entry_price": p.entry_price,
        "stop_loss": p.stop_loss,
        "take_profit": p.take_profit,
        "quantity": p.quantity,
        "position_size_pct": p.position_size_pct,
        "projected_profit_pct": p.projected_profit_pct,
        "projected_loss_pct": p.projected_loss_pct,
        "risk_reward_ratio": p.risk_reward_ratio,
        "confidence": p.confidence,
        "timeframe": p.timeframe,
        "thesis": p.thesis,
        "signals": json.loads(p.signals_json) if p.signals_json else [],
        "indicators": json.loads(p.indicator_json) if p.indicator_json else {},
        "brain_score": p.brain_score,
        "ml_probability": p.ml_probability,
        "scan_score": p.scan_score,
        "proposed_at": proposed_at.isoformat() if proposed_at else None,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "executed_at": p.executed_at.isoformat() if p.executed_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "age_seconds": age_seconds,
        "expires_in_seconds": expires_in_seconds,
        "is_expired": is_expired,
        "expiry_reason": expiry_reason,
        "broker_order_id": p.broker_order_id,
        "trade_id": p.trade_id,
    }
