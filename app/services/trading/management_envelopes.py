"""Read-only Phase 5B helpers for decision/envelope/position reporting.

Phase 5A created the decision bridge. Phase 5B gave application code a semantic
surface for "management envelopes"; Phase 5H made that surface the physical
base table. These helpers intentionally read the semantic envelope surface and
do not mutate live trading state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trade_relation_symbols import (
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)


@dataclass(frozen=True)
class Phase5BParitySummary:
    valid_trades_missing_decision: int
    open_broker_trades_missing_position: int
    orphan_decisions: int
    decisions_without_envelope: int
    broker_envelopes_missing_position: int
    open_position_envelope_mismatches: int

    @property
    def ok(self) -> bool:
        return all(
            value == 0
            for value in (
                self.valid_trades_missing_decision,
                self.open_broker_trades_missing_position,
                self.orphan_decisions,
                self.decisions_without_envelope,
                self.broker_envelopes_missing_position,
                self.open_position_envelope_mismatches,
            )
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "valid_trades_missing_decision": self.valid_trades_missing_decision,
            "open_broker_trades_missing_position": self.open_broker_trades_missing_position,
            "orphan_decisions": self.orphan_decisions,
            "decisions_without_envelope": self.decisions_without_envelope,
            "broker_envelopes_missing_position": self.broker_envelopes_missing_position,
            "open_position_envelope_mismatches": self.open_position_envelope_mismatches,
        }


@dataclass(frozen=True)
class ClosedEnvelopePerformanceSummary:
    trades: int
    wins: int
    pnl: float

    @property
    def win_rate_pct(self) -> float:
        if self.trades <= 0:
            return 0.0
        return round(self.wins / self.trades * 100.0, 1)

    def to_payload(self) -> dict[str, Any]:
        return {
            "trades": int(self.trades),
            "pnl": round(float(self.pnl or 0.0), 2),
            "win_rate": self.win_rate_pct,
        }


def _rows(
    db: Session,
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    try:
        return [dict(r) for r in result.mappings().all()]
    except AttributeError:
        return [dict(r._mapping) for r in result.fetchall()]


def count_open_autotrader_envelopes_by_lane(
    db: Session,
    *,
    user_id: int | None,
    autotrader_version: str = "v1",
) -> dict[str, int]:
    """Count active live AutoTrader management envelopes by asset lane."""
    out = {"equity": 0, "crypto": 0, "options": 0}
    params: dict[str, Any] = {"version": autotrader_version}
    sql = f"""
        SELECT COALESCE(LOWER(NULLIF(t.asset_kind, '')),
                      LOWER(NULLIF(a.asset_type, '')), 'stock') AS at,
               COUNT(*) AS n
          FROM {MANAGEMENT_ENVELOPES_RELATION} t
          LEFT JOIN trading_breakout_alerts a ON a.id = t.related_alert_id
         WHERE t.auto_trader_version = :version
           AND t.status IN ('open', 'working')
    """
    if user_id is not None:
        sql += " AND t.user_id = :uid"
        params["uid"] = user_id
    sql += " GROUP BY at"

    for at, n in db.execute(text(sql), params).fetchall() or []:
        lane = (at or "stock").lower()
        if lane == "crypto":
            out["crypto"] += int(n)
        elif lane in ("option", "options"):
            out["options"] += int(n)
        else:
            out["equity"] += int(n)
    return out


def load_recent_management_envelope_tickers_for_user(
    db: Session,
    *,
    user_id: int,
    limit: int = 200,
) -> list[str]:
    """Return recent envelope tickers for read-only learning/reporting consumers."""
    rows = _rows(
        db,
        f"""
        SELECT ticker
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id = :uid
           AND ticker IS NOT NULL
           AND ticker <> ''
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT :limit
        """,
        {"uid": int(user_id), "limit": max(1, min(int(limit or 200), 1000))},
    )
    return [str(row["ticker"]).upper() for row in rows if row.get("ticker")]


def tca_summary_by_ticker_from_management_envelopes(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
) -> dict[str, Any]:
    """Aggregate TCA slippage from the semantic management-envelope surface."""
    if user_id is None:
        return {
            "ok": True,
            "window_days": days,
            "overall_fills": 0,
            "overall_avg_entry_slippage_bps": None,
            "by_ticker": [],
            "exit_overall_closes": 0,
            "exit_overall_avg_slippage_bps": None,
            "exit_by_ticker": [],
        }

    window_days = max(1, int(days or 90))
    lim = max(1, min(int(limit or 50), 500))
    params = {"uid": int(user_id), "days": window_days, "limit": lim}

    entry_rows = _rows(db, f"""
        SELECT
            ticker,
            COUNT(id)::bigint AS fills,
            AVG(tca_entry_slippage_bps)::double precision AS avg_entry_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE tca_entry_slippage_bps IS NOT NULL
           AND filled_at IS NOT NULL
           AND filled_at >= NOW() - (:days * INTERVAL '1 day')
           AND user_id = :uid
         GROUP BY ticker
         ORDER BY COUNT(id) DESC
         LIMIT :limit
    """, params)
    by_ticker = [
        {
            "ticker": row.get("ticker"),
            "fills": int(row.get("fills") or 0),
            "avg_entry_slippage_bps": (
                round(float(row["avg_entry_slippage_bps"]), 2)
                if row.get("avg_entry_slippage_bps") is not None
                else None
            ),
        }
        for row in entry_rows
    ]

    entry_overall = _rows(db, f"""
        SELECT
            COUNT(id)::bigint AS fills,
            AVG(tca_entry_slippage_bps)::double precision AS avg_entry_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE tca_entry_slippage_bps IS NOT NULL
           AND filled_at IS NOT NULL
           AND filled_at >= NOW() - (:days * INTERVAL '1 day')
           AND user_id = :uid
    """, params)
    entry_total = entry_overall[0] if entry_overall else {}

    exit_rows = _rows(db, f"""
        SELECT
            ticker,
            COUNT(id)::bigint AS closes,
            AVG(tca_exit_slippage_bps)::double precision AS avg_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE tca_exit_slippage_bps IS NOT NULL
           AND status = 'closed'
           AND exit_date IS NOT NULL
           AND exit_date >= NOW() - (:days * INTERVAL '1 day')
           AND user_id = :uid
         GROUP BY ticker
         ORDER BY COUNT(id) DESC
         LIMIT :limit
    """, params)
    exit_by_ticker = [
        {
            "ticker": row.get("ticker"),
            "closes": int(row.get("closes") or 0),
            "avg_exit_slippage_bps": (
                round(float(row["avg_exit_slippage_bps"]), 2)
                if row.get("avg_exit_slippage_bps") is not None
                else None
            ),
        }
        for row in exit_rows
    ]

    exit_overall = _rows(db, f"""
        SELECT
            COUNT(id)::bigint AS closes,
            AVG(tca_exit_slippage_bps)::double precision AS avg_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE tca_exit_slippage_bps IS NOT NULL
           AND status = 'closed'
           AND exit_date IS NOT NULL
           AND exit_date >= NOW() - (:days * INTERVAL '1 day')
           AND user_id = :uid
    """, params)
    exit_total = exit_overall[0] if exit_overall else {}

    return {
        "ok": True,
        "window_days": window_days,
        "overall_fills": int(entry_total.get("fills") or 0),
        "overall_avg_entry_slippage_bps": (
            round(float(entry_total["avg_entry_slippage_bps"]), 2)
            if entry_total.get("avg_entry_slippage_bps") is not None
            else None
        ),
        "by_ticker": by_ticker,
        "exit_overall_closes": int(exit_total.get("closes") or 0),
        "exit_overall_avg_slippage_bps": (
            round(float(exit_total["avg_exit_slippage_bps"]), 2)
            if exit_total.get("avg_exit_slippage_bps") is not None
            else None
        ),
        "exit_by_ticker": exit_by_ticker,
    }


def aggregate_management_envelope_execution_for_pattern(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int,
    window_days: int,
) -> dict[str, Any]:
    """Legacy v1 execution-robustness rollups from management envelopes."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    rows = _rows(db, f"""
        SELECT
            filled_at,
            avg_fill_price,
            broker_status,
            status,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps,
            broker_source
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE scan_pattern_id = :scan_pattern_id
           AND user_id = :user_id
           AND entry_date >= :since
    """, {
        "scan_pattern_id": int(scan_pattern_id),
        "user_id": int(user_id),
        "since": since,
    })
    n_orders = len(rows)
    n_filled = sum(
        1
        for row in rows
        if row.get("filled_at") is not None or row.get("avg_fill_price") is not None
    )
    n_partial = sum(
        1
        for row in rows
        if row.get("broker_status")
        and "partial" in str(row.get("broker_status") or "").lower()
    )
    n_miss = sum(
        1
        for row in rows
        if str(row.get("status") or "").lower() in ("cancelled", "rejected")
        and row.get("filled_at") is None
        and row.get("avg_fill_price") is None
    )
    slips: list[float] = []
    for row in rows:
        for col in (row.get("tca_entry_slippage_bps"), row.get("tca_exit_slippage_bps")):
            if col is not None:
                try:
                    slips.append(abs(float(col)))
                except (TypeError, ValueError):
                    pass
    brokers = [
        ((str(row.get("broker_source") or "manual") or "manual").strip().lower())
        for row in rows
    ]
    broker_mode = max(set(brokers), key=brokers.count) if brokers else None

    return {
        "n_orders": n_orders,
        "n_filled": n_filled,
        "n_partial": n_partial,
        "n_miss": n_miss,
        "slippages_abs_bps": slips,
        "dominant_broker_source": broker_mode,
    }


def load_execution_cost_estimate_envelope_rows(
    db: Session,
    *,
    ticker: str,
    sides: list[str],
    since: datetime,
) -> list[SimpleNamespace]:
    """Load closed management envelopes used by the execution-cost estimator."""
    side_list = [str(side).strip().lower() for side in sides if str(side).strip()]
    if not side_list:
        return []
    side_params = {f"side_{idx}": side for idx, side in enumerate(side_list)}
    side_placeholders = ", ".join(f":side_{idx}" for idx in range(len(side_list)))
    rows = _rows(
        db,
        f"""
        SELECT
            direction,
            entry_price,
            quantity,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE ticker = :ticker
           AND status = 'closed'
           AND entry_date >= :since
           AND LOWER(COALESCE(direction, 'long')) IN ({side_placeholders})
        """,
        {
            "ticker": str(ticker),
            "since": since,
            **side_params,
        },
    )
    return [
        SimpleNamespace(
            direction=row.get("direction"),
            entry_price=row.get("entry_price"),
            quantity=row.get("quantity"),
            tca_entry_slippage_bps=row.get("tca_entry_slippage_bps"),
            tca_exit_slippage_bps=row.get("tca_exit_slippage_bps"),
        )
        for row in rows
    ]


def load_closed_management_envelope_tickers_since(
    db: Session,
    *,
    since: datetime,
) -> list[str]:
    """Return distinct tickers from recently closed management envelopes."""
    rows = _rows(
        db,
        f"""
        SELECT DISTINCT ticker
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE status = 'closed'
           AND entry_date >= :since
           AND ticker IS NOT NULL
           AND ticker <> ''
         ORDER BY ticker
        """,
        {"since": since},
    )
    return [str(row["ticker"]).strip() for row in rows if row.get("ticker")]


def load_edge_reliability_live_envelope_rows(
    db: Session,
    *,
    pattern_id: int,
    since: datetime,
    closed_only: bool = True,
    limit: int | None = None,
) -> list[SimpleNamespace]:
    """Load management-envelope rows used by edge-reliability diagnostics."""
    params: dict[str, Any] = {
        "pattern_id": int(pattern_id),
        "since": since,
    }
    status_clause = "AND status = 'closed'" if closed_only else ""
    limit_clause = ""
    if limit is not None:
        params["limit"] = max(1, int(limit))
        limit_clause = "LIMIT :limit"

    rows = _rows(
        db,
        f"""
        SELECT
            id,
            ticker,
            direction,
            entry_price,
            avg_fill_price,
            exit_price,
            quantity,
            filled_quantity,
            pnl,
            asset_kind,
            tags,
            indicator_snapshot,
            related_alert_id,
            entry_date,
            exit_date
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE scan_pattern_id = :pattern_id
           {status_clause}
           AND (
                entry_date IS NULL
             OR entry_date >= :since
             OR exit_date >= :since
           )
         ORDER BY id DESC
         {limit_clause}
        """,
        params,
    )
    return [
        SimpleNamespace(
            id=row.get("id"),
            ticker=row.get("ticker"),
            direction=row.get("direction"),
            entry_price=row.get("entry_price"),
            avg_fill_price=row.get("avg_fill_price"),
            exit_price=row.get("exit_price"),
            quantity=row.get("quantity"),
            filled_quantity=row.get("filled_quantity"),
            pnl=row.get("pnl"),
            asset_kind=row.get("asset_kind"),
            tags=row.get("tags"),
            indicator_snapshot=row.get("indicator_snapshot"),
            related_alert_id=row.get("related_alert_id"),
            entry_date=row.get("entry_date"),
            exit_date=row.get("exit_date"),
        )
        for row in rows
    ]


def fetch_synergy_retry_envelope_candidates(
    db: Session,
    *,
    uid: int,
    lookback_minutes: int,
    source_reason: str,
    autotrader_version: str,
    query_limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent retry candidates whose open envelope has a different pattern."""
    return _rows(db, f"""
        WITH latest AS (
            SELECT DISTINCT ON (ar.breakout_alert_id)
                   ar.breakout_alert_id,
                   ar.id AS source_run_id,
                   ar.reason,
                   ar.created_at
              FROM trading_autotrader_runs ar
              JOIN trading_breakout_alerts ba
                ON ba.id = ar.breakout_alert_id
             WHERE ar.created_at >= NOW() - (:lookback_minutes * INTERVAL '1 minute')
               AND ba.alert_tier = 'pattern_imminent'
               AND (ba.user_id = :uid OR ba.user_id IS NULL)
             ORDER BY ar.breakout_alert_id, ar.created_at DESC, ar.id DESC
        ),
        eligible AS (
            SELECT ba.id AS alert_id,
                   latest.source_run_id,
                   latest.created_at,
                   COUNT(*) OVER () AS retry_pool
              FROM latest
              JOIN trading_breakout_alerts ba
                ON ba.id = latest.breakout_alert_id
              JOIN LATERAL (
                    SELECT t.id, t.scan_pattern_id, t.entry_date
                      FROM {MANAGEMENT_ENVELOPES_RELATION} t
                     WHERE UPPER(t.ticker) = UPPER(ba.ticker)
                       AND t.status = 'open'
                       AND t.auto_trader_version = :autotrader_version
                       AND (t.user_id = :uid OR t.user_id IS NULL)
                     ORDER BY t.entry_date DESC NULLS LAST, t.id DESC
                     LIMIT 1
              ) open_trade ON TRUE
             WHERE latest.reason = :source_reason
               AND COALESCE(open_trade.scan_pattern_id, 0)
                   <> COALESCE(ba.scan_pattern_id, 0)
             ORDER BY latest.created_at DESC, ba.id DESC
             LIMIT :query_limit
        )
        SELECT alert_id, source_run_id, retry_pool
          FROM eligible
    """, {
        "uid": int(uid),
        "lookback_minutes": int(lookback_minutes),
        "source_reason": source_reason,
        "autotrader_version": autotrader_version,
        "query_limit": int(query_limit),
    })


