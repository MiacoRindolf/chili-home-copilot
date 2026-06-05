"""Lean, read-only AutoTrader deployment report.

One in-app surface that answers "why is (or isn't) the AutoTrader trading?":

  * the decision funnel from ``trading_autotrader_runs`` (placed / scaled_in /
    skipped / blocked / error, grouped by reason),
  * a ``rule_snapshot`` drill-down sample for the top blockers (the per-instance
    forensics that the GROUP-BY-only CLI scripts don't surface),
  * the live gate status (kill switch, drawdown circuit breaker, AutoTrader
    enablement) via main's existing ``runtime_status`` helpers,
  * the candidate-supply backlog (pattern-imminent alerts waiting),
  * deterministic recommended actions derived from the above.

Reuses main's existing pieces (``runtime_status`` + the ``AutoTraderRun`` audit
table); no parallel system, no LLM, no writes. The unified *in-app* view is the
new value — previously this lived only in scattered ``analyze_*`` CLI scripts
plus the ``/monitor/cash-deployment`` endpoint.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ...models.trading import AutoTraderRun
from .runtime_status import autotrader_status, circuit_breaker_status, kill_switch_status

logger = logging.getLogger(__name__)

_PLACED_DECISIONS = {"placed", "scaled_in"}
_BLOCKING_DECISIONS = {"blocked", "skipped"}
_NONE_REASON = "(none)"


def _pct(n: int, total: int) -> float:
    return round((n / total) * 100.0, 1) if total else 0.0


def _recommended_actions(
    funnel: dict[str, Any],
    gates: dict[str, Any],
    supply: dict[str, Any],
) -> list[str]:
    """Deterministic, data-driven hints — no LLM, no stub."""
    actions: list[str] = []
    ks = gates.get("kill_switch") or {}
    cb = gates.get("circuit_breaker") or {}
    at = gates.get("autotrader") or {}

    if ks.get("active"):
        why = f" — {ks.get('reason')}" if ks.get("reason") else ""
        actions.append(
            f"Kill switch is ACTIVE{why}. No automated trades until reset "
            "(see docs/KILL_SWITCH_RUNBOOK.md)."
        )
    if cb.get("tripped"):
        why = f" — {cb.get('reason')}" if cb.get("reason") else ""
        actions.append(
            f"Drawdown circuit breaker is TRIPPED{why}. Trades blocked until "
            "manual reset (see docs/DRAWDOWN_BREAKER_RUNBOOK.md)."
        )
    total = funnel.get("total_runs") or 0
    if total == 0:
        # enabled/live_enabled are PROCESS-LOCAL config: this report may be served by a
        # non-autotrader process (e.g. the web container) whose config disables the
        # autotrader even while the autotrader process itself is running. Only surface
        # them as a cause when there is also no activity in the window to contradict them.
        if not at.get("enabled"):
            actions.append(
                "AutoTrader shows disabled in this process (chili_autotrader_enabled=false) "
                "and produced no runs in the window."
            )
        elif not at.get("live_enabled"):
            actions.append(
                "AutoTrader shows LIVE trading OFF in this process "
                "(chili_autotrader_live_enabled=false) and produced no runs in the window."
            )
        actions.append(
            "No AutoTrader runs in the window — check scanner supply and the brain "
            "worker (no pattern-imminent alerts are being processed)."
        )
    else:
        top = funnel.get("top_blockers") or []
        if top:
            t0 = top[0]
            actions.append(
                f"Top blocker: '{t0['reason']}' ({t0['decision']}, "
                f"{t0.get('pct_of_total', 0)}% of runs) — the dominant reason trades "
                "aren't being placed."
            )

    stale = supply.get("stale_unprocessed")
    if isinstance(stale, int) and stale > 0:
        actions.append(
            f"{stale} pattern-imminent alerts are stale/unprocessed — the AutoTrader "
            "tick may be behind (check the brain/scheduler workers)."
        )
    unproc = supply.get("unprocessed_alerts")
    if (
        isinstance(unproc, int)
        and unproc == 0
        and total
        and (funnel.get("placement_rate_pct") or 0) < 5
    ):
        actions.append(
            "Candidate supply is ~0 and placement rate is low — pattern SUPPLY is the "
            "throughput constraint, not the gates."
        )

    if not actions:
        actions.append("No blocking conditions detected; AutoTrader deployment looks healthy.")
    return actions


def build_autotrader_deployment_report(
    db: Session,
    *,
    user_id: Optional[int] = None,
    hours: int = 24,
    top_n: int = 12,
) -> dict[str, Any]:
    """Build the read-only AutoTrader deployment report. No writes, no LLM."""
    hours = max(1, min(int(hours or 24), 720))
    top_n = max(1, min(int(top_n or 12), 50))
    since = datetime.utcnow() - timedelta(hours=hours)

    q = db.query(
        AutoTraderRun.decision,
        AutoTraderRun.reason,
        func.count(AutoTraderRun.id),
        func.max(AutoTraderRun.created_at),
    ).filter(AutoTraderRun.created_at >= since)
    if user_id is not None:
        q = q.filter(AutoTraderRun.user_id == user_id)
    rows = q.group_by(AutoTraderRun.decision, AutoTraderRun.reason).all()

    total = sum(int(r[2] or 0) for r in rows)
    by_decision: dict[str, int] = {}
    reason_rows: list[dict[str, Any]] = []
    for decision, reason, n, latest in rows:
        d = str(decision or "unknown")
        cnt = int(n or 0)
        by_decision[d] = by_decision.get(d, 0) + cnt
        reason_rows.append(
            {
                "decision": d,
                "reason": (str(reason).strip() if reason else "") or _NONE_REASON,
                "count": cnt,
                "latest": latest.isoformat() if latest is not None else None,
            }
        )

    placed = sum(c for d, c in by_decision.items() if d in _PLACED_DECISIONS)
    top_blockers = sorted(
        (r for r in reason_rows if r["decision"] in _BLOCKING_DECISIONS),
        key=lambda r: r["count"],
        reverse=True,
    )[:top_n]
    for r in top_blockers:
        r["pct_of_total"] = _pct(r["count"], total)

    # Drill-down: attach the most recent rule_snapshot sample for the top blockers
    # (the per-instance forensics the GROUP-BY CLI scripts don't surface).
    for r in top_blockers[:3]:
        filters = [
            AutoTraderRun.decision == r["decision"],
            AutoTraderRun.created_at >= since,
        ]
        if r["reason"] == _NONE_REASON:
            filters.append(or_(AutoTraderRun.reason.is_(None), AutoTraderRun.reason == ""))
        else:
            filters.append(AutoTraderRun.reason == r["reason"])
        if user_id is not None:
            filters.append(AutoTraderRun.user_id == user_id)
        sample = (
            db.query(AutoTraderRun.ticker, AutoTraderRun.created_at, AutoTraderRun.rule_snapshot)
            .filter(*filters)
            .order_by(AutoTraderRun.created_at.desc())
            .first()
        )
        if sample is not None:
            r["sample"] = {
                "ticker": sample[0],
                "at": sample[1].isoformat() if sample[1] is not None else None,
                "rule_snapshot": sample[2] if isinstance(sample[2], dict) else None,
            }

    decision_funnel = {
        "total_runs": total,
        "by_decision": {
            d: {"count": c, "pct": _pct(c, total)}
            for d, c in sorted(by_decision.items(), key=lambda kv: kv[1], reverse=True)
        },
        "placement_rate_pct": _pct(placed, total),
        "top_blockers": top_blockers,
    }

    # Live gate status — reuse main's helpers (the light subset: no broker/market
    # external calls, unlike the full get_runtime_overview).
    at = autotrader_status(db)
    ks = kill_switch_status()
    cb = circuit_breaker_status()
    gates = {
        "autotrader": {
            "state": at.get("state"),
            "enabled": at.get("enabled"),
            "live_enabled": at.get("live_enabled"),
            # enabled/live_enabled reflect THIS serving process's config (process-local);
            # active_in_window is the container-independent ground truth (runs exist).
            "config_is_process_local": True,
            "active_in_window": total > 0,
            "latest_run_at": at.get("latest_run_at"),
            "latest_run_age_seconds": at.get("latest_run_age_seconds"),
        },
        "kill_switch": {
            "active": ks.get("active"),
            "reason": ks.get("reason"),
            "set_at": ks.get("set_at"),
        },
        "circuit_breaker": {
            "tripped": cb.get("tripped"),
            "reason": cb.get("reason"),
        },
    }
    supply = {
        "unprocessed_alerts": at.get("unprocessed_alerts_after_last_run"),
        "unprocessed_stock": at.get("unprocessed_stock_alerts_after_last_run"),
        "unprocessed_crypto": at.get("unprocessed_crypto_alerts_after_last_run"),
        "stale_unprocessed": at.get("stale_unprocessed_alerts"),
        "latest_run_at": at.get("latest_run_at"),
        "latest_run_age_seconds": at.get("latest_run_age_seconds"),
    }

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "window": {"hours": hours, "since": since.isoformat()},
        "user_id": user_id,
        "decision_funnel": decision_funnel,
        "gates": gates,
        "candidate_supply": supply,
        "recommended_actions": _recommended_actions(decision_funnel, gates, supply),
    }
