from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.ross_live_monitor_snapshot import (
    _admission_symbols,
    _actionable_transcript_mentions,
    _resolve_transcript_mentions_to_admissions,
    _snapshot_symbols,
    append_snapshots_jsonl,
    build_monitor_snapshot,
    iter_monitor_snapshots,
    resolve_snapshot_output_path,
)


def test_admission_symbols_extracts_unique_live_payload_symbols() -> None:
    assert _admission_symbols(
        [
            {"payload": {"symbol": "CANF"}},
            {"payload": {"ticker": "JEM"}},
            {"payload": {"symbol": "CANF"}},
            {"payload": {"symbol": ""}},
        ]
    ) == ["CANF", "JEM"]


def test_snapshot_symbols_ignore_transcript_mentions_when_marker_invalid() -> None:
    symbols = _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=[{"symbol": "TC"}, {"symbol": "LHAI"}],
        ross_trade_events=[],
        recent_events=[],
        marker_ok=False,
    )

    assert symbols == []


def test_snapshot_symbols_keep_trade_and_admission_evidence_when_marker_invalid() -> None:
    symbols = _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=[{"symbol": "TC"}],
        ross_trade_events=[{"symbol": "CANF"}],
        recent_events=[{"payload": {"symbol": "JEM"}}],
        marker_ok=False,
    )

    assert symbols == ["CANF", "JEM"]


def test_snapshot_symbols_keep_explicit_symbols_when_marker_invalid() -> None:
    symbols = _snapshot_symbols(
        symbols_arg=["tc"],
        transcript_mentions=[{"symbol": "LHAI"}],
        ross_trade_events=[],
        recent_events=[],
        marker_ok=False,
    )

    assert symbols == ["TC"]


def test_snapshot_symbols_resolve_missing_leading_letter_transcript_alias_to_admission() -> None:
    mentions = [{"symbol": "LRO", "ts": "2026-07-02T12:23:44Z", "text": "LRO starter through five"}]
    events = [{"payload": {"symbol": "CLRO"}}]

    resolved = _resolve_transcript_mentions_to_admissions(mentions, _admission_symbols(events))
    symbols = _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=mentions,
        ross_trade_events=[],
        recent_events=events,
        marker_ok=True,
    )

    assert resolved[0]["symbol"] == "CLRO"
    assert resolved[0]["original_symbol"] == "LRO"
    assert symbols == ["CLRO"]


def test_snapshot_symbols_do_not_resolve_unrelated_transcript_symbol_to_admission() -> None:
    symbols = _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=[{"symbol": "DXTS", "text": "DXTS starter"}],
        ross_trade_events=[],
        recent_events=[{"payload": {"symbol": "DXF"}}],
        marker_ok=True,
    )

    assert symbols == ["DXTS", "DXF"]


def test_snapshot_symbols_filter_negative_no_trade_transcript_mentions() -> None:
    mentions = [
        {"symbol": "SURG", "text": "DSY pulled back too much. S-U-R-G, nope."},
        {"symbol": "USDE", "text": "CWD, 158 million shares of volume, not interested. USDE not interested. LG."},
        {"symbol": "CWD", "text": "we're going to get anything more on it. CWD, 158 million shares of volume, grinding high are not interested. USDE not interested."},
        {"symbol": "CANF", "text": "CANF starter through five"},
    ]

    assert [row["symbol"] for row in _actionable_transcript_mentions(mentions)] == ["CANF"]
    assert _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=mentions,
        ross_trade_events=[],
        recent_events=[],
        marker_ok=True,
    ) == ["CANF"]


