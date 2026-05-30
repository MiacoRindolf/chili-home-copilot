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
