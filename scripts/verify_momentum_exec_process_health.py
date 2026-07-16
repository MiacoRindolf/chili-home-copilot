from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_momentum_worker_runtime import (
    evaluate_a_setup_size_floor_smoke,
    evaluate_iqfeed_bridge_notify_source,
    evaluate_live_loop_timing_config,
    evaluate_ross_entry_shape_smoke,
    evaluate_ross_exit_shape_smoke,
    evaluate_ross_reentry_smoke,
)


REQUIRED_TRUE_ENV = {
    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "live_runner",
    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "event_loop",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "auto_arm",
    "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "ross_universe",
}
REQUIRED_FALSE_ENV = {
    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "scheduled_entry_path",
    "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED": "batch_entry_fallback",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "scheduled_auto_arm",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED": "auto_arm_scheduler_fallback",
}
EXPECTED_ROLE = "momentum_exec_only"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_process_command(proc_root: Path = Path("/proc/1")) -> str:
    try:
        raw = (proc_root / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip().lower()


def evaluate_environment(env: Mapping[str, str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    role = str(env.get("CHILI_SCHEDULER_ROLE") or "").strip()
    if role != EXPECTED_ROLE:
        errors.append(f"wrong_scheduler_role:{role or 'missing'}")
    for key, label in REQUIRED_TRUE_ENV.items():
        if not _truthy(env.get(key)):
            errors.append(f"required_env_disabled:{label}:{key}={env.get(key, '')}")
    for key, label in REQUIRED_FALSE_ENV.items():
        if _truthy(env.get(key)):
            errors.append(f"forbidden_env_enabled:{label}:{key}={env.get(key, '')}")
    return not errors, errors


def evaluate_process_command(command: str) -> tuple[bool, list[str]]:
    lowered = command.lower()
    errors: list[str] = []
    if "scheduler_worker.py" not in lowered:
        errors.append(f"unexpected_process_command:{command or 'missing'}")
    for marker in ("placeholder", "live runner disabled", "sleep(86400)"):
        if marker in lowered:
            errors.append(f"placeholder_process_command:{marker}")
    return not errors, errors


def evaluate_source_markers(repo_root: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    live_runner = (repo_root / "app/services/trading/momentum_neural/live_runner.py").read_text(encoding="utf-8")
    live_runner_loop = (repo_root / "app/services/trading/momentum_neural/live_runner_loop.py").read_text(
        encoding="utf-8"
    )
    risk_policy = (repo_root / "app/services/trading/momentum_neural/risk_policy.py").read_text(encoding="utf-8")
    risk_evaluator = (repo_root / "app/services/trading/momentum_neural/risk_evaluator.py").read_text(encoding="utf-8")
    auto_arm = (repo_root / "app/services/trading/momentum_neural/auto_arm.py").read_text(encoding="utf-8")
    universe = (repo_root / "app/services/trading/momentum_neural/universe.py").read_text(encoding="utf-8")
    iqfeed_trade_bridge = (repo_root / "scripts/iqfeed_trade_bridge.py").read_text(encoding="utf-8")

    entry_ok, entry_errors = evaluate_ross_entry_shape_smoke(
        {
            "source_markers": {
                "has_entry_shape_block": "def _ross_live_entry_shape_block" in live_runner,
                "has_pre_candidate_block": "def _ross_live_pre_candidate_shape_block" in live_runner,
                "has_shape_reason": "ross_live_requires_tick_tape_revalidation" in live_runner,
                "has_pre_candidate_event": "live_entry_pre_candidate_ross_shape_block" in live_runner,
                "has_5m_block": 'frame_used in {"1m", "5m"}' in live_runner,
                "has_tick_label_not_enough": "if not tick_tape_revalidated" in live_runner,
                "has_scheduler_entry_wall": "ross_equity_scheduler_entry_wall" in live_runner,
            }
        }
    )
    reentry_ok, reentry_errors = evaluate_ross_reentry_smoke(
        {
            "source_markers": {
                "has_session_helper": "def _live_same_session_reentry_allowed_for_session" in live_runner,
                "has_ross_family_check": "EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP" in live_runner,
                "has_stock_check": 'asset_class_for_symbol(symbol) == "stock"' in live_runner,
                "has_forced_false": "return False" in live_runner,
            }
        }
    )
    floor_ok, floor_errors = evaluate_a_setup_size_floor_smoke(
        {
            "source_markers": {
                "has_hard_reducer_reason": '"reason": "hard_reducer_respected"' in risk_policy,
                "has_hard_blocker_label": "hard_blockers" in risk_policy,
            }
        }
    )
    exit_ok, exit_errors = evaluate_ross_exit_shape_smoke(
        {
            "source_markers": {
                "has_tick_tape_helper": "def _is_ross_tick_tape_entry" in live_runner,
                "has_smart_hold_for_ross": 'getattr(settings, "chili_momentum_smart_hold_enabled", False)) or _ross_tick_tape_entry'
                in live_runner,
                "legacy_bail_excludes_ross": "and not _ross_tick_tape_entry" in live_runner,
            }
        }
    )
    loop_ok, loop_errors = evaluate_live_loop_timing_config(
        {
            "iqfeed_notify_enabled": True,
            "iqfeed_tape_enabled": True,
            "iqfeed_poll_fallback_enabled": True,
            "iqfeed_poll_seconds": 0.25,
            "min_tick_interval_ms": 250,
            "source_markers": {
                "has_notify_handler": "def _handle_iqfeed_notify_payload" in live_runner_loop,
                "has_notify_admission": "self._admit_iqfeed_symbol(sym, data)" in live_runner_loop,
                "has_refresh_viability": "refresh_viability=True" in live_runner_loop,
                "has_immediate_notify_submit": 'cause="iqfeed_notify"' in live_runner_loop,
                "has_iqfeed_listen_channel": 'channel = "momentum_iqfeed_l1"' in live_runner_loop
                and "LISTEN" in live_runner_loop,
            },
        }
    )
    bridge_ok, bridge_errors = evaluate_iqfeed_bridge_notify_source(
        {
            "source_markers": {
                "has_notify_enabled_flag": "IQFEED_NOTIFY_ENABLED" in iqfeed_trade_bridge,
                "has_notify_channel": "IQFEED_NOTIFY_CHANNEL" in iqfeed_trade_bridge
                and "momentum_iqfeed_l1" in iqfeed_trade_bridge,
                "has_pg_notify_statement": "SELECT pg_notify(:channel, :payload)" in iqfeed_trade_bridge,
                "has_notify_payload_symbol": '"symbol": sym' in iqfeed_trade_bridge,
                "has_notify_payload_observed_at": '"observed_at"' in iqfeed_trade_bridge,
                "has_notify_payload_source": '"source": "iqfeed_l1"' in iqfeed_trade_bridge,
                "has_notify_after_nbbo_branch": "notify_by_symbol" in iqfeed_trade_bridge
                and "IQFEED_NOTIFY_ENABLED and notify_by_symbol" in iqfeed_trade_bridge,
            }
        }
    )
    if not entry_ok:
        errors.extend(entry_errors)
    if not reentry_ok:
        errors.extend(reentry_errors)
    if not floor_ok:
        errors.extend(floor_errors)
    if not exit_ok:
        errors.extend(exit_errors)
    if not loop_ok:
        errors.extend(loop_errors)
    if not bridge_ok:
        errors.extend(bridge_errors)

    required_universe_markers = {
        "risk_evaluator_final_ross_gate": "def _ross_lane_universe_check" in risk_evaluator
        and "ross_smallcap_profile_evidence" in risk_evaluator
        and "ross_universe_risk_check_error" in risk_evaluator,
        "auto_arm_refuses_generic_fallback": "refusing generic broad-equity fallback" in auto_arm,
        "universe_profile_rejects_high_price": "ross_universe_price_above_profile" in universe,
    }
    for label, present in required_universe_markers.items():
        if not present:
            errors.append(f"ross_universe_source_marker_missing:{label}")

    return not errors, errors


def evaluate_process_health(
    *,
    env: Mapping[str, str],
    command: str,
    repo_root: Path,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for ok, part_errors in (
        evaluate_environment(env),
        evaluate_process_command(command),
        evaluate_source_markers(repo_root),
    ):
        if not ok:
            errors.extend(part_errors)
    return not errors, errors


def main(argv: Sequence[str] | None = None) -> int:
    command = _read_process_command()
    ok, errors = evaluate_process_health(env=os.environ, command=command, repo_root=REPO_ROOT)
    if ok:
        print("momentum_exec_process_health_ok")
        return 0
    for err in errors:
        print(err, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
