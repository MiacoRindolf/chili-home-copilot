"""Single source of truth for momentum operator readiness (flags + composites)."""

from __future__ import annotations

from typing import Any, Optional

from ....config import settings
from app.services.broker_manager import get_all_broker_statuses
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SHORT,
    EXECUTION_FAMILY_ALPACA_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    is_momentum_automation_implemented,
    normalize_execution_family,
)
from ..governance import get_kill_switch_status
from .alpaca_orphan_claims import (
    alpaca_asset_class_is_crypto,
    alpaca_symbol_is_crypto_like,
)
from .lane_health import live_runner_driver_configuration


def _scheduler_includes_web_light() -> bool:
    if getattr(settings, "chili_scheduler_runs_externally", False):
        return True
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role in ("all", "web")


def _scheduler_includes_momentum_exec() -> bool:
    if getattr(settings, "chili_scheduler_runs_externally", False):
        return True
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role in ("all", "web", "worker", "cron_only", "momentum_exec_only")


def _local_process_includes_momentum_exec() -> bool:
    """Whether this process role can provide runtime owner evidence.

    ``chili_scheduler_runs_externally`` describes the deployment, not proof that
    the separate process is alive.  Do not let that flag turn an absent heartbeat
    into a green local/runtime signal.
    """
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role in ("all", "web", "worker", "cron_only", "momentum_exec_only")


