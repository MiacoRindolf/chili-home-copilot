from __future__ import annotations

from datetime import datetime
from pathlib import Path

from scripts.audit_ross_symbol_incidents import (
    _read_ross_trade_events,
    filter_sessions_for_incident_window,
    summarize_visual_evidence_status,
    summarize_symbol_incident,
    visual_certification_failures,
    visual_review_queue,
)


def test_symbol_incident_reports_ross_mentioned_without_chili_session() -> None:
    row = summarize_symbol_incident(
        "DXTS",
        sessions=[],
        events=[],
        transcript_mentions=[{"symbol": "DXTS", "ts": "2026-07-01T13:05:00Z", "text": "DXTS starter attempt"}],
    )

    assert row["classification"] == "ross_mentioned_no_chili_session"
    assert row["ross_vs_chili_verdict"] == "ross_mentioned_chili_missed"
    assert row["ross_mentions"][0]["text"] == "DXTS starter attempt"
    assert row["visual_evidence"]["trade_no_trade_certifiable"] is False


def test_symbol_incident_marks_linked_visual_evidence_ready() -> None:
    row = summarize_symbol_incident(
        "JEM",
        sessions=[],
        events=[],
        transcript_mentions=[
            {
                "symbol": "JEM",
                "ts": "2026-07-01T13:05:00Z",
                "text": "JEM reclaim attempt",
                "visual_evidence_id": "vid-ready",
            }
        ],
        visual_evidence_audit={
            "ready_count": 1,
            "total_frames": 120,
            "rows": [
                {
                    "evidence_id": "vid-ready",
                    "ready": True,
                    "frame_count": 120,
                    "missing": [],
                }
            ],
        },
    )

    assert row["visual_evidence"]["status"] == "linked_frame_evidence_ready"
    assert row["visual_evidence"]["trade_no_trade_certifiable"] is True
    assert row["visual_evidence"]["linked_evidence_ids"] == ["vid-ready"]


def test_symbol_incident_keeps_transcript_only_source_non_certifiable() -> None:
    row = summarize_symbol_incident(
        "JEM",
        sessions=[],
        events=[],
        transcript_mentions=[
            {"symbol": "JEM", "ts": "2026-07-01T13:05:00Z", "text": "JEM reclaim attempt"}
        ],
        visual_evidence_audit={
            "ready_count": 1,
            "total_frames": 120,
            "rows": [{"evidence_id": "vid-ready", "ready": True, "frame_count": 120, "missing": []}],
        },
    )

    assert row["visual_evidence"]["status"] == "frame_artifacts_available_but_not_linked"
    assert row["visual_evidence"]["trade_no_trade_certifiable"] is False


def test_symbol_incident_surfaces_candidate_visual_frames_without_certifying(tmp_path) -> None:
    evidence = tmp_path / "vid-tc"
    evidence.mkdir()
    transcript = evidence / "transcript_ts.txt"
    transcript.write_text(
        "[01:11|71] when I sat down this morning, we actually had TC\n"
        "[01:14|74] that was up a good bit\n",
        encoding="utf-8",
    )
    frames = evidence / "frames"
    frames.mkdir()
    for idx in range(69, 74):
        (frames / f"f{idx:04d}.jpg").write_bytes(b"frame")
    row = summarize_symbol_incident(
        "TC",
        sessions=[],
        events=[],
        transcript_mentions=[
            {"symbol": "TC", "ts": "2026-07-01T13:05:00Z", "text": "TC possible setup"}
        ],
        visual_evidence_audit={
            "ready_count": 1,
            "total_frames": 42,
            "rows": [
                {
                    "evidence_id": "vid-tc",
                    "path": str(evidence),
                    "ready": True,
                    "frame_count": 42,
                    "missing": [],
                }
            ],
        },
    )

    visual = row["visual_evidence"]
    assert visual["status"] == "candidate_frame_artifacts_symbol_matched_not_linked"
    assert visual["trade_no_trade_certifiable"] is False
    assert visual["candidate_evidence_matches"][0]["evidence_id"] == "vid-tc"
    assert visual["candidate_evidence_matches"][0]["snippets"][0]["offset_seconds"] == 71.0
    assert [Path(p).name for p in visual["candidate_evidence_matches"][0]["snippets"][0]["review_frame_paths"]] == [
        "f0069.jpg",
        "f0070.jpg",
        "f0071.jpg",
        "f0072.jpg",
        "f0073.jpg",
    ]


