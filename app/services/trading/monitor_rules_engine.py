"""Self-learning rules engine for the pattern position monitor.

Replaces LLM dependency with learned decision rules, graduated per pattern
type.  Works alongside the LLM in shadow mode until accuracy is proven.

Key concepts:
- Signal signature: coarse-grained encoding of the current market state
  relative to the trade (health, PnL, proximity to stop/target, etc.)
- Decision rule: learned mapping from signal signature -> action + level ratios
- Graduation: per-pattern-type lifecycle (bootstrap -> shadow -> graduated -> demoted)
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Graduation thresholds (overridable via adaptive weights) ──────────

BOOTSTRAP_MIN_SAMPLES = 10
SHADOW_MIN_SAMPLES = 30
GRADUATION_AGREEMENT_MIN = 0.80
GRADUATION_BENEFIT_MIN = 0.55
RETRAIN_BENEFIT_MAX = 0.45
REGRESSION_BENEFIT_FLOOR = 0.40
REGRESSION_WINDOW = 10

SHADOW_LLM_SAMPLE_RATE = 0.20        # 1 in 5 evaluations
GRADUATED_LLM_SAMPLE_RATE = 0.05     # 1 in 20 evaluations

SIMPLE_CONDITION_THRESHOLD = 5


# ── Signal Signature ─────────────────────────────────────────────────

def _band(value: float | None, edges: list[float]) -> int:
    """Bucket a continuous value into a discrete band index."""
    if value is None:
        return -1
    for i, edge in enumerate(edges):
        if value < edge:
            return i
    return len(edges)


@dataclass
class SignalSnapshot:
    """All the inputs needed to compute a signal signature."""
    has_critical_invalidation: bool = False
    has_any_invalidation: bool = False
    caution_changed: bool = False
    health_score: float = 1.0
    health_delta: float | None = None
    pnl_pct: float | None = None
    price_vs_stop_pct: float | None = None
    price_vs_target_pct: float | None = None


def compute_signal_signature(snap: SignalSnapshot) -> str:
    """Produce a coarse-grained string key for rule lookup.

    Format: ``crit:{0|1}|inv:{0|1}|caut:{0|1}|hs:{band}|hd:{band}|pnl:{band}|ps:{band}|pt:{band}``
    """
    parts = [
        f"crit:{int(snap.has_critical_invalidation)}",
        f"inv:{int(snap.has_any_invalidation)}",
        f"caut:{int(snap.caution_changed)}",
        f"hs:{_band(snap.health_score, [0.2, 0.4, 0.6, 0.8])}",
        f"hd:{_band(snap.health_delta, [-0.3, -0.1, 0.0, 0.1])}",
        f"pnl:{_band(snap.pnl_pct, [-20, -10, -5, 0, 5, 10, 20])}",
        f"ps:{_band(snap.price_vs_stop_pct, [2, 5, 10, 20])}",
        f"pt:{_band(snap.price_vs_target_pct, [5, 15, 30, 50])}",
    ]
    return "|".join(parts)


def build_signal_snapshot(
    *,
    plan_health,       # TradePlanHealth
    condition_health,  # ConditionHealth
    pnl_pct: float | None,
    current_price: float,
    stop_price: float | None,
    target_price: float | None,
) -> SignalSnapshot:
    """Build a SignalSnapshot from the available evaluation data."""
    ps_pct = None
    if stop_price and current_price and stop_price > 0:
        ps_pct = ((current_price - stop_price) / stop_price) * 100

    pt_pct = None
    if target_price and current_price and target_price > 0:
        pt_pct = ((target_price - current_price) / current_price) * 100

    return SignalSnapshot(
        has_critical_invalidation=getattr(plan_health, "has_critical_invalidation", False),
        has_any_invalidation=getattr(plan_health, "has_any_invalidation", False),
        caution_changed=bool(getattr(plan_health, "caution_signals_changed", [])),
        health_score=getattr(condition_health, "health_score", 1.0),
        health_delta=getattr(condition_health, "health_delta", None),
        pnl_pct=pnl_pct,
        price_vs_stop_pct=ps_pct,
        price_vs_target_pct=pt_pct,
    )


# ── Mechanical Decision ──────────────────────────────────────────────

@dataclass
class MechanicalDecision:
    action: str = "hold"
    new_stop: float | None = None
    new_target: float | None = None
    confidence: float = 0.0
    reasoning: str = ""
    rule_id: int | None = None
    graduation_status: str = "bootstrap"


def lookup_rule(
    db: Session,
    pattern_type: str,
    signal_sig: str,
) -> MechanicalDecision | None:
    """Look up a learned decision rule for this pattern type + signal."""
    from ...models.trading import MonitorDecisionRule

    rule = (
        db.query(MonitorDecisionRule)
        .filter(
            MonitorDecisionRule.pattern_type == pattern_type,
            MonitorDecisionRule.signal_signature == signal_sig,
        )
        .first()
    )
    if not rule or rule.sample_count < BOOTSTRAP_MIN_SAMPLES:
        return None

    return MechanicalDecision(
        action=rule.action,
        new_stop=None,
        new_target=None,
        confidence=rule.benefit_rate,
        reasoning=f"Mechanical rule (n={rule.sample_count}, benefit={rule.benefit_rate:.2f})",
        rule_id=rule.id,
        graduation_status=rule.graduation_status,
    )


def apply_level_ratios(
    decision: MechanicalDecision,
    rule_id: int,
    current_price: float,
    pattern_stop: float | None,
    db: Session,
) -> MechanicalDecision:
    """Fill new_stop / new_target from the rule's learned ratios."""
    from ...models.trading import MonitorDecisionRule

    rule = db.query(MonitorDecisionRule).get(rule_id)
    if not rule:
        return decision

    if decision.action == "tighten_stop" and rule.stop_ratio and current_price > 0:
        decision.new_stop = round(current_price * rule.stop_ratio, 6)
        if pattern_stop and decision.new_stop < pattern_stop:
            decision.new_stop = pattern_stop

    if decision.action == "loosen_target" and rule.target_ratio and current_price > 0:
        decision.new_target = round(current_price * rule.target_ratio, 6)

    return decision


