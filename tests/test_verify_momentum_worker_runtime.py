from __future__ import annotations

import subprocess
from pathlib import Path

import scripts.verify_momentum_worker_runtime as runtime_guard
from scripts.verify_momentum_worker_runtime import (
    LOAD_BEARING_MOMENTUM_ENV_CONTROLS,
    ROSS_CRITICAL_SOURCE_PATHS,
    _docker_containers,
    evaluate_container_set,
    evaluate_a_setup_size_floor_smoke,
    evaluate_compose_image_alignment,
    evaluate_compose_momentum_exec_service,
    evaluate_expected_running_image,
    evaluate_worker_lifecycle_quiet,
    evaluate_iqfeed_bridge_notify_source,
    evaluate_live_loop_timing_config,
    evaluate_no_active_like_sessions,
    evaluate_premarket_binding_config,
    evaluate_premarket_readiness_script_source,
    evaluate_replay_scheduler_snapshot_smoke,
    evaluate_ross_entry_shape_smoke,
    evaluate_ross_event_admission_config,
    evaluate_ross_exit_shape_smoke,
    evaluate_ross_reentry_smoke,
    evaluate_ross_starter_alias_coverage_smoke,
    evaluate_ross_symbol_resolution_smoke,
    evaluate_restored_helper_contract_smoke,
    evaluate_source_reload_freshness,
    evaluate_transcript_gate_config,
)


def _worker(*, env: list[str] | None = None, command: str = "python scripts/scheduler_worker.py", status: str = "running"):
    return {
        "Name": "/chili-clean-recovery-momentum-exec",
        "Image": "chili-app:main-clean-codex-rosshardgate-rhcooldown-starve-20260701-1020",
        "State": {"Running": status == "running", "Status": status},
        "Config": {
            "Entrypoint": ["python"],
            "Cmd": command.split(),
            "Env": env
            or [
                "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=true",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=true",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED=true",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED=true",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED=true",
                "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED=true",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=true",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED=false",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED=false",
                "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED=true",
                "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=false",
                "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=false",
            ],
        },
    }


def test_runtime_guard_accepts_real_canonical_worker() -> None:
    ok, errors = evaluate_container_set([_worker()])

    assert ok is True
    assert errors == []


def test_premarket_readiness_source_rejects_plain_start_resurrection() -> None:
    ok, errors = evaluate_premarket_readiness_script_source(
        {
            "exists": True,
            "text": """
foreach ($c in @('chili-clean-recovery-scheduler','chili-clean-recovery-momentum-exec')) {
  docker start $c
}
""",
        }
    )

    assert ok is False
    assert "premarket_readiness_uses_plain_docker_start_for_momentum_exec" in errors
    assert "premarket_readiness_missing_guard:compose_momentum_exec" in errors


def test_premarket_readiness_source_requires_compose_quarantine_guard() -> None:
    ok, errors = evaluate_premarket_readiness_script_source(
        {
            "exists": True,
            "text": """
$expectedImage = (Select-String -Path "$repo\\.env" -Pattern '^CHILI_MOMENTUM_EXEC_IMAGE=').Line
$currentImage = docker inspect chili-clean-recovery-momentum-exec --format '{{.Config.Image}}'
$currentService = docker inspect chili-clean-recovery-momentum-exec --format '{{ index .Config.Labels "com.chili.service" }}'
docker rename chili-clean-recovery-momentum-exec "chili-clean-recovery-momentum-exec-pre-premarket-stale-$stamp"
docker compose --profile live-momentum up -d --no-deps momentum-exec-worker
""",
        }
    )

    assert ok is True
    assert errors == []


def test_docker_container_listing_skips_stale_inspect_ids(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_text(cmd, *, timeout=None):
        calls.append(tuple(cmd))
        if cmd[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"ID":"gone"}\n{"ID":"live"}\n', stderr="")
        if cmd == ["docker", "inspect", "gone"]:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="No such object")
        if cmd == ["docker", "inspect", "live"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"Name":"/chili-clean-recovery-momentum-exec","State":{"Running":true}}]',
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr("scripts.verify_momentum_worker_runtime._run_text", fake_run_text)

    rows = _docker_containers()

    assert rows == [{"Name": "/chili-clean-recovery-momentum-exec", "State": {"Running": True}}]
    assert ("docker", "inspect", "gone") in calls
    assert ("docker", "inspect", "live") in calls


def test_docker_container_listing_does_not_inspect_unrelated_containers(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_text(cmd, *, timeout=None):
        calls.append(tuple(cmd))
        if cmd[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    '{"ID":"postgres","Names":"chili-postgres"}\n'
                    '{"ID":"worker","Names":"chili-clean-recovery-momentum-exec"}\n'
                    '{"ID":"legacy","Names":"chili-ross-live-worker"}\n'
                ),
                stderr="",
            )
        if cmd == ["docker", "inspect", "worker"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"Name":"/chili-clean-recovery-momentum-exec","State":{"Running":true}}]',
                stderr="",
            )
        if cmd == ["docker", "inspect", "legacy"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"Name":"/chili-ross-live-worker","State":{"Running":false}}]',
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr("scripts.verify_momentum_worker_runtime._run_text", fake_run_text)

    rows = _docker_containers()

    assert [row["Name"] for row in rows] == [
        "/chili-clean-recovery-momentum-exec",
        "/chili-ross-live-worker",
    ]
    assert ("docker", "inspect", "postgres") not in calls


def test_runtime_guard_main_reports_container_stage_timeout(monkeypatch, capsys) -> None:
    def timeout_containers():
        raise subprocess.TimeoutExpired(["docker", "ps", "-a"], timeout=7)

    monkeypatch.setattr(runtime_guard, "_docker_containers", timeout_containers)

    code = runtime_guard.main([])

    captured = capsys.readouterr()
    assert code == 1
    assert "runtime_guard_stage_timeout:container_set:timeout=7" in captured.err


def test_runtime_guard_main_reports_stage_command_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(runtime_guard, "_docker_worker_lifecycle_config", lambda: {"quiet_seconds": 90.0, "events": []})

    def failing_transcript_config():
        raise subprocess.CalledProcessError(23, ["docker", "exec"], stderr="boom")

    monkeypatch.setattr(runtime_guard, "_docker_transcript_gate_config", failing_transcript_config)

    code = runtime_guard.main([])

    captured = capsys.readouterr()
    assert code == 1
    assert "runtime_guard_stage_failed:transcript_config:returncode=23" in captured.err


