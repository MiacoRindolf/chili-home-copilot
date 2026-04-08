"""Evaluate momentum automation sessions against config policy + governance (Phase 6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumStrategyVariant, MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
    is_documented_execution_family,
    is_momentum_automation_implemented,
    normalize_execution_family,
)
from ..governance import get_kill_switch_status, is_kill_switch_active
from .live_fsm import LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY
from .paper_fsm import LIVE_INTENT_STATES, PAPER_CONCURRENT_STATES
from .risk_policy import MomentumAutomationRiskPolicy, POLICY_VERSION, resolve_effective_risk_policy

# Count toward concurrency limits (pre-runner + paper/live runner actives until terminal).
_CONCURRENT_STATES = (
    frozenset(PAPER_CONCURRENT_STATES) | frozenset(LIVE_INTENT_STATES) | frozenset(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY)
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _check(
    cid: str,
    ok: bool,
    *,
    severity: str,
    message: str,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "severity": severity, "message": message, "detail": detail or {}}


def count_concurrent_automation_sessions(
    db: Session,
    *,
    user_id: int,
    mode: Optional[str] = None,
    exclude_session_id: Optional[int] = None,
) -> int:
    """Active pre-runner sessions only (cancelled/archived/expired excluded by state set)."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.state.in_(_CONCURRENT_STATES),
    )
    if mode in ("paper", "live"):
        q = q.filter(TradingAutomationSession.mode == mode)
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    return int(q.count())


def _viability_age_seconds(via: MomentumSymbolViability) -> float:
    ts = via.freshness_ts
    if ts is None:
        return 1e9
    if ts.tzinfo:
        ts = ts.replace(tzinfo=None)
    return max(0.0, (_utcnow() - ts).total_seconds())


