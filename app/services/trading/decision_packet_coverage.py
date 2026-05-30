"""Decision-packet lineage coverage diagnostics.

This module is intentionally audit-only. It answers "where are we still
missing packet lineage?" without blocking signals, alerts, paper fills, or
live orders. That lets CHILI keep collecting learning samples while the
operator can see which surfaces still need stronger closed-loop truth.
"""
from __future__ import annotations

import heapq
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


_GREEN_FLOOR = 0.95
_YELLOW_FLOOR = 0.80


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _status(coverage: float | None, total: int) -> str:
    if total <= 0 or coverage is None:
        return "unknown"
    if coverage >= _GREEN_FLOOR:
        return "green"
    if coverage >= _YELLOW_FLOOR:
        return "yellow"
    return "red"


def _surface(total: int, linked: int) -> dict[str, Any]:
    total_i = int(total or 0)
    linked_i = max(0, min(int(linked or 0), total_i))
    missing_i = max(0, total_i - linked_i)
    coverage = _ratio(linked_i, total_i)
    return {
        "total": total_i,
        "linked": linked_i,
        "missing": missing_i,
        "coverage": coverage,
        "status": _status(coverage, total_i),
    }


def _worst_status(surfaces: dict[str, dict[str, Any]]) -> str:
    rank = {"red": 3, "yellow": 2, "green": 1, "unknown": 0}
    observed = [str(v.get("status") or "unknown") for v in surfaces.values()]
    if not observed:
        return "unknown"
    return max(observed, key=lambda s: rank.get(s, 0))


def _user_clause(user_id: int | None, table_alias: str = "") -> tuple[str, dict[str, Any]]:
    if user_id is None:
        return "", {}
    prefix = f"{table_alias}." if table_alias else ""
    return f" AND {prefix}user_id = :user_id", {"user_id": int(user_id)}