def test_snapshot_symbols_filter_passive_review_transcript_mentions() -> None:
    mentions = [
        {
            "symbol": "CWD",
            "text": "And so 163 was a big level for CWD, 218 was the 200 EMA, and it's outside the channel right now. So that's kind of off my radar.",
        },
        {
            "symbol": "CETX",
            "text": "CETX had this nice pullback here to this high volume 5-minute candle. The very high volume 5-minute candle at the time I had that line.",
        },
        {
            "symbol": "USDA",
            "text": "a stock that could potentially curl and go to high of day. Now, USDA had this, let me put the volume on the chart here.",
        },
        {
            "symbol": "USDE",
            "text": "is after hours you can get offerings. Now let's go to um USDE. Now this was a stop.",
        },
        {
            "symbol": "CRM",
            "text": "CRM, the swing trade idea is continuing here as we're holding 163. Now I don't really have another target there.",
        },
        {
            "symbol": "CWD",
            "text": "in target 672, it would have been a great idea and trade. But the penny stock traders kept trading CWD because it was in this channel.",
        },
        {
            "symbol": "CETX",
            "text": "Understanding that dynamic is really important to not incur losses. So of course I would have preferred CETX to curl. You could have got a dollar per share.",
        },
        {
            "symbol": "CETX",
            "text": "probably give one example on CETX earlier. So you know remember CETX was competing against CWD. So I had both of them on the chart.",
        },
        {
            "symbol": "CWD",
            "text": "probably give one example on CETX earlier. So you know remember CETX was competing against CWD. So I had both of them on the chart.",
        },
        {
            "symbol": "MST",
            "text": "Apple coin X Incorporated Bitcoin's going up MST is going up and maybe some of these garbage crypto plays that are trading on the NASDAQ.",
        },
        {
            "symbol": "USDE",
            "text": "You USDE, yeah. So even though USDE didn't do well so far, it's showing you the possibility of a curl. So USDE's name stays.",
        },
        {"symbol": "CANF", "text": "CANF starter through five"},
    ]

    assert [row["symbol"] for row in _actionable_transcript_mentions(mentions)] == ["CANF"]
    assert _snapshot_symbols(
        symbols_arg=[],
        transcript_mentions=mentions,
        ross_trade_events=[],
        recent_events=[],
        marker_ok=True,
    ) == ["CANF"]


def test_monitor_snapshot_includes_readiness_and_incidents() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["CANF"],
        readiness_ok=True,
        readiness_reason="ross_live_window_ready",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 1, "min_checked": 1},
            "transcript": {"warrior_session_reason": "warrior_session_ok", "running_daemons": []},
        },
        incidents=[
            {
                "symbol": "CANF",
                "classification": "entered",
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 1,
                "exit_count": 1,
                "latest_reasons": [],
            }
        ],
        since_minutes=30.0,
        readiness_since_minutes=5.0,
        mode="live",
        profile="prestream",
    )

    assert snapshot["ok"] is True
    assert snapshot["read_only"] is True
    assert snapshot["mode"] == "live"
    assert snapshot["profile"] == "prestream"
    assert snapshot["readiness_since_minutes"] == 5.0
    assert snapshot["readiness"]["reason"] == "ross_live_window_ready"
    assert snapshot["readiness"]["admission_checked"] == 1
    assert snapshot["symbols_requested"] == ["CANF"]
    assert snapshot["incidents"][0]["classification"] == "entered"
    assert snapshot["attention_count"] == 0
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is False


def test_monitor_snapshot_flags_late_or_missed_incidents_for_operator_attention() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["CANF", "JEM"],
        readiness_ok=True,
        readiness_reason="ross_live_window_ready",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 2, "min_checked": 1},
            "transcript": {"warrior_session_reason": "warrior_session_ok", "running_daemons": [123]},
        },
        incidents=[
            {
                "symbol": "CANF",
                "classification": "entered",
                "ross_vs_chili_verdict": "chili_entered_too_late_for_ross_scalp",
                "timing": {"ross_entry_speed_class": "too_late_for_ross_scalp"},
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 1,
                "exit_count": 1,
                "latest_reasons": [],
            },
            {
                "symbol": "JEM",
                "classification": "admitted_watched_or_blocked",
                "ross_vs_chili_verdict": "chili_saw_but_did_not_enter",
                "ross_mentions": [{"text": "Watching JEM for a breakout attempt"}],
                "ross_trades": [],
                "timing": {"ross_entry_speed_class": "unknown", "ross_reference": "mention"},
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 0,
                "exit_count": 0,
                "latest_reasons": [{"reason": "waiting_for_vwap_reclaim"}],
            },
        ],
        since_minutes=30.0,
        readiness_since_minutes=5.0,
        mode="live",
        profile="live",
    )

    assert snapshot["attention_count"] == 2
    assert snapshot["attention_symbols"] == ["CANF", "JEM"]
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is True
    assert snapshot["incidents"][0]["operator_attention"]["speed"] == "too_late_for_ross_scalp"
    assert snapshot["incidents"][1]["operator_attention"]["latest_reasons"] == ["waiting_for_vwap_reclaim"]