def count_probation_envelopes_since(
    db: Session,
    *,
    uid: int | None,
    autotrader_version: str,
    start_utc: Any,
    entry_execution_key: str,
    probation_flag_key: str,
    probation_true_flag: str,
    probation_false_flag: str,
    pattern_id: int | None = None,
) -> int:
    """Count probation-tagged management envelopes since a UTC cutoff."""
    pattern_clause = ""
    params: dict[str, Any] = {
        "uid": uid,
        "version": autotrader_version,
        "start_utc": start_utc,
        "flag": probation_true_flag,
        "entry_execution_key": entry_execution_key,
        "probation_flag_key": probation_flag_key,
        "false_flag": probation_false_flag,
    }
    if pattern_id is not None:
        pattern_clause = "AND scan_pattern_id = :pattern_id"
        params["pattern_id"] = int(pattern_id)
    row = db.execute(text(f"""
        SELECT COUNT(*) AS n
        FROM {MANAGEMENT_ENVELOPES_RELATION}
        WHERE user_id IS NOT DISTINCT FROM :uid
          AND COALESCE(auto_trader_version, '') = :version
          AND entry_date >= :start_utc
          AND COALESCE(
              jsonb_extract_path_text(
                  indicator_snapshot,
                  :entry_execution_key,
                  :probation_flag_key
              ),
              :false_flag
          ) = :flag
          {pattern_clause}
    """), params).scalar()
    return int(row or 0)


