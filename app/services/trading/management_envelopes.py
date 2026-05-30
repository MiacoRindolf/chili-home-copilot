"""Read-only Phase 5B helpers for decision/envelope/position reporting.

Phase 5A created the decision bridge. Phase 5B gave application code a semantic
surface for "management envelopes"; Phase 5H made that surface the physical
base table. These helpers intentionally read the semantic envelope surface and
do not mutate live trading state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


MANAGEMENT_ENVELOPES_RELATION = "trading_management_envelopes"
LEGACY_TRADES_COMPAT_RELATION = "trading_trades"


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


def _option_envelope_predicate_sql(alias: str = "t") -> str:
    """SQL predicate matching option envelopes that bracket code must skip."""
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
    """Load non-option broker envelopes eligible for bracket reconciliation."""
    params: dict[str, Any] = {}
    scope_clause = (
        "( (t.status = 'open' AND t.broker_source IS NOT NULL)"
        " OR ("
        "     bi.id IS NOT NULL"
        "     AND t.broker_source IS NOT NULL"
        "     AND t.status <> 'open'"
        "     AND COALESCE(bi.intent_state, '') NOT IN ('reconciled', 'authoritative_closed', 'closed')"
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
    """Load open non-option broker envelopes for the missing-stop watchdog."""
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
          AND COALESCE(bi.intent_state, '') NOT IN ('reconciled', 'authoritative_closed', 'closed')
          {user_filter}
        ORDER BY t.id
    """, params)


def fetch_naked_coinbase_bracket_intent_rows(
    db: Session,
    *,
    adoptable_states: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    """Fetch Coinbase bracket intents missing a broker stop for open envelopes."""
    states: list[str] = []
    for state in adoptable_states:
        normalized = str(state or "").strip().lower()
        if normalized:
            states.append(normalized)
    return _rows(db, f"""
        SELECT
            bi.id           AS intent_id,
            bi.trade_id     AS trade_id,
            bi.ticker       AS ticker,
            bi.quantity     AS quantity,
            bi.intent_state AS intent_state,
            bi.broker_source AS broker_source
        FROM trading_bracket_intents bi
        JOIN {MANAGEMENT_ENVELOPES_RELATION} t ON t.id = bi.trade_id
        WHERE t.status = 'open'
          AND LOWER(COALESCE(t.broker_source, '')) = 'coinbase'
          AND LOWER(COALESCE(bi.broker_source, '')) = 'coinbase'
          AND bi.broker_stop_order_id IS NULL
          AND LOWER(COALESCE(bi.intent_state, '')) = ANY(:states)
        ORDER BY bi.id
    """, {"states": states})


def phase5b_parity_summary(db: Session) -> Phase5BParitySummary:
    """Return the Phase 5B read-model health counters.

    Green state means every valid Trade row has a decision, every open broker
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
