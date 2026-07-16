from __future__ import annotations

import json
import subprocess

from scripts import audit_momentum_live_replay as cli


class _FakeSession:
    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _summary(
    *,
    session_rows: int = 3,
    traces_seen: int | None = None,
    adapter_starvation_ready: bool = False,
    scheduler_ready: bool = False,
    scheduler_priority_level: str | None = None,
    pnl_ready: bool = False,
    realized_vs_expected_ready: bool = False,
    broker_attribution_ready: bool = False,
    trace_ready: bool = True,
    lifecycle_ready: bool = False,
    window_complete: bool = True,
) -> dict:
    return {
        "ok": True,
        "read_only": True,
        "inputs": {
            "since_utc": "2026-07-02T15:00:00+00:00",
            "until_utc": "2026-07-02T15:05:00+00:00",
            "session_rows": session_rows,
        },
        "certification": {
            "scheduler_priority_claim_ready": scheduler_ready,
            "scheduler_priority_claim_level": (
                scheduler_priority_level
                or ("scheduler_pressure_observed" if scheduler_ready else "single_snapshot_batch_only")
            ),
            "pnl_minmax_claim_ready": pnl_ready,
            "pnl_minmax_blocker": (
                "" if pnl_ready else "live_export_has_no_historical_intra_session_snapshot_timeline"
            ),
            "broker_outcome_attribution_ready": broker_attribution_ready,
            "adapter_unavailable_starvation_claim_ready": adapter_starvation_ready,
            "realized_vs_selected_expected_claim_ready": realized_vs_expected_ready,
        },
        "pnl_evidence": {
            "selected_count": 1,
            "broker_outcome_count": 1 if realized_vs_expected_ready else 0,
            "selected_with_realized_count": 1 if realized_vs_expected_ready else 0,
            "selected_missing_outcome_count": 0 if realized_vs_expected_ready else 1,
            "complete_selected_outcomes": realized_vs_expected_ready,
            "realized_vs_selected_expected_claim_ready": realized_vs_expected_ready,
            "realized_pnl_usd": 18.5 if realized_vs_expected_ready else 0.0,
            "selected_expected_pnl_usd": 20.0,
            "realized_vs_selected_expected_usd": -1.5 if realized_vs_expected_ready else -20.0,
            "pnl_minmax_claim_ready": False,
        },
        "replay_evidence_debt": [
            {
                "missing_evidence": "multi_snapshot_scheduler_timeline",
                "blocks": ["scheduler_priority", "adapter_starvation", "pnl_minmax"],
                "evidence_needed": "Two or more persisted scheduler/event-loop snapshot steps.",
                "how_to_collect": "Audit a tradable window with event-loop ticks.",
                "claim_gate": True,
                "enablement_gate": False,
            }
        ],
        "pnl_attribution": {
            "selected_session_ids": [101],
            "realized_session_ids": [101] if broker_attribution_ready else [],
            "selected_without_outcome_ids": [] if broker_attribution_ready else [101],
        },
        "scheduler": {
            "selected_count": 1,
            "priority_evidence": {
                "budget_skip_count": 1 if scheduler_ready else 0,
                "steps_with_budget_skip_and_selection": 1 if scheduler_ready else 0,
                "delayed_then_selected_count": 0,
                "selected_count": 1,
                "scheduler_priority_claim_ready": scheduler_ready,
            },
            "starvation_evidence": {
                "free_skip_count": 1 if adapter_starvation_ready else 0,
                "unavailable_free_skip_count": 1 if adapter_starvation_ready else 0,
                "steps_with_free_skip_and_selection": 1 if adapter_starvation_ready else 0,
                "steps_with_unavailable_free_skip_and_selection": 1 if adapter_starvation_ready else 0,
                "adapter_unavailable_starvation_claim_ready": adapter_starvation_ready,
            },
        },
        "setup_trace": {
            "events_seen": 4 if traces_seen is None else traces_seen,
            "traces_seen": (1 if trace_ready else 2) if traces_seen is None else traces_seen,
            "finding_count": 0 if trace_ready else 2,
            "finding_reasons": (
                []
                if trace_ready
                else ["setup_alias_missing_structural_stop", "wait_reason_not_tick_armed"]
            ),
            "findings": (
                []
                if trace_ready
                else [
                    {
                        "session_id": 101,
                        "ts": "2026-07-02T15:00:01Z",
                        "event_type": "live_entry_candidate",
                        "setup_alias": "abcd_break_tick_ok",
                        "reason": "setup_alias_missing_structural_stop",
                        "detail": {"setup_alias": "abcd_break_tick_ok"},
                    },
                    {
                        "session_id": 102,
                        "ts": "2026-07-02T15:00:02Z",
                        "event_type": "live_entry_wait",
                        "setup_alias": "vwap_reclaim",
                        "reason": "wait_reason_not_tick_armed",
                        "detail": {"source_wait_reason": "waiting_for_vwap_reclaim"},
                    },
                ]
            ),
            "certification": {
                "trace_coverage_ok": trace_ready,
                "lifecycle_order_ok": lifecycle_ready,
                "lifecycle_claim_ready": lifecycle_ready,
                "complete_lifecycle_count": 1 if lifecycle_ready else 0,
                "window_completeness_ok": window_complete,
                "possible_truncated_window_issue_count": 0 if window_complete else 2,
            },
            "lifecycle_summary": {
                "stage_counts": {"setup_trace": 1},
                "event_type_counts": {"live_entry_wait": 1},
                "trace_alias_counts": {"vwap_reclaim": 1},
                "wait_reason_counts": {"waiting_for_vwap_reclaim": 1},
                "sessions_with_entry_fill": 0,
                "sessions_with_trailing_armed": 0,
                "sessions_with_add_submit": 0,
                "sessions_with_add_fill": 0,
                "sessions_with_exit_fill": 0,
                "sessions_with_entry_and_exit": 0,
            }
        },
    }