def test_symbol_incident_matches_known_asr_symbol_aliases_for_visual_frames(tmp_path) -> None:
    evidence = tmp_path / "vid-asr"
    evidence.mkdir()
    transcript = evidence / "transcript_ts.txt"
    transcript.write_text(
        "[04:30|270] made bigger moves. LH AI, a little too cheap\n"
        "[05:11|311] cheap. GEM, this is the one from yesterday\n",
        encoding="utf-8",
    )
    frames = evidence / "frames"
    frames.mkdir()
    for idx in range(268, 314):
        (frames / f"f{idx:04d}.jpg").write_bytes(b"frame")
    audit = {
        "ready_count": 1,
        "total_frames": 46,
        "rows": [
            {
                "evidence_id": "vid-asr",
                "path": str(evidence),
                "ready": True,
                "frame_count": 46,
                "missing": [],
            }
        ],
    }

    lhai = summarize_symbol_incident(
        "LHAI",
        sessions=[],
        events=[],
        transcript_mentions=[{"symbol": "LHAI", "ts": "2026-07-01T13:05:00Z", "text": "LHAI source"}],
        visual_evidence_audit=audit,
    )
    jem = summarize_symbol_incident(
        "JEM",
        sessions=[],
        events=[],
        transcript_mentions=[{"symbol": "JEM", "ts": "2026-07-01T13:06:00Z", "text": "JEM source"}],
        visual_evidence_audit=audit,
    )

    assert lhai["visual_evidence"]["candidate_evidence_matches"][0]["snippets"][0]["offset_seconds"] == 270.0
    assert jem["visual_evidence"]["candidate_evidence_matches"][0]["snippets"][0]["offset_seconds"] == 311.0


def test_symbol_incident_matches_canf_asr_alias_only_with_scanner_context(tmp_path) -> None:
    evidence = tmp_path / "vid-canf"
    evidence.mkdir()
    transcript = evidence / "transcript_ts.txt"
    transcript.write_text(
        "[08:30|510] CF. So, CF\n"
        "[08:31|511] hits the running up scanner at 503 right there\n"
        "[08:31|511] CF generic unrelated ticker mention\n",
        encoding="utf-8",
    )
    frames = evidence / "frames"
    frames.mkdir()
    for idx in range(508, 514):
        (frames / f"f{idx:04d}.jpg").write_bytes(b"frame")

    row = summarize_symbol_incident(
        "CANF",
        sessions=[],
        events=[],
        transcript_mentions=[{"symbol": "CANF", "ts": "2026-07-01T13:05:00Z", "text": "CANF source"}],
        visual_evidence_audit={
            "ready_count": 1,
            "total_frames": 6,
            "rows": [
                {
                    "evidence_id": "vid-canf",
                    "path": str(evidence),
                    "ready": True,
                    "frame_count": 6,
                    "missing": [],
                }
            ],
        },
    )

    snippets = row["visual_evidence"]["candidate_evidence_matches"][0]["snippets"]
    assert [snippet["offset_seconds"] for snippet in snippets] == [510.0]
    assert "generic unrelated" not in snippets[0]["text"]


