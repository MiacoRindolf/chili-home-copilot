"""Read-model helpers for momentum automation outcomes (Phase 9)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant
from .evolution import (
    aggregate_recent_outcomes_for_symbol_variant,
    evolution_summary_for_operator,
    paper_vs_live_performance_slices,
)


def momentum_outcomes_table_present(db: Session) -> bool:
    try:
        return "momentum_automation_outcomes" in set(sa_inspect(db.bind).get_table_names())
    except Exception:
        return False


def list_recent_momentum_outcomes(
    db: Session,
    *,
    limit: int = 40,
    user_id: Optional[int] = None,
    variant_id: Optional[int] = None,
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    execution_family: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not momentum_outcomes_table_present(db):
        return []
    lim = max(1, min(int(limit), 200))
    q = db.query(MomentumAutomationOutcome).order_by(MomentumAutomationOutcome.created_at.desc())
    if user_id is not None:
        q = q.filter(MomentumAutomationOutcome.user_id == int(user_id))
    if variant_id is not None:
        q = q.filter(MomentumAutomationOutcome.variant_id == int(variant_id))
    if symbol:
        q = q.filter(MomentumAutomationOutcome.symbol == symbol.strip().upper())
    if mode:
        q = q.filter(MomentumAutomationOutcome.mode == mode.lower().strip())
    if execution_family:
        q = q.filter(
            MomentumAutomationOutcome.execution_family == execution_family.strip().lower()
        )
    rows = q.limit(lim).all()
    return [_outcome_brief(r) for r in rows]


def _outcome_brief(r: MomentumAutomationOutcome, raw_summary: Any = None) -> dict[str, Any]:
    credit, regrade, ingest = (
        _summary_payloads_from_raw(raw_summary) if raw_summary is not None else _summary_payloads(r)
    )
    contributes = bool(r.contributes_to_evolution)
    return {
        "id": r.id,
        "session_id": r.session_id,
        "symbol": r.symbol,
        "variant_id": r.variant_id,
        "execution_family": r.execution_family,
        "mode": r.mode,
        "terminal_state": r.terminal_state,
        "outcome_class": r.outcome_class,
        "realized_pnl_usd": r.realized_pnl_usd,
        "return_bps": r.return_bps,
        "hold_seconds": r.hold_seconds,
        "exit_reason": r.exit_reason,
        "evidence_weight": r.evidence_weight,
        "contributes_to_evolution": contributes,
        "evolution_credit": credit,
        "evolution_credit_reason_codes": list(credit.get("reason_codes") or []),
        "evolution_credit_regrade": regrade,
        "evolution_ingest": ingest,
        "reingest_required": _reingest_required_from_payloads(contributes, regrade, ingest),
        "terminal_at": r.terminal_at.isoformat() if r.terminal_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _credit_payload(row: MomentumAutomationOutcome) -> dict[str, Any]:
    credit, _, _ = _summary_payloads(row)
    return dict(credit)


def _credit_reason_codes(row: MomentumAutomationOutcome) -> list[str]:
    credit = _credit_payload(row)
    reasons = _blocked_credit_reason_codes_from_payload(credit)
    if bool(row.contributes_to_evolution):
        return []
    return reasons


def _blocked_credit_reason_codes_from_payload(credit: dict[str, Any]) -> list[str]:
    reasons = [str(r) for r in (credit.get("reason_codes") or []) if str(r)]
    if not reasons:
        return ["credit_reason_missing"]
    return reasons


def _credit_rate(credited: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(float(credited) / float(total), 4)


def _regrade_payload(row: MomentumAutomationOutcome) -> dict[str, Any]:
    _, marker, _ = _summary_payloads(row)
    return dict(marker)


def _reingest_required(row: MomentumAutomationOutcome) -> bool:
    marker = _regrade_payload(row)
    contributes = bool(row.contributes_to_evolution)
    ingest = _ingest_payload(row) if contributes else {}
    return _reingest_required_from_payloads(contributes, marker, ingest)


def _ingest_payload(row: MomentumAutomationOutcome) -> dict[str, Any]:
    _, _, marker = _summary_payloads(row)
    return dict(marker)


def _summary_payloads(row: MomentumAutomationOutcome) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_summary = row.extracted_summary_json
    return _summary_payloads_from_raw(raw_summary)


def _summary_payloads_from_raw(raw_summary: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    credit = summary.get("evolution_credit")
    regrade = summary.get("evolution_credit_regrade_v1")
    ingest = summary.get("evolution_ingest_v1")
    return (
        credit if isinstance(credit, dict) else {},
        regrade if isinstance(regrade, dict) else {},
        ingest if isinstance(ingest, dict) else {},
    )


def _reingest_required_from_payloads(
    contributes: bool,
    regrade: dict[str, Any],
    ingest: dict[str, Any],
) -> bool:
    return (
        bool(contributes)
        and bool(regrade.get("requires_reingest"))
        and not bool(regrade.get("reingested_at_utc"))
        and not bool(ingest.get("contribution_applied_at_utc"))
    )


def _repair_recommendation(reason_code: str, n: int) -> dict[str, Any]:
    code = str(reason_code or "unknown")
    base = {"reason_code": code, "n": int(n or 0)}
    if code in {"missing_entry_decision_packet", "entry_decision_packet_missing"}:
        return {
            **base,
            "repair_kind": "decision_packet_lineage",
            "dry_run_endpoint": "POST /api/trading/brain/decision-packet-coverage/repair?target=automation_ledger&apply=false",
            "apply_endpoint": "POST /api/trading/brain/decision-packet-coverage/repair?target=automation_ledger&apply=true",
            "follow_up_endpoint": "POST /api/trading/brain/momentum/evolution-credit/regrade?apply=false",
            "expected_effect": "Recover credit only where an existing entry packet can be proven.",
        }
    if code == "decision_snapshot_invalid":
        return {
            **base,
            "repair_kind": "decision_snapshot_seal",
            "dry_run_endpoint": "POST /api/trading/brain/decision-packet-coverage/repair?target=packet_snapshots&apply=false",
            "apply_endpoint": "POST /api/trading/brain/decision-packet-coverage/repair?target=packet_snapshots&apply=true",
            "follow_up_endpoint": "POST /api/trading/brain/momentum/evolution-credit/regrade?apply=false",
            "expected_effect": "Recover credit only when the packet can be sealed into a replayable snapshot.",
        }
    if code == "economic_ledger_parity_missing":
        return {
            **base,
            "repair_kind": "automation_ledger_parity",
            "dry_run_endpoint": "POST /api/trading/brain/ledger/automation-parity/reconcile?apply=false",
            "apply_endpoint": "POST /api/trading/brain/ledger/automation-parity/reconcile?apply=true",
            "follow_up_endpoint": "POST /api/trading/brain/momentum/evolution-credit/regrade?apply=false",
            "expected_effect": "Recover credit only where existing automation fill events reconcile with outcome P&L.",
        }
    if code == "economic_ledger_parity_mismatch":
        return {
            **base,
            "repair_kind": "ledger_disagreement_review",
            "dry_run_endpoint": "POST /api/trading/brain/ledger/automation-parity/reconcile?include_disagreed=true&apply=false",
            "apply_endpoint": "POST /api/trading/brain/ledger/automation-parity/reconcile?include_disagreed=true&apply=true",
            "follow_up_endpoint": "POST /api/trading/brain/momentum/evolution-credit/regrade?apply=false",
            "expected_effect": "Refresh stale disagreements; persistent mismatches should stay audit-only.",
        }
    if code == "missing_economic_result":
        return {
            **base,
            "repair_kind": "outcome_capture_gap",
            "dry_run_endpoint": None,
            "apply_endpoint": None,
            "follow_up_endpoint": None,
            "expected_effect": "No safe repair unless broker/runtime economics can be reconstructed.",
        }
    if code.startswith("non_strategy_outcome_") or code in {"no_entry", "paper_synthetic_quote_source"}:
        return {
            **base,
            "repair_kind": "audit_only_expected",
            "dry_run_endpoint": None,
            "apply_endpoint": None,
            "follow_up_endpoint": None,
            "expected_effect": "Keep the row for diagnostics, but do not use it for strategy learning.",
        }
    return {
        **base,
        "repair_kind": "manual_review",
        "dry_run_endpoint": None,
        "apply_endpoint": None,
        "follow_up_endpoint": None,
        "expected_effect": "Unknown blocker; inspect examples before changing learning credit.",
    }


def _repair_recommendations(reason_counts: Counter[str]) -> list[dict[str, Any]]:
    return [_repair_recommendation(reason, n) for reason, n in reason_counts.most_common()]


def evolution_credit_diagnostics(
    db: Session,
    *,
    days: int = 30,
    user_id: Optional[int] = None,
    mode: Optional[str] = None,
    execution_family: Optional[str] = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Summarize which outcome rows are eligible to train evolution.

    This is intentionally read-only. Rows blocked from evolution still remain
    durable audit samples; this diagnostic explains why they are not allowed to
    update the neural learner yet.
    """
    default_window_days = 30
    max_window_days = 365
    default_row_limit = 1000
    max_row_limit = 10_000
    window_source = default_window_days if days is None else days
    limit_source = default_row_limit if limit is None else limit
    window_days = max(1, min(int(window_source), max_window_days))
    row_limit = max(1, min(int(limit_source), max_row_limit))
    out: dict[str, Any] = {
        "table_present": momentum_outcomes_table_present(db),
        "mode": "audit_only",
        "window_days": window_days,
        "row_limit": row_limit,
        "row_limit_reached": False,
        "filters": {
            "user_id": int(user_id) if user_id is not None else None,
            "mode": mode.lower().strip() if mode else None,
            "execution_family": execution_family.strip().lower() if execution_family else None,
        },
        "total": 0,
        "credited": 0,
        "blocked": 0,
        "credit_rate": None,
        "reason_counts": [],
        "by_mode": [],
        "by_execution_family": [],
        "reingest_required": 0,
        "reingest_examples": [],
        "blocked_examples": [],
        "recommended_repairs": [],
    }
    if not out["table_present"]:
        return out

    since = datetime.utcnow() - timedelta(days=window_days)
    q = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.terminal_at >= since)
        .order_by(MomentumAutomationOutcome.created_at.desc())
    )
    if user_id is not None:
        q = q.filter(MomentumAutomationOutcome.user_id == int(user_id))
    if mode:
        q = q.filter(MomentumAutomationOutcome.mode == mode.lower().strip())
    if execution_family:
        q = q.filter(MomentumAutomationOutcome.execution_family == execution_family.strip().lower())

    rows = q.limit(row_limit + 1).all()
    if len(rows) > row_limit:
        out["row_limit_reached"] = True
        rows = rows[:row_limit]

    total = len(rows)
    credited = 0
    reason_counts: Counter[str] = Counter()
    by_mode: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "credited": 0, "blocked": 0})
    by_family: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "credited": 0, "blocked": 0})
    blocked_examples: list[dict[str, Any]] = []
    reingest_examples: list[dict[str, Any]] = []
    reingest_required = 0

    for row in rows:
        credit, regrade, ingest = _summary_payloads(row)
        is_credited = bool(row.contributes_to_evolution)
        if is_credited:
            credited += 1
        needs_reingest = _reingest_required_from_payloads(is_credited, regrade, ingest) if is_credited else False
        if needs_reingest:
            reingest_required += 1
            if len(reingest_examples) < 10:
                reingest_examples.append(
                    {
                        "outcome_id": int(row.id) if row.id is not None else None,
                        "session_id": int(row.session_id),
                        "symbol": row.symbol,
                        "mode": row.mode,
                        "execution_family": row.execution_family,
                        "outcome_class": row.outcome_class,
                        "entry_decision_packet_id": credit.get("entry_decision_packet_id"),
                        "regraded_at_utc": regrade.get("regraded_at_utc"),
                        "terminal_at": row.terminal_at.isoformat() if row.terminal_at else None,
                    }
                )
        mode_key = str(row.mode or "unknown")
        family_key = str(row.execution_family or "unknown")
        by_mode[mode_key]["total"] += 1
        by_family[family_key]["total"] += 1
        if is_credited:
            by_mode[mode_key]["credited"] += 1
            by_family[family_key]["credited"] += 1
            continue

        by_mode[mode_key]["blocked"] += 1
        by_family[family_key]["blocked"] += 1
        reasons = _blocked_credit_reason_codes_from_payload(credit)
        reason_counts.update(reasons)
        if len(blocked_examples) < 10:
            blocked_examples.append(
                {
                    "outcome_id": int(row.id) if row.id is not None else None,
                    "session_id": int(row.session_id),
                    "symbol": row.symbol,
                    "mode": row.mode,
                    "execution_family": row.execution_family,
                    "outcome_class": row.outcome_class,
                    "reason_codes": reasons,
                    "entry_decision_packet_id": credit.get("entry_decision_packet_id"),
                    "terminal_at": row.terminal_at.isoformat() if row.terminal_at else None,
                }
            )

    def _bucket_rows(src: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        rows_out: list[dict[str, Any]] = []
        for key, counts in src.items():
            rows_out.append(
                {
                    "key": key,
                    **counts,
                    "credit_rate": _credit_rate(counts["credited"], counts["total"]),
                }
            )
        rows_out.sort(key=lambda r: (-int(r["total"]), str(r["key"])))
        return rows_out

    out.update(
        {
            "total": total,
            "credited": credited,
            "blocked": total - credited,
            "credit_rate": _credit_rate(credited, total),
            "reason_counts": [
                {"reason_code": reason, "n": int(n)}
                for reason, n in reason_counts.most_common()
            ],
            "by_mode": _bucket_rows(by_mode),
            "by_execution_family": _bucket_rows(by_family),
            "reingest_required": reingest_required,
            "reingest_examples": reingest_examples,
            "blocked_examples": blocked_examples,
            "recommended_repairs": _repair_recommendations(reason_counts),
        }
    )
    return out


def get_variant_feedback_summary(db: Session, *, variant_id: int, days: int = 14) -> dict[str, Any]:
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(variant_id)).one_or_none()
    brief = _variant_brief(v) if v else None
    return {
        "variant": brief,
        "paper_vs_live": paper_vs_live_performance_slices(db, variant_id=int(variant_id), days=days),
        "evolution": evolution_summary_for_operator(db, variant_id=int(variant_id)),
    }