def summarize_closed_envelope_performance(
    db: Session,
    *,
    user_id: int | None,
    since: datetime,
) -> ClosedEnvelopePerformanceSummary:
    """Summarize closed management-envelope PnL since a cutoff."""
    row = db.execute(text(f"""
        SELECT
            COUNT(*)::bigint AS trades,
            SUM(CASE WHEN COALESCE(pnl, 0) > 0 THEN 1 ELSE 0 END)::bigint AS wins,
            SUM(COALESCE(pnl, 0))::double precision AS pnl
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'closed'
           AND exit_date >= :since
    """), {"uid": user_id, "since": since}).mappings().first()
    if not row:
        return ClosedEnvelopePerformanceSummary(trades=0, wins=0, pnl=0.0)
    return ClosedEnvelopePerformanceSummary(
        trades=int(row["trades"] or 0),
        wins=int(row["wins"] or 0),
        pnl=float(row["pnl"] or 0.0),
    )


def load_closed_envelope_execution_rows(
    db: Session,
    *,
    user_id: int | None,
    since: datetime,
) -> list[dict[str, Any]]:
    """Load closed management-envelope fields needed for execution-quality reports."""
    return _rows(db, f"""
        SELECT
            id,
            ticker,
            entry_price,
            indicator_snapshot,
            tags,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'closed'
           AND entry_date >= :since
    """, {"uid": user_id, "since": since})