def heuristic_adjustment(
    *,
    plan_health: Any,
    condition_health: Any,
    pnl_pct: float | None,
    current_price: float,
    current_stop: float | None,
    current_target: float | None,
    pattern_stop: float | None,
    delta_urgent: float = -0.3,
    health_healthy: float = 0.8,
    trade_direction: str = "long",
) -> MechanicalDecision | None:
    """Deterministic position action for clear-cut cases. Returns None → caller may use LLM.

    Only consulted when the orchestrator would otherwise call the premium LLM. Preserves
    quality on unambiguous risk/health combinations; ambiguous states return None.
    """
    if current_price <= 0:
        return None

    is_long = (trade_direction or "long").lower() != "short"

    has_crit = bool(getattr(plan_health, "has_critical_invalidation", False))
    has_any = bool(getattr(plan_health, "has_any_invalidation", False))
    caution = bool(getattr(plan_health, "caution_signals_changed", []))
    h_score = float(getattr(condition_health, "health_score", 1.0) or 1.0)
    h_delta = getattr(condition_health, "health_delta", None)

    # Clear risk-off: critical thesis break + meaningful loss (same pnl sign convention as monitor).
    if has_crit and pnl_pct is not None and pnl_pct < -5.0:
        return MechanicalDecision(
            action="exit_now",
            confidence=0.9,
            reasoning="Heuristic: critical invalidation with loss beyond -5% — exit.",
            graduation_status="heuristic",
        )

    # Healthy structure, no plan warnings — no premium model needed.
    if not has_any and not caution and h_score >= health_healthy:
        return MechanicalDecision(
            action="hold",
            confidence=0.88,
            reasoning="Heuristic: pattern health strong, no invalidations or caution flips — hold.",
            graduation_status="heuristic",
        )

    # Rapid deterioration very close to stop → mechanical tighten (long).
    if is_long:
        if (
            h_delta is not None
            and h_delta <= delta_urgent
            and current_stop is not None
            and current_stop > 0
            and current_price > current_stop
        ):
            dist_pct = (current_price - current_stop) / max(current_price, 1e-9) * 100.0
            if dist_pct <= 2.0:
                mid = (current_price + current_stop) / 2.0
                floor = float(pattern_stop) if pattern_stop is not None else current_stop
                new_stop = max(mid, floor, current_stop)
                if new_stop < current_price * 0.999:
                    return MechanicalDecision(
                        action="tighten_stop",
                        new_stop=round(new_stop, 6),
                        confidence=0.82,
                        reasoning=(
                            "Heuristic: urgent health delta with price within ~2% of stop — "
                            "tighten toward midpoint."
                        ),
                        graduation_status="heuristic",
                    )
        if current_target is not None and current_target > 0 and current_price >= current_target * 0.998:
            new_t = max(current_target, current_price * 1.02)
            if new_t > current_price * 1.001:
                return MechanicalDecision(
                    action="loosen_target",
                    new_target=round(new_t, 6),
                    confidence=0.78,
                    reasoning="Heuristic: spot at/above plan target — raise target to trail.",
                    graduation_status="heuristic",
                )
    else:
        # Short: stop above price
        if (
            h_delta is not None
            and h_delta <= delta_urgent
            and current_stop is not None
            and current_stop > 0
            and current_price < current_stop
        ):
            dist_pct = (current_stop - current_price) / max(current_price, 1e-9) * 100.0
            if dist_pct <= 2.0:
                mid = (current_price + current_stop) / 2.0
                cap = float(pattern_stop) if pattern_stop is not None else current_stop
                new_stop = min(mid, cap, current_stop)
                if new_stop > current_price * 1.001:
                    return MechanicalDecision(
                        action="tighten_stop",
                        new_stop=round(new_stop, 6),
                        confidence=0.82,
                        reasoning=(
                            "Heuristic: urgent health delta with price within ~2% of stop (short) — "
                            "tighten toward midpoint."
                        ),
                        graduation_status="heuristic",
                    )
        if current_target is not None and current_target > 0 and current_price <= current_target * 1.002:
            new_t = min(current_target, current_price * 0.98)
            if 0 < new_t < current_price * 0.999:
                return MechanicalDecision(
                    action="loosen_target",
                    new_target=round(new_t, 6),
                    confidence=0.78,
                    reasoning="Heuristic: spot at/below plan target (short) — lower target to trail.",
                    graduation_status="heuristic",
                )

    return None


