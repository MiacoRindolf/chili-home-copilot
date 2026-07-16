from __future__ import annotations

import json

from scripts.summarize_ross_live_monitor import read_monitor_snapshots, summarize_monitor_snapshots


def test_summarize_monitor_snapshots_surfaces_attention_and_latest_symbol_state() -> None:
    snapshots = [
        {
            "ok": False,
            "as_of_utc": "2026-07-02T10:58:00+00:00",
            "readiness": {"reason": "ross_live_window_ready"},
            "attention_symbols": ["CANF"],
            "incidents": [
                {
                    "symbol": "CANF",
                    "classification": "entered",
                    "ross_vs_chili_verdict": "chili_entered_too_late_for_ross_scalp",
                    "operator_attention": {
                        "needs_review": True,
                        "reason": "chili_entered_too_late_for_ross_scalp",
                    },
                    "timing": {
                        "ross_entry_speed_class": "too_late_for_ross_scalp",
                        "ross_reference_to_entry_latency_s": 45.0,
                    },
                    "session_count": 1,
                    "admission_count": 1,
                    "entry_count": 1,
                    "exit_count": 1,
                    "latest_reasons": [],
                }
            ],
        },
        {
            "ok": True,
            "as_of_utc": "2026-07-02T10:58:02+00:00",
            "readiness": {"reason": "ross_live_window_ready"},
            "attention_symbols": ["JEM"],
            "incidents": [
                {
                    "symbol": "JEM",
                    "classification": "admitted_watched_or_blocked",
                    "ross_vs_chili_verdict": "chili_saw_but_did_not_enter",
                    "operator_attention": {"needs_review": True, "reason": "chili_saw_but_did_not_enter"},
                    "timing": {"ross_entry_speed_class": "unknown"},
                    "session_count": 1,
                    "admission_count": 1,
                    "entry_count": 0,
                    "exit_count": 0,
                    "latest_reasons": [{"reason": "waiting_for_vwap_reclaim"}],
                }
            ],
        },
    ]

    summary = summarize_monitor_snapshots(snapshots)

    assert summary["snapshot_count"] == 2
    assert summary["ok_snapshot_count"] == 1
    assert summary["attention_symbols"] == ["CANF", "JEM"]
    assert summary["verdict_counts"]["chili_entered_too_late_for_ross_scalp"] == 1
    assert summary["verdict_counts"]["chili_saw_but_did_not_enter"] == 1
    canf = next(row for row in summary["latest_symbols"] if row["symbol"] == "CANF")
    jem = next(row for row in summary["latest_symbols"] if row["symbol"] == "JEM")
    assert canf["needs_review"] is True
    assert canf["ross_reference_to_entry_latency_s"] == 45.0
    assert jem["latest_reasons"] == ["waiting_for_vwap_reclaim"]


def test_read_monitor_snapshots_ignores_bad_lines_and_resolves_date_template(tmp_path) -> None:
    path = tmp_path / "ross_live_monitor_20260701.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ok": True, "as_of_utc": "2026-07-01T11:00:00+00:00"}),
                "not-json",
                json.dumps(["wrong-shape"]),
            ]
        ),
        encoding="utf-8",
    )

    rows = read_monitor_snapshots(tmp_path / "missing_ross_live_monitor_{date}.jsonl")

    assert rows == []
    assert read_monitor_snapshots(path) == [{"ok": True, "as_of_utc": "2026-07-01T11:00:00+00:00"}]
