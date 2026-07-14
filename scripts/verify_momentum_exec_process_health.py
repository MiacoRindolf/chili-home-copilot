from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROLE = "momentum_exec_only"
EXPECTED_IQFEED_BRIDGE_BUILD = (
    "iqfeed-l1-quote-provenance-v2+sha256:dc0185e65439364c"
)

REQUIRED_TRUE_ENV = {
    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "live_runner",
    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "event_loop",
    "CHILI_AUTOPILOT_PRICE_BUS_ENABLED": "event_loop_price_bus",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "auto_arm",
    "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "ross_universe",
}
REQUIRED_FALSE_ENV = {
    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "scheduled_entry_path",
    "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED": "batch_entry_fallback",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "scheduled_auto_arm",
    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED": "auto_arm_scheduler_fallback",
}
REQUIRED_EXACT_ENV = {
    "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD": (
        "iqfeed_authoritative_bridge_build",
        EXPECTED_IQFEED_BRIDGE_BUILD,
    ),
}


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
    for key, (label, expected) in REQUIRED_EXACT_ENV.items():
        if str(env.get(key) or "").strip() != expected:
            errors.append(f"required_env_mismatch:{label}:{key}")
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


def _read_source(repo_root: Path, rel: str) -> str:
    try:
        return (repo_root / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def evaluate_source_markers(repo_root: Path) -> tuple[bool, list[str]]:
    risk_evaluator = _read_source(repo_root, "app/services/trading/momentum_neural/risk_evaluator.py")
    auto_arm = _read_source(repo_root, "app/services/trading/momentum_neural/auto_arm.py")
    universe = _read_source(repo_root, "app/services/trading/momentum_neural/universe.py")
    config = _read_source(repo_root, "app/config.py")

    markers = {
        "risk_evaluator_final_ross_gate": (
            "def _ross_lane_universe_check" in risk_evaluator
            and "ross_equity_universe" in risk_evaluator
            and "ross_universe_risk_check_error" in risk_evaluator
        ),
        "auto_arm_refuses_generic_fallback": (
            "refusing generic broad-equity fallback" in auto_arm
            and "ross_universe_skipped" in auto_arm
        ),
        "universe_profile_rejects_high_price": (
            "def ross_smallcap_profile_evidence" in universe
            and "ross_universe_price_above_profile" in universe
        ),
        "config_requires_ross_universe": "chili_momentum_ross_equity_universe_required" in config,
    }
    errors = [f"ross_universe_source_marker_missing:{label}" for label, present in markers.items() if not present]
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