def test_monitor_snapshot_does_not_page_stale_transcript_only_context() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["EHGO"],
        readiness_ok=False,
        readiness_reason="warrior_disclaimer_blocking",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 1, "min_checked": 0},
            "transcript": {"warrior_session_reason": "warrior_disclaimer_blocking", "running_daemons": []},
        },
        incidents=[
            {
                "symbol": "EHGO",
                "classification": "admitted_ticked",
                "ross_vs_chili_verdict": "chili_admitted_and_ticked_no_entry",
                "ross_trades": [],
                "timing": {
                    "ross_reference": "mention",
                    "ross_entry_speed_class": "unknown",
                    "ross_reference_to_admission_latency_s": 6768.712,
                    "ross_reference_to_entry_latency_s": None,
                },
                "session_count": 2,
                "admission_count": 1,
                "entry_count": 0,
                "exit_count": 0,
                "latest_reasons": [{"reason": "wide_bbo_spread"}],
            }
        ],
        since_minutes=720.0,
        readiness_since_minutes=30.0,
        mode="live",
        profile="prestream",
    )

    assert snapshot["attention_count"] == 0
    assert snapshot["attention_symbols"] == []
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is False
    assert snapshot["incidents"][0]["operator_attention"]["reason"] == "stale_transcript_only_context"


def test_monitor_snapshot_does_not_page_when_chili_saw_symbol_before_transcript_mention() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["TC"],
        readiness_ok=False,
        readiness_reason="warrior_disclaimer_blocking",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 1, "min_checked": 0},
            "transcript": {"warrior_session_reason": "warrior_disclaimer_blocking", "running_daemons": []},
        },
        incidents=[
            {
                "symbol": "TC",
                "classification": "admitted_watched_or_blocked",
                "ross_vs_chili_verdict": "chili_saw_but_did_not_enter",
                "ross_trades": [],
                "timing": {
                    "ross_reference": "mention",
                    "ross_entry_speed_class": "unknown",
                    "ross_reference_to_admission_latency_s": -1006.235,
                    "ross_reference_to_entry_latency_s": None,
                },
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 0,
                "exit_count": 0,
                "latest_reasons": [{"reason": "waiting_for_tick_reclaim"}],
            }
        ],
        since_minutes=720.0,
        readiness_since_minutes=30.0,
        mode="live",
        profile="prestream",
    )

    assert snapshot["attention_count"] == 0
    assert snapshot["attention_symbols"] == []
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is False
    assert snapshot["incidents"][0]["operator_attention"]["reason"] == "chili_seen_before_transcript_reference"


def test_monitor_snapshot_does_not_page_autonomous_chili_no_entry_without_ross_reference() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["AVXX"],
        readiness_ok=True,
        readiness_reason="ross_live_window_ready",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 1, "min_checked": 1},
            "transcript": {"warrior_session_reason": "warrior_session_ok", "running_daemons": [123]},
        },
        incidents=[
            {
                "symbol": "AVXX",
                "classification": "admitted_ticked",
                "ross_vs_chili_verdict": "chili_admitted_and_ticked_no_entry",
                "ross_mentions": [],
                "ross_trades": [],
                "timing": {"ross_entry_speed_class": "unknown", "ross_reference": None},
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 0,
                "exit_count": 0,
                "latest_reasons": [{"reason": "below_baseline_and_losing"}],
            }
        ],
        since_minutes=30.0,
        readiness_since_minutes=30.0,
        mode="live",
        profile="live",
    )

    assert snapshot["attention_count"] == 0
    assert snapshot["attention_symbols"] == []
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is False
    assert snapshot["incidents"][0]["operator_attention"]["reason"] == "autonomous_chili_watch_no_ross_reference"


