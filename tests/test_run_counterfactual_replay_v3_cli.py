from __future__ import annotations

import json
from datetime import datetime, timezone

from app.services.trading.momentum_neural.counterfactual_replay import (
    CounterfactualReplayResult,
    SymbolReplayResult,
)
from scripts import run_counterfactual_replay_v3 as cli


class _FakeSession:
    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _result(*, label_ready: bool) -> CounterfactualReplayResult:
    source_events = [
        {
            "ts": "2026-07-01T13:00:00+00:00",
            "source": "ross_admission",
            "certifiable": label_ready,
            "text": "JEM watch",
        }
    ]
    return CounterfactualReplayResult(
        since=datetime(2026, 7, 1, 7, tzinfo=timezone.utc),
        until=datetime(2026, 7, 2, 7, tzinfo=timezone.utc),
        symbols=["JEM"],
        results=[
            SymbolReplayResult(
                symbol="JEM",
                ok=True,
                confidence="tick_quote_complete",
                confidence_reasons=[],
                tape_rows=10,
                trade_rows=10,
                micro_bars=3,
                source_events=source_events,
                trades=[],
                candidate_count=1,
                skipped_reasons={},
                gate_reason_counts={},
                first_candidate={"ts": "2026-07-01T13:01:00+00:00", "reason": "vwap_reclaim"},
            )
        ],
    )


def _limited_no_candidate_result() -> CounterfactualReplayResult:
    return CounterfactualReplayResult(
        since=datetime(2026, 7, 1, 7, tzinfo=timezone.utc),
        until=datetime(2026, 7, 2, 7, tzinfo=timezone.utc),
        symbols=["JEM"],
        results=[
            SymbolReplayResult(
                symbol="JEM",
                ok=True,
                confidence="tick_quote_complete_limited",
                confidence_reasons=["sampled_tape_max_ticks_500", "ross_source_not_certified"],
                tape_rows=500,
                trade_rows=100,
                micro_bars=10,
                source_events=[
                    {
                        "ts": "2026-07-01T13:00:00+00:00",
                        "source": "ross_transcript",
                        "certifiable": False,
                        "text": "JEM watch",
                    }
                ],
                trades=[],
                candidate_count=0,
                skipped_reasons={},
                gate_reason_counts={"waiting_for_vwap_reclaim": 10},
                first_candidate=None,
            )
        ],
    )


def _cap_sensitive_result(max_ticks: int | None) -> CounterfactualReplayResult:
    candidate_count = 0 if max_ticks == 500 else 1
    first_candidate = (
        None
        if candidate_count <= 0
        else {"ts": "2026-07-01T15:22:57+00:00", "reason": "ross_breakout_starter_tick"}
    )
    return CounterfactualReplayResult(
        since=datetime(2026, 7, 1, 7, tzinfo=timezone.utc),
        until=datetime(2026, 7, 2, 7, tzinfo=timezone.utc),
        symbols=["JEM"],
        results=[
            SymbolReplayResult(
                symbol="JEM",
                ok=True,
                confidence="tick_quote_complete_limited" if max_ticks else "tick_quote_complete",
                confidence_reasons=([f"sampled_tape_max_ticks_{max_ticks}"] if max_ticks else []),
                tape_rows=max_ticks or 1000,
                trade_rows=100,
                micro_bars=10,
                source_events=[
                    {
                        "ts": "2026-07-01T13:00:00+00:00",
                        "source": "ross_transcript",
                        "certifiable": False,
                        "text": "JEM watch",
                    }
                ],
                trades=[],
                candidate_count=candidate_count,
                skipped_reasons={},
                gate_reason_counts={},
                first_candidate=first_candidate,
            )
        ],
    )