def test_runtime_guard_main_reports_lifecycle_stage_before_later_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(
        runtime_guard,
        "_docker_worker_lifecycle_config",
        lambda: {
            "quiet_seconds": 90.0,
            "now": "2026-07-02T13:25:00+00:00",
            "events": [
                {
                    "Action": "kill",
                    "timeNano": 1782998656000000000,
                    "Actor": {"Attributes": {"signal": "15"}},
                }
            ],
        },
    )

    code = runtime_guard.main([])

    captured = capsys.readouterr()
    assert code == 1
    assert "worker_lifecycle_quiet_failed:action=kill" in captured.err


def test_runtime_guard_tracks_ross_feed_health_source_freshness() -> None:
    assert "/app/app/services/trading/momentum_neural/ross_feed_health.py" in ROSS_CRITICAL_SOURCE_PATHS


def test_runtime_guard_tracks_live_fsm_source_freshness() -> None:
    assert "/app/app/services/trading/momentum_neural/live_fsm.py" in ROSS_CRITICAL_SOURCE_PATHS


def test_compose_momentum_exec_worker_uses_canonical_container_name() -> None:
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")

    service_idx = text.index("  momentum-exec-worker:")
    next_service_idx = text.index("\n  broker-sync-worker:", service_idx)
    service_block = text[service_idx:next_service_idx]

    assert "container_name: chili-clean-recovery-momentum-exec" in service_block


def test_compose_momentum_exec_worker_has_process_healthcheck() -> None:
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")

    service_idx = text.index("  momentum-exec-worker:")
    next_service_idx = text.index("\n  broker-sync-worker:", service_idx)
    service_block = text[service_idx:next_service_idx]

    assert "healthcheck:" in service_block
    assert 'test: ["CMD", "python", "scripts/verify_momentum_exec_process_health.py"]' in service_block


def test_compose_momentum_exec_worker_uses_lane_specific_live_defaults() -> None:
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")

    service_idx = text.index("  momentum-exec-worker:")
    next_service_idx = text.index("\n  broker-sync-worker:", service_idx)
    service_block = text[service_idx:next_service_idx]

    assert "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=${CHILI_MOMENTUM_EXEC_LIVE_RUNNER_ENABLED:-true}" in service_block
    assert "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=${CHILI_MOMENTUM_EXEC_AUTO_ARM_LIVE_ENABLED:-true}" in service_block
    assert (
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=${CHILI_MOMENTUM_EXEC_LIVE_RUNNER_SCHEDULER_ENABLED:-false}"
        in service_block
    )
    assert (
        "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=${CHILI_MOMENTUM_EXEC_BATCH_FALLBACK_ENABLED:-false}"
        in service_block
    )
    assert (
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_SECONDS=${CHILI_MOMENTUM_EXEC_IQFEED_POLL_SECONDS:-0.25}"
        in service_block
    )
    assert "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=${CHILI_MOMENTUM_LIVE_RUNNER_ENABLED" not in service_block
    assert "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=${CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED" not in service_block


def test_generic_compose_services_cannot_inherit_live_runner_from_env() -> None:
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")

    chili_idx = text.index("  chili:")
    brain_idx = text.index("\n  brain:", chili_idx)
    chili_block = text[chili_idx:brain_idx]
    scheduler_idx = text.index("  scheduler-worker:")
    momentum_idx = text.index("\n  momentum-exec-worker:", scheduler_idx)
    scheduler_block = text[scheduler_idx:momentum_idx]

    for service_block in (chili_block, scheduler_block):
        assert "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=0" in service_block
        assert "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=0" in service_block
        assert "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=${CHILI_MOMENTUM_LIVE_RUNNER_ENABLED" not in service_block
        assert (
            "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=${CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED"
            not in service_block
        )


def test_runtime_guard_allows_real_worker_even_when_image_name_contains_setup_audit() -> None:
    worker = _worker()
    worker["Image"] = "chili-app:main-clean-codex-setup-audit-20260701-141813"

    ok, errors = evaluate_container_set([worker])

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_disabled_placeholder_as_canonical() -> None:
    ok, errors = evaluate_container_set(
        [
            _worker(
                command="-c import time; print('CHILI momentum placeholder: live runner disabled'); time.sleep(86400)",
                env=[
                    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=0",
                    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=0",
                    "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=0",
                    "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED=true",
                ],
            )
        ]
    )

    assert ok is False
    assert "canonical_placeholder_command:placeholder" in errors
    assert "canonical_placeholder_command:live runner disabled" in errors
    assert any(err.startswith("canonical_env_disabled:live_runner") for err in errors)
    assert any(err.startswith("canonical_env_disabled:event_loop") for err in errors)


def test_runtime_guard_rejects_missing_event_driven_lane_flags() -> None:
    ok, errors = evaluate_container_set(
        [
            _worker(
                env=[
                    "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=true",
                    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=true",
                    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED=false",
                    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED=false",
                    "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED=false",
                    "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED=false",
                    "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=true",
                    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED=true",
                    "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED=true",
                    "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED=true",
                    "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=false",
                    "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=false",
                ],
            )
        ]
    )

    assert ok is False
    assert "canonical_env_disabled:iqfeed_tape:CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED=false" in errors
    assert "canonical_env_disabled:iqfeed_notify:CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED=false" in errors
    assert (
        "canonical_env_disabled:iqfeed_poll_fallback:"
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED=false"
    ) in errors
    assert "canonical_env_disabled:ross_event_admission:CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED=false" in errors
    assert "canonical_auto_arm_scheduler_enabled" in errors
    assert "canonical_auto_arm_scheduler_fallback_enabled" in errors


def test_runtime_guard_rejects_split_duplicate_live_worker() -> None:
    duplicate = _worker()
    duplicate["Name"] = "/chili-ross-live-worker"

    ok, errors = evaluate_container_set([_worker(), duplicate])

    assert ok is False
    assert "duplicate_live_worker_running:chili-ross-live-worker" in errors