def _readiness_numbers(exec_json: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not exec_json:
        return out
    for k in (
        "spread_bps",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "product_tradable",
        "extra",
    ):
        if k in exec_json:
            out[k] = exec_json.get(k)
    ex = exec_json.get("extra")
    if isinstance(ex, dict):
        for k2 in ("spread_bps", "market_data_retrieved_at_utc", "market_data_max_age_seconds"):
            if k2 in ex and k2 not in out:
                out[k2] = ex[k2]
    return out


def evaluate_proposed_momentum_automation(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    variant_id: int,
    mode: str,
    execution_family: str = "coinbase_spot",
    exclude_session_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Server-side risk gate for operator flows (paper draft, live arm, confirm).

    Returns stable dict: allowed, severity, checks, warnings, errors, governance_state, ...
    Archived/expired/cancelled sessions do not count toward concurrency (query filter).
    """
    policy = MomentumAutomationRiskPolicy.from_settings()
    sym = symbol.strip().upper()
    m = mode.lower().strip()
    ef = normalize_execution_family(execution_family)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    gov = get_kill_switch_status()
    governance_state = {"kill_switch_active": bool(gov.get("active")), "kill_switch_reason": gov.get("reason")}

    # ── Governance / kill switch ──────────────────────────────────────
    if is_kill_switch_active():
        if m == "live" and policy.disable_live_if_governance_inhibit:
            checks.append(
                _check(
                    "governance_kill_switch",
                    False,
                    severity="block",
                    message="Kill switch active — live automation progression blocked.",
                    detail=governance_state,
                )
            )
        elif m == "paper" and policy.block_paper_when_kill_switch:
            checks.append(
                _check(
                    "governance_kill_switch_paper",
                    False,
                    severity="block",
                    message="Kill switch active — paper automation blocked by policy.",
                    detail=governance_state,
                )
            )
        else:
            checks.append(
                _check(
                    "governance_kill_switch",
                    True,
                    severity="ok",
                    message="Kill switch active but mode not blocked by policy.",
                    detail=governance_state,
                )
            )
    else:
        checks.append(
            _check("governance_kill_switch", True, severity="ok", message="Kill switch inactive.", detail=gov)
        )

    # ── Execution family (strategy logic vs routing seam — Phase 11) ─────
    if not is_documented_execution_family(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"Unknown execution_family {ef!r} (not in documented registry).",
                detail={"execution_family": ef},
            )
        )
    elif not is_momentum_automation_implemented(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"execution_family {ef!r} is documented but not implemented yet.",
                detail={"execution_family": ef},
            )
        )
    else:
        checks.append(
            _check(
                "execution_family",
                True,
                severity="ok",
                message="execution_family supported for momentum automation.",
                detail={"execution_family": ef},
            )
        )

    v_row = (
        db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(variant_id)).one_or_none()
    )
    if v_row is not None:
        vef = normalize_execution_family(v_row.execution_family)
        if vef != ef:
            checks.append(
                _check(
                    "execution_family_variant_alignment",
                    False,
                    severity="block",
                    message="Requested execution_family does not match variant.execution_family.",
                    detail={"request": ef, "variant_execution_family": vef, "variant_id": int(variant_id)},
                )
            )
        else:
            checks.append(
                _check(
                    "execution_family_variant_alignment",
                    True,
                    severity="ok",
                    message="execution_family matches variant row.",
                    detail={"execution_family": ef},
                )
            )

    # ── Viability row ───────────────────────────────────────────────────
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == int(variant_id))
        .one_or_none()
    )
    viability_state: dict[str, Any] = {"row_present": via is not None}
    freshness_state: dict[str, Any] = {"viability_age_sec": None, "fresh": False}
    if not via:
        checks.append(
            _check(
                "viability_present",
                False,
                severity="block",
                message="No durability viability row for symbol/variant.",
            )
        )
    else:
        viability_state.update(
            {
                "viability_score": via.viability_score,
                "paper_eligible": via.paper_eligible,
                "live_eligible": via.live_eligible,
                "freshness_ts": via.freshness_ts.isoformat() if via.freshness_ts else None,
            }
        )
        age = _viability_age_seconds(via)
        fresh = not policy.require_fresh_viability or age <= policy.viability_max_age_seconds
        freshness_state = {"viability_age_sec": round(age, 3), "fresh": fresh}
        checks.append(
            _check(
                "viability_present",
                True,
                severity="ok",
                message="Viability row present.",
            )
        )
        if policy.require_fresh_viability and not fresh:
            sev = "block" if m == "live" else "warn"
            checks.append(
                _check(
                    "viability_freshness",
                    False,
                    severity=sev,
                    message=f"Viability snapshot stale (age {age:.0f}s > max {policy.viability_max_age_seconds}s).",
                    detail=freshness_state,
                )
            )
        else:
            checks.append(
                _check(
                    "viability_freshness",
                    True,
                    severity="ok",
                    message="Viability freshness within policy.",
                    detail=freshness_state,
                )
            )

        if m == "paper":
            ok_pe = bool(via.paper_eligible)
            checks.append(
                _check(
                    "paper_eligible",
                    ok_pe,
                    severity="block" if not ok_pe else "ok",
                    message="Paper eligible" if ok_pe else "Not paper-eligible per neural viability.",
                )
            )
        if m == "live":
            ok_le = bool(via.live_eligible)
            if policy.require_live_eligible_for_live:
                checks.append(
                    _check(
                        "live_eligible",
                        ok_le,
                        severity="block" if not ok_le else "ok",
                        message="Live eligible" if ok_le else "Not live-eligible per neural viability.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "live_eligible",
                        ok_le,
                        severity="warn" if not ok_le else "ok",
                        message="Live eligibility optional by policy.",
                    )
                )

        # ── Execution readiness (spread / slip / fee) ──────────────────
        ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        nums = _readiness_numbers(ex)
        max_spread = policy.max_spread_bps_live if m == "live" else policy.max_spread_bps_paper
        spread = nums.get("spread_bps")
        if spread is not None:
            try:
                sb = float(spread)
                ok_sp = sb <= max_spread
                checks.append(
                    _check(
                        "spread_bps",
                        ok_sp,
                        severity="block" if not ok_sp and m == "live" else ("warn" if not ok_sp else "ok"),
                        message=f"Spread {sb} bps vs max {max_spread} ({m}).",
                        detail={"spread_bps": sb, "max": max_spread},
                    )
                )
            except (TypeError, ValueError):
                checks.append(
                    _check(
                        "spread_bps",
                        False,
                        severity="warn",
                        message="Spread bps missing or invalid in readiness JSON.",
                    )
                )
        else:
            checks.append(
                _check(
                    "spread_bps",
                    False,
                    severity="warn" if m == "live" else "ok",
                    message="No spread_bps in viability execution readiness (cannot enforce cap).",
                )
            )

        slip = nums.get("slippage_estimate_bps")
        if slip is not None:
            try:
                sl = float(slip)
                ok_sl = sl <= policy.max_estimated_slippage_bps
                checks.append(
                    _check(
                        "slippage_estimate_bps",
                        ok_sl,
                        severity="block" if not ok_sl and m == "live" else ("warn" if not ok_sl else "ok"),
                        message=f"Slippage est {sl} bps vs max {policy.max_estimated_slippage_bps}.",
                    )
                )
            except (TypeError, ValueError):
                pass
        else:
            warnings.append("slippage_estimate_bps not present — cap not enforced.")

        fee = nums.get("fee_to_target_ratio")
        if fee is not None:
            try:
                fr = float(fee)
                ok_f = fr <= policy.max_fee_to_target_ratio
                checks.append(
                    _check(
                        "fee_to_target_ratio",
                        ok_f,
                        severity="block" if not ok_f and m == "live" else ("warn" if not ok_f else "ok"),
                        message=f"Fee/target {fr:.3f} vs max {policy.max_fee_to_target_ratio:.3f}.",
                    )
                )
            except (TypeError, ValueError):
                pass

        pt = nums.get("product_tradable")
        if pt is False and m == "live":
            checks.append(
                _check(
                    "product_tradable",
                    False,
                    severity="block",
                    message="Product marked not tradable in readiness metadata.",
                )
            )

        # Strict Coinbase freshness (optional)
        if policy.require_strict_coinbase_freshness and settings.chili_coinbase_strict_freshness:
            max_age = float(
                min(policy.stale_market_data_max_age_sec, settings.chili_coinbase_market_data_max_age_sec)
            )
            md_age = nums.get("market_data_max_age_seconds")
            if md_age is not None:
                try:
                    mda = float(md_age)
                    ok_md = mda <= max_age
                    checks.append(
                        _check(
                            "market_data_freshness",
                            ok_md,
                            severity="block" if not ok_md and m == "live" else ("warn" if not ok_md else "ok"),
                            message=f"Market data age {mda}s vs max {max_age}s.",
                        )
                    )
                except (TypeError, ValueError):
                    pass
            else:
                checks.append(
                    _check(
                        "market_data_freshness",
                        False,
                        severity="warn",
                        message="Strict freshness requested but market_data_max_age_seconds missing.",
                    )
                )

    # ── Concurrency ─────────────────────────────────────────────────────
    total_ct = count_concurrent_automation_sessions(db, user_id=user_id, exclude_session_id=exclude_session_id)
    ok_tot = total_ct < policy.max_concurrent_sessions
    checks.append(
        _check(
            "max_concurrent_sessions",
            ok_tot,
            severity="block" if not ok_tot else "ok",
            message=f"Concurrent sessions {total_ct} / max {policy.max_concurrent_sessions}.",
            detail={"count": total_ct},
        )
    )
    if m == "live":
        live_ct = count_concurrent_automation_sessions(
            db, user_id=user_id, mode="live", exclude_session_id=exclude_session_id
        )
        ok_lv = live_ct < policy.max_concurrent_live_sessions
        checks.append(
            _check(
                "max_concurrent_live_sessions",
                ok_lv,
                severity="block" if not ok_lv else "ok",
                message=f"Concurrent live sessions {live_ct} / max {policy.max_concurrent_live_sessions}.",
                detail={"count": live_ct},
            )
        )

    # ── PnL / notional (deferred — runner does not track yet) ───────────
    checks.append(
        _check(
            "daily_loss_cap",
            True,
            severity="warn",
            message="Daily loss / per-trade loss caps not enforced until runner PnL (Phase 7+).",
            detail={"max_daily_loss_usd": policy.max_daily_loss_usd},
        )
    )
    checks.append(
        _check(
            "notional_cap",
            True,
            severity="warn",
            message="Max notional per trade not enforced until orders (Phase 7+).",
            detail={"max_notional_per_trade_usd": policy.max_notional_per_trade_usd},
        )
    )

    # ── Aggregate severity ────────────────────────────────────────────────
    has_block = any(c.get("severity") == "block" and not c.get("ok") for c in checks)
    has_warn = any(c.get("severity") == "warn" and not c.get("ok") for c in checks)
    allowed = not has_block
    if has_block:
        severity = "block"
    elif has_warn:
        severity = "warn"
    else:
        severity = "ok"

    for c in checks:
        if not c.get("ok") and c.get("severity") == "warn":
            warnings.append(str(c.get("message", "")))
        if not c.get("ok") and c.get("severity") == "block":
            errors.append(str(c.get("message", "")))

    evaluated_at = datetime.now(timezone.utc).isoformat()
    return {
        "allowed": allowed,
        "severity": severity,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "effective_policy_summary": {
            "policy_version": POLICY_VERSION,
            "mode": m,
            "execution_family": ef,
            "max_spread_bps": policy.max_spread_bps_live if m == "live" else policy.max_spread_bps_paper,
            "max_concurrent_sessions": policy.max_concurrent_sessions,
            "max_concurrent_live_sessions": policy.max_concurrent_live_sessions,
        },
        "governance_state": governance_state,
        "freshness_state": freshness_state,
        "viability_state": viability_state,
        "evaluated_at_utc": evaluated_at,
    }


def evaluate_existing_automation_session(
    db: Session,
    *,
    user_id: int,
    session_id: int,
) -> dict[str, Any]:
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {
            "allowed": False,
            "severity": "block",
            "checks": [_check("session", False, severity="block", message="Session not found.")],
            "warnings": [],
            "errors": ["Session not found."],
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    return evaluate_proposed_momentum_automation(
        db,
        user_id=user_id,
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode=sess.mode,
        execution_family=sess.execution_family,
        exclude_session_id=int(sess.id),
    )


def summarize_risk_from_snapshot(snap: Any) -> dict[str, Any]:
    """Light read-model for list views (persisted evaluation only)."""
    if not isinstance(snap, dict):
        return {"severity": "unknown", "allowed": True, "reasons": []}
    mr = snap.get("momentum_risk")
    if not isinstance(mr, dict):
        return {"severity": "unknown", "allowed": True, "reasons": ["no_risk_evaluation_stored"]}
    reasons = list(mr.get("errors") or [])[:4]
    reasons.extend(list(mr.get("warnings") or [])[:2])
    return {
        "severity": mr.get("severity", "unknown"),
        "allowed": bool(mr.get("allowed", True)),
        "evaluated_at_utc": mr.get("evaluated_at_utc"),
        "reasons": reasons[:6],
        "governance_inhibit": bool((mr.get("governance_state") or {}).get("kill_switch_active")),
    }