def test_symbol_incident_uses_review_manifest_without_certifying_scanner_only(tmp_path) -> None:
    evidence = tmp_path / "vid-tc"
    evidence.mkdir()
    (evidence / "transcript_ts.txt").write_text("[01:11|71] we actually had TC\n", encoding="utf-8")
    row = summarize_symbol_incident(
        "TC",
        sessions=[],
        events=[],
        transcript_mentions=[
            {"symbol": "TC", "ts": "2026-07-01T13:05:00Z", "text": "TC possible setup"}
        ],
        visual_evidence_audit={
            "ready_count": 1,
            "total_frames": 42,
            "rows": [{"evidence_id": "vid-tc", "path": str(evidence), "ready": True, "frame_count": 42}],
        },
        visual_review_manifest={
            "reviews": [
                {
                    "symbol": "TC",
                    "evidence_id": "vid-tc",
                    "evidence_type": "scanner_selection_context",
                    "trade_no_trade_certifiable": False,
                    "reviewed_frame_paths": ["frames/f0071.jpg"],
                    "observation": "scanner only",
                    "review_doc": "review.md",
                }
            ]
        },
    )

    visual = row["visual_evidence"]
    assert visual["status"] == "reviewed_frame_evidence_noncertifying"
    assert visual["trade_no_trade_certifiable"] is False
    assert visual["reviewed_visual_evidence"][0]["evidence_type"] == "scanner_selection_context"
    assert visual["candidate_evidence_matches"][0]["evidence_id"] == "vid-tc"


def test_symbol_incident_review_manifest_can_explicitly_certify_chart_context() -> None:
    row = summarize_symbol_incident(
        "TC",
        sessions=[],
        events=[],
        visual_review_manifest={
            "reviews": [
                {
                    "symbol": "TC",
                    "evidence_id": "vid-chart",
                    "evidence_type": "chart_trade_context",
                    "trade_no_trade_certifiable": True,
                    "reviewed_frame_paths": ["frames/f0123.jpg"],
                    "observation": "chart setup reviewed",
                }
            ]
        },
    )

    assert row["visual_evidence"]["status"] == "reviewed_frame_evidence_trade_certified"
    assert row["visual_evidence"]["trade_no_trade_certifiable"] is True


def test_symbol_incident_preserves_post_opportunity_visual_review_boundary() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[],
        events=[],
        visual_review_manifest={
            "reviews": [
                {
                    "symbol": "CANF",
                    "evidence_id": "vid-canf-recap",
                    "evidence_type": "post_opportunity_chart_review_context",
                    "trade_no_trade_certifiable": False,
                    "ross_trade_outcome_certifiable": True,
                    "source_before_opportunity_certifiable": False,
                    "reviewed_frame_paths": ["frames/f0529.jpg", "frames/f0575.jpg"],
                    "observation": "post-trade chart and P&L recap, not a source-before-opportunity frame",
                }
            ]
        },
    )

    visual = row["visual_evidence"]
    assert visual["status"] == "reviewed_frame_evidence_noncertifying"
    assert visual["trade_no_trade_certifiable"] is False
    reviewed = visual["reviewed_visual_evidence"][0]
    assert reviewed["ross_trade_outcome_certifiable"] is True
    assert reviewed["source_before_opportunity_certifiable"] is False


def test_visual_evidence_summary_counts_certification_buckets() -> None:
    rows = [
        {
            "symbol": "TC",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_noncertifying",
                "trade_no_trade_certifiable": False,
            },
        },
        {
            "symbol": "JEM",
            "visual_evidence": {
                "status": "candidate_frame_artifacts_symbol_matched_not_linked",
                "trade_no_trade_certifiable": False,
            },
        },
        {
            "symbol": "CANF",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_trade_certified",
                "trade_no_trade_certifiable": True,
            },
        },
    ]

    summary = summarize_visual_evidence_status(rows)

    assert summary["symbol_count"] == 3
    assert summary["certifiable_count"] == 1
    assert summary["uncertified_count"] == 2
    assert summary["certifiable_symbols"] == ["CANF"]
    assert summary["candidate_symbols"] == ["JEM"]
    assert summary["reviewed_noncertifying_symbols"] == ["TC"]
    assert summary["status_counts"]["reviewed_frame_evidence_noncertifying"] == 1