def test_runtime_guard_ignores_stopped_pre_container_history() -> None:
    stale = _worker(status="exited")
    stale["Name"] = "/chili-clean-recovery-momentum-exec-pre-rhoutagecooldown-20260701-092340"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_running_pre_momentum_container() -> None:
    stale = _worker()
    stale["Name"] = "/chili-clean-recovery-momentum-exec-pre-rhoutagecooldown-20260701-092340"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is False
    assert "stale_live_container_running:chili-clean-recovery-momentum-exec-pre-rhoutagecooldown-20260701-092340" in errors


def test_runtime_guard_rejects_running_pre_scheduler_container() -> None:
    stale = _worker()
    stale["Name"] = "/chili-clean-recovery-scheduler-pre-rhoutagecooldown-20260701-092340"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is False
    assert "stale_live_container_running:chili-clean-recovery-scheduler-pre-rhoutagecooldown-20260701-092340" in errors


def test_runtime_guard_rejects_running_legacy_scheduler_container() -> None:
    stale = _worker()
    stale["Name"] = "/chili-clean-recovery-scheduler"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is False
    assert "stale_scheduler_container_running:chili-clean-recovery-scheduler" in errors


def test_runtime_guard_ignores_stopped_legacy_scheduler_container() -> None:
    stale = _worker(status="exited")
    stale["Name"] = "/chili-clean-recovery-scheduler"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_running_quarantined_placeholder_container() -> None:
    stale = _worker(command="-c import time; print('CHILI momentum placeholder: live runner disabled'); time.sleep(86400)")
    stale["Name"] = "/chili-clean-recovery-momentum-exec-placeholder-quarantined-20260701-1145"

    ok, errors = evaluate_container_set([_worker(), stale])

    assert ok is False
    assert "stale_live_container_running:chili-clean-recovery-momentum-exec-placeholder-quarantined-20260701-1145" in errors


def test_runtime_guard_rejects_batch_fallback_unless_explicitly_allowed() -> None:
    env = [
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED=true",
        "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED=true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED=false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED=false",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=false",
        "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=true",
    ]

    ok, errors = evaluate_container_set([_worker(env=env)])
    assert ok is False
    assert "canonical_batch_fallback_enabled" in errors

    allowed_ok, allowed_errors = evaluate_container_set([_worker(env=env)], allow_batch_fallback=True)
    assert allowed_ok is True
    assert allowed_errors == []


