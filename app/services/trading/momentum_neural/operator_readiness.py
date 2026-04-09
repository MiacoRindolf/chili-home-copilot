"""Single source of truth for momentum operator readiness (flags + composites)."""

from __future__ import annotations

from typing import Any, Optional

from ....config import settings
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import is_momentum_automation_implemented, normalize_execution_family
from ..governance import get_kill_switch_status
from ...broker_manager import get_all_broker_statuses


def _scheduler_includes_web_light() -> bool:
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role in ("all", "web")


def build_momentum_operator_readiness(
    *,
    execution_family: str = "coinbase_spot",
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """Structured readiness for paper/live momentum automation (no DB)."""
    ef = normalize_execution_family(execution_family)
    exec_impl = is_momentum_automation_implemented(ef)

    mesh_on = bool(mesh_enabled())
    neural_on = bool(settings.chili_momentum_neural_enabled)
    coinbase_adapter = bool(settings.chili_coinbase_spot_adapter_enabled)

    paper_runner = bool(settings.chili_momentum_paper_runner_enabled)
    live_runner = bool(settings.chili_momentum_live_runner_enabled)
    paper_sched_on = bool(settings.chili_momentum_paper_runner_scheduler_enabled)
    live_sched_on = bool(settings.chili_momentum_live_runner_scheduler_enabled)
    web_light = _scheduler_includes_web_light()

    paper_scheduler_would_run = web_light and paper_runner and paper_sched_on
    live_scheduler_would_run = web_light and live_runner and live_sched_on

    brokers = get_all_broker_statuses()
    coinbase_connected = bool(brokers.get("coinbase", {}).get("connected"))

    gov = get_kill_switch_status()
    kill_active = bool(gov.get("active"))
    block_paper_ks = bool(settings.chili_momentum_risk_block_paper_when_kill_switch)
    inhibit_live_gov = bool(settings.chili_momentum_risk_disable_live_if_governance_inhibit)

    broker_ready_for_live = coinbase_connected and coinbase_adapter
    if symbol and broker_ready_for_live:
        # Crypto path expects Coinbase for -USD; readiness already Coinbase-specific.
        _ = symbol

    execution_ready = exec_impl and coinbase_adapter
    governance_blocks_live = kill_active and inhibit_live_gov
    governance_blocks_paper = kill_active and block_paper_ks

    runnable_paper_now = (
        exec_impl
        and neural_on
        and paper_runner
        and not governance_blocks_paper
    )
    runnable_live_now = (
        exec_impl
        and neural_on
        and live_runner
        and broker_ready_for_live
        and execution_ready
        and not governance_blocks_live
    )

    return {
        "execution_family": ef,
        "execution_family_implemented": exec_impl,
        "mesh_enabled": mesh_on,
        "momentum_neural_enabled": neural_on,
        "coinbase_spot_adapter_enabled": coinbase_adapter,
        "broker_coinbase_connected": coinbase_connected,
        "broker_ready_for_live": broker_ready_for_live,
        "paper_runner_enabled": paper_runner,
        "live_runner_enabled": live_runner,
        "paper_runner_scheduler_enabled": paper_sched_on,
        "live_runner_scheduler_enabled": live_sched_on,
        "scheduler_role": (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower(),
        "scheduler_includes_web_light_jobs": web_light,
        "paper_scheduler_would_run": paper_scheduler_would_run,
        "live_scheduler_would_run": live_scheduler_would_run,
        "kill_switch_active": kill_active,
        "kill_switch_reason": gov.get("reason"),
        "governance_blocks_paper": governance_blocks_paper,
        "governance_blocks_live": governance_blocks_live,
        "execution_ready": execution_ready,
        "runnable_paper_now": runnable_paper_now,
        "runnable_live_now": runnable_live_now,
    }


def blocked_reason_for_session(
    *,
    mode: str,
    readiness: dict[str, Any],
    canonical_state: str,
) -> Optional[str]:
    """Why automation cannot progress (None if no blanket block)."""
    if not readiness.get("execution_family_implemented"):
        return "execution_family_not_implemented"
    if not readiness.get("momentum_neural_enabled"):
        return "momentum_neural_disabled"
    m = (mode or "").lower()
    if m == "paper":
        if readiness.get("governance_blocks_paper"):
            return "governance_kill_switch"
        if canonical_state in ("draft", "queued") and not readiness.get("paper_runner_enabled"):
            return "paper_runner_disabled"
        if canonical_state == "queued" and not readiness.get("paper_scheduler_would_run"):
            if not readiness.get("paper_runner_enabled"):
                return "paper_runner_disabled"
            return "paper_scheduler_not_running"
    if m == "live":
        if readiness.get("governance_blocks_live"):
            return "governance_kill_switch"
        if canonical_state in ("armed_pending_runner", "queued_live") and not readiness.get("live_runner_enabled"):
            return "live_runner_disabled"
        if canonical_state == "queued_live" and not readiness.get("live_scheduler_would_run"):
            if not readiness.get("live_runner_enabled"):
                return "live_runner_disabled"
            return "live_scheduler_not_running"
        if not readiness.get("broker_ready_for_live"):
            return "broker_not_ready"
    return None


def next_action_required(
    *,
    mode: str,
    state: str,
    canonical_state: str,
    readiness: dict[str, Any],
    blocked: Optional[str],
) -> str:
    """Short operator-facing CTA string."""
    if blocked == "broker_not_ready":
        return "Connect Coinbase Advanced (or fix credentials) for live execution."
    if blocked == "paper_runner_disabled" and (mode or "").lower() == "paper":
        return "Enable CHILI_MOMENTUM_PAPER_RUNNER_ENABLED (and optional scheduler) or use dev tick."
    if blocked == "live_runner_disabled" and (mode or "").lower() == "live":
        return "Enable CHILI_MOMENTUM_LIVE_RUNNER_ENABLED after confirming venue readiness."
    if blocked == "paper_scheduler_not_running":
        return "Scheduler must run web-light jobs (CHILI_SCHEDULER_ROLE=all|web) with paper runner scheduler enabled."
    if blocked == "live_scheduler_not_running":
        return "Scheduler must run web-light jobs with live runner scheduler enabled."
    if blocked == "governance_kill_switch":
        return "Governance kill-switch active — resolve before automation."
    if blocked == "execution_family_not_implemented":
        return "This execution family is not implemented for automation (coinbase_spot only today)."
    if blocked == "momentum_neural_disabled":
        return "Enable CHILI_MOMENTUM_NEURAL_ENABLED."

    if canonical_state == "live_arm_pending":
        return "Confirm live arm in the modal or cancel the pending session."
    if canonical_state == "armed_pending_runner":
        return "Live arm confirmed; enable live runner or trigger a runner tick — session is not executing yet."
    if canonical_state == "queued_live":
        return "Waiting for live runner batch/tick."
    if canonical_state == "queued":
        return "Waiting for paper runner batch/tick."
    if canonical_state == "draft":
        return "Paper session recorded; enable paper runner to advance from draft/queue."

    return "Monitor session on Trading or open Automation for full history."
