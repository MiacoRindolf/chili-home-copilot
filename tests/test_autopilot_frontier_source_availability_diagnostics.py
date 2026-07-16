from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_source_availability_diagnostics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_availability_diagnostics",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_live_probe_uses_exact_read_only_56_source_command(tmp_path):
    diagnostics = _load_module()
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_runner(args, _timeout_seconds, input_text=None):
        calls.append((tuple(args), input_text))
        if tuple(args) == ("codex", "--version"):
            return subprocess.CompletedProcess(args, 0, stdout="codex-cli 0.144.1", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="frontier-probe-ok", stderr="")

    summary = diagnostics.run_diagnostics(
        source_kinds=("codex",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )

    source = summary["sources"][0]
    assert source["probe_status"] == "live_probe_passed"
    assert source["blocker"] == "missing_source_bundle"
    assert source["source_auth_mode"] == "account"
    assert "--model gpt-5.6-sol" in source["probe_command"]
    assert "--sandbox read-only" in source["probe_command"]
    assert "model_reasoning_effort=\"xhigh\"" in source["probe_command"]
    assert source["source_runner_command"].startswith(
        "python scripts/autopilot_frontier_source_runner.py --source-kind codex"
    )
    assert calls[-1][1] == "Return exactly: frontier-probe-ok\n"


def test_codex_live_probe_classifies_outdated_cli(tmp_path):
    diagnostics = _load_module()

    def fake_runner(args, _timeout_seconds, input_text=None):
        if tuple(args) == ("codex", "--version"):
            return subprocess.CompletedProcess(args, 0, stdout="codex-cli old", stderr="")
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="The gpt-5.6-sol model requires a newer version of Codex.",
        )

    summary = diagnostics.run_diagnostics(
        source_kinds=("codex",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )

    source = summary["sources"][0]
    assert source["probe_status"] == "cli_outdated"
    assert source["blocker"] == "codex_cli_outdated"
    assert "Update the Codex CLI" in source["next_action"]


def test_codex_live_probe_classifies_usage_quota_without_blaming_auth(tmp_path):
    diagnostics = _load_module()

    def fake_runner(args, _timeout_seconds, input_text=None):
        if tuple(args) == ("codex", "--version"):
            return subprocess.CompletedProcess(args, 0, stdout="codex-cli 0.144.1", stderr="")
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(
                "You've hit your usage limit. Visit the usage page to purchase more credits "
                "or try again at 3:45 PM."
            ),
        )

    summary = diagnostics.run_diagnostics(
        source_kinds=("codex",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )

    source = summary["sources"][0]
    assert source["probe_status"] == "quota_exhausted"
    assert source["blocker"] == "codex_quota_exhausted"
    assert source["credential_status"] == "account_quota_limited"
    assert "usage window to reset" in source["next_action"]


def test_claude_live_probe_uses_exact_fable5_max_effort_command(tmp_path):
    diagnostics = _load_module()
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_runner(args, _timeout_seconds, input_text=None):
        command = tuple(args)
        calls.append((command, input_text))
        if command == ("claude", "--version"):
            return subprocess.CompletedProcess(args, 0, stdout="2.1.206 (Claude Code)", stderr="")
        if command == ("claude", "auth", "status", "--json"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    '{"loggedIn":true,"authMethod":"claude.ai",'
                    '"apiProvider":"firstParty","subscriptionType":"max"}'
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    summary = diagnostics.run_diagnostics(
        source_kinds=("claude",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )

    source = summary["sources"][0]
    assert source["probe_status"] == "live_probe_passed"
    assert source["blocker"] == "missing_source_bundle"
    assert source["source_auth_mode"] == "subscription"
    assert "--model claude-fable-5" in source["probe_command"]
    assert "--effort max" in source["probe_command"]
    assert calls[-1][1] == "Reply with exactly: ok\n"


def test_claude_live_probe_classifies_fable5_model_unavailable(tmp_path):
    diagnostics = _load_module()

    def fake_runner(args, _timeout_seconds, input_text=None):
        command = tuple(args)
        if command == ("claude", "--version"):
            return subprocess.CompletedProcess(args, 0, stdout="2.1.158 (Claude Code)", stderr="")
        if command == ("claude", "auth", "status", "--json"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    '{"loggedIn":true,"authMethod":"claude.ai",'
                    '"apiProvider":"firstParty","subscriptionType":"max"}'
                ),
                stderr="",
            )
        if command == ("claude", "setup-token", "--help"):
            return subprocess.CompletedProcess(args, 0, stdout="usage", stderr="")
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="There's an issue with the selected model. It may not exist (404).",
            stderr="",
        )

    summary = diagnostics.run_diagnostics(
        source_kinds=("claude",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )

    source = summary["sources"][0]
    assert source["probe_status"] == "model_unavailable"
    assert source["blocker"] == "claude_fable5_unavailable"
    assert "Update Claude Code" in source["next_action"]
    assert "claude-fable-5" in source["next_action"]