# ── Graduation Logic ─────────────────────────────────────────────────

def get_graduation_status(
    db: Session,
    pattern_type: str,
    signal_sig: str,
) -> str:
    """Return current graduation status for this pattern+signal."""
    from ...models.trading import MonitorDecisionRule

    rule = (
        db.query(MonitorDecisionRule)
        .filter(
            MonitorDecisionRule.pattern_type == pattern_type,
            MonitorDecisionRule.signal_signature == signal_sig,
        )
        .first()
    )
    if not rule:
        return "bootstrap"
    return rule.graduation_status


def should_shadow_llm(graduation_status: str) -> bool:
    """Decide whether to call the LLM as shadow-validator this cycle."""
    if graduation_status == "bootstrap":
        return True
    if graduation_status == "shadow":
        return random.random() < SHADOW_LLM_SAMPLE_RATE
    if graduation_status == "graduated":
        return random.random() < GRADUATED_LLM_SAMPLE_RATE
    return True  # demoted -> always LLM


def is_pattern_simple(rules_json: dict | None) -> bool:
    """True if pattern has fewer than SIMPLE_CONDITION_THRESHOLD conditions."""
    if not rules_json:
        return True
    conditions = rules_json.get("conditions", [])
    return len(conditions) < SIMPLE_CONDITION_THRESHOLD


