"""Single source of truth for momentum operator readiness (flags + composites)."""

from __future__ import annotations

from typing import Any, Optional

from ....config import settings
from app.services.broker_manager import get_all_broker_statuses
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    is_momentum_automation_implemented,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
)
from ..governance import get_kill_switch_status
from .feature_flags import audit_momentum_settings_fallbacks, build_momentum_feature_flag_readiness


def _scheduler_includes_web_light() -> bool:
    if getattr(settings, "chili_scheduler_runs_externally", False):
        return True
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role in ("all", "web")


def _agentic_mcp_token_bundle_status() -> dict[str, Any]:
    """Cheap, secret-safe Agentic auth-bundle status for operator diagnostics."""
    try:
        from ..venue.rh_mcp_client import _load_token_bundle, bundle_is_routable, resolve_mcp_token

        bundle = _load_token_bundle()
        token = resolve_mcp_token()
        return {
            "token_present": bool(token),
            "token_bundle_present": isinstance(bundle, dict),
            "token_bundle_routable": bool(bundle_is_routable()),
        }
    except Exception as exc:
        return {
            "token_present": False,
            "token_bundle_present": False,
            "token_bundle_routable": False,
            "token_status_error": exc.__class__.__name__,
        }


def _agentic_mcp_adapter_status() -> dict[str, Any]:
    """Auth-aware readiness for the sanctioned RH Agentic rail, with a safe reason.

    This intentionally exposes only booleans and coarse reason strings: no token,
    account number, endpoint, or raw broker text is returned.
    """
    status: dict[str, Any] = {
        "enabled": False,
        "reason": "not_checked",
        **_agentic_mcp_token_bundle_status(),
    }
    try:
        factory = resolve_live_spot_adapter_factory(EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP)
        adapter = factory()
        is_enabled = getattr(adapter, "is_enabled", None)
        if not callable(is_enabled):
            status["reason"] = "adapter_missing_is_enabled"
            return status
        enabled = bool(is_enabled())
        status["enabled"] = enabled
        if enabled:
            status["reason"] = "agentic_adapter_enabled"
            return status
        if bool(getattr(adapter, "_pin_invalid", False)):
            status["reason"] = "pinned_account_not_agentic"
            return status
        auth_error = str(getattr(adapter, "_execution_auth_error", "") or "").strip()
        if auth_error:
            prefix = "execution_auth_transient" if bool(
                getattr(adapter, "_execution_auth_transient_unavailable", False)
            ) else "execution_auth"
            status["reason"] = f"{prefix}:{auth_error[:120]}"
            return status
        if not status.get("token_present"):
            status["reason"] = "no_token"
            return status
        if status.get("token_bundle_present") and not status.get("token_bundle_routable"):
            status["reason"] = "token_bundle_not_routable"
            return status
        status["reason"] = "adapter_is_enabled_false"
        return status
    except Exception as exc:
        status["reason"] = f"adapter_status_error:{exc.__class__.__name__}"
        return status


def _agentic_mcp_adapter_enabled() -> bool:
    """Backward-compatible bool surface for tests/callers."""
    return bool(_agentic_mcp_adapter_status().get("enabled"))


def _default_readiness_execution_family(symbol: Optional[str] = None) -> str:
    """Infer the live readiness venue when callers do not pass one explicitly."""
    sym = str(symbol or "").strip().upper()
    if sym.endswith("-USD"):
        return EXECUTION_FAMILY_COINBASE_SPOT
    return str(getattr(settings, "chili_equity_execution_rail", None) or EXECUTION_FAMILY_ROBINHOOD_SPOT)