def get_symbol_variant_feedback_summary(
    db: Session, *, symbol: str, variant_id: int, days: int = 14
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    agg = aggregate_recent_outcomes_for_symbol_variant(db, symbol=sym, variant_id=int(variant_id), days=days)
    slices = paper_vs_live_performance_slices(db, variant_id=int(variant_id), days=days)
    return {
        "symbol": sym,
        "variant_id": int(variant_id),
        "symbol_variant_window": agg,
        "variant_paper_slice": slices["paper"],
        "variant_live_slice": slices["live"],
    }


def _variant_brief(v: Optional[MomentumStrategyVariant]) -> Optional[dict[str, Any]]:
    if not v:
        return None
    return {
        "id": v.id,
        "family": v.family,
        "strategy_family": v.family,
        "variant_key": v.variant_key,
        "version": v.version,
        "label": v.label,
        "execution_family": v.execution_family,
    }


def aggregate_outcome_counts_by_execution_family(
    db: Session,
    *,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Read-model: durable outcome rows grouped by execution_family (Phase 11 seam)."""
    if not momentum_outcomes_table_present(db):
        return []
    since = datetime.utcnow() - timedelta(days=max(1, min(int(days), 120)))
    rows = (
        db.query(MomentumAutomationOutcome.execution_family, func.count(MomentumAutomationOutcome.id))
        .filter(MomentumAutomationOutcome.terminal_at >= since)
        .group_by(MomentumAutomationOutcome.execution_family)
        .all()
    )
    return [{"execution_family": str(ef), "n": int(c)} for ef, c in rows]


def get_session_feedback_row(db: Session, *, session_id: int) -> Optional[dict[str, Any]]:
    if not momentum_outcomes_table_present(db):
        return None
    r = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
    )
    if not r:
        return None
    summary = r.extracted_summary_json
    d = _outcome_brief(r, raw_summary=summary)
    d["governance_context_json"] = r.governance_context_json
    d["extracted_summary_json"] = summary
    return d