def _one_mapping(db: Session, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    row = db.execute(text(sql), params).mappings().one()
    return dict(row)


def _mapping_rows(db: Session, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows = db.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def _breakdown_row(row: dict[str, Any], *label_keys: str) -> dict[str, Any]:
    surface = _surface(row.get("total", 0), row.get("linked", 0))
    labels = {key: row.get(key) for key in label_keys}
    return {**labels, **surface}


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[str(key)] = value.isoformat() + "Z"
        else:
            out[str(key)] = value
    return out


def _recommended_next_fixes(
    surfaces: dict[str, dict[str, Any]],
    breakdowns: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Produce operator hints without changing policy or execution."""
    fixes: list[dict[str, Any]] = []

    for row in breakdowns.get("alerts_signal_by_type", []):
        missing = int(row.get("missing") or 0)
        if missing <= 0:
            continue
        fixes.append(
            {
                "surface": "alerts_signal",
                "key": str(row.get("alert_type") or "unknown"),
                "missing": missing,
                "coverage": row.get("coverage"),
                "suggested_action": "route this signal alert type through decision-packet creation or reuse",
            }
        )

    for row in breakdowns.get("economic_ledger_fills_by_source_event", []):
        missing = int(row.get("missing") or 0)
        if missing <= 0:
            continue
        fixes.append(
            {
                "surface": "economic_ledger_fills",
                "key": f"{row.get('source') or 'unknown'}:{row.get('event_type') or 'unknown'}",
                "missing": missing,
                "coverage": row.get("coverage"),
                "suggested_action": "pass decision_packet_id into this ledger fill provenance",
            }
        )

    fallback_actions = {
        "trade_packets": "link created trades to their decision packet before/after broker submission",
        "automation_entry_fills": "pass the entry decision packet id into simulated fill persistence",
        "packet_snapshots": "seal every packet with a replayable decision snapshot at creation time",
        "directional_outcomes": "backfill outcome rows from linked alert decision_packet_id",
    }
    for name, action in fallback_actions.items():
        surface = surfaces.get(name) or {}
        missing = int(surface.get("missing") or 0)
        if missing <= 0:
            continue
        fixes.append(
            {
                "surface": name,
                "key": name,
                "missing": missing,
                "coverage": surface.get("coverage"),
                "suggested_action": action,
            }
        )

    return _top_recommended_fixes(fixes, limit=5)


def _fix_priority(row: dict[str, Any]) -> tuple[int, str]:
    return (-int(row.get("missing") or 0), str(row.get("surface") or ""))


def _top_recommended_fixes(fixes: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not fixes:
        return []
    return [
        row
        for _, _, row in heapq.nsmallest(
            limit,
            ((_fix_priority(row), idx, row) for idx, row in enumerate(fixes)),
        )
    ]


def decision_packet_coverage_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
    user_id: int | None = None,
    example_limit: int = 5,
) -> dict[str, Any]:
    """Return packet-lineage coverage by trading surface.

    The summary is soft observability, not policy. It should be safe to call
    from dashboards, assistant context, and scheduled diagnostics.
    """
    hours = max(1, int(lookback_hours or 24))
    since = datetime.utcnow() - timedelta(hours=hours)
    limit = max(0, min(int(example_limit or 0), 25))
    surfaces: dict[str, dict[str, Any]] = {}
    breakdowns: dict[str, list[dict[str, Any]]] = {}
    missing_examples: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    alert_user_sql, alert_user_params = _user_clause(user_id, "a")
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE a.decision_packet_id IS NOT NULL) AS linked,
                COUNT(*) FILTER (
                    WHERE a.scan_pattern_id IS NOT NULL
                       OR a.alert_type IN (
                            'breakout_triggered',
                            'crypto_breakout',
                            'crypto_squeeze_firing',
                            'new_top_pick',
                            'pattern_breakout_imminent',
                            'strategy_proposed'
                       )
                ) AS signal_total,
                COUNT(*) FILTER (
                    WHERE a.decision_packet_id IS NOT NULL
                      AND (
                        a.scan_pattern_id IS NOT NULL
                        OR a.alert_type IN (
                            'breakout_triggered',
                            'crypto_breakout',
                            'crypto_squeeze_firing',
                            'new_top_pick',
                            'pattern_breakout_imminent',
                            'strategy_proposed'
                        )
                      )
                ) AS signal_linked
            FROM trading_alerts a
            WHERE a.created_at >= :since
            {alert_user_sql}
            """,
            {"since": since, **alert_user_params},
        )
        surfaces["alerts_all"] = _surface(r.get("total", 0), r.get("linked", 0))
        surfaces["alerts_signal"] = _surface(r.get("signal_total", 0), r.get("signal_linked", 0))
        rows = _mapping_rows(
            db,
            f"""
            SELECT
                a.alert_type,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE a.decision_packet_id IS NOT NULL) AS linked
            FROM trading_alerts a
            WHERE a.created_at >= :since
              AND (
                a.scan_pattern_id IS NOT NULL
                OR a.alert_type IN (
                    'breakout_triggered',
                    'crypto_breakout',
                    'crypto_squeeze_firing',
                    'new_top_pick',
                    'pattern_breakout_imminent',
                    'strategy_proposed'
                )
              )
            {alert_user_sql}
            GROUP BY a.alert_type
            ORDER BY
                COUNT(*) FILTER (WHERE a.decision_packet_id IS NULL) DESC,
                COUNT(*) DESC,
                a.alert_type ASC
            LIMIT 20
            """,
            {"since": since, **alert_user_params},
        )
        breakdowns["alerts_signal_by_type"] = [
            _breakdown_row(r, "alert_type") for r in rows
        ]
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:alerts_signal */
                SELECT
                    a.id AS alert_id,
                    a.alert_type,
                    a.ticker,
                    a.scan_pattern_id,
                    a.created_at
                FROM trading_alerts a
                WHERE a.created_at >= :since
                  AND a.decision_packet_id IS NULL
                  AND (
                    a.scan_pattern_id IS NOT NULL
                    OR a.alert_type IN (
                        'breakout_triggered',
                        'crypto_breakout',
                        'crypto_squeeze_firing',
                        'new_top_pick',
                        'pattern_breakout_imminent',
                        'strategy_proposed'
                    )
                  )
                {alert_user_sql}
                ORDER BY a.created_at DESC
                LIMIT :example_limit
                """,
                {"since": since, "example_limit": limit, **alert_user_params},
            )
            missing_examples["alerts_signal"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["alerts"] = str(exc)

    outcome_user_sql = ""
    outcome_params: dict[str, Any] = {"since": since}
    if user_id is not None:
        outcome_user_sql = " AND a.user_id = :user_id"
        outcome_params["user_id"] = int(user_id)
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE p.decision_packet_id IS NOT NULL) AS linked
            FROM pattern_alert_directional_outcome p
            LEFT JOIN trading_alerts a ON a.id = p.alert_id
            WHERE p.evaluated_at >= :since
            {outcome_user_sql}
            """,
            outcome_params,
        )
        surfaces["directional_outcomes"] = _surface(r.get("total", 0), r.get("linked", 0))
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:directional_outcomes */
                SELECT
                    p.id AS outcome_id,
                    p.alert_id,
                    p.ticker,
                    p.scan_pattern_id,
                    p.evaluated_at
                FROM pattern_alert_directional_outcome p
                LEFT JOIN trading_alerts a ON a.id = p.alert_id
                WHERE p.evaluated_at >= :since
                  AND p.decision_packet_id IS NULL
                {outcome_user_sql}
                ORDER BY p.evaluated_at DESC
                LIMIT :example_limit
                """,
                {**outcome_params, "example_limit": limit},
            )
            missing_examples["directional_outcomes"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["directional_outcomes"] = str(exc)

    trade_user_sql, trade_user_params = _user_clause(user_id, "t")
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE p.id IS NOT NULL) AS linked
            FROM trading_management_envelopes t
            LEFT JOIN trading_decision_packets p ON p.linked_trade_id = t.id
            WHERE t.entry_date >= :since
              AND COALESCE(t.status, '') NOT IN ('cancelled', 'rejected')
            {trade_user_sql}
            """,
            {"since": since, **trade_user_params},
        )
        surfaces["trade_packets"] = _surface(r.get("total", 0), r.get("linked", 0))
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:trade_packets */
                SELECT
                    t.id AS trade_id,
                    t.ticker,
                    t.broker_source,
                    t.status,
                    t.entry_date
                FROM trading_management_envelopes t
                LEFT JOIN trading_decision_packets p ON p.linked_trade_id = t.id
                WHERE t.entry_date >= :since
                  AND COALESCE(t.status, '') NOT IN ('cancelled', 'rejected')
                  AND p.id IS NULL
                {trade_user_sql}
                ORDER BY t.entry_date DESC
                LIMIT :example_limit
                """,
                {"since": since, "example_limit": limit, **trade_user_params},
            )
            missing_examples["trade_packets"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["trade_packets"] = str(exc)

    fill_user_sql = ""
    fill_params: dict[str, Any] = {"since": since}
    if user_id is not None:
        fill_user_sql = " AND s.user_id = :user_id"
        fill_params["user_id"] = int(user_id)
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(f.id) AS total,
                COUNT(f.id) FILTER (WHERE f.decision_packet_id IS NOT NULL) AS linked
            FROM trading_automation_simulated_fills f
            LEFT JOIN trading_automation_sessions s ON s.id = f.session_id
            WHERE f.created_at >= :since
              AND f.fill_type = 'entry'
            {fill_user_sql}
            """,
            fill_params,
        )
        surfaces["automation_entry_fills"] = _surface(r.get("total", 0), r.get("linked", 0))
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:automation_entry_fills */
                SELECT
                    f.id AS fill_id,
                    f.session_id,
                    f.symbol,
                    f.lane,
                    f.created_at
                FROM trading_automation_simulated_fills f
                LEFT JOIN trading_automation_sessions s ON s.id = f.session_id
                WHERE f.created_at >= :since
                  AND f.fill_type = 'entry'
                  AND f.decision_packet_id IS NULL
                {fill_user_sql}
                ORDER BY f.created_at DESC
                LIMIT :example_limit
                """,
                {**fill_params, "example_limit": limit},
            )
            missing_examples["automation_entry_fills"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["automation_entry_fills"] = str(exc)

    ledger_user_sql, ledger_user_params = _user_clause(user_id, "e")
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id'
                ) AS linked
            FROM trading_economic_ledger e
            WHERE e.created_at >= :since
              AND e.event_type IN ('entry_fill', 'exit_fill', 'partial_fill')
            {ledger_user_sql}
            """,
            {"since": since, **ledger_user_params},
        )
        surfaces["economic_ledger_fills"] = _surface(r.get("total", 0), r.get("linked", 0))
        rows = _mapping_rows(
            db,
            f"""
            SELECT
                e.source,
                e.event_type,
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id'
                ) AS linked
            FROM trading_economic_ledger e
            WHERE e.created_at >= :since
              AND e.event_type IN ('entry_fill', 'exit_fill', 'partial_fill')
            {ledger_user_sql}
            GROUP BY e.source, e.event_type
            ORDER BY
                COUNT(*) FILTER (
                    WHERE NOT (COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id')
                ) DESC,
                COUNT(*) DESC,
                e.source ASC,
                e.event_type ASC
            LIMIT 20
            """,
            {"since": since, **ledger_user_params},
        )
        breakdowns["economic_ledger_fills_by_source_event"] = [
            _breakdown_row(r, "source", "event_type") for r in rows
        ]
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:economic_ledger_fills */
                SELECT
                    e.id AS ledger_event_id,
                    e.source,
                    e.event_type,
                    e.ticker,
                    e.trade_id,
                    e.paper_trade_id,
                    e.created_at
                FROM trading_economic_ledger e
                WHERE e.created_at >= :since
                  AND e.event_type IN ('entry_fill', 'exit_fill', 'partial_fill')
                  AND NOT (COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id')
                {ledger_user_sql}
                ORDER BY e.created_at DESC
                LIMIT :example_limit
                """,
                {"since": since, "example_limit": limit, **ledger_user_params},
            )
            missing_examples["economic_ledger_fills"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["economic_ledger_fills"] = str(exc)

    packet_user_sql, packet_user_params = _user_clause(user_id, "p")
    try:
        r = _one_mapping(
            db,
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE COALESCE(p.allocator_input_json, '{{}}'::jsonb) ? 'decision_snapshot'
                ) AS linked
            FROM trading_decision_packets p
            WHERE p.created_at >= :since
            {packet_user_sql}
            """,
            {"since": since, **packet_user_params},
        )
        surfaces["packet_snapshots"] = _surface(r.get("total", 0), r.get("linked", 0))
        if limit > 0:
            rows = _mapping_rows(
                db,
                f"""
                /* coverage_examples:packet_snapshots */
                SELECT
                    p.id AS decision_packet_id,
                    p.chosen_ticker,
                    p.source_surface,
                    p.outcome_status,
                    p.created_at
                FROM trading_decision_packets p
                WHERE p.created_at >= :since
                  AND NOT (COALESCE(p.allocator_input_json, '{{}}'::jsonb) ? 'decision_snapshot')
                {packet_user_sql}
                ORDER BY p.created_at DESC
                LIMIT :example_limit
                """,
                {"since": since, "example_limit": limit, **packet_user_params},
            )
            missing_examples["packet_snapshots"] = [_jsonable_row(r) for r in rows]
    except Exception as exc:
        errors["packet_snapshots"] = str(exc)

    return {
        "ok": not errors,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "lookback_hours": hours,
        "user_id": int(user_id) if user_id is not None else None,
        "status": _worst_status(surfaces),
        "surfaces": surfaces,
        "breakdowns": breakdowns,
        "missing_examples": missing_examples,
        "recommended_next_fixes": _recommended_next_fixes(surfaces, breakdowns),
        "errors": errors,
        "mode": "audit_only",
    }