def test_counterfactual_cli_summary_only_surfaces_label_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=True))

    code = cli.main(["--date", "2026-07-01", "--symbols", "JEM", "--summary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["opportunity_label_summary"]["label_ready_symbol_count"] == 1
    assert payload["opportunity_label_summary"]["pnl_minmax_label_ready"] is True
    assert "results" not in payload


def test_counterfactual_cli_opportunity_label_guard_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _limited_no_candidate_result())

    code = cli.main(
        ["--date", "2026-07-01", "--symbols", "JEM", "--summary-only", "--require-opportunity-labels"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["certification_failures"][0].startswith(
        "counterfactual_opportunity_labels_not_ready:"
    )


def test_counterfactual_cli_source_certification_queue_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=False))

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--source-certification-queue-only",
            "--require-opportunity-labels",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "results" not in payload
    assert "opportunity_label_summary" not in payload
    assert payload["label_ready_symbol_count"] == 0
    assert payload["status_counts"] == {"source_not_certified": 1}
    assert payload["source_certification_queue"][0]["symbol"] == "JEM"
    assert payload["source_certification_queue"][0]["status"] == "source_not_certified"
    assert payload["certification_failures"][0].startswith(
        "counterfactual_opportunity_labels_not_ready:"
    )


def test_counterfactual_cli_source_queue_marker_commands_include_custom_manifest(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=False))
    manifest = tmp_path / "custom review manifest.json"

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--source-certification-queue-only",
            "--visual-review-manifest",
            str(manifest),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    row = payload["source_certification_queue"][0]
    assert code == 0
    assert f'--visual-review-manifest "{manifest}"' in row["marker_command_template"]
    assert f'--visual-review-manifest "{manifest}"' in row["marker_dry_run_command_template"]


