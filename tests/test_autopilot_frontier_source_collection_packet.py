from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_source_collection_packet.py"
AVAILABILITY_SCRIPT_PATH = (
    REPO_ROOT / "scripts" / "autopilot_frontier_source_availability_diagnostics.py"
)


def _load_packet_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_collection_packet",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_availability_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_availability_diagnostics",
        AVAILABILITY_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bundle_dir(tmp_path: Path) -> Path:
    from scripts.autopilot_frontier_prompt_pack_bundle import build_prompt_pack_bundle

    bundle = tmp_path / "frontier_model_prompt_packs"
    build_prompt_pack_bundle(output_dir=bundle)
    return bundle


def test_collection_packet_writes_codex_and_claude_packets_by_default(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    output_dir = tmp_path / "packets"

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=output_dir,
    )

    assert summary["schema"] == packet.FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION
    assert summary["status"] == "passed"
    assert summary["source_kinds"] == ["codex", "claude"]
    packets = {item["source_kind"]: item for item in summary["packets"]}
    assert packets["codex"]["model_name"] == "gpt-5.6-sol"
    assert packets["codex"]["source_runner_command"].startswith(
        "python scripts/autopilot_frontier_source_runner.py --source-kind codex"
    )
    assert packets["claude"]["model_name"] == "claude-fable-5"
    for source_kind in ("codex", "claude"):
        packet_path = Path(packets[source_kind]["packet"])
        assert packet_path.is_file()
        text = packet_path.read_text(encoding="utf-8")
        assert f"- Source kind: {source_kind}" in text
        assert "Prompt pack SHA-256" in text
        assert "Response staging file" in text
        assert "Automated source runner command" in text
        assert "Recommended recorder command" in text
        assert "Write/import recorder command" in text
        assert "Intake validation command" in text
        assert "Publish scorecards command" in text
        assert "--all-cases" in text
        assert "--no-write" in text
        assert "--allow-partial --json --no-write" in text
        assert "--publish-scorecards --json" in text
        assert "All-Cases Response Contract" in text
        assert "Enforced Case Matrix" in text
        assert "Post-Import Validation Loop" in text
        assert "real-chili-preflight-candidate-wins" in text
        assert "Single-case fallback command" in text
        assert "autopilot_frontier_source_evidence_recorder.py" in text
        assert "autopilot_frontier_source_runner.py" in text
        assert "Claims about PR state" in text
        assert "do not mutate source/tests, git, PR state" in text
    markdown = packet.render_summary(summary)
    assert "Dry-run recorder command" in markdown
    assert "Source runner" in markdown
    assert "Write/import command" in markdown
    assert "Intake validation" in markdown
    assert "Publish command" in markdown


def test_collection_packet_no_write_leaves_output_dir_empty(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    output_dir = tmp_path / "packets"

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=tmp_path / "raw_sources",
        output_dir=output_dir,
        write=False,
    )

    assert summary["status"] == "passed"
    assert summary["write"] is False
    assert not output_dir.exists()


