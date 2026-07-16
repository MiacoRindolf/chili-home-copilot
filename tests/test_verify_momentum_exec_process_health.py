from __future__ import annotations

from pathlib import Path

from scripts.verify_momentum_exec_process_health import (
    evaluate_environment,
    evaluate_process_command,
    evaluate_process_health,
    evaluate_source_markers,
)


def _healthy_env() -> dict[str, str]:
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


def test_process_health_accepts_repo_source_and_live_event_env() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    ok, errors = evaluate_process_health(
        env=_healthy_env(),
        command="python scripts/scheduler_worker.py",
        repo_root=repo_root,
    )

    assert ok is True
    assert errors == []


def test_environment_rejects_disabled_live_loop() -> None:
    env = _healthy_env()
    env["CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED"] = "0"

    ok, errors = evaluate_environment(env)

    assert ok is False
    assert "required_env_disabled:event_loop:CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=0" in errors


def test_environment_rejects_scheduled_entry_fallbacks() -> None:
    env = _healthy_env()
    env["CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED"] = "1"
    env["CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED"] = "true"

    ok, errors = evaluate_environment(env)

    assert ok is False
    assert "forbidden_env_enabled:scheduled_entry_path:CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=1" in errors
    assert "forbidden_env_enabled:batch_entry_fallback:CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=true" in errors


def test_process_command_rejects_placeholder() -> None:
    ok, errors = evaluate_process_command(
        "python -c import time; print('CHILI momentum placeholder: live runner disabled'); time.sleep(86400)"
    )

    assert ok is False
    assert "unexpected_process_command:python -c import time; print('CHILI momentum placeholder: live runner disabled'); time.sleep(86400)" in errors
    assert "placeholder_process_command:placeholder" in errors
    assert "placeholder_process_command:live runner disabled" in errors
    assert "placeholder_process_command:sleep(86400)" in errors


def test_source_markers_cover_ross_lane_hard_gates() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    ok, errors = evaluate_source_markers(repo_root)

    assert ok is True
    assert errors == []


def test_source_markers_reject_missing_iqfeed_notify_submit(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "app/services/trading/momentum_neural"
    dst = tmp_path / "app/services/trading/momentum_neural"
    dst.mkdir(parents=True)
    for name in ("live_runner.py", "risk_policy.py", "risk_evaluator.py", "auto_arm.py", "universe.py"):
        (dst / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    scripts_dst = tmp_path / "scripts"
    scripts_dst.mkdir()
    (scripts_dst / "iqfeed_trade_bridge.py").write_text(
        (repo_root / "scripts/iqfeed_trade_bridge.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loop_text = (src / "live_runner_loop.py").read_text(encoding="utf-8").replace('cause="iqfeed_notify"', 'cause="heartbeat"')
    (dst / "live_runner_loop.py").write_text(loop_text, encoding="utf-8")

    ok, errors = evaluate_source_markers(tmp_path)

    assert ok is False
    assert 'live_loop_source_marker_missing:cause="iqfeed_notify"' in errors


def test_source_markers_reject_missing_iqfeed_bridge_notify(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "app/services/trading/momentum_neural"
    dst = tmp_path / "app/services/trading/momentum_neural"
    dst.mkdir(parents=True)
    for name in ("live_runner.py", "live_runner_loop.py", "risk_policy.py", "risk_evaluator.py", "auto_arm.py", "universe.py"):
        (dst / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    scripts_dst = tmp_path / "scripts"
    scripts_dst.mkdir()
    bridge_text = (repo_root / "scripts/iqfeed_trade_bridge.py").read_text(encoding="utf-8").replace(
        "SELECT pg_notify(:channel, :payload)",
        "SELECT 1",
    )
    (scripts_dst / "iqfeed_trade_bridge.py").write_text(bridge_text, encoding="utf-8")

    ok, errors = evaluate_source_markers(tmp_path)

    assert ok is False
    assert "iqfeed_bridge_notify_source_marker_missing:SELECT pg_notify" in errors