def test_monitor_snapshot_does_not_page_autonomous_chili_watched_block_without_ross_reference() -> None:
    snapshot = build_monitor_snapshot(
        symbols=["IPW"],
        readiness_ok=True,
        readiness_reason="ross_live_window_ready",
        readiness_detail={
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": "ok",
            "admission_reason": "ross_event_admission_runtime_ok",
            "admission": {"checked": 1, "min_checked": 1},
            "transcript": {"warrior_session_reason": "warrior_session_ok", "running_daemons": [123]},
        },
        incidents=[
            {
                "symbol": "IPW",
                "classification": "admitted_watched_or_blocked",
                "ross_vs_chili_verdict": "chili_saw_but_did_not_enter",
                "ross_mentions": [],
                "ross_trades": [],
                "timing": {"ross_entry_speed_class": "unknown", "ross_reference": None},
                "session_count": 1,
                "admission_count": 1,
                "entry_count": 0,
                "exit_count": 0,
                "latest_reasons": [{"reason": "waiting_for_tick_reclaim"}],
            }
        ],
        since_minutes=30.0,
        readiness_since_minutes=30.0,
        mode="live",
        profile="live",
    )

    assert snapshot["attention_count"] == 0
    assert snapshot["attention_symbols"] == []
    assert snapshot["incidents"][0]["operator_attention"]["needs_review"] is False
    assert snapshot["incidents"][0]["operator_attention"]["reason"] == "autonomous_chili_watch_no_ross_reference"


def test_monitor_watch_iterator_is_bounded_and_sleeps_between_snapshots() -> None:
    calls = []
    events = []

    def snapshot_fn():
        calls.append(len(calls) + 1)
        return {"ok": True, "seq": calls[-1]}

    snapshots = iter_monitor_snapshots(
        max_iterations=3,
        interval_seconds=2.0,
        snapshot_fn=snapshot_fn,
        sleep_fn=lambda seconds: events.append(("sleep", seconds)),
        on_snapshot=lambda snapshot: events.append(("snapshot", snapshot["seq"])),
    )

    assert [row["seq"] for row in snapshots] == [1, 2, 3]
    assert events == [
        ("snapshot", 1),
        ("sleep", 2.0),
        ("snapshot", 2),
        ("sleep", 2.0),
        ("snapshot", 3),
    ]


def test_append_snapshots_jsonl_writes_one_snapshot_per_line(tmp_path) -> None:
    out = tmp_path / "ross_monitor.jsonl"

    append_snapshots_jsonl(out, [{"ok": True, "seq": 1}, {"ok": False, "seq": 2}])
    append_snapshots_jsonl(out, [{"ok": True, "seq": 3}])

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"ok": True, "seq": 1}, {"ok": False, "seq": 2}, {"ok": True, "seq": 3}]


def test_append_snapshots_jsonl_resolves_date_template(tmp_path) -> None:
    out_template = tmp_path / "ross_live_monitor_{date}.jsonl"

    resolved = resolve_snapshot_output_path(out_template)
    assert "{date}" not in str(resolved)

    written = append_snapshots_jsonl(out_template, [{"ok": True, "seq": 1}])

    assert written.name.startswith("ross_live_monitor_")
    assert written.name.endswith(".jsonl")
    assert json.loads(written.read_text(encoding="utf-8")) == {"ok": True, "seq": 1}


def test_snapshot_output_date_template_uses_pacific_trading_day(tmp_path) -> None:
    out_template = tmp_path / "ross_live_monitor_{date}.jsonl"
    utc_next_day = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)

    resolved = resolve_snapshot_output_path(out_template, now_utc=utc_next_day)

    assert resolved.name == "ross_live_monitor_20260701.jsonl"