def load_closed_pattern_envelope_rows(
    db: Session,
    *,
    pattern_id: int,
    user_id: int | None,
    since: datetime,
) -> list[dict[str, Any]]:
    """Load closed management envelopes for pattern-performance attribution."""
    user_clause = ""
    params: dict[str, Any] = {
        "pattern_id": int(pattern_id),
        "since": since,
    }
    if user_id is not None:
        user_clause = "AND user_id = :uid"
        params["uid"] = int(user_id)
    return _rows(db, f"""
        SELECT
            id,
            ticker,
            direction,
            entry_price,
            exit_price,
            quantity,
            pnl,
            asset_kind,
            tags,
            indicator_snapshot,
            entry_date,
            exit_date,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE scan_pattern_id = :pattern_id
           AND status = 'closed'
           AND exit_date >= :since
           {user_clause}
         ORDER BY exit_date ASC
    """, params)


def load_closed_review_envelope_rows(
    db: Session,
    *,
    user_id: int,
    since: datetime,
) -> list[dict[str, Any]]:
    """Load closed management envelopes needed by post-trade review reports."""
    return _rows(db, f"""
        SELECT
            id,
            ticker,
            scan_pattern_id,
            pnl,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id = :uid
           AND status = 'closed'
           AND exit_date >= :since
         ORDER BY exit_date ASC
    """, {"uid": int(user_id), "since": since})