def test_counterfactual_cli_joined_certification_queue_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=False))
    monkeypatch.setattr(cli, "audit_visual_evidence_root", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(cli, "_read_visual_review_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cli,
        "_visual_evidence_status",
        lambda **_kwargs: {
            "status": "frame_artifacts_available_but_not_linked",
            "reason": "source rows are not linked",
            "trade_no_trade_certifiable": False,
            "candidate_evidence_matches": [
                {
                    "evidence_id": "vid-jem",
                    "snippets": [
                        {
                            "review_frame_paths": [
                                "project_ws/AgentOps/ross_video_evidence/vid-jem/frames/f0001.jpg"
                            ]
                        }
                    ],
                }
            ],
            "reviewed_visual_evidence": [],
        },
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--joined-certification-queue-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "results" not in payload
    assert payload["source_certification_queue"][0]["symbol"] == "JEM"
    joined = payload["source_visual_joined_queue"][0]
    assert joined["symbol"] == "JEM"
    assert joined["replay_confidence"] == "tick_quote_complete"
    assert joined["replay_confidence_reasons"] == []
    assert joined["gate_reason_counts"] == {}
    assert joined["first_candidate"]["reason"] == "vwap_reclaim"
    assert joined["visual_status"] == "frame_artifacts_available_but_not_linked"
    assert joined["visual_candidate_evidence_count"] == 1
    assert joined["visual_trade_no_trade_certifiable"] is False
    assert joined["visual_review_frame_paths"] == [
        "project_ws/AgentOps/ross_video_evidence/vid-jem/frames/f0001.jpg"
    ]
    assert joined["visual_manifest_review_template"]["symbol"] == "JEM"
    assert joined["visual_manifest_review_template"]["evidence_id"] == "vid-jem"
    assert joined["visual_manifest_review_template"]["trade_no_trade_certifiable"] is False
    assert joined["visual_manifest_review_template"]["ross_trade_outcome_certifiable"] is False
    assert joined["visual_manifest_review_template"]["source_before_opportunity_certifiable"] is False
    assert joined["source_action_required"] == joined["action_required"]
    assert joined["next_action"] == "link_reviewed_chart_context_frames_to_source_if_certifying"
    assert joined["next_action_reason"] == "frame_artifacts_available_but_not_certifying_yet"


def test_counterfactual_cli_joined_queue_uses_visual_aware_next_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=False))
    monkeypatch.setattr(cli, "audit_visual_evidence_root", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(cli, "_read_visual_review_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cli,
        "_visual_evidence_status",
        lambda **_kwargs: {
            "status": "reviewed_frame_evidence_noncertifying",
            "reason": "reviewed frames are no-entry context",
            "trade_no_trade_certifiable": False,
            "candidate_evidence_matches": [{"evidence_id": "vid-jem"}],
            "reviewed_visual_evidence": [{"evidence_id": "vid-jem", "trade_no_trade_certifiable": False}],
        },
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--joined-certification-queue-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    joined = payload["source_visual_joined_queue"][0]
    assert code == 0
    assert joined["visual_status"] == "reviewed_frame_evidence_noncertifying"
    assert joined["next_action"] == "find_different_pre_opportunity_chart_trade_source_or_keep_noncertifying"
    assert joined["next_action_reason"] == "reviewed_local_frames_do_not_certify_positive_entry_context"


def test_counterfactual_cli_joined_queue_reruns_sample_limited_no_candidate(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _limited_no_candidate_result())
    monkeypatch.setattr(cli, "audit_visual_evidence_root", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(cli, "_read_visual_review_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cli,
        "_visual_evidence_status",
        lambda **_kwargs: {
            "status": "reviewed_frame_evidence_noncertifying",
            "reason": "reviewed frames are no-entry context",
            "trade_no_trade_certifiable": False,
            "candidate_evidence_matches": [{"evidence_id": "vid-jem"}],
            "reviewed_visual_evidence": [{"evidence_id": "vid-jem", "trade_no_trade_certifiable": False}],
        },
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--joined-certification-queue-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    joined = payload["source_visual_joined_queue"][0]
    assert code == 0
    assert joined["candidate_count"] == 0
    assert joined["next_action"] == "rerun_replay_with_higher_or_uncapped_ticks_before_gate_shape_claim"
    assert joined["next_action_reason"] == "no_candidate_under_sampled_tape_cap"
    assert joined["sample_limit_rerun_ready"] is True
    assert joined["sample_limit_rerun_blocker"] is None
    assert (
        joined["sample_limit_uncapped_rerun_command"]
        == 'python scripts\\run_counterfactual_replay_v3.py --since "2026-07-01T07:00:00+00:00" '
        '--until "2026-07-02T07:00:00+00:00" --symbols JEM --joined-certification-queue-text'
    )


def test_counterfactual_cli_tick_cap_sweep_surfaces_sample_sensitive_symbol(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_counterfactual_replay",
        lambda *_args, **kwargs: _cap_sensitive_result(kwargs.get("max_ticks")),
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--tick-cap-sweep",
            "500",
            "1000",
            "none",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["tick_cap_sweep"] == ["500", "1000", "uncapped"]
    assert payload["sample_sensitive_symbols"] == ["JEM"]
    assert payload["stability"][0]["candidate_presence_stable"] is False
    assert [row["candidate_count"] for row in payload["rows"]] == [0, 1, 1]
    assert [row["tick_cap"] for row in payload["cap_runtime_seconds"]] == ["500", "1000", "uncapped"]
    assert all(isinstance(row["runtime_seconds"], float) for row in payload["cap_runtime_seconds"])
    assert all(isinstance(row["runtime_seconds"], float) for row in payload["rows"])
    assert isinstance(payload["stability"][0]["max_runtime_seconds"], float)


def test_counterfactual_cli_tick_cap_sweep_single_cap_is_not_comparison_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        cli,
        "run_counterfactual_replay",
        lambda *_args, **kwargs: _cap_sensitive_result(kwargs.get("max_ticks")),
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--tick-cap-sweep",
            "500",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    stability = payload["stability"][0]
    assert code == 0
    assert stability["tested_cap_count"] == 1
    assert stability["comparison_ready"] is False
    assert stability["sample_sensitive"] is False
    assert payload["cap_runtime_seconds"][0]["tick_cap"] == "500"
    assert isinstance(payload["cap_runtime_seconds"][0]["runtime_seconds"], float)


def test_counterfactual_cli_joined_certification_queue_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _limited_no_candidate_result())
    monkeypatch.setattr(cli, "audit_visual_evidence_root", lambda *_args, **_kwargs: {"rows": []})
    monkeypatch.setattr(cli, "_read_visual_review_manifest", lambda *_args, **_kwargs: {})
    manifest = "D:\\tmp\\custom review manifest.json"
    monkeypatch.setattr(
        cli,
        "_visual_evidence_status",
        lambda **_kwargs: {
            "status": "candidate_frame_artifacts_symbol_matched_not_linked",
            "reason": "candidate frames exist",
            "trade_no_trade_certifiable": False,
            "candidate_evidence_matches": [{"evidence_id": "vid-jem"}],
            "reviewed_visual_evidence": [],
        },
    )

    code = cli.main(
        [
            "--date",
            "2026-07-01",
            "--symbols",
            "JEM",
            "--joined-certification-queue-text",
            "--visual-review-manifest",
            manifest,
        ]
    )

    text = capsys.readouterr().out
    assert code == 0
    assert "SYMBOL | REPLAY_STATUS | VISUAL_STATUS | SAMPLE | CANDIDATES | REVIEWED | ACTION" in text
    assert "NOTE | capped_replay_rows_need_higher_or_uncapped_replay_before_gate_shape_claim" in text
    assert (
        "JEM | source_not_certified | candidate_frame_artifacts_symbol_matched_not_linked | "
        "limited:sampled_tape_max_ticks_500 | 1 | 0"
    ) in text
    assert "review_candidate_frame_paths_and_update_manifest_if_chart_context_certifies" in text
    assert (
        'RERUN_UNCAPPED: python scripts\\run_counterfactual_replay_v3.py --since "2026-07-01T07:00:00+00:00" '
        '--until "2026-07-02T07:00:00+00:00" --symbols JEM --joined-certification-queue-text'
    ) in text
    assert "PREFLIGHT_BLOCKED: missing_opportunity_timestamp; template=python scripts\\mark_ross_trade_event.py JEM" in text
    assert "PREFLIGHT: python scripts\\mark_ross_trade_event.py JEM" not in text
    assert "--dry-run" in text
    assert f'--visual-review-manifest "{manifest}"' in text
    assert not text.lstrip().startswith("{")


def test_counterfactual_cli_pnl_minmax_label_guard_passes_when_all_symbols_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(cli, "run_counterfactual_replay", lambda *_args, **_kwargs: _result(label_ready=True))

    code = cli.main(
        ["--date", "2026-07-01", "--symbols", "JEM", "--summary-only", "--require-pnl-minmax-labels"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["certification_failures"] == []


def test_counterfactual_cli_visual_boundary_only_does_not_run_replay(monkeypatch, capsys) -> None:
    def fail_replay(*_args, **_kwargs):
        raise AssertionError("replay should not run for visual boundary only")

    monkeypatch.setattr(cli, "run_counterfactual_replay", fail_replay)
    monkeypatch.setattr(
        cli,
        "audit_visual_evidence_root",
        lambda *_args, **_kwargs: {"ready_count": 1, "not_ready_count": 0, "total_frames": 10, "rows": []},
    )
    monkeypatch.setattr(
        cli,
        "_read_visual_review_manifest",
        lambda *_args, **_kwargs: {
            "reviews": [
                {
                    "symbol": "JEM",
                    "evidence_id": "vid-jem",
                    "evidence_type": "active_chart_review_context",
                    "trade_no_trade_certifiable": False,
                    "reviewed_frame_paths": ["frames/f0001.jpg"],
                }
            ]
        },
    )

    code = cli.main(["--symbols", "JEM", "--visual-certification-boundary-only"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["replay_executed"] is False
    assert payload["boundary"] == "visual_review_only_no_pnl_claim"
    assert payload["rows"][0]["visual_status"] == "reviewed_frame_evidence_noncertifying"
    assert payload["certifying_symbol_count"] == 0
    assert (
        payload["certifying_symbol_count_semantics"]
        == "legacy_trade_no_trade_only_not_source_before_or_pnl_certification"
    )
    assert payload["trade_no_trade_certifying_symbol_count"] == 0
    assert payload["source_before_certifying_symbol_count"] == 0
    assert payload["pnl_source_certifying_symbol_count"] == 0
    assert payload["ross_outcome_certifying_symbol_count"] == 0


def test_counterfactual_cli_visual_boundary_text_reports_certification_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "audit_visual_evidence_root",
        lambda *_args, **_kwargs: {"ready_count": 1, "not_ready_count": 0, "total_frames": 10, "rows": []},
    )
    monkeypatch.setattr(
        cli,
        "_read_visual_review_manifest",
        lambda *_args, **_kwargs: {
            "reviews": [
                {
                    "symbol": "CANF",
                    "evidence_id": "vid-canf",
                    "evidence_type": "chart_trade_context",
                    "trade_no_trade_certifiable": True,
                    "ross_trade_outcome_certifiable": False,
                    "source_before_opportunity_certifiable": True,
                    "reviewed_frame_paths": ["frames/f0001.jpg"],
                }
            ]
        },
    )

    code = cli.main(["--symbols", "CANF", "--visual-certification-boundary-text"])

    text = capsys.readouterr().out
    assert code == 0
    assert (
        "SYMBOL | VISUAL_STATUS | REVIEWED | CANDIDATES | TRADE_NO_TRADE | SOURCE_BEFORE | "
        "OUTCOME | BOUNDARY"
    ) in text
    assert (
        "CANF | reviewed_frame_evidence_trade_certified | 1 | 0 | true | true | false | "
        "visual_review_only_no_pnl_claim"
    ) in text