def build_momentum_operator_readiness(
    *,
    execution_family: str = "coinbase_spot",
    symbol: Optional[str] = None,
    asset_class: Optional[str] = None,
) -> dict[str, Any]:
    """Structured readiness for paper/live momentum automation (no DB)."""
    ef = normalize_execution_family(execution_family)
    exec_impl = is_momentum_automation_implemented(ef)

    mesh_on = bool(mesh_enabled())
    neural_on = bool(settings.chili_momentum_neural_enabled)
    coinbase_adapter = bool(settings.chili_coinbase_spot_adapter_enabled)
    robinhood_adapter = bool(getattr(settings, "chili_robinhood_spot_adapter_enabled", False))
    alpaca_adapter = bool(getattr(settings, "chili_alpaca_enabled", False))
    is_robinhood = ef == EXECUTION_FAMILY_ROBINHOOD_SPOT
    is_alpaca = ef in {
        EXECUTION_FAMILY_ALPACA_SPOT,
        EXECUTION_FAMILY_ALPACA_SHORT,
    }
    alpaca_quarantine_reason: str | None = None
    if is_alpaca:
        if not bool(getattr(settings, "chili_alpaca_paper", True)):
            alpaca_quarantine_reason = "alpaca_live_posture_not_certified"
        elif (
            alpaca_symbol_is_crypto_like(symbol)
            or alpaca_asset_class_is_crypto(asset_class)
        ):
            alpaca_quarantine_reason = "alpaca_crypto_execution_not_certified"
        elif ef == EXECUTION_FAMILY_ALPACA_SHORT:
            alpaca_quarantine_reason = "alpaca_short_execution_not_certified"

    paper_runner = bool(settings.chili_momentum_paper_runner_enabled)
    live_runner = bool(settings.chili_momentum_live_runner_enabled)
    paper_sched_on = bool(settings.chili_momentum_paper_runner_scheduler_enabled)
    live_sched_on = bool(settings.chili_momentum_live_runner_scheduler_enabled)
    live_loop_on = bool(
        getattr(settings, "chili_momentum_live_runner_loop_enabled", False)
    )
    price_bus_on = bool(getattr(settings, "chili_autopilot_price_bus_enabled", False))
    web_light = _scheduler_includes_web_light()
    momentum_exec = _scheduler_includes_momentum_exec()

    paper_scheduler_would_run = web_light and paper_runner and paper_sched_on
    posture_mode, posture_error = live_runner_driver_configuration()
    live_driver_mode = (
        "scheduled_batch"
        if posture_mode == "scheduled_auto_arm"
        else posture_mode
    )
    live_driver_config_valid = bool(
        posture_error is None and live_driver_mode is not None
    )
    external_scheduler = bool(
        getattr(settings, "chili_scheduler_runs_externally", False)
    )
    local_momentum_exec = _local_process_includes_momentum_exec()
    local_loop_signal_available = bool(
        live_driver_mode == "event_loop"
        and local_momentum_exec
    )
    local_loop_running: bool | None = None
    if local_loop_signal_available:
        try:
            from .live_runner_loop import is_live_runner_loop_running

            local_loop_running = bool(is_live_runner_loop_running())
        except Exception:
            local_loop_running = False
    if live_driver_mode == "event_loop":
        if local_loop_signal_available:
            live_driver_runtime_state = (
                "running" if local_loop_running is True else "not_running"
            )
        else:
            # The DB-aware runner-health surface can later replace this unknown
            # with durable heartbeat truth. This no-DB helper must not invent it.
            live_driver_runtime_state = "unknown_external"
    else:
        live_driver_runtime_state = (
            "configured" if live_driver_config_valid else "not_configured"
        )
    live_driver_would_run = bool(
        live_runner
        and momentum_exec
        and live_driver_config_valid
        and (
            live_driver_mode != "event_loop"
            or live_driver_runtime_state == "running"
        )
    )
    # Backward-compatible response key.  In canonical event-loop mode this now
    # means "the one live driver would run", not "enable the forbidden batch".
    live_scheduler_would_run = live_driver_would_run

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

    # Alpaca is certified only for the paper/equity/long lane.  Compute quarantine
    # before constructing the adapter so live posture, crypto, and unfinished short
    # shapes cannot even perform an adapter readiness probe.
    alpaca_ready = False
    if is_alpaca and alpaca_quarantine_reason is None and alpaca_adapter:
        try:
            from ..venue.alpaca_spot import AlpacaSpotAdapter

            alpaca_ready = bool(AlpacaSpotAdapter().is_enabled())
        except Exception:
            alpaca_ready = False

    gov = get_kill_switch_status()
    kill_active = bool(gov.get("active"))
    block_paper_ks = bool(settings.chili_momentum_risk_block_paper_when_kill_switch)
    inhibit_live_gov = bool(settings.chili_momentum_risk_disable_live_if_governance_inhibit)

    # Broker readiness is per-venue: a robinhood_spot session must NOT be gated on
    # Coinbase (it would block with "connect Coinbase" even when RH is trade-ready).
    if is_robinhood:
        broker_ready_for_live = robinhood_connected and robinhood_adapter and robinhood_can_trade
        execution_ready = exec_impl and robinhood_adapter
    elif is_alpaca:
        broker_ready_for_live = bool(
            alpaca_quarantine_reason is None and alpaca_adapter and alpaca_ready
        )
        execution_ready = bool(
            alpaca_quarantine_reason is None and exec_impl and alpaca_adapter
        )
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
        and live_driver_would_run
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
        "broker_coinbase_connected": coinbase_connected,
        "broker_coinbase_can_trade": coinbase_can_trade,
        "broker_robinhood_connected": robinhood_connected,
        "broker_robinhood_can_trade": robinhood_can_trade,
        "alpaca_spot_adapter_enabled": alpaca_adapter,
        "broker_alpaca_ready": alpaca_ready,
        "execution_quarantine_reason": alpaca_quarantine_reason,
        "broker_ready_for_live": broker_ready_for_live,
        "paper_runner_enabled": paper_runner,
        "live_runner_enabled": live_runner,
        "paper_runner_scheduler_enabled": paper_sched_on,
        "live_runner_scheduler_enabled": live_sched_on,
        "live_runner_loop_enabled": live_loop_on,
        "live_runner_price_bus_enabled": price_bus_on,
        "live_driver_mode": live_driver_mode,
        "live_driver_config_valid": live_driver_config_valid,
        "live_driver_config_error": posture_error,
        "scheduler_includes_momentum_exec_jobs": momentum_exec,
        "local_process_includes_momentum_exec_jobs": local_momentum_exec,
        "live_event_loop_process_signal_available": local_loop_signal_available,
        "live_event_loop_running": local_loop_running,
        "live_driver_runtime_state": live_driver_runtime_state,
        "external_scheduler_configured": external_scheduler,
        "live_driver_would_run": live_driver_would_run,
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
    m = (mode or "").lower()
    if m == "live" and readiness.get("execution_quarantine_reason"):
        return str(readiness["execution_quarantine_reason"])
    if not readiness.get("execution_family_implemented"):
        return "execution_family_not_implemented"
    if not readiness.get("momentum_neural_enabled"):
        return "momentum_neural_disabled"
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
        if canonical_state == "queued_live" and not readiness.get("live_driver_would_run"):
            if not readiness.get("live_runner_enabled"):
                return "live_runner_disabled"
            if not readiness.get("live_driver_config_valid"):
                return "live_driver_misconfigured"
            if (
                readiness.get("live_driver_mode") == "event_loop"
                and readiness.get("live_driver_runtime_state")
                == "unknown_external"
            ):
                return "live_event_loop_health_unverified"
            if (
                readiness.get("live_driver_mode") == "event_loop"
                and readiness.get("live_event_loop_process_signal_available")
                and readiness.get("live_event_loop_running") is not True
            ):
                return "live_event_loop_not_running"
            return "live_driver_not_running"
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
        _ef_r = (readiness.get("execution_family") or "")
        if _ef_r == EXECUTION_FAMILY_ROBINHOOD_SPOT:
            return "Connect Robinhood + enable CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED for live equity execution."
        if _ef_r == EXECUTION_FAMILY_ALPACA_SPOT:
            return "Enable the Alpaca adapter with paper keys for the certified paper equity-long lane."
        return "Connect Coinbase Advanced (or fix credentials) for live execution."
    if blocked in {
        "alpaca_live_posture_not_certified",
        "alpaca_crypto_execution_not_certified",
        "alpaca_short_execution_not_certified",
        "alpaca_account_scope_unfrozen_or_mismatched",
    }:
        return "This Alpaca execution shape is quarantined; only paper equity long execution is certified."
    if blocked == "paper_runner_disabled" and (mode or "").lower() == "paper":
        return "Enable CHILI_MOMENTUM_PAPER_RUNNER_ENABLED (and optional scheduler) or use dev tick."
    if blocked == "live_runner_disabled" and (mode or "").lower() == "live":
        return "Enable CHILI_MOMENTUM_LIVE_RUNNER_ENABLED after confirming venue readiness."
    if blocked == "paper_scheduler_not_running":
        return "Scheduler must run web-light jobs (CHILI_SCHEDULER_ROLE=all|web) with paper runner scheduler enabled."
    if blocked == "live_driver_misconfigured":
        if readiness.get("live_runner_loop_enabled") and not readiness.get(
            "live_runner_price_bus_enabled"
        ):
            return "Event-loop mode requires the price bus; keep the legacy live batch disabled."
        return "Enable exactly one live owner: the event loop or the legacy batch, never both."
    if blocked == "live_event_loop_not_running":
        return "Start or repair the dedicated event-loop owner; keep the legacy live batch disabled."
    if blocked == "live_event_loop_health_unverified":
        return "Verify the dedicated event-loop heartbeat/owner fence; keep the legacy live batch disabled."
    if blocked in {"live_driver_not_running", "live_scheduler_not_running"}:
        if readiness.get("live_driver_mode") == "event_loop":
            return "Start or repair the dedicated event-loop owner; do not enable the legacy live batch."
        return "Start the configured live-runner owner in the momentum execution scheduler."
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