def load_recent_ticker_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    ticker: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Load recent management envelopes for ticker-specific context reports."""
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return []
    return _rows(db, f"""
        SELECT
            id,
            user_id,
            ticker,
            direction,
            quantity,
            entry_price,
            exit_price,
            entry_date,
            exit_date,
            pnl,
            status,
            related_alert_id,
            stop_loss,
            take_profit,
            broker_source,
            asset_kind,
            tags,
            indicator_snapshot
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND UPPER(ticker) = :ticker
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT :limit
    """, {"uid": user_id, "ticker": symbol, "limit": max(1, int(limit))})


def load_pattern_tagged_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Load recent pattern-tagged management envelopes for evidence reports."""
    return _rows(db, f"""
        SELECT
            id,
            ticker,
            direction,
            entry_price,
            exit_price,
            pnl,
            status,
            entry_date,
            exit_date,
            pattern_tags
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND pattern_tags IS NOT NULL
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT :limit
    """, {"uid": user_id, "limit": max(1, int(limit))})


def load_audit_export_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Load management-envelope rows for the public audit export trade section."""
    return _rows(db, f"""
        SELECT
            id,
            ticker,
            direction,
            quantity,
            entry_price,
            exit_price,
            entry_date,
            exit_date,
            pnl,
            status,
            broker_source,
            tca_entry_slippage_bps,
            tca_exit_slippage_bps,
            scan_pattern_id,
            pattern_tags
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND entry_date >= :start
           AND entry_date <= :end
         ORDER BY entry_date ASC NULLS LAST
    """, {"uid": user_id, "start": start, "end": end})


def load_trades_api_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Load base management-envelope fields used by the public /trades list."""
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": max(1, min(int(limit or 50), 500)),
    }
    status_clause = ""
    if status:
        status_clause = "AND status = :status"
        params["status"] = str(status)

    return _rows(db, f"""
        SELECT
            id,
            ticker,
            direction,
            entry_price,
            exit_price,
            quantity,
            entry_date,
            exit_date,
            status,
            pnl,
            tags,
            notes,
            broker_source,
            broker_status,
            broker_order_id,
            filled_at,
            avg_fill_price,
            tca_reference_entry_price,
            tca_entry_slippage_bps,
            tca_reference_exit_price,
            tca_exit_slippage_bps,
            strategy_proposal_id,
            scan_pattern_id,
            position_id
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           {status_clause}
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT :limit
    """, params)


def load_trades_api_envelope_objects(
    db: Session,
    *,
    user_id: int | None,
    status: str | None = None,
    limit: int = 50,
) -> list[Any]:
    """Load /trades rows as read-only envelope-shaped runtime objects."""
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": max(1, min(int(limit or 50), 500)),
    }
    status_clause = ""
    if status:
        status_clause = "AND status = :status"
        params["status"] = str(status)

    rows = _rows(db, f"""
        SELECT *
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           {status_clause}
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT :limit
    """, params)
    return [_envelope_runtime_object(row) for row in rows]