def get_complexity_band(rules_json: dict | None) -> str:
    if is_pattern_simple(rules_json):
        return "simple"
    return "complex"


# ── Rule Aggregation (called from learning cycle) ────────────────────

def aggregate_decision_outcomes(db: Session) -> dict[str, Any]:
    """Aggregate resolved PatternMonitorDecision rows into MonitorDecisionRule
    entries.  Called by learn_from_monitor_decisions.

    Returns summary stats.
    """
    from ...models.trading import PatternMonitorDecision, MonitorDecisionRule, ScanPattern
    from sqlalchemy import func

    cutoff = datetime.utcnow() - timedelta(days=90)
    rows = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.was_beneficial.isnot(None),
            PatternMonitorDecision.created_at >= cutoff,
            PatternMonitorDecision.conditions_snapshot.isnot(None),
        )
        .all()
    )
    if not rows:
        return {"rules_updated": 0, "rows_processed": 0}

    # Group by (pattern_type, signal_signature)
    buckets: dict[tuple[str, str], list] = {}
    for r in rows:
        snap = r.conditions_snapshot or {}
        if not snap:
            continue

        # Resolve pattern type
        ptype = "unknown"
        if r.scan_pattern_id:
            sp = db.query(ScanPattern).filter(ScanPattern.id == r.scan_pattern_id).first()
            if sp:
                ptype = (sp.name or f"pattern_{sp.id}")[:120]

        # Reconstruct signal snapshot from stored conditions_snapshot
        tp = snap.get("trade_plan", {})
        sig = _signature_from_snapshot(snap, tp)

        key = (ptype, sig)
        buckets.setdefault(key, []).append(r)

    rules_updated = 0
    for (ptype, sig), decisions in buckets.items():
        n = len(decisions)
        beneficial = sum(1 for d in decisions if d.was_beneficial)
        benefit_rate = beneficial / n if n else 0.0

        # Action is the most common action among beneficial decisions (or overall)
        action_counts: dict[str, int] = {}
        for d in decisions:
            action_counts[d.action] = action_counts.get(d.action, 0) + 1
        best_action = max(action_counts, key=action_counts.get) if action_counts else "hold"

        # Compute average stop/target ratios from decisions
        stop_ratios = []
        target_ratios = []
        for d in decisions:
            if d.new_stop and d.price_at_decision and d.price_at_decision > 0:
                stop_ratios.append(d.new_stop / d.price_at_decision)
            if d.new_target and d.price_at_decision and d.price_at_decision > 0:
                target_ratios.append(d.new_target / d.price_at_decision)

        avg_stop_ratio = sum(stop_ratios) / len(stop_ratios) if stop_ratios else None
        avg_target_ratio = sum(target_ratios) / len(target_ratios) if target_ratios else None

        # LLM agreement: how often mechanical_action == action (LLM)
        agreement_count = sum(
            1 for d in decisions
            if d.mechanical_action and d.mechanical_action == d.action
        )
        mech_total = sum(1 for d in decisions if d.mechanical_action)
        agreement_rate = agreement_count / mech_total if mech_total else 0.0

        # Rolling benefit (last REGRESSION_WINDOW decisions)
        recent = sorted(decisions, key=lambda d: d.created_at, reverse=True)[:REGRESSION_WINDOW]
        rolling = [bool(d.was_beneficial) for d in recent]

        # Determine graduation status
        existing = (
            db.query(MonitorDecisionRule)
            .filter(
                MonitorDecisionRule.pattern_type == ptype,
                MonitorDecisionRule.signal_signature == sig,
            )
            .first()
        )

        grad_status = _compute_graduation(
            sample_count=n,
            benefit_rate=benefit_rate,
            agreement_rate=agreement_rate,
            rolling_benefit=rolling,
            current_status=existing.graduation_status if existing else "bootstrap",
        )

        if existing:
            existing.action = best_action
            existing.stop_ratio = avg_stop_ratio
            existing.target_ratio = avg_target_ratio
            existing.sample_count = n
            existing.benefit_rate = round(benefit_rate, 4)
            existing.llm_agreement_rate = round(agreement_rate, 4)
            existing.graduation_status = grad_status
            existing.rolling_benefit = {"recent": rolling}
        else:
            db.add(MonitorDecisionRule(
                pattern_type=ptype,
                signal_signature=sig,
                action=best_action,
                stop_ratio=avg_stop_ratio,
                target_ratio=avg_target_ratio,
                sample_count=n,
                benefit_rate=round(benefit_rate, 4),
                llm_agreement_rate=round(agreement_rate, 4),
                graduation_status=grad_status,
                rolling_benefit={"recent": rolling},
            ))
        rules_updated += 1

    db.flush()
    return {"rules_updated": rules_updated, "rows_processed": len(rows)}


