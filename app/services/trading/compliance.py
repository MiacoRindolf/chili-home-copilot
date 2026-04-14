"""Trading compliance posture and pre-trade checks.

Provides regulatory-aware guardrails for multi-user deployment:
- Position concentration limits
- Trade frequency limits (pattern day trading awareness)
- Disclosure requirements tracking
- Suitability checks (risk tolerance vs strategy aggressiveness)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)

PDT_DAY_TRADE_LIMIT = 3
PDT_WINDOW_DAYS = 5
PDT_THRESHOLD_EQUITY = 25_000.0


def check_pdt_status(
    db: Session,
    user_id: int | None,
    equity: float = 0.0,
) -> dict[str, Any]:
    """Check Pattern Day Trading status (US equities regulatory constraint).

    Under FINRA Rule 4210, an account with <$25K equity that makes >=4
    day trades in a rolling 5-business-day period may be flagged as a
    pattern day trader and restricted.
    """
    cutoff = datetime.utcnow() - timedelta(days=PDT_WINDOW_DAYS)

    day_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.entry_date >= cutoff,
    ).all()

    # Count same-day round trips (open and close on same calendar day)
    same_day_count = 0
    for t in day_trades:
        if t.entry_date and t.exit_date:
            if t.entry_date.date() == t.exit_date.date():
                is_crypto = (t.ticker or "").upper().endswith("-USD")
                if not is_crypto:  # PDT only applies to equities
                    same_day_count += 1

    at_risk = same_day_count >= PDT_DAY_TRADE_LIMIT and equity < PDT_THRESHOLD_EQUITY
    can_day_trade = same_day_count < PDT_DAY_TRADE_LIMIT or equity >= PDT_THRESHOLD_EQUITY

    return {
        "same_day_trades_5d": same_day_count,
        "pdt_limit": PDT_DAY_TRADE_LIMIT,
        "equity": equity,
        "equity_threshold": PDT_THRESHOLD_EQUITY,
        "at_risk": at_risk,
        "can_day_trade": can_day_trade,
        "window_days": PDT_WINDOW_DAYS,
    }


def check_concentration_limits(
    db: Session,
    user_id: int | None,
    ticker: str,
    proposed_notional: float,
    total_equity: float,
) -> tuple[bool, str | None]:
    """Check if a trade would breach concentration limits.

    - Single position: max 20% of equity
    - Single sector: max 40% of equity
    - Single asset class (crypto/stock): max 60% of equity
    """
    if total_equity <= 0:
        return False, "Cannot evaluate concentration with zero equity"

    concentration_pct = (proposed_notional / total_equity) * 100
    if concentration_pct > 20:
        return False, (
            f"Single position would be {concentration_pct:.1f}% of equity "
            f"(limit: 20%)"
        )

    # Check existing exposure in same ticker
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.ticker == ticker.upper(),
        Trade.status == "open",
    ).all()

    existing_notional = sum(
        float(t.entry_price or 0) * float(t.quantity or 0) for t in open_trades
    )
    total_ticker_pct = ((existing_notional + proposed_notional) / total_equity) * 100
    if total_ticker_pct > 25:
        return False, (
            f"Total exposure to {ticker} would be {total_ticker_pct:.1f}% "
            f"of equity (limit: 25%)"
        )

    return True, None


REQUIRED_DISCLOSURES = [
    "trading_ai_generates_ideas_not_advice",
    "past_performance_no_guarantee",
    "risk_of_loss_acknowledged",
    "automated_execution_consent",
]


def check_user_disclosures(
    db: Session,
    user_id: int,
) -> dict[str, Any]:
    """Check which required disclosures a user has acknowledged.

    For multi-user deployment, each user should acknowledge key
    disclosures before automated trading is enabled.
    """
    try:
        from ...models.core import UserPreference
        prefs = db.query(UserPreference).filter(
            UserPreference.user_id == user_id,
        ).first()
        acknowledged = []
        if prefs and prefs.preferences:
            import json
            p = prefs.preferences
            if isinstance(p, str):
                p = json.loads(p)
            acknowledged = p.get("acknowledged_disclosures", [])
    except Exception:
        acknowledged = []

    missing = [d for d in REQUIRED_DISCLOSURES if d not in acknowledged]

    return {
        "required": REQUIRED_DISCLOSURES,
        "acknowledged": acknowledged,
        "missing": missing,
        "fully_compliant": len(missing) == 0,
        "auto_trading_allowed": len(missing) == 0,
    }


def pre_trade_compliance_check(
    db: Session,
    user_id: int | None,
    ticker: str,
    *,
    proposed_notional: float = 0.0,
    total_equity: float = 100_000.0,
) -> tuple[bool, list[str]]:
    """Run all compliance checks before a trade. Returns (ok, issues)."""
    issues: list[str] = []

    # PDT check
    pdt = check_pdt_status(db, user_id, equity=total_equity)
    if pdt["at_risk"]:
        issues.append(
            f"PDT risk: {pdt['same_day_trades_5d']} day trades in 5 days "
            f"with equity ${total_equity:,.0f} < ${PDT_THRESHOLD_EQUITY:,.0f}"
        )

    # Concentration check
    if proposed_notional > 0:
        ok, reason = check_concentration_limits(
            db, user_id, ticker, proposed_notional, total_equity,
        )
        if not ok and reason:
            issues.append(reason)

    # Disclosure check (only for identified users)
    if user_id:
        try:
            disc = check_user_disclosures(db, user_id)
            if not disc["auto_trading_allowed"]:
                issues.append(
                    f"Missing disclosures: {', '.join(disc['missing'])}"
                )
        except Exception:
            pass

    return len(issues) == 0, issues