def test_certification_failures_flag_scheduler_and_pnl_overclaims() -> None:
    failures = cli._certification_failures(
        _summary(),
        require_scheduler_priority_claim=True,
        require_adapter_starvation_claim=True,
        require_pnl_minmax_claim=True,
        require_realized_vs_expected_pnl=True,
        require_broker_outcome_attribution=True,
        require_setup_trace_coverage=True,
        require_setup_lifecycle_claim=True,
    )

    assert failures == [
        "scheduler_priority_claim_not_ready:single_snapshot_batch_only",
        "adapter_unavailable_starvation_claim_not_ready:free=0:same_step_selected=0",
        "pnl_minmax_claim_not_ready:live_export_has_no_historical_intra_session_snapshot_timeline",
        "realized_vs_expected_pnl_not_ready:selected=1:missing=1:outcomes=0",
        "broker_outcome_attribution_not_ready:selected=1:realized=0:missing=1",
        "setup_lifecycle_claim_not_ready:trace=True:order=False:complete=0:window=True:truncated=0",
    ]


def test_certification_failures_flag_empty_current_window_evidence() -> None:
    failures = cli._certification_failures(
        _summary(session_rows=0, traces_seen=0),
        require_current_window_evidence=True,
    )

    assert failures == ["current_window_evidence_not_ready:sessions=0:traces=0:findings=0"]


def test_certification_failures_do_not_turn_empty_window_into_setup_trace_gap() -> None:
    failures = cli._certification_failures(
        _summary(session_rows=0, traces_seen=0, trace_ready=False),
        require_setup_trace_coverage=True,
    )

    assert failures == []


def test_certification_failures_keep_empty_window_strict_when_required() -> None:
    failures = cli._certification_failures(
        _summary(session_rows=0, traces_seen=0, trace_ready=False),
        require_current_window_evidence=True,
        require_setup_trace_coverage=True,
    )

    assert failures == ["current_window_evidence_not_ready:sessions=0:traces=0:findings=2"]


def test_certification_failures_accept_current_window_with_setup_traces() -> None:
    failures = cli._certification_failures(
        _summary(session_rows=1, traces_seen=1),
        require_current_window_evidence=True,
    )

    assert failures == []


def test_certification_failures_accept_adapter_starvation_evidence() -> None:
    failures = cli._certification_failures(
        _summary(adapter_starvation_ready=True),
        require_adapter_starvation_claim=True,
    )

    assert failures == []


def test_certification_failures_accept_realized_vs_expected_pnl_evidence() -> None:
    failures = cli._certification_failures(
        _summary(realized_vs_expected_ready=True),
        require_realized_vs_expected_pnl=True,
    )

    assert failures == []


def test_certification_failures_flag_setup_trace_coverage_gap() -> None:
    failures = cli._certification_failures(
        _summary(trace_ready=False),
        require_setup_trace_coverage=True,
    )

    assert failures == [
        "setup_trace_coverage_not_ready:"
        "findings=2:"
        "reasons=setup_alias_missing_structural_stop=1,wait_reason_not_tick_armed=1"
    ]


def test_live_replay_cli_strict_claims_fail_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main(["--require-pnl-minmax-claim"])

    out = capsys.readouterr().out
    assert code == 2
    assert '"ok": false' in out
    assert "pnl_minmax_claim_not_ready" in out