def repair_directional_outcome_packet_links(
    db: Session,
    *,
    lookback_hours: int = 720,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Backfill outcome packet ids from linked alerts.

    This is intentionally narrow and safe: it only updates
    ``pattern_alert_directional_outcome.decision_packet_id`` where the linked
    ``trading_alerts`` row already has a non-null packet id. It does not create
    packets, alter trades, or affect trading gates.
    """
    hours = max(1, int(lookback_hours or 720))
    limit_i = max(0, min(int(limit or 0), 5000))
    since = datetime.utcnow() - timedelta(hours=hours)
    user_sql = ""
    params: dict[str, Any] = {"since": since, "limit": limit_i}
    if user_id is not None:
        user_sql = " AND a.user_id = :user_id"
        params["user_id"] = int(user_id)

    candidates = _mapping_rows(
        db,
        f"""
        /* repair_candidates:directional_outcomes */
        SELECT
            p.id AS outcome_id,
            p.alert_id,
            p.ticker,
            p.scan_pattern_id,
            a.decision_packet_id,
            p.evaluated_at
        FROM pattern_alert_directional_outcome p
        JOIN trading_alerts a ON a.id = p.alert_id
        WHERE p.evaluated_at >= :since
          AND p.decision_packet_id IS NULL
          AND a.decision_packet_id IS NOT NULL
        {user_sql}
        ORDER BY p.evaluated_at DESC
        LIMIT :limit
        """,
        params,
    )

    if dry_run or not candidates or limit_i <= 0:
        return {
            "ok": True,
            "dry_run": True,
            "lookback_hours": hours,
            "limit": limit_i,
            "candidate_count": len(candidates),
            "applied_count": 0,
            "candidates": [_jsonable_row(r) for r in candidates],
            "repair": "directional_outcomes_from_alert_decision_packet",
        }

    try:
        applied = _mapping_rows(
            db,
            f"""
            /* repair_apply:directional_outcomes */
            WITH candidates AS (
                SELECT
                    p.id AS outcome_id,
                    a.decision_packet_id
                FROM pattern_alert_directional_outcome p
                JOIN trading_alerts a ON a.id = p.alert_id
                WHERE p.evaluated_at >= :since
                  AND p.decision_packet_id IS NULL
                  AND a.decision_packet_id IS NOT NULL
                {user_sql}
                ORDER BY p.evaluated_at DESC
                LIMIT :limit
            )
            UPDATE pattern_alert_directional_outcome p
            SET decision_packet_id = candidates.decision_packet_id
            FROM candidates
            WHERE p.id = candidates.outcome_id
            RETURNING
                p.id AS outcome_id,
                p.alert_id,
                p.ticker,
                p.scan_pattern_id,
                p.decision_packet_id,
                p.evaluated_at
            """,
            params,
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "dry_run": False,
        "lookback_hours": hours,
        "limit": limit_i,
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "applied": [_jsonable_row(r) for r in applied],
        "repair": "directional_outcomes_from_alert_decision_packet",
    }


def repair_automation_ledger_packet_links(
    db: Session,
    *,
    lookback_hours: int = 720,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Backfill automation ledger packet provenance from simulated fills.

    This only touches ``trading_economic_ledger.provenance_json`` for
    automation fill events when the automation session has exactly one known
    decision packet across its simulated fills. Ambiguous sessions are skipped
    by construction.
    """
    hours = max(1, int(lookback_hours or 720))
    limit_i = max(0, min(int(limit or 0), 5000))
    since = datetime.utcnow() - timedelta(hours=hours)
    user_sql = ""
    params: dict[str, Any] = {"since": since, "limit": limit_i}
    if user_id is not None:
        user_sql = " AND e.user_id = :user_id"
        params["user_id"] = int(user_id)

    candidate_sql = f"""
        WITH fill_packets AS (
            SELECT
                f.session_id,
                MIN(f.decision_packet_id)::bigint AS decision_packet_id,
                COUNT(DISTINCT f.decision_packet_id) AS packet_count
            FROM trading_automation_simulated_fills f
            WHERE f.decision_packet_id IS NOT NULL
            GROUP BY f.session_id
            HAVING COUNT(DISTINCT f.decision_packet_id) = 1
        )
        SELECT
            e.id AS ledger_event_id,
            e.source,
            e.event_type,
            e.ticker,
            e.trade_id,
            fp.session_id,
            fp.decision_packet_id,
            e.created_at
        FROM trading_economic_ledger e
        JOIN fill_packets fp ON e.trade_id = -fp.session_id
        WHERE e.created_at >= :since
          AND e.source = 'automation'
          AND e.event_type IN ('entry_fill', 'exit_fill', 'partial_fill')
          AND NOT (COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id')
        {user_sql}
        ORDER BY e.created_at DESC
        LIMIT :limit
    """
    candidates = _mapping_rows(
        db,
        f"""
        /* repair_candidates:automation_ledger */
        {candidate_sql}
        """,
        params,
    )

    if dry_run or not candidates or limit_i <= 0:
        return {
            "ok": True,
            "dry_run": True,
            "lookback_hours": hours,
            "limit": limit_i,
            "candidate_count": len(candidates),
            "applied_count": 0,
            "candidates": [_jsonable_row(r) for r in candidates],
            "repair": "automation_ledger_from_simulated_fill_packet",
        }

    try:
        applied = _mapping_rows(
            db,
            f"""
            /* repair_apply:automation_ledger */
            WITH fill_packets AS (
                SELECT
                    f.session_id,
                    MIN(f.decision_packet_id)::bigint AS decision_packet_id,
                    COUNT(DISTINCT f.decision_packet_id) AS packet_count
                FROM trading_automation_simulated_fills f
                WHERE f.decision_packet_id IS NOT NULL
                GROUP BY f.session_id
                HAVING COUNT(DISTINCT f.decision_packet_id) = 1
            ),
            candidates AS (
                SELECT
                    e.id AS ledger_event_id,
                    fp.session_id,
                    fp.decision_packet_id
                FROM trading_economic_ledger e
                JOIN fill_packets fp ON e.trade_id = -fp.session_id
                WHERE e.created_at >= :since
                  AND e.source = 'automation'
                  AND e.event_type IN ('entry_fill', 'exit_fill', 'partial_fill')
                  AND NOT (COALESCE(e.provenance_json, '{{}}'::jsonb) ? 'decision_packet_id')
                {user_sql}
                ORDER BY e.created_at DESC
                LIMIT :limit
            )
            UPDATE trading_economic_ledger e
            SET provenance_json = jsonb_set(
                COALESCE(e.provenance_json, '{{}}'::jsonb),
                '{{decision_packet_id}}',
                to_jsonb(candidates.decision_packet_id),
                true
            )
            FROM candidates
            WHERE e.id = candidates.ledger_event_id
            RETURNING
                e.id AS ledger_event_id,
                e.source,
                e.event_type,
                e.ticker,
                e.trade_id,
                candidates.session_id,
                candidates.decision_packet_id,
                e.created_at
            """,
            params,
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "dry_run": False,
        "lookback_hours": hours,
        "limit": limit_i,
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "applied": [_jsonable_row(r) for r in applied],
        "repair": "automation_ledger_from_simulated_fill_packet",
    }


def repair_trade_packet_links_from_proposals(
    db: Session,
    *,
    lookback_hours: int = 720,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Backfill packet -> trade links from proposal allocation decisions.

    This only sets ``trading_decision_packets.linked_trade_id`` when the trade
    points to a proposal whose ``allocation_decision_json`` already names a
    valid packet. It skips packets already linked elsewhere, trades already
    linked by another packet, and ambiguous one-packet-to-many-trades cases.
    """
    hours = max(1, int(lookback_hours or 720))
    limit_i = max(0, min(int(limit or 0), 5000))
    since = datetime.utcnow() - timedelta(hours=hours)
    user_sql = ""
    params: dict[str, Any] = {"since": since, "limit": limit_i}
    if user_id is not None:
        user_sql = " AND t.user_id = :user_id"
        params["user_id"] = int(user_id)

    candidate_cte = f"""
        WITH proposal_raw AS (
            SELECT
                t.id AS trade_id,
                t.ticker,
                t.strategy_proposal_id,
                t.entry_date,
                pr.allocation_decision_json ->> 'decision_packet_id' AS decision_packet_id_text
            FROM trading_management_envelopes t
            JOIN trading_proposals pr ON pr.id = t.strategy_proposal_id
            WHERE t.entry_date >= :since
              AND COALESCE(t.status, '') NOT IN ('cancelled', 'rejected')
              AND t.strategy_proposal_id IS NOT NULL
              AND pr.allocation_decision_json ? 'decision_packet_id'
              AND (pr.allocation_decision_json ->> 'decision_packet_id') ~ '^[0-9]+$'
            {user_sql}
        ),
        proposal_links AS (
            SELECT
                raw.trade_id,
                raw.ticker,
                raw.strategy_proposal_id,
                raw.entry_date,
                raw.decision_packet_id_text::bigint AS decision_packet_id
            FROM proposal_raw raw
            JOIN trading_decision_packets p
              ON p.id = raw.decision_packet_id_text::bigint
            LEFT JOIN trading_decision_packets existing_trade_packet
              ON existing_trade_packet.linked_trade_id = raw.trade_id
            WHERE p.linked_trade_id IS NULL
              AND existing_trade_packet.id IS NULL
        ),
        unique_packet_links AS (
            SELECT decision_packet_id
            FROM proposal_links
            GROUP BY decision_packet_id
            HAVING COUNT(DISTINCT trade_id) = 1
        )
    """
    candidates = _mapping_rows(
        db,
        f"""
        /* repair_candidates:trade_packets */
        {candidate_cte}
        SELECT
            pl.trade_id,
            pl.ticker,
            pl.strategy_proposal_id,
            pl.decision_packet_id,
            pl.entry_date
        FROM proposal_links pl
        JOIN unique_packet_links upl ON upl.decision_packet_id = pl.decision_packet_id
        ORDER BY pl.entry_date DESC
        LIMIT :limit
        """,
        params,
    )

    if dry_run or not candidates or limit_i <= 0:
        return {
            "ok": True,
            "dry_run": True,
            "lookback_hours": hours,
            "limit": limit_i,
            "candidate_count": len(candidates),
            "applied_count": 0,
            "candidates": [_jsonable_row(r) for r in candidates],
            "repair": "trade_packets_from_proposal_allocation",
        }

    try:
        applied = _mapping_rows(
            db,
            f"""
            /* repair_apply:trade_packets */
            {candidate_cte},
            limited AS (
                SELECT
                    pl.trade_id,
                    pl.ticker,
                    pl.strategy_proposal_id,
                    pl.decision_packet_id,
                    pl.entry_date
                FROM proposal_links pl
                JOIN unique_packet_links upl ON upl.decision_packet_id = pl.decision_packet_id
                ORDER BY pl.entry_date DESC
                LIMIT :limit
            )
            UPDATE trading_decision_packets p
            SET linked_trade_id = limited.trade_id
            FROM limited
            WHERE p.id = limited.decision_packet_id
              AND p.linked_trade_id IS NULL
            RETURNING
                p.id AS decision_packet_id,
                p.linked_trade_id AS trade_id,
                limited.ticker,
                limited.strategy_proposal_id,
                limited.entry_date
            """,
            params,
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "dry_run": False,
        "lookback_hours": hours,
        "limit": limit_i,
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "applied": [_jsonable_row(r) for r in applied],
        "repair": "trade_packets_from_proposal_allocation",
    }


def repair_packet_snapshot_seals(
    db: Session,
    *,
    lookback_hours: int = 720,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Seal existing decision packets that are missing replay snapshots.

    Uses ``decision_ledger.seal_decision_packet_snapshot`` so the repair path
    has the same fingerprint contract as normal packet creation. It only
    mutates packet JSON payloads and does not affect execution policy.
    """
    hours = max(1, int(lookback_hours or 720))
    limit_i = max(0, min(int(limit or 0), 5000))
    since = datetime.utcnow() - timedelta(hours=hours)
    user_sql, user_params = _user_clause(user_id, "p")
    params: dict[str, Any] = {"since": since, "limit": limit_i, **user_params}

    candidates = _mapping_rows(
        db,
        f"""
        /* repair_candidates:packet_snapshots */
        SELECT
            p.id AS decision_packet_id,
            p.chosen_ticker,
            p.source_surface,
            p.outcome_status,
            p.created_at
        FROM trading_decision_packets p
        WHERE p.created_at >= :since
          AND NOT (COALESCE(p.allocator_input_json, '{{}}'::jsonb) ? 'decision_snapshot')
        {user_sql}
        ORDER BY p.created_at DESC
        LIMIT :limit
        """,
        params,
    )

    if dry_run or not candidates or limit_i <= 0:
        return {
            "ok": True,
            "dry_run": True,
            "lookback_hours": hours,
            "limit": limit_i,
            "candidate_count": len(candidates),
            "applied_count": 0,
            "candidates": [_jsonable_row(r) for r in candidates],
            "repair": "packet_snapshot_seals",
        }

    try:
        from ...models.trading import TradingDecisionPacket
        from .decision_ledger import seal_decision_packet_snapshot

        applied: list[dict[str, Any]] = []
        for row in candidates:
            packet_id = row.get("decision_packet_id")
            if packet_id is None:
                continue
            packet = db.get(TradingDecisionPacket, int(packet_id))
            if packet is None:
                continue
            existing = (packet.allocator_input_json or {}).get("decision_snapshot")
            if isinstance(existing, dict) and existing.get("fingerprint_sha256"):
                continue
            seal = seal_decision_packet_snapshot(packet)
            applied.append(
                {
                    "decision_packet_id": int(packet.id),
                    "chosen_ticker": packet.chosen_ticker,
                    "source_surface": packet.source_surface,
                    "outcome_status": packet.outcome_status,
                    "snapshot_id": seal.get("snapshot_id"),
                    "fingerprint_sha256": seal.get("fingerprint_sha256"),
                }
            )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "dry_run": False,
        "lookback_hours": hours,
        "limit": limit_i,
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "applied": applied,
        "repair": "packet_snapshot_seals",
    }


__all__ = [
    "decision_packet_coverage_summary",
    "repair_automation_ledger_packet_links",
    "repair_directional_outcome_packet_links",
    "repair_packet_snapshot_seals",
    "repair_trade_packet_links_from_proposals",
]