def test_visual_certification_failures_require_trade_certifying_frames() -> None:
    rows = [
        {
            "symbol": "TC",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_noncertifying",
                "reason": "reviewed_frames_do_not_show_chart_trade_context",
                "trade_no_trade_certifiable": False,
            },
        },
        {
            "symbol": "CANF",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_trade_certified",
                "trade_no_trade_certifiable": True,
            },
        },
    ]

    failures = visual_certification_failures(rows)

    assert failures == [
        {
            "symbol": "TC",
            "status": "reviewed_frame_evidence_noncertifying",
            "reason": "reviewed_frames_do_not_show_chart_trade_context",
        }
    ]


def test_visual_review_queue_surfaces_candidate_and_reviewed_frame_work() -> None:
    rows = [
        {
            "symbol": "TC",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_noncertifying",
                "reason": "reviewed_frames_do_not_show_chart_trade_context",
                "trade_no_trade_certifiable": False,
                "reviewed_visual_evidence": [{"evidence_id": "vid-tc", "evidence_type": "scanner"}],
            },
        },
        {
            "symbol": "DXF",
            "visual_evidence": {
                "status": "no_ross_source_evidence",
                "reason": "No Ross source row was available",
                "trade_no_trade_certifiable": False,
                "candidate_evidence_matches": [
                    {
                        "evidence_id": "vid-dxf",
                        "snippets": [
                            {
                                "review_frame_paths": [
                                    "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0001.jpg",
                                    "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0002.jpg",
                                ]
                            }
                        ],
                    }
                ],
            },
        },
        {
            "symbol": "CANF",
            "visual_evidence": {
                "status": "reviewed_frame_evidence_trade_certified",
                "trade_no_trade_certifiable": True,
            },
        },
    ]

    queue = visual_review_queue(rows)

    assert [row["symbol"] for row in queue] == ["TC", "DXF"]
    assert queue[0]["action_required"] == "find_chart_trade_context_frames_or_keep_noncertifying"
    assert queue[0]["reviewed_evidence_count"] == 1
    assert queue[1]["action_required"] == (
        "review_candidate_frame_paths_and_update_manifest_if_chart_context_certifies"
    )
    assert queue[1]["candidate_evidence_count"] == 1
    assert queue[1]["review_frame_paths"] == [
        "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0001.jpg",
        "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0002.jpg",
    ]
    assert queue[1]["review_frame_paths_absolute"][0].endswith(
        "project_ws\\AgentOps\\ross_video_evidence\\vid-dxf\\frames\\f0001.jpg"
    )
    assert queue[1]["manifest_review_template"] == {
        "symbol": "DXF",
        "evidence_id": "vid-dxf",
        "evidence_type": "chart_trade_context",
        "trade_no_trade_certifiable": False,
        "ross_trade_outcome_certifiable": False,
        "source_before_opportunity_certifiable": False,
        "reviewed_frame_paths": [
            "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0001.jpg",
            "project_ws/AgentOps/ross_video_evidence/vid-dxf/frames/f0002.jpg",
        ],
        "observation": "FILL_AFTER_REVIEWING_CHART_VWAP_HOD_PULLBACK_CANDLES_TAPE_L2_CONTEXT",
        "review_doc": "docs/STRATEGY/CC_REPORTS/FILL_REVIEW_DOC.md",
    }


def test_symbol_incident_reports_admitted_not_ticked() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[{"id": 1, "symbol": "CANF", "state": "watching"}],
        events=[
            {
                "session_id": 1,
                "ts": "2026-07-01T13:05:01Z",
                "event_type": "ross_event_admitted",
                "payload": {"symbol": "CANF", "source": "iqfeed_l1", "ticked": 0},
            }
        ],
    )

    assert row["classification"] == "admitted_not_ticked"
    assert row["ross_vs_chili_verdict"] == "chili_admitted_without_tick"
    assert row["admission_count"] == 1