def test_collection_packet_tracks_ready_and_missing_source_state(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    codex = raw_root / "codex"
    raw = codex / "raw"
    raw.mkdir(parents=True)
    prompt_pack = bundle / "codex" / "prompt_pack.md"
    prompt_sha = hashlib.sha256(prompt_pack.read_bytes()).hexdigest()
    (codex / "metadata.json").write_text(
        json.dumps(
            {
                "model_name": "gpt-5.6-sol",
                "prompt_pack_sha256": prompt_sha,
            }
        ),
        encoding="utf-8",
    )
    (codex / "prompt_pack.md").write_bytes(prompt_pack.read_bytes())
    (codex / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (raw / "candidate.json").write_text("{}\n", encoding="utf-8")

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=tmp_path / "packets",
        source_kinds=["all"],
    )

    packets = {item["source_kind"]: item for item in summary["packets"]}
    assert packets["codex"]["status"] == "ready"
    assert packets["codex"]["missing_files"] == []
    assert packets["claude"]["status"] == "missing"
    assert any("claude" in item for item in packets["claude"]["missing_files"])
    assert packets["local_model"]["status"] == "missing"
    assert packets["local_model"]["source_runner_command"].startswith(
        "python scripts/autopilot_frontier_source_runner.py --source-kind local_model"
    )


def test_collection_packet_rejects_stale_model_and_cross_root_availability(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    codex = raw_root / "codex"
    raw = codex / "raw"
    raw.mkdir(parents=True)
    stale_prompt = "# stale gpt-5.5 prompt\n"
    stale_sha = hashlib.sha256(stale_prompt.encode("utf-8")).hexdigest()
    (codex / "metadata.json").write_text(
        json.dumps({"model_name": "gpt-5.5", "prompt_pack_sha256": stale_sha}),
        encoding="utf-8",
    )
    (codex / "prompt_pack.md").write_text(stale_prompt, encoding="utf-8")
    (codex / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (raw / "candidate.json").write_text("{}\n", encoding="utf-8")
    availability_report = tmp_path / "availability.md"
    availability_report.write_text(
        "\n".join(
            [
                "# CHILI Frontier Source Availability Diagnostics",
                f"- Raw source root: {tmp_path / 'different_root'}",
                "- Codex source status: ready",
                "- Codex probe status: live_probe_passed",
                "- Codex blocker: none",
                "- Codex source runner command: stale-cross-root-command",
                "- Codex next action: none",
            ]
        ),
        encoding="utf-8",
    )

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=tmp_path / "packets",
        availability_report=availability_report,
        source_kinds=["codex"],
    )

    codex_packet = summary["packets"][0]
    assert codex_packet["status"] == "partial"
    assert any("model_name=gpt-5.5 expected=gpt-5.6-sol" in issue for issue in codex_packet["missing_files"])
    assert any("prompt_pack_sha256" in issue for issue in codex_packet["missing_files"])
    assert codex_packet["availability_probe_status"] == ""
    assert codex_packet["source_runner_command"] != "stale-cross-root-command"


def test_claude_collection_packet_includes_availability_recovery(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    output_dir = tmp_path / "packets"
    availability_report = tmp_path / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
    availability_report.write_text(
        "\n".join(
            [
                "# CHILI Frontier Source Availability Diagnostics",
                "",
                "- Claude source status: partial",
                "- Claude probe status: auth_failed",
                "- Claude blocker: claude_auth_failed",
                "- Claude credential status: env_credentials_absent; logged_in",
                "- Claude source auth mode: subscription",
                "- Claude API-key probe status: api_key_missing",
                "- Claude source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json",
                "- Claude next action: Run `claude setup-token` in a trusted interactive terminal; then collect/import a real all-cases Claude response.",
            ]
        ),
        encoding="utf-8",
    )

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=output_dir,
        availability_report=availability_report,
        source_kinds=["claude"],
    )

    packet_payload = summary["packets"][0]
    packet_text = Path(packet_payload["packet"]).read_text(encoding="utf-8")
    summary_text = packet.render_summary(summary)

    assert packet_payload["availability_probe_status"] == "auth_failed"
    assert packet_payload["availability_blocker"] == "claude_auth_failed"
    assert packet_payload["availability_source_auth_mode"] == "subscription"
    assert packet_payload["availability_api_key_probe_status"] == "api_key_missing"
    assert "claude setup-token" in packet_payload["availability_next_action"]
    assert "autopilot_frontier_source_runner.py --source-kind claude" in packet_payload[
        "source_runner_command"
    ]
    assert "- Availability probe status: auth_failed" in packet_text
    assert "- Automated source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json" in packet_text
    assert "## Availability Recovery" in packet_text
    assert "- Blocker: claude_auth_failed" in packet_text
    assert "- Source auth mode: subscription" in packet_text
    assert "- API-key probe status: api_key_missing" in packet_text
    assert "Run `claude setup-token`" in packet_text
    assert "Availability report" in summary_text
    assert "claude_auth_failed" in summary_text


def test_collection_packet_rejects_missing_prompt_pack_manifest(tmp_path):
    packet = _load_packet_module()

    try:
        packet.build_collection_packets(
            prompt_pack_bundle_dir=tmp_path / "missing_bundle",
            raw_source_root=tmp_path / "raw_sources",
            output_dir=tmp_path / "packets",
        )
    except packet.FrontierSourceCollectionPacketError as exc:
        assert "does not exist" in str(exc) or "invalid" in str(exc)
    else:
        raise AssertionError("missing prompt-pack manifest should be rejected")


def test_collection_packet_cli_json_no_write(tmp_path, capsys):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    output_dir = tmp_path / "packets"

    exit_code = packet.main(
        [
            "--prompt-pack-bundle-dir",
            str(bundle),
            "--raw-source-root",
            str(tmp_path / "raw_sources"),
            "--output-dir",
            str(output_dir),
            "--source-kind",
            "codex",
            "--json",
            "--no-write",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == packet.FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION
    assert payload["source_kinds"] == ["codex"]
    packet_payload = payload["packets"][0]
    assert "codex_all_cases_response.txt" in packet_payload["response_staging_file"]
    assert "--no-write" in packet_payload["dry_run_recorder_command"]
    assert "--allow-partial --json --no-write" in packet_payload["validation_command"]
    assert "--publish-scorecards --json" in packet_payload["publish_command"]
    assert "autopilot_frontier_source_evidence_recorder.py" in packet_payload[
        "all_cases_recorder_command"
    ]
    assert "--all-cases" in packet_payload["all_cases_recorder_command"]
    assert "--no-write" not in packet_payload["all_cases_recorder_command"]
    assert "--case-id <case-id>" in packet_payload["recorder_command"]
    assert not output_dir.exists()


def test_collection_packet_cli_json_still_writes_human_summary(tmp_path, capsys):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    output_dir = tmp_path / "packets"
    summary_output = tmp_path / "summary.md"

    exit_code = packet.main(
        [
            "--prompt-pack-bundle-dir",
            str(bundle),
            "--raw-source-root",
            str(tmp_path / "raw_sources"),
            "--output-dir",
            str(output_dir),
            "--summary-output",
            str(summary_output),
            "--source-kind",
            "claude",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["packets"][0]["model_name"] == "claude-fable-5"
    assert summary_output.is_file()
    summary_text = summary_output.read_text(encoding="utf-8")
    assert "claude-fable-5" in summary_text
    assert "claude-opus" not in summary_text


def test_source_availability_records_claude_auth_failure(tmp_path, monkeypatch):
    availability = _load_availability_module()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list[tuple[str, ...]] = []

    def fake_runner(args, _timeout, _input_text=None):
        calls.append(tuple(args))
        if tuple(args) == ("claude", "--version"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout="2.1.202 (Claude Code)\n",
                stderr="",
            )
        if tuple(args) == ("claude", "auth", "status", "--json"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "apiProvider": "firstParty",
                        "email": "user@example.com",
                        "subscriptionType": "max",
                    }
                ),
                stderr="",
            )
        if tuple(args) == ("claude", "setup-token", "--help"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout="Usage: claude setup-token [options]\n",
                stderr="",
            )
        return availability.subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="Failed to authenticate. API Error: 401 Invalid authentication credentials",
        )

    summary = availability.run_diagnostics(
        source_kinds=("claude",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )
    markdown = availability.render_report(summary)
    source = summary["sources"][0]

    assert summary["status"] == "warning"
    assert source["probe_status"] == "auth_failed"
    assert source["blocker"] == "claude_auth_failed"
    assert source["credential_status"].endswith("logged_in")
    assert "auth_method=claude.ai" in source["credential_detail"]
    assert "subscription=max" in source["credential_detail"]
    assert source["source_auth_mode"] == "subscription"
    assert source["api_key_probe_status"] == "api_key_missing"
    assert "--source-auth-mode auto" in source["source_runner_command"]
    assert "claude setup-token --help" in source["credential_detail"]
    assert "setup_token_available" in source["next_action"]
    assert "user@example.com" not in source["credential_detail"]
    assert "claude setup-token" in source["next_action"]
    assert "claude auth logout" in source["next_action"]
    assert "claude auth login --claudeai" in source["next_action"]
    assert "ANTHROPIC_API_KEY" in source["next_action"]
    assert "current source runner auth mode is subscription" in source["next_action"]
    assert "- Claude probe status: auth_failed" in markdown
    assert "- Claude credential status: env_credentials_absent; logged_in" in markdown
    assert "- Claude source auth mode: subscription" in markdown
    assert "- Claude API-key probe status: api_key_missing" in markdown
    assert "- Claude source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json" in markdown
    assert "401 Invalid authentication credentials" in markdown
    assert calls[0] == ("claude", "--version")
    assert calls[1] == ("claude", "auth", "status", "--json")
    assert calls[2][0:3] == ("claude", "--print", "--model")
    assert calls[3] == ("claude", "setup-token", "--help")


def test_source_availability_uses_claude_api_key_probe_when_available(tmp_path, monkeypatch):
    availability = _load_availability_module()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    calls: list[tuple[str, ...]] = []

    def fake_runner(args, _timeout, _input_text=None):
        calls.append(tuple(args))
        if tuple(args) == ("claude", "--version"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout="2.1.202 (Claude Code)\n",
                stderr="",
            )
        if tuple(args) == ("claude", "auth", "status", "--json"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "loggedIn": False,
                        "authMethod": "api_key",
                        "apiProvider": "anthropic",
                        "subscriptionType": "none",
                    }
                ),
                stderr="",
            )
        if tuple(args)[0:4] == ("claude", "--bare", "--print", "--model"):
            return availability.subprocess.CompletedProcess(
                args,
                0,
                stdout="ok\n",
                stderr="",
            )
        return availability.subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="unexpected command",
        )

    summary = availability.run_diagnostics(
        source_kinds=("claude",),
        raw_source_root=tmp_path / "raw_sources",
        runner=fake_runner,
        probe_live=True,
    )
    markdown = availability.render_report(summary)
    source = summary["sources"][0]
    serialized = json.dumps(summary, sort_keys=True) + markdown

    assert summary["status"] == "warning"
    assert source["probe_status"] == "live_probe_passed"
    assert source["blocker"] == "missing_source_bundle"
    assert source["source_auth_mode"] == "api_key"
    assert source["api_key_probe_status"] == "api_key_available"
    assert "env_credentials_present:ANTHROPIC_API_KEY" in source["credential_status"]
    assert source["probe_command"].startswith("claude --bare --print --model")
    assert "--source-auth-mode auto" in source["source_runner_command"]
    assert "- Claude source auth mode: api_key" in markdown
    assert "- Claude API-key probe status: api_key_available" in markdown
    assert "sk-ant-test-secret" not in serialized
    assert calls[0] == ("claude", "--version")
    assert calls[1] == ("claude", "auth", "status", "--json")
    assert calls[2][0:4] == ("claude", "--bare", "--print", "--model")
    assert ("claude", "setup-token", "--help") not in calls


def test_source_availability_command_wrapper_reports_missing_executable():
    availability = _load_availability_module()

    result = availability._run_command(
        ("definitely_missing_chili_frontier_probe_command", "--version"),
        5,
    )

    assert result.returncode == 127
    assert "FileNotFoundError" in result.stderr or "WinError 2" in result.stderr


def test_source_availability_wraps_windows_cmd_shim(monkeypatch):
    availability = _load_availability_module()
    monkeypatch.setattr(availability.sys, "platform", "win32")
    monkeypatch.setattr(
        availability.shutil,
        "which",
        lambda name: "C:\\Users\\rindo\\AppData\\Roaming\\npm\\claude.cmd"
        if name == "claude"
        else None,
    )

    command = availability._subprocess_args(("claude", "--version"))

    assert command == [
        "cmd.exe",
        "/d",
        "/c",
        "C:\\Users\\rindo\\AppData\\Roaming\\npm\\claude.cmd",
        "--version",
    ]


def test_source_availability_cli_source_kind_overrides_default(tmp_path, capsys):
    availability = _load_availability_module()
    exit_code = availability.main(
        [
            "--source-kind",
            "claude",
            "--raw-source-root",
            str(tmp_path / "raw_sources"),
            "--output",
            str(tmp_path / "availability.md"),
            "--json",
            "--no-write",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["source_count"] == 1
    assert [item["source_kind"] for item in payload["sources"]] == ["claude"]
