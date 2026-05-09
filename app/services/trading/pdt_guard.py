"""PDT (Pattern Day Trader) entry gate.

Operator audit 2026-04-29 (third-pass) found 1,333 of 1,349 monitor exits
in 24h were rejected by Robinhood with reason "Sell may cause PDT
designation." The autotrader was opening positions it could not legally
close intraday, then re-attempting the close on every monitor pass and
burning ~55 broker calls/hour on guaranteed-fail exits.

This module is a **pre-trade entry gate**: before the autotrader places a
new buy order, it must consult :func:`can_open_intraday_round_trip` to
decide whether opening would create a PDT exposure the system cannot
exit. If the answer is "no", the entry is blocked at the funnel.

PDT rules (SEC):
- A "Pattern Day Trader" is any margin-account holder who executes 4 or
  more day-trades within any rolling 5-business-day window.
- A "day trade" = open AND close the same security on the same day.
- An account flagged as PDT with equity below $25,000 cannot day-trade.
- The threshold values ($25,000 and 4 trades) are SEC-defined, NOT
  CHILI policy or fallbacks: they are baked into the broker's reject
  rule. We mirror them.

Per the operator's "no hardcoded fallback values" principle (memory
``feedback_no_hardcoded_fallbacks``):

* ``account_equity`` is fetched live from the broker on every call
  (cached 60s). If the fetch fails, we propagate ``None`` and the
  gate **refuses** to open -- we do NOT assume "probably above $25K".
* ``day_trades_5d`` is computed from ``trading_trades`` via a real SQL
  count of intraday round-trips in the last 5 business days. If the
  query fails, propagate ``None`` and refuse.

The numeric constants ``25000`` and ``3`` (3 prior day-trades, so the
4th would trigger) are **not** statistical fallbacks; they are the SEC
rule. Documented as such inline.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# SEC PDT rule constants (not configurable; these come from regulation):
PDT_EQUITY_THRESHOLD_USD = 25_000.0
PDT_MAX_DAY_TRADES_5D = 3   # 4th would trigger PDT designation


# f-pdt-count-broker-confirmed-only (2026-05-08): exit reasons that mark a
# trade row as a reconciliation artifact rather than a broker-confirmed
# day-trade. Operator audit 2026-05-08 found 14 phantom rows with
# ``exit_reason='broker_reconcile_position_gone'`` AND ``broker_order_id
# IS NULL`` AND ``last_fill_at IS NULL`` -- chili synthesized closes
# when its reconciler couldn't find positions at the broker. R31/R32
# (commits 539e1c2 + 7af3d49, 2026-04-30) fixed this for the crypto book;
# the equity reconciler is the Phase B follow-up. In the meantime, the
# PDT count must exclude these rows so the operator's account doesn't
# self-lock from non-FINRA-day-trade artifacts.
_RECONCILE_ARTIFACT_EXIT_REASONS = frozenset({
    "broker_reconcile_position_gone",
    "forced_unwind_reconcile",
    # f-crypto-stale-trade-closer (2026-05-08, Phase E): two new exit
    # reasons emitted by the crypto-stale sweep (mirror of R31/R32 for
    # the crypto book). Both are reconciliation artifacts, NOT FINRA
    # day-trades: layer 1 cancels orders that never filled at the
    # broker; layer 2 closes positions the broker has reported at zero
    # quantity for N consecutive sweeps. Excluding them from the PDT
    # count keeps the operator's account from self-locking the same
    # way the equity-book wipeout did pre-R32.
    "entry_never_filled",
    "broker_position_reconciled_to_zero",
})


# Local cache for the broker portfolio fetch (60s TTL). The cache key is
# fixed because the broker call is global; if the autotrader runs for
# multiple users this would need to key by user_id.
_PORTFOLIO_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_PORTFOLIO_CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class PdtGateResult:
    """Outcome of consulting the PDT gate before opening a position."""

    allowed: bool
    reason: str
    account_equity_usd: float | None
    day_trades_5d: int | None
    snapshot: dict[str, Any]

    def to_audit_str(self) -> str:
        eq = (
            f"${self.account_equity_usd:,.0f}"
            if self.account_equity_usd is not None else "?"
        )
        dt = str(self.day_trades_5d) if self.day_trades_5d is not None else "?"
        return f"pdt_gate[{self.reason}]:eq={eq}:dt5d={dt}/{PDT_MAX_DAY_TRADES_5D}"


def _fetch_account_equity_usd() -> float | None:
    """Live broker fetch of account equity. Cached 60s.

    Returns ``None`` on any failure -- the caller MUST treat None as
    "unknown" and refuse the entry (do NOT assume above-threshold).
    """
    now = time.time()
    cached = _PORTFOLIO_CACHE.get("value")
    cached_ts = float(_PORTFOLIO_CACHE.get("ts", 0.0))
    if cached is not None and (now - cached_ts) < _PORTFOLIO_CACHE_TTL_S:
        return cached
    try:
        from ...services.broker_service import get_portfolio  # type: ignore
        portfolio = get_portfolio() or {}
        eq_raw = portfolio.get("equity")
        if eq_raw is None:
            _PORTFOLIO_CACHE["ts"] = now
            _PORTFOLIO_CACHE["value"] = None
            return None
        eq = float(eq_raw)
        _PORTFOLIO_CACHE["ts"] = now
        _PORTFOLIO_CACHE["value"] = eq
        return eq
    except Exception:
        logger.warning(
            "[pdt_guard] account-equity fetch failed; will refuse entry",
            exc_info=True,
        )
        _PORTFOLIO_CACHE["ts"] = now
        _PORTFOLIO_CACHE["value"] = None
        return None


def _count_day_trades_5d(db: Session, *, user_id: int | None = None) -> int | None:
    """Count round-trip day trades (opened AND closed same calendar day)
    in the trailing 5 business days from ``trading_trades``.

    R35 (2026-04-30): explicitly EXCLUDE crypto rows. SEC PDT regulation
    applies only to securities accounts; crypto is a 24/7 cash market and
    same-day crypto round-trips do not count as "day trades" toward the
    4-in-5-business-days threshold. Without this filter the post-R34
    crypto cadence would have inflated the count past the 3-trip ceiling
    and refused EVERY entry, including legitimate equity entries when
    the equity sub-ledger was below the PDT cap.

    Returns ``None`` on query failure; the caller MUST treat None as
    "unknown" and refuse the entry.
    """
    try:
        # Approximate "5 business days" with a 9-calendar-day lookback so
        # we cover all weekends and most short market closures. The exact
        # calendar conversion is the broker's responsibility for the
        # canonical PDT determination; this is a conservative pre-check
        # that errs on the side of refusing.
        cutoff = datetime.utcnow() - timedelta(days=9)
        # f-pdt-count-broker-confirmed-only (2026-05-08): three new
        # exclusions so the count covers ONLY broker-confirmed day-trades.
        #   * ``broker_order_id IS NOT NULL``: the exit was a real
        #     broker-issued order (not a chili-synthesized close).
        #   * ``last_fill_at IS NOT NULL``: the broker actually reported
        #     a fill. ``filled_at`` is the older entry-side timestamp and
        #     can be set on non-fill paths -- ``last_fill_at`` is the
        #     authoritative broker-truth column.
        #   * ``exit_reason NOT IN`` reconcile-artifact set: rows whose
        #     close was driven by the reconciler not finding the position
        #     are R31/R32-style wipeouts, not FINRA day-trades.
        sql = """
            SELECT COUNT(*) AS n
            FROM trading_trades
            WHERE status = 'closed'
              AND entry_date IS NOT NULL
              AND exit_date IS NOT NULL
              AND DATE(entry_date) = DATE(exit_date)
              AND exit_date > :cutoff
              AND ticker NOT LIKE '%-USD'
              AND broker_order_id IS NOT NULL
              AND last_fill_at IS NOT NULL
              AND COALESCE(exit_reason, '') NOT IN :reconcile_reasons
        """
        params: dict[str, Any] = {
            "cutoff": cutoff,
            "reconcile_reasons": tuple(_RECONCILE_ARTIFACT_EXIT_REASONS),
        }
        if user_id is not None:
            sql += " AND user_id = :uid"
            params["uid"] = int(user_id)
        # ``expanding=True`` lets sqlalchemy bind the IN-list tuple safely.
        stmt = text(sql).bindparams(
            bindparam("reconcile_reasons", expanding=True),
        )
        row = db.execute(stmt, params).fetchone()
        if row is None:
            return None
        return int(row.n or 0)
    except Exception:
        logger.warning(
            "[pdt_guard] day-trade count query failed; will refuse entry",
            exc_info=True,
        )
        return None


def can_open_intraday_round_trip(
    db: Session,
    *,
    user_id: int | None = None,
    ticker: str | None = None,
) -> PdtGateResult:
    """Decide whether a new entry can be safely opened given PDT exposure.

    Returns a :class:`PdtGateResult` with ``allowed=True`` only when:
      0. R35: ticker is crypto (``...-USD``) -- PDT is securities-only,
         crypto is a 24/7 cash market and exempt by regulation.
      1. Account equity is known AND >= $25,000 (PDT does not apply), OR
      2. Account equity is known AND < $25,000 AND day_trades_5d < 3.

    Any unknown input (equity or day-trade count) → ``allowed=False``,
    ``reason='unknown_state_refuse'``. The operator's no-hardcoded-fallback
    rule explicitly forbids assuming "probably above threshold" or
    "probably zero day trades".
    """
    # R35 (2026-04-30): crypto bypass. SEC PDT regulation governs margin
    # securities trading; crypto sits outside the rule. Without this
    # short-circuit, post-R34 the crypto entry funnel was 100% blocked by
    # 'pdt_limit_reached:43>=3' since the count included crypto round-trips.
    if ticker and ticker.upper().endswith("-USD"):
        return PdtGateResult(
            allowed=True,
            reason="crypto_not_pdt_eligible",
            account_equity_usd=None,
            day_trades_5d=None,
            snapshot={
                "ticker": ticker,
                "asset_class": "crypto",
                "pdt_equity_threshold_usd": PDT_EQUITY_THRESHOLD_USD,
                "pdt_max_day_trades_5d": PDT_MAX_DAY_TRADES_5D,
            },
        )

    equity = _fetch_account_equity_usd()
    day_trades = _count_day_trades_5d(db, user_id=user_id)

    snapshot = {
        "account_equity_usd": equity,
        "day_trades_5d": day_trades,
        "pdt_equity_threshold_usd": PDT_EQUITY_THRESHOLD_USD,
        "pdt_max_day_trades_5d": PDT_MAX_DAY_TRADES_5D,
    }

    if equity is None or day_trades is None:
        return PdtGateResult(
            allowed=False,
            reason="unknown_state_refuse",
            account_equity_usd=equity,
            day_trades_5d=day_trades,
            snapshot=snapshot,
        )

    if equity >= PDT_EQUITY_THRESHOLD_USD:
        return PdtGateResult(
            allowed=True,
            reason="above_pdt_threshold",
            account_equity_usd=equity,
            day_trades_5d=day_trades,
            snapshot=snapshot,
        )

    if day_trades < PDT_MAX_DAY_TRADES_5D:
        return PdtGateResult(
            allowed=True,
            reason="under_day_trade_limit",
            account_equity_usd=equity,
            day_trades_5d=day_trades,
            snapshot=snapshot,
        )

    return PdtGateResult(
        allowed=False,
        reason=f"pdt_limit_reached:{day_trades}>={PDT_MAX_DAY_TRADES_5D}",
        account_equity_usd=equity,
        day_trades_5d=day_trades,
        snapshot=snapshot,
    )