def test_symbol_incident_reports_admitted_watched_or_blocked_with_reasons() -> None:
    row = summarize_symbol_incident(
        "JEM",
        sessions=[{"id": 7, "symbol": "JEM", "state": "watching"}],
        events=[
            {
                "session_id": 7,
                "ts": "2026-07-01T13:05:01Z",
                "event_type": "ross_event_admitted",
                "payload": {"symbol": "JEM", "source": "ross_transcript", "ticked": 1},
            },
            {
                "session_id": 7,
                "ts": "2026-07-01T13:05:02Z",
                "event_type": "live_entry_wait",
                "payload": {"reason": "waiting_for_vwap_reclaim"},
            },
            {
                "session_id": 7,
                "ts": "2026-07-01T13:05:03Z",
                "event_type": "live_entry_blocked",
                "payload": {"reason": "wide_bbo_spread"},
            },
        ],
    )

    assert row["classification"] == "admitted_watched_or_blocked"
    assert row["ross_vs_chili_verdict"] == "chili_saw_but_did_not_enter"
    assert "waiting_for_vwap_reclaim" in row["operator_summary"]
    assert [r["reason"] for r in row["latest_reasons"][:2]] == ["wide_bbo_spread", "waiting_for_vwap_reclaim"]


def test_symbol_incident_reports_entered_and_exited() -> None:
    row = summarize_symbol_incident(
        "TC",
        sessions=[{"id": 9, "symbol": "TC", "state": "live_finished"}],
        events=[
            {"session_id": 9, "ts": "2026-07-01T17:31:53Z", "event_type": "entry_fill", "payload": {}},
            {"session_id": 9, "ts": "2026-07-01T17:32:02Z", "event_type": "exit_fill", "payload": {"reason": "target"}},
        ],
    )

    assert row["classification"] == "entered"
    assert row["ross_vs_chili_verdict"] == "chili_entered_and_exited"
    assert row["entry_count"] == 1
    assert row["exit_count"] == 1
    assert row["states"] == {"live_finished": 1}


def test_symbol_incident_reports_ross_to_chili_latency_fields() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[{"id": 11, "symbol": "CANF", "state": "live_finished"}],
        transcript_mentions=[
            {"symbol": "CANF", "ts": "2026-07-01T13:05:00Z", "text": "CANF first pullback scalp"}
        ],
        events=[
            {
                "session_id": 11,
                "ts": "2026-07-01T13:05:01Z",
                "event_type": "ross_event_admitted",
                "payload": {"symbol": "CANF", "source": "iqfeed_l1", "ticked": 1},
            },
            {"session_id": 11, "ts": "2026-07-01T13:05:09Z", "event_type": "entry_fill", "payload": {}},
            {"session_id": 11, "ts": "2026-07-01T13:05:13Z", "event_type": "exit_fill", "payload": {}},
        ],
    )

    assert row["timing"]["first_ross_mention_ts"] == "2026-07-01T13:05:00+00:00"
    assert row["timing"]["first_ross_trade_ts"] is None
    assert row["timing"]["ross_reference"] == "mention"
    assert row["timing"]["first_chili_admission_ts"] == "2026-07-01T13:05:01+00:00"
    assert row["timing"]["first_chili_entry_ts"] == "2026-07-01T13:05:09+00:00"
    assert row["timing"]["ross_to_admission_latency_s"] == 1.0
    assert row["timing"]["ross_to_entry_latency_s"] == 9.0
    assert row["timing"]["admission_to_entry_latency_s"] == 8.0
    assert row["timing"]["entry_to_exit_latency_s"] == 4.0
    assert row["timing"]["ross_entry_speed_class"] == "ross_scalp_window"