def load_monitor_decision_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    action: str | None,
    limit: int,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Load monitor-decision rows joined to management-envelope display fields."""
    action_value = (action or "").strip() or None
    params = {
        "uid": user_id,
        "action": action_value,
        "limit": max(1, int(limit)),
        "offset": max(0, int(offset)),
    }
    scoped_sql = f"""
        WITH scoped AS (
            SELECT
                d.id,
                d.trade_id,
                t.ticker,
                t.direction,
                d.breakout_alert_id,
                d.scan_pattern_id,
                d.health_score,
                d.health_delta,
                d.conditions_snapshot,
                d.action,
                d.old_stop,
                d.new_stop,
                d.old_target,
                d.new_target,
                d.llm_confidence,
                d.llm_reasoning,
                d.mechanical_action,
                d.mechanical_stop,
                d.mechanical_target,
                d.decision_source,
                d.price_at_decision,
                d.price_after_1h,
                d.price_after_4h,
                d.was_beneficial,
                d.created_at
              FROM trading_pattern_monitor_decisions d
              JOIN {MANAGEMENT_ENVELOPES_RELATION} t ON t.id = d.trade_id
             WHERE t.user_id IS NOT DISTINCT FROM :uid
               AND (:action IS NULL OR d.action = :action)
        )
    """
    rows = _rows(db, scoped_sql + """
        SELECT
            COUNT(*) OVER()::int AS total_count,
            *
          FROM scoped
         ORDER BY created_at DESC NULLS LAST, id DESC
         LIMIT :limit OFFSET :offset
    """, params)
    if rows:
        total = int(rows[0].get("total_count") or 0)
    elif params["offset"] > 0:
        total = int(
            db.execute(
                text(scoped_sql + " SELECT COUNT(*)::int AS total_count FROM scoped"),
                params,
            ).scalar()
            or 0
        )
    else:
        total = 0
    return total, rows


def load_imminent_alert_actioned_envelope_ids(
    db: Session,
    *,
    user_id: int | None,
) -> set[int]:
    """Return alert ids already acted on by open/closed management envelopes."""
    rows = _rows(db, f"""
        SELECT DISTINCT related_alert_id
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE related_alert_id IS NOT NULL
           AND status IN ('open', 'closed')
           AND user_id IS NOT DISTINCT FROM :uid
    """, {"uid": user_id})
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int(row["related_alert_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _envelope_runtime_object(row: dict[str, Any]) -> SimpleNamespace:
    """Expose a management-envelope row through the legacy attribute contract."""
    return SimpleNamespace(**row)


def load_open_stop_position_envelope_objects(
    db: Session,
    *,
    user_id: int | None,
) -> list[Any]:
    """Load open management envelopes as read-only envelope-shaped runtime objects."""
    rows = _rows(db, f"""
        SELECT *
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'open'
         ORDER BY entry_date DESC, id DESC
    """, {"uid": user_id})
    return [_envelope_runtime_object(row) for row in rows]


def load_open_active_setup_envelope_objects(
    db: Session,
    *,
    user_id: int | None,
) -> list[Any]:
    """Load active setup card candidates as read-only envelope-shaped objects."""
    rows = _rows(db, f"""
        SELECT *
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'open'
           AND entry_price > 0
         ORDER BY entry_date DESC, id DESC
    """, {"uid": user_id})
    return [_envelope_runtime_object(row) for row in rows]


def load_autotrader_desk_live_envelope_objects(
    db: Session,
    *,
    user_id: int,
) -> list[Any]:
    """Load live AutoTrader desk rows as read-only envelope-shaped objects."""
    rows = _rows(db, f"""
        SELECT *
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE user_id = :uid
           AND status = 'open'
           AND (
                auto_trader_version = 'v1'
             OR scan_pattern_id IS NOT NULL
             OR related_alert_id IS NOT NULL
             OR stop_loss IS NOT NULL
             OR take_profit IS NOT NULL
           )
         ORDER BY id DESC
    """, {"uid": int(user_id)})
    return [_envelope_runtime_object(row) for row in rows]


def load_stop_decision_envelope_rows(
    db: Session,
    *,
    user_id: int | None,
    trade_id: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Load stop-decision history through the management-envelope table."""
    limit_n = int(limit)
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": limit_n,
    }
    if trade_id is not None:
        params["trade_id"] = int(trade_id)
        return _rows(db, f"""
            SELECT
                d.id,
                d.trade_id,
                d.as_of_ts,
                d.state,
                d.old_stop,
                d.new_stop,
                d.trigger,
                d.reason,
                d.executed
              FROM trading_stop_decisions d
              JOIN {MANAGEMENT_ENVELOPES_RELATION} t ON t.id = d.trade_id
             WHERE t.user_id IS NOT DISTINCT FROM :uid
               AND d.trade_id = :trade_id
             ORDER BY d.as_of_ts DESC, d.id DESC
             LIMIT :limit
        """, params)

    return _rows(db, f"""
        WITH scoped AS MATERIALIZED (
            SELECT id
              FROM {MANAGEMENT_ENVELOPES_RELATION}
             WHERE user_id IS NOT DISTINCT FROM :uid
        ),
        per_trade AS (
            SELECT
                d.id,
                d.trade_id,
                d.as_of_ts,
                d.state,
                d.old_stop,
                d.new_stop,
                d.trigger,
                d.reason,
                d.executed
              FROM scoped s
              CROSS JOIN LATERAL (
                    SELECT
                        id,
                        trade_id,
                        as_of_ts,
                        state,
                        old_stop,
                        new_stop,
                        trigger,
                        reason,
                        executed
                      FROM trading_stop_decisions
                     WHERE trade_id = s.id
                     ORDER BY as_of_ts DESC, id DESC
                     LIMIT :limit
              ) d
        )
        SELECT *
          FROM per_trade
         ORDER BY as_of_ts DESC, id DESC
         LIMIT :limit
    """, params)


def _option_envelope_predicate_sql(alias: str = "t") -> str:
    snap = f"COALESCE({alias}.indicator_snapshot, '{{}}'::jsonb)"
    breakout = f"({snap}->'breakout_alert')"
    return f"""
        (
            LOWER(COALESCE({alias}.asset_kind, '')) IN ('option', 'options')
            OR LOWER(COALESCE({alias}.tags, '')) LIKE '%option%'
            OR COALESCE({snap} ? 'option_meta', FALSE)
            OR LOWER(COALESCE({snap}->>'asset_type', '')) IN ('option', 'options')
            OR LOWER(COALESCE({snap}->>'options_path', '')) IN ('1', 'true', 'yes', 'on')
            OR COALESCE({breakout} ? 'option_meta', FALSE)
            OR LOWER(COALESCE({breakout}->>'asset_type', '')) IN ('option', 'options')
            OR LOWER(COALESCE({breakout}->>'options_path', '')) IN ('1', 'true', 'yes', 'on')
        )
    """