def build_momentum_operator_readiness(
    *,
    execution_family: str | None = None,
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """Structured readiness for paper/live momentum automation (no DB)."""
    ef = normalize_execution_family(execution_family or _default_readiness_execution_family(symbol))
    exec_impl = is_momentum_automation_implemented(ef)

    mesh_on = bool(mesh_enabled())
    neural_on = bool(settings.chili_momentum_neural_enabled)
    coinbase_adapter = bool(settings.chili_coinbase_spot_adapter_enabled)
    robinhood_adapter = bool(getattr(settings, "chili_robinhood_spot_adapter_enabled", False))
    is_robinhood_spot = ef == EXECUTION_FAMILY_ROBINHOOD_SPOT
    is_robinhood_agentic = ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    agentic_status = _agentic_mcp_adapter_status() if is_robinhood_agentic else {
        "enabled": False,
        "reason": "not_agentic_execution_family",
    }
    agentic_adapter = bool(agentic_status.get("enabled"))

    paper_runner = bool(settings.chili_momentum_paper_runner_enabled)
    live_runner = bool(settings.chili_momentum_live_runner_enabled)
    paper_sched_on = bool(settings.chili_momentum_paper_runner_scheduler_enabled)
    live_sched_on = bool(settings.chili_momentum_live_runner_scheduler_enabled)
    web_light = _scheduler_includes_web_light()

    paper_scheduler_would_run = web_light and paper_runner and paper_sched_on
    live_scheduler_would_run = web_light and live_runner and live_sched_on

    brokers = get_all_broker_statuses()
    coinbase_connected = bool(brokers.get("coinbase", {}).get("connected"))
    # Sell-scope preflight: a connected but view-only / buy-only Coinbase key lets
    # live ENTRIES through but blocks EXITS ("403 Missing Required Scopes" on sell).
    # Require verified TRADE permission before allowing live. docs/DESIGN/MOMENTUM_LANE.md
    coinbase_can_trade = False
    if coinbase_connected:
        try:
            from app.services.coinbase_service import can_trade as _cb_can_trade
            coinbase_can_trade = bool(_cb_can_trade())
        except Exception:
            coinbase_can_trade = False

    # Robinhood (equities + RH-crypto): an authenticated RH session implies TRADE
    # capability — there is no Coinbase-style view-only API scope — so "connected"
    # is the can-trade signal. docs/DESIGN/MOMENTUM_LANE.md
    robinhood_connected = bool(brokers.get("robinhood", {}).get("connected"))
    robinhood_can_trade = robinhood_connected

    gov = get_kill_switch_status()
    kill_active = bool(gov.get("active"))
    block_paper_ks = bool(settings.chili_momentum_risk_block_paper_when_kill_switch)
    inhibit_live_gov = bool(settings.chili_momentum_risk_disable_live_if_governance_inhibit)

    # Broker readiness is per-venue: a robinhood_spot session must NOT be gated on
    # Coinbase (it would block with "connect Coinbase" even when RH is trade-ready).
    if is_robinhood_agentic:
        broker_ready_for_live = agentic_adapter
        execution_ready = exec_impl and agentic_adapter
    elif is_robinhood_spot:
        broker_ready_for_live = robinhood_connected and robinhood_adapter and robinhood_can_trade
        execution_ready = exec_impl and robinhood_adapter
    else:
        broker_ready_for_live = coinbase_connected and coinbase_adapter and coinbase_can_trade
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
        "robinhood_spot_adapter_enabled": robinhood_adapter,
        "robinhood_agentic_mcp_adapter_enabled": agentic_adapter,
        "robinhood_agentic_mcp_adapter_reason": agentic_status.get("reason"),
        "robinhood_agentic_mcp_token_present": bool(agentic_status.get("token_present")),
        "robinhood_agentic_mcp_token_bundle_present": bool(agentic_status.get("token_bundle_present")),
        "robinhood_agentic_mcp_token_bundle_routable": bool(agentic_status.get("token_bundle_routable")),
        "broker_coinbase_connected": coinbase_connected,
        "broker_coinbase_can_trade": coinbase_can_trade,
        "broker_robinhood_connected": robinhood_connected,
        "broker_robinhood_can_trade": robinhood_can_trade,
        "broker_robinhood_agentic_connected": agentic_adapter,
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
        "momentum_feature_flags": build_momentum_feature_flag_readiness(settings),
        "momentum_settings_fallback_audit": {
            key: value
            for key, value in audit_momentum_settings_fallbacks(settings).items()
            if key != "rows" and key != "missing_rows"
        },
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
        re_block = readiness.get("_repeatable_edge_block_live")
        if re_block:
            return str(re_block)
        alloc_block = readiness.get("_allocator_block_live")
        if alloc_block:
            return str(alloc_block)
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
        ef = readiness.get("execution_family") or ""
        if ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
            return "Refresh Robinhood Agentic MCP auth/token and verify the pinned Agentic account before live equity execution."
        if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT:
            return "Connect Robinhood + enable CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED for live equity execution."
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
    if blocked == "execution_robustness_critical":
        return "Linked scan pattern execution robustness is critical — live blocked by policy."
    if blocked == "same_ticker_conflict":
        return "Portfolio allocator sees an existing same-symbol live bet with better or equivalent quality."
    if blocked == "sector_cap":
        return "Portfolio allocator hit the sector concentration cap for this live request."
    if blocked == "correlation_bucket_cap":
        return "Portfolio allocator hit the correlation bucket cap for this live request."
    if blocked == "quality_stack_critical":
        return "Allocator suppressed this candidate because drift and execution quality are both critical."

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