def _compute_graduation(
    *,
    sample_count: int,
    benefit_rate: float,
    agreement_rate: float,
    rolling_benefit: list[bool],
    current_status: str,
) -> str:
    """Determine the graduation status for a rule."""
    if sample_count < BOOTSTRAP_MIN_SAMPLES:
        return "bootstrap"

    # Regression detection for graduated rules
    if current_status == "graduated":
        if len(rolling_benefit) >= REGRESSION_WINDOW:
            rolling_rate = sum(rolling_benefit) / len(rolling_benefit)
            if rolling_rate < REGRESSION_BENEFIT_FLOOR:
                logger.info(
                    "[rules_engine] Demoting rule: rolling benefit %.2f < %.2f",
                    rolling_rate, REGRESSION_BENEFIT_FLOOR,
                )
                return "demoted"
        return "graduated"

    # Retrain if benefit is poor
    if sample_count >= SHADOW_MIN_SAMPLES and benefit_rate < RETRAIN_BENEFIT_MAX:
        return "demoted"

    # Graduate if sufficient samples + good agreement + good benefit
    if (
        sample_count >= SHADOW_MIN_SAMPLES
        and agreement_rate >= GRADUATION_AGREEMENT_MIN
        and benefit_rate >= GRADUATION_BENEFIT_MIN
    ):
        return "graduated"

    if sample_count >= BOOTSTRAP_MIN_SAMPLES:
        return "shadow"

    return "bootstrap"


def _signature_from_snapshot(snap: dict, trade_plan: dict) -> str:
    """Reconstruct a signal signature from a stored conditions_snapshot."""
    invalidations = trade_plan.get("invalidations_triggered", [])
    caution = trade_plan.get("caution_signals_changed", [])

    return compute_signal_signature(SignalSnapshot(
        has_critical_invalidation=any(
            i.get("severity") == "critical" for i in invalidations
        ),
        has_any_invalidation=len(invalidations) > 0,
        caution_changed=len(caution) > 0,
        health_score=snap.get("health_score", 1.0),
        health_delta=snap.get("health_delta"),
        pnl_pct=snap.get("pnl_pct"),
        price_vs_stop_pct=snap.get("price_vs_stop_pct"),
        price_vs_target_pct=snap.get("price_vs_target_pct"),
    ))


# ── Plan Accuracy Tracking ───────────────────────────────────────────

def update_plan_accuracy(
    db: Session,
    pattern_type: str,
    complexity_band: str,
    llm_correct: bool,
    mechanical_correct: bool,
    agreed: bool,
) -> None:
    """Increment accuracy counters for a pattern type after outcome scoring."""
    from ...models.trading import MonitorPlanAccuracy

    row = (
        db.query(MonitorPlanAccuracy)
        .filter(
            MonitorPlanAccuracy.pattern_type == pattern_type,
            MonitorPlanAccuracy.complexity_band == complexity_band,
        )
        .first()
    )
    if not row:
        row = MonitorPlanAccuracy(
            pattern_type=pattern_type,
            complexity_band=complexity_band,
        )
        db.add(row)
        db.flush()

    row.total_count += 1
    if llm_correct:
        row.llm_correct_count += 1
    if mechanical_correct:
        row.mechanical_correct_count += 1
    if agreed:
        row.agreement_count += 1

    # Check graduation
    if row.total_count >= SHADOW_MIN_SAMPLES:
        mech_accuracy = row.mechanical_correct_count / row.total_count
        if mech_accuracy >= 0.80 and (row.mechanical_correct_count / max(row.total_count, 1)) >= 0.60:
            row.graduation_status = "graduated"
        elif mech_accuracy < 0.70 and row.graduation_status == "graduated":
            row.graduation_status = "demoted"
        elif row.total_count >= BOOTSTRAP_MIN_SAMPLES:
            if row.graduation_status == "bootstrap":
                row.graduation_status = "shadow"

    db.flush()