def test_runtime_guard_accepts_worker_started_after_critical_source_mtimes() -> None:
    ok, errors = evaluate_source_reload_freshness(
        {
            "started_at": "2026-07-02T00:11:29+00:00",
            "source_mtimes": {
                "/app/app/services/trading/momentum_neural/live_runner.py": "2026-07-02T00:05:16+00:00",
                "/app/app/services/trading/momentum_neural/live_fsm.py": 1782950903.0,
            },
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_worker_older_than_critical_source_mtime() -> None:
    ok, errors = evaluate_source_reload_freshness(
        {
            "started_at": "2026-07-02T00:01:08+00:00",
            "source_mtimes": {
                "/app/app/services/trading/momentum_neural/live_runner.py": "2026-07-02T00:05:16+00:00",
            },
        }
    )

    assert ok is False
    assert any(err.startswith("source_reload_freshness_failed:worker_older_than_source") for err in errors)


def test_worker_lifecycle_quiet_rejects_recent_external_kill() -> None:
    ok, errors = evaluate_worker_lifecycle_quiet(
        {
            "quiet_seconds": 90.0,
            "now": "2026-07-02T13:25:00+00:00",
            "events": [
                {
                    "Action": "kill",
                    "timestamp": "2026-07-02T13:24:17+00:00",
                    "Actor": {"Attributes": {"signal": "9"}},
                }
            ],
        }
    )

    assert ok is False
    assert errors == [
        "worker_lifecycle_quiet_failed:action=kill:time=2026-07-02T13:24:17+00:00:"
        "quiet_seconds=90:exit=:signal=9"
    ]


def test_worker_lifecycle_quiet_accepts_disruption_before_health_window() -> None:
    ok, errors = evaluate_worker_lifecycle_quiet(
        {
            "quiet_seconds": 90.0,
            "now": "2026-07-02T13:25:00+00:00",
            "events": [
                {
                    "Action": "die",
                    "timestamp": "2026-07-02T13:22:59+00:00",
                    "Actor": {"Attributes": {"exitCode": "137"}},
                }
            ],
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_compose_image_aligned_with_running_worker() -> None:
    ok, errors = evaluate_compose_image_alignment(
        {
            "running_image": "chili-app:main-clean-codex-rosslane-turnover-20260701-161914",
            "compose_image": "chili-app:main-clean-codex-rosslane-turnover-20260701-161914",
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_compose_image_that_would_recreate_stale_worker() -> None:
    ok, errors = evaluate_compose_image_alignment(
        {
            "running_image": "chili-app:main-clean-codex-rosslane-turnover-20260701-161914",
            "compose_image": "chili-app:main-clean-codex-setup-audit-20260701-1135",
        }
    )

    assert ok is False
    assert errors == [
        "compose_image_alignment_failed:running=chili-app:main-clean-codex-rosslane-turnover-20260701-161914:"
        "compose=chili-app:main-clean-codex-setup-audit-20260701-1135"
    ]


def test_runtime_guard_accepts_expected_running_image() -> None:
    ok, errors = evaluate_expected_running_image(
        {"running_image": "chili-app:main-clean-codex-regression-restore-20260702-044118"},
        "chili-app:main-clean-codex-regression-restore-20260702-044118",
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_expected_running_image_drift() -> None:
    ok, errors = evaluate_expected_running_image(
        {"running_image": "chili-app:main-clean-codex-rosslane-turnover-20260701-161914"},
        "chili-app:main-clean-codex-regression-restore-20260702-044118",
    )

    assert ok is False
    assert errors == [
        "expected_image_alignment_failed:running=chili-app:main-clean-codex-rosslane-turnover-20260701-161914:"
        "expected=chili-app:main-clean-codex-regression-restore-20260702-044118"
    ]


def _compose_momentum_exec_service(
    *,
    command: list[str] | str | None = None,
    environment: dict[str, str] | None = None,
    container_name: str = "chili-clean-recovery-momentum-exec",
) -> dict:
    env = {
        "CHILI_SCHEDULER_ROLE": "momentum_exec_only",
        "CHILI_AUTOTRADER_ENABLED": "false",
        "CHILI_AUTOTRADER_CRYPTO_ENABLED": "false",
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED": "true",
        "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED": "true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "true",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "true",
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED": "false",
        "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED": "false",
        "CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL": "1m",
        "CHILI_MOMENTUM_EARLY_PREMARKET_MIN_MOVERS": "1",
        "CHILI_MOMENTUM_TICK_FIRST_PULLBACK_ENABLED": "true",
        "CHILI_MOMENTUM_MIDDAY_DEWEIGHT_ENABLED": "true",
        "CHILI_MOMENTUM_PYRAMID_ENABLED": "true",
        "CHILI_MOMENTUM_PYRAMID_DISCRETE_ADD_ENABLED": "true",
        "CHILI_MOMENTUM_PYRAMID_SKIP_VIABILITY_RECHECK": "true",
        "CHILI_MOMENTUM_PYRAMID_ADD_SUBMIT_RETRY_MAX": "2",
        "CHILI_MOMENTUM_SCALE_GRID_ENABLED": "true",
        "CHILI_MOMENTUM_ADD_INTO_HALT_ENABLED": "true",
        "CHILI_MOMENTUM_STOP_L2_CONFIRM_ENABLED": "true",
        "CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED": "false",
        "CHILI_MOMENTUM_EXIT_OFI_HIDDEN_SELLER_ENABLED": "false",
        "CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED": "false",
    }
    if environment:
        env.update(environment)
    return {
        "container_name": container_name,
        "profiles": ["live-momentum"],
        "command": command or ["python", "scripts/scheduler_worker.py"],
        "environment": env,
    }


def test_compose_service_guard_accepts_real_momentum_exec_service() -> None:
    ok, errors = evaluate_compose_momentum_exec_service(_compose_momentum_exec_service())

    assert ok is True
    assert errors == []


def test_compose_service_guard_rejects_placeholder_canonical_service() -> None:
    ok, errors = evaluate_compose_momentum_exec_service(
        _compose_momentum_exec_service(
            command="python -c import time; print('CHILI momentum placeholder: live runner disabled'); time.sleep(86400)",
            environment={
                "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "0",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "0",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "0",
            },
        )
    )

    assert ok is False
    assert "compose_momentum_exec_service_failed:placeholder_command:placeholder" in errors
    assert "compose_momentum_exec_service_failed:placeholder_command:live runner disabled" in errors
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:live_runner") for err in errors)
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:event_loop") for err in errors)


def test_compose_service_guard_rejects_scheduled_or_batch_fallback_entry_lane() -> None:
    ok, errors = evaluate_compose_momentum_exec_service(
        _compose_momentum_exec_service(
            environment={
                "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "true",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "true",
                "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED": "true",
            }
        )
    )

    assert ok is False
    assert "compose_momentum_exec_service_failed:scheduled_live_runner_enabled" in errors
    assert "compose_momentum_exec_service_failed:auto_arm_scheduler_enabled" in errors
    assert "compose_momentum_exec_service_failed:batch_fallback_enabled" in errors


def test_compose_service_guard_rejects_dropped_event_driven_lane_flags() -> None:
    ok, errors = evaluate_compose_momentum_exec_service(
        _compose_momentum_exec_service(
            environment={
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED": "false",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED": "false",
                "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED": "false",
                "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED": "false",
            }
        )
    )

    assert ok is False
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:iqfeed_tape") for err in errors)
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:iqfeed_notify") for err in errors)
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:iqfeed_poll_fallback") for err in errors)
    assert any(err.startswith("compose_momentum_exec_service_failed:env_disabled:ross_event_admission") for err in errors)


def test_compose_momentum_exec_worker_keeps_load_bearing_runner_flags() -> None:
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    service = text.split("  momentum-exec-worker:", 1)[1].split("\n  broker-sync-worker:", 1)[0]

    missing = sorted(flag for flag in LOAD_BEARING_MOMENTUM_ENV_CONTROLS if flag not in service)
    assert missing == []


def test_compose_service_guard_rejects_dropped_load_bearing_env_controls() -> None:
    service = _compose_momentum_exec_service()
    env = service["environment"]
    del env["CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL"]
    del env["CHILI_MOMENTUM_EARLY_PREMARKET_MIN_MOVERS"]
    del env["CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED"]
    del env["CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED"]

    ok, errors = evaluate_compose_momentum_exec_service(service)

    assert ok is False
    assert (
        "compose_momentum_exec_service_failed:missing_load_bearing_env:"
        "CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL"
    ) in errors
    assert (
        "compose_momentum_exec_service_failed:missing_load_bearing_env:"
        "CHILI_MOMENTUM_EARLY_PREMARKET_MIN_MOVERS"
    ) in errors
    assert (
        "compose_momentum_exec_service_failed:missing_load_bearing_env:"
        "CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED"
    ) in errors
    assert (
        "compose_momentum_exec_service_failed:missing_load_bearing_env:"
        "CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED"
    ) in errors


def test_restored_helper_contract_smoke_rejects_missing_runtime_helpers() -> None:
    ok, errors = evaluate_restored_helper_contract_smoke(
        {
            "is_real_entry_outcome_success": True,
            "is_real_entry_outcome_no_fill": False,
            "live_ofi_reader_callable": False,
            "ladder_distribution_callable": True,
            "target_prints_callable": True,
            "catalyst_grade_strong": 0,
            "catalyst_news_max_age_const": 30,
            "catalyst_news_max_age_setting": 30.0,
        }
    )

    assert ok is False
    assert "restored_helper_contract_failed:live_ofi_reader_callable:False" in errors
    assert "restored_helper_contract_failed:catalyst_grade_strong:0" in errors
    assert "restored_helper_contract_failed:catalyst_news_max_age_const:30" in errors
    assert "restored_helper_contract_failed:catalyst_news_max_age_setting:30.0" in errors


def test_restored_helper_contract_smoke_accepts_runtime_contracts() -> None:
    ok, errors = evaluate_restored_helper_contract_smoke(
        {
            "is_real_entry_outcome_success": True,
            "is_real_entry_outcome_no_fill": False,
            "live_ofi_reader_callable": True,
            "ladder_distribution_callable": True,
            "target_prints_callable": True,
            "catalyst_grade_strong": 3,
            "catalyst_news_max_age_const": 120,
            "catalyst_news_max_age_setting": 120.0,
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_premarket_binding_values() -> None:
    ok, errors = evaluate_premarket_binding_config(
        {
            "pullback_entry_interval": "1m",
            "early_premarket_min_movers": 1,
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_stale_premarket_binding_values() -> None:
    ok, errors = evaluate_premarket_binding_config(
        {
            "pullback_entry_interval": "5m",
            "early_premarket_min_movers": 3,
        }
    )

    assert ok is False
    assert "premarket_binding_failed:pullback_entry_interval:5m" in errors
    assert "premarket_binding_failed:early_premarket_min_movers:3" in errors


def test_runtime_guard_rejects_scheduled_live_runner_even_without_batch_fallback() -> None:
    env = [
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED=true",
        "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED=true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=true",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED=false",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED=false",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=true",
        "CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED=false",
    ]

    ok, errors = evaluate_container_set([_worker(env=env)])

    assert ok is False
    assert "canonical_scheduler_live_runner_enabled" in errors


def test_runtime_guard_accepts_safe_transcript_gate_config() -> None:
    ok, errors = evaluate_transcript_gate_config(
        {
            "bridge_enabled": True,
            "require_warrior_session_ok": True,
            "warrior_session_ok_path": r"D:\CHILI-Docker\chili-data\ross_stream\warrior_session_ok.json",
            "warrior_session_ok_max_age_seconds": 30.0,
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_transcript_bridge_without_warrior_marker() -> None:
    ok, errors = evaluate_transcript_gate_config(
        {
            "bridge_enabled": True,
            "require_warrior_session_ok": False,
            "warrior_session_ok_path": r"D:\CHILI-Docker\chili-data\ross_stream\warrior_session_ok.json",
            "warrior_session_ok_max_age_seconds": 30.0,
        }
    )

    assert ok is False
    assert "ross_transcript_marker_not_required" in errors


def test_runtime_guard_rejects_missing_transcript_marker_path() -> None:
    ok, errors = evaluate_transcript_gate_config(
        {
            "bridge_enabled": True,
            "require_warrior_session_ok": True,
            "warrior_session_ok_path": "",
            "warrior_session_ok_max_age_seconds": 30.0,
        }
    )

    assert ok is False
    assert "ross_transcript_marker_path_missing" in errors


def test_runtime_guard_rejects_stale_friendly_transcript_marker_age() -> None:
    ok, errors = evaluate_transcript_gate_config(
        {
            "bridge_enabled": True,
            "require_warrior_session_ok": True,
            "warrior_session_ok_path": r"D:\CHILI-Docker\chili-data\ross_stream\warrior_session_ok.json",
            "warrior_session_ok_max_age_seconds": 300.0,
        }
    )

    assert ok is False
    assert "ross_transcript_marker_max_age_unsafe:300" in errors


def test_runtime_guard_accepts_immediate_ross_event_admission_tick() -> None:
    ok, errors = evaluate_ross_event_admission_config({"tick_count": 1})

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_zero_ross_event_admission_tick_count() -> None:
    ok, errors = evaluate_ross_event_admission_config({"tick_count": 0})

    assert ok is False
    assert "ross_event_admission_tick_count_too_low:0" in errors


def test_runtime_guard_accepts_subsecond_live_loop_timing() -> None:
    ok, errors = evaluate_live_loop_timing_config(
        {
            "iqfeed_notify_enabled": True,
            "iqfeed_tape_enabled": True,
            "iqfeed_poll_fallback_enabled": True,
            "iqfeed_poll_seconds": 0.25,
            "min_tick_interval_ms": 250,
            "source_markers": {
                "has_notify_handler": True,
                "has_notify_admission": True,
                "has_refresh_viability": True,
                "has_immediate_notify_submit": True,
                "has_iqfeed_listen_channel": True,
            },
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_seconds_scale_iqfeed_poll_fallback() -> None:
    ok, errors = evaluate_live_loop_timing_config(
        {
            "iqfeed_notify_enabled": True,
            "iqfeed_tape_enabled": True,
            "iqfeed_poll_fallback_enabled": True,
            "iqfeed_poll_seconds": 2.0,
            "min_tick_interval_ms": 250,
        }
    )

    assert ok is False
    assert "live_loop_iqfeed_poll_seconds_too_slow:2" in errors


def test_runtime_guard_rejects_disabled_iqfeed_notify_or_tape_paths() -> None:
    ok, errors = evaluate_live_loop_timing_config(
        {
            "iqfeed_notify_enabled": False,
            "iqfeed_tape_enabled": False,
            "iqfeed_poll_fallback_enabled": True,
            "iqfeed_poll_seconds": 0.25,
            "min_tick_interval_ms": 250,
        }
    )

    assert ok is False
    assert "live_loop_iqfeed_notify_disabled" in errors
    assert "live_loop_iqfeed_tape_disabled" in errors


def test_runtime_guard_rejects_missing_iqfeed_notify_source_markers() -> None:
    ok, errors = evaluate_live_loop_timing_config(
        {
            "iqfeed_notify_enabled": True,
            "iqfeed_tape_enabled": True,
            "iqfeed_poll_fallback_enabled": True,
            "iqfeed_poll_seconds": 0.25,
            "min_tick_interval_ms": 250,
            "source_markers": {
                "has_notify_handler": True,
                "has_notify_admission": False,
                "has_refresh_viability": False,
                "has_immediate_notify_submit": False,
                "has_iqfeed_listen_channel": True,
            },
        }
    )

    assert ok is False
    assert "live_loop_source_marker_missing:_admit_iqfeed_symbol" in errors
    assert "live_loop_source_marker_missing:refresh_viability=True" in errors
    assert 'live_loop_source_marker_missing:cause="iqfeed_notify"' in errors


def test_runtime_guard_accepts_iqfeed_bridge_notify_source_markers() -> None:
    ok, errors = evaluate_iqfeed_bridge_notify_source(
        {
            "source_markers": {
                "has_notify_enabled_flag": True,
                "has_notify_channel": True,
                "has_pg_notify_statement": True,
                "has_notify_payload_symbol": True,
                "has_notify_payload_observed_at": True,
                "has_notify_payload_source": True,
                "has_notify_after_nbbo_branch": True,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_missing_iqfeed_bridge_notify_source_markers() -> None:
    ok, errors = evaluate_iqfeed_bridge_notify_source(
        {
            "source_markers": {
                "has_notify_enabled_flag": True,
                "has_notify_channel": True,
                "has_pg_notify_statement": False,
                "has_notify_payload_symbol": False,
                "has_notify_payload_observed_at": True,
                "has_notify_payload_source": False,
                "has_notify_after_nbbo_branch": True,
            }
        }
    )

    assert ok is False
    assert "iqfeed_bridge_notify_source_marker_missing:SELECT pg_notify" in errors
    assert 'iqfeed_bridge_notify_source_marker_missing:"symbol": sym' in errors
    assert 'iqfeed_bridge_notify_source_marker_missing:"source": "iqfeed_l1"' in errors


def test_runtime_guard_accepts_ross_entry_shape_smoke() -> None:
    ok, errors = evaluate_ross_entry_shape_smoke(
        {
            "jem_1m_breakout_attempt_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "lhai_5m_abcd_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "lhai_tick_label_no_frame_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "lhai_pre_candidate_micro_error_5m_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "canf_tick_first_pullback_allow": None,
            "jem_tick_breakout_allow": None,
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_ross_entry_shape_source_markers() -> None:
    ok, errors = evaluate_ross_entry_shape_smoke(
        {
            "source_markers": {
                "has_entry_shape_block": True,
                "has_pre_candidate_block": True,
                "has_shape_reason": True,
                "has_pre_candidate_event": True,
                "has_5m_block": True,
                "has_tick_label_not_enough": True,
                "has_scheduler_entry_wall": True,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_missing_scheduler_entry_wall_marker() -> None:
    ok, errors = evaluate_ross_entry_shape_smoke(
        {
            "source_markers": {
                "has_entry_shape_block": True,
                "has_pre_candidate_block": True,
                "has_shape_reason": True,
                "has_pre_candidate_event": True,
                "has_5m_block": True,
                "has_tick_label_not_enough": True,
                "has_scheduler_entry_wall": False,
            }
        }
    )

    assert ok is False
    assert "ross_entry_shape_source_marker_missing:ross_equity_scheduler_entry_wall" in errors


def test_runtime_guard_rejects_stale_ross_entry_shape_smoke() -> None:
    ok, errors = evaluate_ross_entry_shape_smoke(
        {
            "jem_1m_breakout_attempt_block": None,
            "lhai_5m_abcd_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "lhai_tick_label_no_frame_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "lhai_pre_candidate_micro_error_5m_block": {"reason": "ross_live_requires_tick_tape_revalidation"},
            "canf_tick_first_pullback_allow": None,
            "jem_tick_breakout_allow": None,
        }
    )

    assert ok is False
    assert any(err.startswith("ross_entry_shape_smoke_failed:jem_1m_breakout_attempt_block") for err in errors)


def test_runtime_guard_accepts_ross_reentry_smoke() -> None:
    ok, errors = evaluate_ross_reentry_smoke(
        {
            "ross_equity_reentry_allowed": False,
            "crypto_reentry_allowed": True,
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_ross_reentry_source_markers() -> None:
    ok, errors = evaluate_ross_reentry_smoke(
        {
            "source_markers": {
                "has_session_helper": True,
                "has_ross_family_check": True,
                "has_stock_check": True,
                "has_forced_false": True,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_ross_equity_reentry_smoke() -> None:
    ok, errors = evaluate_ross_reentry_smoke(
        {
            "ross_equity_reentry_allowed": True,
            "crypto_reentry_allowed": True,
        }
    )

    assert ok is False
    assert any(err.startswith("ross_reentry_smoke_failed:ross_equity_reentry_allowed") for err in errors)


def test_runtime_guard_accepts_a_setup_size_floor_smoke() -> None:
    ok, errors = evaluate_a_setup_size_floor_smoke(
        {
            "reason": "hard_reducer_respected",
            "hard_reducers": {"severe_liquidity": 0.5},
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_a_setup_size_floor_source_markers() -> None:
    ok, errors = evaluate_a_setup_size_floor_smoke(
        {
            "source_markers": {
                "has_hard_reducer_reason": True,
                "has_hard_blocker_label": False,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_accepts_ross_starter_alias_coverage_smoke() -> None:
    ok, errors = evaluate_ross_starter_alias_coverage_smoke(
        {
            "setup_trace": {
                "setup_alias": "ross_breakout_starter_tick",
                "setup_coverage": "structural_a_setup",
                "structural_stop_covered": True,
                "a_setup_floor_covered": True,
                "source_wait_tick_armed": True,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_ross_starter_alias_without_a_floor_coverage() -> None:
    ok, errors = evaluate_ross_starter_alias_coverage_smoke(
        {
            "setup_trace": {
                "setup_alias": "ross_breakout_starter_tick",
                "setup_coverage": "structural_a_setup",
                "structural_stop_covered": True,
                "a_setup_floor_covered": False,
                "source_wait_tick_armed": True,
            }
        }
    )

    assert ok is False
    assert "ross_starter_alias_coverage_failed:a_setup_floor_covered:False" in errors


def test_runtime_guard_accepts_ross_exit_shape_source_markers() -> None:
    ok, errors = evaluate_ross_exit_shape_smoke(
        {
            "source_markers": {
                "has_tick_tape_helper": True,
                "has_smart_hold_for_ross": True,
                "legacy_bail_excludes_ross": True,
            }
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_ross_exit_shape_without_legacy_exclusion() -> None:
    ok, errors = evaluate_ross_exit_shape_smoke(
        {
            "source_markers": {
                "has_tick_tape_helper": True,
                "has_smart_hold_for_ross": True,
                "legacy_bail_excludes_ross": False,
            }
        }
    )

    assert ok is False
    assert any(err.startswith("ross_exit_shape_source_marker_missing") for err in errors)


def test_runtime_guard_rejects_a_setup_hard_blocker_label() -> None:
    ok, errors = evaluate_a_setup_size_floor_smoke(
        {
            "reason": "hard_blocker",
            "hard_blockers": {"severe_liquidity": 0.5},
        }
    )

    assert ok is False
    assert "a_setup_size_floor_smoke_failed:reason:hard_blocker" in errors
    assert "a_setup_size_floor_smoke_failed:hard_blockers_label_present" in errors


def test_runtime_guard_accepts_ross_symbol_resolution_smoke() -> None:
    ok, errors = evaluate_ross_symbol_resolution_smoke(
        {
            "warnings": [
                {
                    "mentioned_symbol": "DXTS",
                    "reason": "mentioned_symbol_unresolved_near_market_symbol",
                    "near_symbols": [{"symbol": "DXF", "edit_distance": 2}],
                }
            ]
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_missing_ross_symbol_resolution_smoke() -> None:
    ok, errors = evaluate_ross_symbol_resolution_smoke({"warnings": []})

    assert ok is False
    assert any(err.startswith("ross_symbol_resolution_smoke_failed:no_warning") for err in errors)


def test_runtime_guard_accepts_replay_scheduler_snapshot_smoke() -> None:
    ok, errors = evaluate_replay_scheduler_snapshot_smoke(
        {
            "snapshot_enabled": True,
            "emitter_callable": True,
            "venue_state_callable": True,
            "event_type": "live_replay_scheduler_snapshot",
            "audit_contract": {
                "has_evidence_status": True,
                "has_missing_evidence": True,
                "has_multi_snapshot_missing_key": True,
                "has_counterfactual_missing_key": True,
                "has_opportunity_label_missing_key": True,
                "pnl_minmax_claim_ready": False,
            },
            "source_markers": {
                "has_first_class_flag": True,
                "has_snapshot_event_type": True,
                "has_emitter": True,
                "has_best_effort_comment": True,
                "has_plan_hook": True,
                "has_payload_selected_ids": True,
                "has_payload_prefilter_results": True,
                "has_payload_venue_states": True,
                "has_replay_evidence_status": True,
                "has_replay_missing_evidence": True,
                "has_opportunity_label_evidence": True,
                "has_opportunity_label_export": True,
                "has_pnl_minmax_label_gate": True,
                "has_event_snapshot_export": True,
                "has_event_loop_snapshot_emitter": True,
            },
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_missing_replay_scheduler_snapshot_marker() -> None:
    ok, errors = evaluate_replay_scheduler_snapshot_smoke(
        {
            "snapshot_enabled": False,
            "emitter_callable": True,
            "venue_state_callable": True,
            "event_type": "live_replay_scheduler_snapshot",
            "audit_contract": {
                "has_evidence_status": False,
                "has_missing_evidence": True,
                "has_multi_snapshot_missing_key": True,
                "has_counterfactual_missing_key": True,
                "has_opportunity_label_missing_key": False,
                "pnl_minmax_claim_ready": True,
            },
            "source_markers": {
                "has_first_class_flag": True,
                "has_snapshot_event_type": True,
                "has_emitter": False,
                "has_best_effort_comment": True,
                "has_plan_hook": True,
                "has_payload_selected_ids": True,
                "has_payload_prefilter_results": True,
                "has_payload_venue_states": True,
                "has_replay_evidence_status": False,
                "has_replay_missing_evidence": True,
                "has_opportunity_label_evidence": False,
                "has_opportunity_label_export": False,
                "has_pnl_minmax_label_gate": False,
                "has_event_snapshot_export": False,
                "has_event_loop_snapshot_emitter": True,
            },
        }
    )

    assert ok is False
    assert "replay_snapshot_smoke_failed:snapshot_enabled:False" in errors
    assert "replay_audit_contract_failed:has_evidence_status:False" in errors
    assert "replay_audit_contract_failed:has_opportunity_label_missing_key:False" in errors
    assert "replay_audit_contract_failed:pnl_minmax_claim_ready:True" in errors
    assert "replay_snapshot_source_marker_missing:has_emitter" in errors
    assert "replay_snapshot_source_marker_missing:has_replay_evidence_status" in errors
    assert "replay_snapshot_source_marker_missing:has_opportunity_label_evidence" in errors
    assert "replay_snapshot_source_marker_missing:has_opportunity_label_export" in errors
    assert "replay_snapshot_source_marker_missing:has_pnl_minmax_label_gate" in errors
    assert "replay_snapshot_source_marker_missing:has_event_snapshot_export" in errors


def test_runtime_guard_accepts_zero_active_like_sessions() -> None:
    ok, errors = evaluate_no_active_like_sessions({"active_like_count": 0, "rows": []})

    assert ok is True
    assert errors == []


def test_runtime_guard_allows_passive_watches_for_reload_preflight() -> None:
    ok, errors = evaluate_no_active_like_sessions(
        {
            "active_like_count": 32,
            "passive_watch_count": 32,
            "reload_blocking_count": 0,
            "rows": [{"id": 101, "symbol": "JEM", "state": "watching_live"}],
            "reload_blocking_rows": [],
        }
    )

    assert ok is True
    assert errors == []


def test_runtime_guard_rejects_reload_blocking_live_risk() -> None:
    ok, errors = evaluate_no_active_like_sessions(
        {
            "active_like_count": 2,
            "passive_watch_count": 1,
            "reload_blocking_count": 1,
            "rows": [
                {"id": 101, "symbol": "JEM", "state": "watching_live"},
                {"id": 102, "symbol": "IPW", "state": "live_pending_entry"},
            ],
            "reload_blocking_rows": [{"id": 102, "symbol": "IPW", "state": "live_pending_entry"}],
        }
    )

    assert ok is False
    assert "reload_blocking_live_risk_present:count=1:passive_watch_count=1" in errors[0]
    assert "IPW" in errors[0]


def test_runtime_guard_rejects_active_like_sessions_for_reload_preflight() -> None:
    ok, errors = evaluate_no_active_like_sessions(
        {
            "active_like_count": 2,
            "rows": [
                {"id": 101, "symbol": "JEM", "state": "watching_live"},
                {"id": 102, "symbol": "IPW", "state": "live_entered"},
            ],
        }
    )

    assert ok is False
    assert "active_like_sessions_present:count=2" in errors[0]
    assert "JEM" in errors[0]


def test_runtime_guard_active_session_preflight_only_accepts_zero(monkeypatch, capsys) -> None:
    def fail_if_heavy_stage_runs():
        raise AssertionError("heavy runtime stage should not run")

    monkeypatch.setattr(runtime_guard, "_docker_containers", fail_if_heavy_stage_runs)
    monkeypatch.setattr(
        runtime_guard,
        "_docker_active_like_sessions_config",
        lambda: {"active_like_count": 0, "rows": []},
    )

    code = runtime_guard.main(["--active-session-preflight-only"])

    captured = capsys.readouterr()
    assert code == 0
    assert "momentum_worker_no_reload_blocking_live_risk" in captured.out


def test_runtime_guard_active_session_preflight_only_rejects_active_rows(monkeypatch, capsys) -> None:
    def fail_if_heavy_stage_runs():
        raise AssertionError("heavy runtime stage should not run")

    monkeypatch.setattr(runtime_guard, "_docker_containers", fail_if_heavy_stage_runs)
    monkeypatch.setattr(
        runtime_guard,
        "_docker_active_like_sessions_config",
        lambda: {
            "active_like_count": 1,
            "rows": [{"id": 10390, "symbol": "JEM", "state": "watching_live"}],
        },
    )

    code = runtime_guard.main(["--active-session-preflight-only"])

    captured = capsys.readouterr()
    assert code == 1
    assert "active_like_sessions_present:count=1" in captured.err
    assert "JEM" in captured.err


def test_runtime_guard_reload_preflight_only_accepts_clean_reload_window(monkeypatch, capsys) -> None:
    def fail_if_heavy_stage_runs():
        raise AssertionError("heavy runtime stage should not run")

    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(runtime_guard, "_docker_worker_lifecycle_config", lambda: {"quiet_seconds": 90.0, "events": []})
    monkeypatch.setattr(runtime_guard, "_docker_active_like_sessions_config", lambda: {"active_like_count": 0, "rows": []})
    monkeypatch.setattr(
        runtime_guard,
        "_docker_source_reload_freshness_config",
        lambda: {
            "started_at": "2026-07-02T17:20:00+00:00",
            "source_mtimes": {"/app/app/services/trading/momentum_neural/live_runner.py": "2026-07-02T17:19:00+00:00"},
        },
    )
    monkeypatch.setattr(runtime_guard, "_docker_compose_momentum_exec_service_config", _compose_momentum_exec_service)
    monkeypatch.setattr(runtime_guard, "_docker_transcript_gate_config", fail_if_heavy_stage_runs)

    code = runtime_guard.main(["--reload-preflight-only"])

    captured = capsys.readouterr()
    assert code == 0
    assert "momentum_worker_reload_preflight_ok" in captured.out


def test_runtime_guard_reload_preflight_only_rejects_lifecycle_noise(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(runtime_guard, "_docker_active_like_sessions_config", lambda: {"active_like_count": 0, "rows": []})
    monkeypatch.setattr(
        runtime_guard,
        "_docker_source_reload_freshness_config",
        lambda: {
            "started_at": "2026-07-02T17:20:00+00:00",
            "source_mtimes": {"/app/app/services/trading/momentum_neural/live_runner.py": "2026-07-02T17:19:00+00:00"},
        },
    )
    monkeypatch.setattr(runtime_guard, "_docker_compose_momentum_exec_service_config", _compose_momentum_exec_service)
    monkeypatch.setattr(
        runtime_guard,
        "_docker_worker_lifecycle_config",
        lambda: {
            "quiet_seconds": 90.0,
            "now": "2026-07-02T16:36:00+00:00",
            "events": [
                {
                    "Action": "restart",
                    "timeNano": 1783010100000000000,
                    "Actor": {"Attributes": {"exitCode": "137"}},
                }
            ],
        },
    )

    code = runtime_guard.main(["--reload-preflight-only"])

    captured = capsys.readouterr()
    assert code == 1
    assert "worker_lifecycle_quiet_failed:action=restart" in captured.err


def test_runtime_guard_reload_preflight_only_rejects_stale_mounted_source(monkeypatch, capsys) -> None:
    def fail_if_lifecycle_check_runs():
        raise AssertionError("lifecycle check should not run after source freshness fails")

    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(runtime_guard, "_docker_active_like_sessions_config", lambda: {"active_like_count": 0, "rows": []})
    monkeypatch.setattr(
        runtime_guard,
        "_docker_source_reload_freshness_config",
        lambda: {
            "started_at": "2026-07-02T17:20:00+00:00",
            "source_mtimes": {"/app/scripts/verify_momentum_worker_runtime.py": "2026-07-02T17:21:00+00:00"},
        },
    )
    monkeypatch.setattr(runtime_guard, "_docker_worker_lifecycle_config", fail_if_lifecycle_check_runs)

    code = runtime_guard.main(["--reload-preflight-only"])

    captured = capsys.readouterr()
    assert code == 1
    assert "source_reload_freshness_failed:worker_older_than_source" in captured.err


def test_runtime_guard_reload_preflight_only_rejects_active_rows(monkeypatch, capsys) -> None:
    def fail_if_lifecycle_check_runs():
        raise AssertionError("lifecycle check should not run while active sessions block reload")

    def fail_if_source_check_runs():
        raise AssertionError("source freshness should not run while active sessions block reload")

    monkeypatch.setattr(runtime_guard, "_docker_containers", lambda: [_worker()])
    monkeypatch.setattr(runtime_guard, "_docker_worker_lifecycle_config", fail_if_lifecycle_check_runs)
    monkeypatch.setattr(runtime_guard, "_docker_source_reload_freshness_config", fail_if_source_check_runs)
    monkeypatch.setattr(
        runtime_guard,
        "_docker_active_like_sessions_config",
        lambda: {
            "active_like_count": 1,
            "rows": [{"id": 10390, "symbol": "JEM", "state": "watching_live"}],
        },
    )

    code = runtime_guard.main(["--reload-preflight-only"])

    captured = capsys.readouterr()
    assert code == 1
    assert "active_like_sessions_present:count=1" in captured.err
