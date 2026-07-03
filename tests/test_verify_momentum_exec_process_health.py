from __future__ import annotations

from pathlib import Path

from scripts.verify_momentum_exec_process_health import (
    evaluate_environment,
    evaluate_process_command,
    evaluate_process_health,
    evaluate_source_markers,
)


def _good_env() -> dict[str, str]:
    return {
        "CHILI_SCHEDULER_ROLE": "momentum_exec_only",
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "true",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "false",
        "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED": "false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED": "false",
    }


def test_environment_requires_momentum_exec_ross_runtime_flags() -> None:
    ok, errors = evaluate_environment(_good_env())
    assert ok is True
    assert errors == []

    bad = _good_env()
    bad["CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED"] = "false"
    ok, errors = evaluate_environment(bad)
    assert ok is False
    assert any("required_env_disabled:ross_universe" in err for err in errors)


def test_process_command_rejects_placeholder_worker() -> None:
    ok, errors = evaluate_process_command("python scripts/scheduler_worker.py")
    assert ok is True
    assert errors == []

    ok, errors = evaluate_process_command("python -c sleep(86400)")
    assert ok is False
    assert any("unexpected_process_command" in err for err in errors)
    assert any("placeholder_process_command:sleep(86400)" in err for err in errors)


def test_source_markers_cover_ross_universe_gate() -> None:
    ok, errors = evaluate_source_markers(Path(__file__).resolve().parents[1])
    assert ok is True
    assert errors == []


def test_process_health_combines_env_command_and_source() -> None:
    ok, errors = evaluate_process_health(
        env=_good_env(),
        command="python scripts/scheduler_worker.py",
        repo_root=Path(__file__).resolve().parents[1],
    )
    assert ok is True
    assert errors == []