def test_live_replay_cli_setup_lifecycle_claim_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main(["--require-setup-lifecycle-claim"])

    out = capsys.readouterr().out
    assert code == 2
    assert '"ok": false' in out
    assert "setup_lifecycle_claim_not_ready:trace=True:order=False:complete=0:window=True:truncated=0" in out


def test_live_replay_cli_setup_trace_coverage_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary(trace_ready=False))

    code = cli.main(["--require-setup-trace-coverage"])

    out = capsys.readouterr().out
    assert code == 2
    assert '"ok": false' in out
    assert "setup_trace_coverage_not_ready" in out


def test_live_replay_cli_setup_trace_coverage_passes_without_lifecycle_claim(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary(trace_ready=True))

    code = cli.main(["--require-setup-trace-coverage"])

    out = capsys.readouterr().out
    assert code == 0
    assert '"ok": true' in out
    assert "setup_lifecycle_claim_not_ready" not in out


def test_summary_only_payload_surfaces_lifecycle_counts() -> None:
    payload = cli._summary_only_payload(_summary())

    counts = payload["setup_trace_lifecycle_counts"]
    assert counts["stage_counts"] == {"setup_trace": 1}
    assert counts["event_type_counts"] == {"live_entry_wait": 1}
    assert counts["trace_alias_counts"] == {"vwap_reclaim": 1}
    assert counts["wait_reason_counts"] == {"waiting_for_vwap_reclaim": 1}
    assert counts["sessions_with_entry_fill"] == 0


def test_live_replay_cli_broker_outcome_attribution_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main(["--require-broker-outcome-attribution"])

    out = capsys.readouterr().out
    assert code == 2
    assert '"ok": false' in out
    assert "broker_outcome_attribution_not_ready:selected=1:realized=0:missing=1" in out


def test_live_replay_cli_broker_outcome_attribution_passes_when_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(broker_attribution_ready=True),
    )

    code = cli.main(["--require-broker-outcome-attribution"])

    out = capsys.readouterr().out
    assert code == 0
    assert '"ok": true' in out
    assert "broker_outcome_attribution_not_ready" not in out


def test_live_replay_cli_live_trade_certification_preset_fails_on_any_missing_leg(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(trace_ready=False, lifecycle_ready=False),
    )

    code = cli.main(["--require-live-trade-certification"])

    out = capsys.readouterr().out
    assert code == 2
    assert "broker_outcome_attribution_not_ready" in out
    assert "setup_trace_coverage_not_ready" in out
    assert "setup_lifecycle_claim_not_ready" in out
    assert "pnl_minmax_claim_not_ready" not in out


def test_live_replay_cli_live_trade_certification_preset_passes_without_pnl_minmax(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(
            broker_attribution_ready=True,
            trace_ready=True,
            lifecycle_ready=True,
        ),
    )

    code = cli.main(["--require-live-trade-certification"])

    out = capsys.readouterr().out
    assert code == 0
    assert '"ok": true' in out
    assert "certification_failures" not in out


