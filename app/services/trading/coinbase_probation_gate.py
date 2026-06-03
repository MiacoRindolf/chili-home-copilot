"""Venue-level realized-edge probation for Coinbase AutoTrader entries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ...models.trading import Trade


LOW_CONFIDENCE_EXIT_REASONS = {
    "",
    "coinbase_position_sync_gone",
    "broker_reconcile_no_exit_price",
    "broker_reconcile_position_gone",
    "position_sync_gone",
    "reconcile_position_gone",
}


@dataclass(frozen=True)
class CoinbaseProbationDecision:
    allowed: bool
    reason: str
    snapshot: dict[str, Any]


_CACHE: dict[tuple[Any, ...], tuple[datetime, CoinbaseProbationDecision]] = {}


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def reset_coinbase_probation_cache() -> None:
    _CACHE.clear()


def _setting(settings_obj: Any | None, name: str, default: Any) -> Any:
    if settings_obj is not None and hasattr(settings_obj, name):
        return getattr(settings_obj, name)
    try:
        from ...config import settings as live_settings

        return getattr(live_settings, name, default)
    except Exception:
        return default


def _is_low_confidence_exit(reason: str | None) -> bool:
    text = str(reason or "").strip().lower()
    return (
        text in LOW_CONFIDENCE_EXIT_REASONS
        or text.startswith("coinbase_position_sync_gone")
        or text.startswith("broker_reconcile_")
    )


def _crypto_autotrader_filter(cutoff: datetime) -> tuple[Any, ...]:
    scope = func.lower(func.coalesce(Trade.management_scope, ""))
    version = func.lower(func.coalesce(Trade.auto_trader_version, ""))
    ticker = func.lower(func.coalesce(Trade.ticker, ""))
    asset_kind = func.lower(func.coalesce(Trade.asset_kind, ""))
    return (
        func.lower(func.coalesce(Trade.broker_source, "")) == "coinbase",
        func.lower(func.coalesce(Trade.status, "")) == "closed",
        or_(asset_kind == "crypto", ticker.like("%-usd")),
        or_(
            Trade.exit_date >= cutoff,
            and_(Trade.exit_date.is_(None), Trade.entry_date >= cutoff),
        ),
        or_(
            scope == "auto_trader_v1",
            version == "auto_trader_v1",
            Trade.scan_pattern_id.isnot(None),
        ),
    )


def _payoff_ratio(wins: list[float], losses: list[float]) -> float | None:
    if not wins:
        return 0.0
    if not losses:
        return None
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss <= 0.0:
        return None
    return avg_win / avg_loss


def coinbase_live_probation_check(
    db: Session | None,
    *,
    settings_obj: Any | None = None,
    now: datetime | None = None,
) -> CoinbaseProbationDecision:
    """Block live Coinbase entries when recent venue evidence is negative.

    The gate is venue-wide by design. It is not trying to rank individual
    patterns; it prevents the AutoTrader from paying live Coinbase fees while
    recent Coinbase-managed exits show negative expectancy or opaque reconcile
    provenance. Blocked live entries still use the caller's paper-shadow path.
    """
    enabled = bool(
        _setting(settings_obj, "chili_coinbase_autotrader_probation_enabled", True)
    )
    now = now or _utcnow_naive()
    if not enabled:
        return CoinbaseProbationDecision(
            allowed=True,
            reason="coinbase_probation:disabled",
            snapshot={"enabled": False},
        )

    window_days = max(
        1,
        int(
            _setting(
                settings_obj,
                "chili_coinbase_autotrader_probation_window_days",
                30,
            )
            or 30
        ),
    )
    min_closed = max(
        0,
        int(
            _setting(
                settings_obj,
                "chili_coinbase_autotrader_probation_min_closed_trades",
                25,
            )
            or 0
        ),
    )
    max_low_conf_rate = max(
        0.0,
        min(
            1.0,
            float(
                _setting(
                    settings_obj,
                    "chili_coinbase_autotrader_probation_max_low_confidence_exit_rate",
                    0.35,
                )
                or 0.0
            ),
        ),
    )
    min_low_conf_exits = max(
        0,
        int(
            _setting(
                settings_obj,
                "chili_coinbase_autotrader_probation_min_low_confidence_exits",
                10,
            )
            or 0
        ),
    )
    min_avg_pnl = float(
        _setting(settings_obj, "chili_coinbase_autotrader_probation_min_avg_pnl_usd", 0.0)
        or 0.0
    )
    min_payoff_ratio = max(
        0.0,
        float(
            _setting(
                settings_obj,
                "chili_coinbase_autotrader_probation_min_payoff_ratio",
                1.0,
            )
            or 0.0
        ),
    )
    cache_seconds = max(
        0,
        int(
            _setting(
                settings_obj,
                "chili_coinbase_autotrader_probation_cache_seconds",
                60,
            )
            or 0
        ),
    )

    if db is None:
        return CoinbaseProbationDecision(
            allowed=True,
            reason="coinbase_probation:no_db_context",
            snapshot={
                "enabled": True,
                "window_days": window_days,
                "unavailable": "no_db_context",
            },
        )

    cache_key = (
        window_days,
        min_closed,
        max_low_conf_rate,
        min_low_conf_exits,
        min_avg_pnl,
        min_payoff_ratio,
    )
    cached = _CACHE.get(cache_key)
    if cached is not None and cache_seconds > 0:
        cached_at, cached_decision = cached
        if (now - cached_at).total_seconds() <= cache_seconds:
            return cached_decision

    cutoff = now - timedelta(days=window_days)
    try:
        rows = (
            db.query(Trade.pnl, Trade.exit_reason)
            .filter(*_crypto_autotrader_filter(cutoff))
            .all()
        )
    except Exception as exc:
        decision = CoinbaseProbationDecision(
            allowed=False,
            reason="coinbase_probation:realized_evidence_unavailable",
            snapshot={
                "enabled": True,
                "window_days": window_days,
                "error": type(exc).__name__,
            },
        )
        _CACHE[cache_key] = (now, decision)
        return decision

    pnls = [float(row.pnl or 0.0) for row in rows]
    wins = [pnl for pnl in pnls if pnl > 0.0]
    losses = [pnl for pnl in pnls if pnl < 0.0]
    closed_count = len(rows)
    pnl_sum = sum(pnls)
    avg_pnl = pnl_sum / closed_count if closed_count else 0.0
    low_conf_count = sum(1 for row in rows if _is_low_confidence_exit(row.exit_reason))
    low_conf_rate = (low_conf_count / closed_count) if closed_count else 0.0
    payoff_ratio = _payoff_ratio(wins, losses)
    snapshot = {
        "enabled": True,
        "window_days": window_days,
        "closed_count": closed_count,
        "min_closed_trades": min_closed,
        "pnl_usd": round(pnl_sum, 6),
        "avg_pnl_usd": round(avg_pnl, 6),
        "min_avg_pnl_usd": min_avg_pnl,
        "win_rate": round((len(wins) / closed_count) if closed_count else 0.0, 6),
        "wins": len(wins),
        "losses": len(losses),
        "payoff_ratio": round(payoff_ratio, 6) if payoff_ratio is not None else None,
        "min_payoff_ratio": min_payoff_ratio,
        "low_confidence_exit_count": low_conf_count,
        "low_confidence_exit_rate": round(low_conf_rate, 6),
        "max_low_confidence_exit_rate": max_low_conf_rate,
        "min_low_confidence_exits": min_low_conf_exits,
        "as_of": now.isoformat(),
    }

    reason = "coinbase_probation:passed"
    allowed = True
    if (
        low_conf_count >= min_low_conf_exits
        and low_conf_rate > max_low_conf_rate
    ):
        allowed = False
        reason = "coinbase_probation:low_confidence_exit_provenance"
    elif closed_count < min_closed:
        allowed = False
        reason = "coinbase_probation:insufficient_realized_venue_evidence"
    elif avg_pnl <= min_avg_pnl:
        allowed = False
        reason = "coinbase_probation:negative_realized_venue_ev"
    elif (
        min_payoff_ratio > 0.0
        and payoff_ratio is not None
        and payoff_ratio < min_payoff_ratio
    ):
        allowed = False
        reason = "coinbase_probation:weak_realized_payoff_ratio"

    decision = CoinbaseProbationDecision(
        allowed=allowed,
        reason=reason,
        snapshot=snapshot,
    )
    _CACHE[cache_key] = (now, decision)
    return decision