def load_bracket_reconciliation_scope(
    db: Session,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Load management envelopes eligible for bracket reconciliation.

    The contract mirrors the reconciler's historical compatibility-view scope:
    open broker-backed envelopes plus non-open envelopes whose bracket intent is
    still unresolved. Paper and option envelopes remain out of scope.
    """
    params: dict[str, Any] = {}
    scope_clause = (
        "( (t.status = 'open' AND t.broker_source IS NOT NULL)"
        " OR ("
        "     bi.id IS NOT NULL"
        "     AND t.broker_source IS NOT NULL"
        "     AND t.status <> 'open'"
        "     AND bi.intent_state NOT IN ('reconciled', 'authoritative_closed', 'closed')"
        "   )"
        " )"
    )
    filters = [scope_clause, f"NOT {_option_envelope_predicate_sql('t')}"]
    if user_id is not None:
        filters.append("t.user_id = :uid")
        params["uid"] = int(user_id)

    return _rows(db, f"""
        SELECT
            t.id AS trade_id,
            t.user_id,
            t.ticker,
            t.direction,
            t.quantity,
            t.status AS trade_status,
            t.pending_exit_status,
            t.pending_exit_reason,
            t.broker_source,
            bi.id AS bracket_intent_id,
            bi.intent_state,
            bi.stop_price,
            bi.target_price
        FROM {MANAGEMENT_ENVELOPES_RELATION} AS t
        LEFT JOIN trading_bracket_intents AS bi
          ON bi.trade_id = t.id
        WHERE {' AND '.join(filters)}
        ORDER BY t.id
    """, params)


def load_stale_bracket_watchdog_candidates(
    db: Session,
    *,
    user_id: int | None = None,
    stale_after_sec: int,
) -> list[dict[str, Any]]:
    """Load open broker-backed envelopes for the missing-stop watchdog."""
    params: dict[str, Any] = {"stale_sec": int(stale_after_sec)}
    user_filter = ""
    if user_id is not None:
        user_filter = " AND t.user_id = :uid"
        params["uid"] = int(user_id)

    return _rows(db, f"""
        WITH last_rec AS (
            SELECT DISTINCT ON (trade_id)
                trade_id, kind, severity, observed_at
            FROM trading_bracket_reconciliation_log
            WHERE observed_at >= (NOW() - INTERVAL '24 hours')
            ORDER BY trade_id, observed_at DESC
        )
        SELECT
            t.id AS trade_id,
            t.ticker,
            t.broker_source,
            bi.id AS bracket_intent_id,
            bi.last_observed_at,
            r.kind,
            r.severity,
            r.observed_at,
            EXTRACT(EPOCH FROM (NOW() - COALESCE(r.observed_at, bi.created_at))) AS age_sec
        FROM {MANAGEMENT_ENVELOPES_RELATION} AS t
        JOIN trading_bracket_intents AS bi ON bi.trade_id = t.id
        LEFT JOIN last_rec AS r ON r.trade_id = t.id
        WHERE t.status = 'open'
          AND t.broker_source IS NOT NULL
          AND NOT {_option_envelope_predicate_sql('t')}
          AND bi.intent_state NOT IN ('reconciled', 'authoritative_closed', 'closed')
          {user_filter}
        ORDER BY t.id
    """, params)


def load_coinbase_orphan_adoption_candidates(
    db: Session,
    *,
    adoptable_states: list[str] | tuple[str, ...] | set[str],
) -> list[dict[str, Any]]:
    """Load naked Coinbase bracket intents from the management-envelope surface.

    Candidate rows are intentionally narrow: open Coinbase envelopes whose
    bracket intent has no broker stop id and is still in an adoption-capable
    state. This keeps the one-shot orphan adoption pass away from the legacy
    trade compatibility table while preserving its broker-truth contract.
    """
    return _rows(db, f"""
        SELECT
            bi.id            AS intent_id,
            bi.trade_id      AS trade_id,
            bi.ticker        AS ticker,
            bi.quantity      AS quantity,
            bi.intent_state  AS intent_state,
            bi.broker_source AS broker_source
        FROM trading_bracket_intents bi
        JOIN {MANAGEMENT_ENVELOPES_RELATION} t ON t.id = bi.trade_id
        WHERE t.status = 'open'
          AND LOWER(COALESCE(t.broker_source, '')) = 'coinbase'
          AND bi.broker_source = 'coinbase'
          AND bi.broker_stop_order_id IS NULL
          AND LOWER(bi.intent_state) = ANY(:states)
        ORDER BY bi.id
    """, {"states": list(adoptable_states)})


def phase5b_parity_summary(db: Session) -> Phase5BParitySummary:
    """Return the Phase 5B read-model health counters.

    Green state means every valid management envelope has a decision, every open broker
    envelope has a position, and the joined read model has no broken semantic
    links. Corrupt legacy dust rows are excluded by the first counter's
    ``entry_price > 0 AND quantity > 0`` predicate.
    """
    row = _rows(db, """
        WITH phase5a AS (
            SELECT
                COUNT(*) FILTER (
                    WHERE entry_price > 0
                      AND quantity > 0
                      AND decision_id IS NULL
                )::bigint AS valid_trades_missing_decision,
                COUNT(*) FILTER (
                    WHERE status = 'open'
                      AND broker_source IS NOT NULL
                      AND btrim(broker_source) <> ''
                      AND position_id IS NULL
                )::bigint AS open_broker_trades_missing_position
            FROM trading_management_envelopes
        ),
        phase5a_view AS (
            SELECT COALESCE(orphan_decisions, 0)::bigint AS orphan_decisions
            FROM trading_phase5a_envelope_parity
            LIMIT 1
        ),
        phase5b AS (
            SELECT
                COUNT(*) FILTER (
                    WHERE linkage_status = 'decision_without_envelope'
                )::bigint AS decisions_without_envelope,
                COUNT(*) FILTER (
                    WHERE linkage_status = 'broker_envelope_missing_position'
                )::bigint AS broker_envelopes_missing_position,
                COUNT(*) FILTER (
                    WHERE linkage_status = 'open_position_envelope_mismatch'
                )::bigint AS open_position_envelope_mismatches
            FROM trading_phase5b_decision_envelope_position
        )
        SELECT *
        FROM phase5a
        CROSS JOIN phase5a_view
        CROSS JOIN phase5b
    """)[0]
    return Phase5BParitySummary(
        valid_trades_missing_decision=int(row["valid_trades_missing_decision"] or 0),
        open_broker_trades_missing_position=int(
            row["open_broker_trades_missing_position"] or 0
        ),
        orphan_decisions=int(row["orphan_decisions"] or 0),
        decisions_without_envelope=int(row["decisions_without_envelope"] or 0),
        broker_envelopes_missing_position=int(
            row["broker_envelopes_missing_position"] or 0
        ),
        open_position_envelope_mismatches=int(
            row["open_position_envelope_mismatches"] or 0
        ),
    )


def fetch_decision_envelopes(
    db: Session,
    *,
    limit: int = 100,
    status: str | None = None,
    broker_source: str | None = None,
    ticker: str | None = None,
    only_linkage_issues: bool = False,
) -> list[dict[str, Any]]:
    """Fetch joined decision/envelope/position rows for read-only reporting."""
    lim = max(1, min(int(limit or 100), 1000))
    where = ["1=1"]
    params: dict[str, Any] = {"limit": lim}
    if status:
        where.append("envelope_status = :status")
        params["status"] = status
    if broker_source:
        where.append("LOWER(COALESCE(broker_source, '')) = :broker_source")
        params["broker_source"] = broker_source.strip().lower()
    if ticker:
        where.append("UPPER(COALESCE(envelope_ticker, decision_ticker)) = :ticker")
        params["ticker"] = ticker.strip().upper()
    if only_linkage_issues:
        where.append("linkage_status <> 'linked'")

    return _rows(db, f"""
        SELECT *
        FROM trading_phase5b_decision_envelope_position
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(envelope_entry_date, decision_entry_date) DESC,
                 decision_id DESC
        LIMIT :limit
    """, params)


def pattern_decision_performance(
    db: Session,
    *,
    days: int = 30,
    min_closed: int = 1,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Aggregate decision/envelope outcomes by scan_pattern_id.

    This is intentionally read-only and uses the Phase 5B joined view. It lets
    us separate entry-decision quality from broker/envelope state before the
    physical table rename.
    """
    window_days = max(1, int(days or 30))
    min_closed_n = max(0, int(min_closed or 0))
    lim = max(1, min(int(limit or 50), 500))
    return _rows(db, """
        SELECT
            scan_pattern_id,
            COUNT(*)::bigint AS decisions,
            COUNT(*) FILTER (WHERE envelope_status = 'closed')::bigint AS closed_envelopes,
            COUNT(*) FILTER (WHERE envelope_status = 'open')::bigint AS open_envelopes,
            ROUND(SUM(COALESCE(envelope_pnl, 0))::numeric, 4) AS total_pnl,
            ROUND(AVG(envelope_pnl)::numeric, 4) AS avg_pnl,
            ROUND(AVG(tca_entry_slippage_bps)::numeric, 2) AS avg_entry_slippage_bps,
            ROUND(AVG(tca_exit_slippage_bps)::numeric, 2) AS avg_exit_slippage_bps,
            COUNT(*) FILTER (
                WHERE linkage_status NOT IN (
                    'linked',
                    'historical_broker_envelope_missing_position'
                )
            )::bigint AS linkage_issues,
            COUNT(*) FILTER (
                WHERE linkage_status = 'historical_broker_envelope_missing_position'
            )::bigint AS historical_linkage_debt
        FROM trading_phase5b_decision_envelope_position
        WHERE decision_entry_date >= NOW() - (:days * INTERVAL '1 day')
        GROUP BY scan_pattern_id
        HAVING COUNT(*) FILTER (WHERE envelope_status = 'closed') >= :min_closed
        ORDER BY total_pnl DESC NULLS LAST, decisions DESC
        LIMIT :limit
    """, {"days": window_days, "min_closed": min_closed_n, "limit": lim})