def test_live_replay_cli_summary_only_keeps_certification_failures(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary(trace_ready=False))

    code = cli.main(["--require-setup-trace-coverage", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["ok"] is False
    assert payload["counts"]["since_utc"] == "2026-07-02T15:00:00+00:00"
    assert payload["counts"]["until_utc"] == "2026-07-02T15:05:00+00:00"
    assert payload["counts"]["session_rows"] == 3
    assert payload["counts"]["setup_trace_events_seen"] == 4
    assert payload["counts"]["setup_trace_traces_seen"] == 2
    assert payload["counts"]["setup_trace_findings"] == 2
    assert payload["certification_failures"] == [
        "setup_trace_coverage_not_ready:"
        "findings=2:"
        "reasons=setup_alias_missing_structural_stop=1,wait_reason_not_tick_armed=1"
    ]
    assert payload["setup_trace_finding_reasons"] == {
        "setup_alias_missing_structural_stop": 1,
        "wait_reason_not_tick_armed": 1,
    }
    assert payload["pnl_evidence"]["realized_vs_selected_expected_claim_ready"] is False
    assert payload["pnl_evidence"]["selected_missing_outcome_count"] == 1
    assert payload["replay_evidence_debt"][0]["missing_evidence"] == "multi_snapshot_scheduler_timeline"
    assert payload["replay_evidence_debt"][0]["claim_gate"] is True
    assert payload["replay_evidence_debt"][0]["enablement_gate"] is False
    assert payload["scheduler_starvation_evidence"] == {
        "free_skip_count": 0,
        "unavailable_free_skip_count": 0,
        "steps_with_free_skip_and_selection": 0,
        "steps_with_unavailable_free_skip_and_selection": 0,
        "adapter_unavailable_starvation_claim_ready": False,
    }
    assert payload["scheduler_priority_evidence"] == {
        "budget_skip_count": 0,
        "steps_with_budget_skip_and_selection": 0,
        "delayed_then_selected_count": 0,
        "selected_count": 1,
        "scheduler_priority_claim_ready": False,
    }
    assert payload["setup_trace_finding_ts_range"] == {
        "count_with_ts": 2,
        "first_ts": "2026-07-02T15:00:01Z",
        "last_ts": "2026-07-02T15:00:02Z",
    }
    assert payload["setup_trace_finding_samples"] == [
        {
            "session_id": 101,
            "ts": "2026-07-02T15:00:01Z",
            "event_type": "live_entry_candidate",
            "setup_alias": "abcd_break_tick_ok",
            "reason": "setup_alias_missing_structural_stop",
        },
        {
            "session_id": 102,
            "ts": "2026-07-02T15:00:02Z",
            "event_type": "live_entry_wait",
            "setup_alias": "vwap_reclaim",
            "reason": "wait_reason_not_tick_armed",
        },
    ]
    assert "setup_trace" not in payload
    assert "scheduler" not in payload


def test_live_replay_cli_adapter_starvation_claim_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main(["--require-adapter-starvation-claim", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["certification_failures"] == [
        "adapter_unavailable_starvation_claim_not_ready:free=0:same_step_selected=0"
    ]


def test_live_replay_cli_scheduler_priority_claim_passes_with_pressure_evidence(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(scheduler_ready=True),
    )

    code = cli.main(["--require-scheduler-priority-claim", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["scheduler_priority_evidence"]["scheduler_priority_claim_ready"] is True


def test_live_replay_cli_realized_vs_expected_pnl_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main(["--require-realized-vs-expected-pnl", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["certification_failures"] == [
        "realized_vs_expected_pnl_not_ready:selected=1:missing=1:outcomes=0"
    ]


def test_live_replay_cli_realized_vs_expected_pnl_passes_when_outcomes_complete(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(realized_vs_expected_ready=True),
    )

    code = cli.main(["--require-realized-vs-expected-pnl", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["pnl_evidence"]["realized_vs_selected_expected_claim_ready"] is True


def test_live_replay_cli_adapter_starvation_claim_passes_with_same_step_evidence(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(adapter_starvation_ready=True),
    )

    code = cli.main(["--require-adapter-starvation-claim", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["scheduler_starvation_evidence"]["adapter_unavailable_starvation_claim_ready"] is True


def test_live_replay_cli_current_window_evidence_fails_closed_on_empty_window(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(session_rows=0, traces_seen=0),
    )

    code = cli.main(["--require-current-window-evidence", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["certification_failures"] == [
        "current_window_evidence_not_ready:sessions=0:traces=0:findings=0"
    ]


def test_live_replay_cli_can_scope_since_canonical_worker_start(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["docker_cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="2026-07-02T05:48:19.753515345Z\n", stderr="")

    def fake_audit(_db, **kwargs):
        captured["audit_kwargs"] = kwargs
        return _summary()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", fake_audit)

    code = cli.main(["--since-canonical-worker-start", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert captured["docker_cmd"] == [
        "docker",
        "inspect",
        "chili-clean-recovery-momentum-exec",
        "--format",
        "{{.State.StartedAt}}",
    ]
    assert captured["audit_kwargs"]["since"] == "2026-07-02T05:48:19.753515345Z"
    assert payload["ok"] is True


def test_live_replay_cli_setup_lifecycle_failure_reports_truncated_window(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(window_complete=False),
    )

    code = cli.main(["--require-setup-lifecycle-claim"])

    out = capsys.readouterr().out
    assert code == 2
    assert "setup_lifecycle_claim_not_ready:trace=True:order=False:complete=0:window=False:truncated=2" in out


def test_live_replay_cli_setup_lifecycle_claim_passes_when_certified(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_live_replay_audit",
        lambda *_args, **_kwargs: _summary(lifecycle_ready=True),
    )

    code = cli.main(["--require-setup-lifecycle-claim"])

    out = capsys.readouterr().out
    assert code == 0
    assert '"ok": true' in out
    assert "certification_failures" not in out


def test_live_replay_cli_non_strict_keeps_read_only_smoke_green(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_live_replay_audit", lambda *_args, **_kwargs: _summary())

    code = cli.main([])

    out = capsys.readouterr().out
    assert code == 0
    assert '"ok": true' in out
    assert "certification_failures" not in out