def get_plan_graduation(db: Session, pattern_type: str, complexity_band: str) -> str:
    """Current plan-graduation status for a pattern type."""
    from ...models.trading import MonitorPlanAccuracy

    row = (
        db.query(MonitorPlanAccuracy)
        .filter(
            MonitorPlanAccuracy.pattern_type == pattern_type,
            MonitorPlanAccuracy.complexity_band == complexity_band,
        )
        .first()
    )
    return row.graduation_status if row else "bootstrap"


# ── Materiality Gate ─────────────────────────────────────────────────

def should_evaluate(
    current_price: float,
    last_price: float | None,
    current_indicators: dict[str, Any] | None,
    last_snapshot: dict | None,
    stop_price: float | None,
    target_price: float | None,
    *,
    price_change_pct: float = 1.5,
    danger_zone_pct: float = 5.0,
) -> tuple[bool, str]:
    """Lightweight materiality check.  Returns (should_run, reason).

    Called before the full evaluation to avoid unnecessary LLM/indicator work.
    """
    if last_price is None or last_price <= 0:
        return True, "first_evaluation"

    if current_price <= 0:
        return False, "invalid_price"

    # Price moved enough?
    pct_change = abs((current_price - last_price) / last_price) * 100
    if pct_change >= price_change_pct:
        return True, f"price_moved_{pct_change:.1f}pct"

    # Danger zone: near stop or target
    if stop_price and stop_price > 0:
        dist_to_stop = ((current_price - stop_price) / stop_price) * 100
        if 0 < dist_to_stop <= danger_zone_pct:
            return True, f"near_stop_{dist_to_stop:.1f}pct"
        if dist_to_stop <= 0:
            return True, "below_stop"

    if target_price and target_price > 0:
        dist_to_target = ((target_price - current_price) / current_price) * 100
        if 0 < dist_to_target <= danger_zone_pct:
            return True, f"near_target_{dist_to_target:.1f}pct"
        if dist_to_target <= 0:
            return True, "above_target"

    # Key indicator crossed a threshold?
    if current_indicators and last_snapshot:
        last_conditions = last_snapshot.get("condition_results", [])
        last_met_set = {
            c.get("indicator") for c in last_conditions if c.get("met")
        }
        current_met_set = set()
        for c in last_conditions:
            ind = c.get("indicator")
            if ind and ind in current_indicators:
                from .pattern_engine import _eval_condition
                if _eval_condition(c, current_indicators):
                    current_met_set.add(ind)

        if last_met_set != current_met_set:
            flipped = last_met_set.symmetric_difference(current_met_set)
            return True, f"indicator_flipped:{','.join(sorted(flipped)[:3])}"

    return False, "no_material_change"


# ── Neural Mesh State Updates ────────────────────────────────────────

def update_mesh_node_state(db: Session, node_id: str, state_data: dict) -> None:
    """Update a brain_node_states row with fresh local_state."""
    from ...models.trading import BrainNodeState
    try:
        ns = db.query(BrainNodeState).filter(BrainNodeState.node_id == node_id).first()
        if ns:
            current = ns.local_state or {}
            current.update(state_data)
            ns.local_state = current
            ns.updated_at = datetime.utcnow()
            db.flush()
    except Exception as e:
        logger.debug("[rules_engine] Mesh state update failed for %s: %s", node_id, e)