def test_symbol_incident_prefers_ross_trade_time_for_latency() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[{"id": 11, "symbol": "CANF", "state": "live_finished"}],
        transcript_mentions=[
            {"symbol": "CANF", "ts": "2026-07-01T13:04:40Z", "text": "CANF on watch"}
        ],
        ross_trade_events=[
            {"symbol": "CANF", "ts": "2026-07-01T13:05:00Z", "action": "buy", "price": 4.25}
        ],
        events=[
            {"session_id": 11, "ts": "2026-07-01T13:05:09Z", "event_type": "entry_fill", "payload": {}},
        ],
    )

    assert row["ross_trades"][0]["action"] == "buy"
    assert row["timing"]["first_ross_trade_ts"] == "2026-07-01T13:05:00+00:00"
    assert row["timing"]["ross_reference"] == "trade"
    assert row["timing"]["ross_to_entry_latency_s"] == 29.0
    assert row["timing"]["ross_trade_to_entry_latency_s"] == 9.0
    assert row["timing"]["ross_reference_to_entry_latency_s"] == 9.0
    assert row["timing"]["ross_entry_speed_class"] == "ross_scalp_window"


def test_symbol_incident_latency_speed_classes() -> None:
    base = {
        "sessions": [{"id": 1, "symbol": "JEM", "state": "live_finished"}],
        "transcript_mentions": [{"symbol": "JEM", "ts": "2026-07-01T13:05:00Z", "text": "JEM breakout attempt"}],
    }

    late = summarize_symbol_incident(
        "JEM",
        **base,
        events=[{"session_id": 1, "ts": "2026-07-01T13:05:20Z", "event_type": "entry_fill", "payload": {}}],
    )
    too_late = summarize_symbol_incident(
        "JEM",
        **base,
        events=[{"session_id": 1, "ts": "2026-07-01T13:05:45Z", "event_type": "entry_fill", "payload": {}}],
    )

    assert late["timing"]["ross_entry_speed_class"] == "late_for_scalp"
    assert too_late["timing"]["ross_entry_speed_class"] == "too_late_for_ross_scalp"


def test_symbol_incident_verdict_flags_late_ross_scalp_entry() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[{"id": 12, "symbol": "CANF", "state": "live_finished"}],
        ross_trade_events=[
            {"symbol": "CANF", "ts": "2026-07-01T13:05:00Z", "action": "buy", "price": 4.25}
        ],
        events=[
            {"session_id": 12, "ts": "2026-07-01T13:05:45Z", "event_type": "entry_fill", "payload": {}},
            {"session_id": 12, "ts": "2026-07-01T13:05:51Z", "event_type": "exit_fill", "payload": {}},
        ],
    )

    assert row["classification"] == "entered"
    assert row["timing"]["ross_entry_speed_class"] == "too_late_for_ross_scalp"
    assert row["timing"]["ross_reference_to_entry_latency_s"] == 45.0
    assert row["ross_vs_chili_verdict"] == "chili_entered_too_late_for_ross_scalp"
    assert "late or different setup" in row["operator_summary"]


def test_symbol_incident_verdict_keeps_fast_ross_scalp_entry_ok() -> None:
    row = summarize_symbol_incident(
        "CANF",
        sessions=[{"id": 13, "symbol": "CANF", "state": "live_finished"}],
        ross_trade_events=[
            {"symbol": "CANF", "ts": "2026-07-01T13:05:00Z", "action": "buy", "price": 4.25}
        ],
        events=[
            {"session_id": 13, "ts": "2026-07-01T13:05:09Z", "event_type": "entry_fill", "payload": {}},
            {"session_id": 13, "ts": "2026-07-01T13:05:13Z", "event_type": "exit_fill", "payload": {}},
        ],
    )

    assert row["classification"] == "entered"
    assert row["timing"]["ross_entry_speed_class"] == "ross_scalp_window"
    assert row["ross_vs_chili_verdict"] == "chili_entered_and_exited"


def test_read_ross_trade_events_jsonl_filters_and_normalizes(tmp_path) -> None:
    path = tmp_path / "ross_trade_events.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"symbol":"canf","ts":"2026-07-01T13:05:00Z","action":"buy","price":4.25}',
                '{"symbol":"JEM","ts":"2026-07-01T13:06:00Z","text":"failed breakout"}',
                '{"symbol":"OLD","ts":"2026-06-01T13:06:00Z"}',
                'not-json',
            ]
        ),
        encoding="utf-8",
    )

    rows = _read_ross_trade_events(path, since_minutes=60 * 24 * 10)

    assert [row["symbol"] for row in rows] == ["CANF", "JEM"]
    assert rows[0]["action"] == "buy"
    assert rows[0]["price"] == 4.25
    assert rows[1]["note"] == "failed breakout"


def test_symbol_incident_counts_session_snapshot_fill_lifecycle() -> None:
    row = summarize_symbol_incident(
        "LHAI",
        sessions=[
            {
                "id": 10344,
                "symbol": "LHAI",
                "state": "live_finished",
                "updated_at": "2026-07-01T17:33:33Z",
                "risk_snapshot": {
                    "momentum_live_execution": {
                        "entry_submitted": True,
                        "entry_order_id": "entry-1",
                        "entry_trigger_reason": "abcd_break_tick_ok",
                        "exit_order_id": "exit-1",
                        "last_exit_price": 1.5314,
                        "last_exit_reason": "bailout",
                        "realized_pnl_usd": -5.1766,
                    }
                },
            }
        ],
        events=[],
    )

    assert row["classification"] == "entered"
    assert row["ross_vs_chili_verdict"] == "chili_entered_and_exited"
    assert row["entry_count"] == 1
    assert row["exit_count"] == 1
    assert row["entry_event_count"] == 0
    assert row["entry_session_evidence_count"] == 1
    assert [r["reason"] for r in row["latest_reasons"][:2]] == ["bailout", "abcd_break_tick_ok"]
    assert "risk_snapshot" not in row["latest_session"]
    assert row["latest_session"]["live_execution"]["entry_trigger_reason"] == "abcd_break_tick_ok"
    assert row["latest_session"]["live_execution"]["realized_pnl_usd"] == -5.1766


def test_symbol_incident_can_include_full_risk_snapshot_when_requested() -> None:
    row = summarize_symbol_incident(
        "TC",
        sessions=[
            {
                "id": 10343,
                "symbol": "TC",
                "state": "live_cooldown",
                "risk_snapshot": {
                    "momentum_live_execution": {
                        "entry_submitted": True,
                        "entry_order_id": "entry-tc",
                        "entry_trigger_reason": "first_pullback_tick_ok",
                        "large_nested_payload": {"kept_only_when_requested": True},
                    }
                },
            }
        ],
        events=[],
        include_risk_snapshot=True,
    )

    assert row["latest_session"]["live_execution"]["entry_order_id"] == "entry-tc"
    assert row["latest_session"]["risk_snapshot"]["momentum_live_execution"]["large_nested_payload"] == {
        "kept_only_when_requested": True
    }


def test_incident_session_filter_defaults_to_live_created_inside_window() -> None:
    cutoff = datetime.fromisoformat("2026-07-01T12:00:00")
    sessions = [
        {
            "id": 1,
            "symbol": "JEM",
            "mode": "paper",
            "created_at": "2026-06-12T12:58:23+00:00",
            "updated_at": "2026-07-01T17:16:11+00:00",
        },
        {
            "id": 2,
            "symbol": "JEM",
            "mode": "live",
            "created_at": "2026-07-01T13:01:00+00:00",
            "updated_at": "2026-07-01T13:02:00+00:00",
        },
    ]

    assert [row["id"] for row in filter_sessions_for_incident_window(sessions, cutoff=cutoff)] == [2]
    assert [row["id"] for row in filter_sessions_for_incident_window(sessions, cutoff=cutoff, mode="all")] == [2]
