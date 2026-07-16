from __future__ import annotations

import json

import pytest

from scripts.mark_ross_trade_event import append_ross_trade_event, build_ross_trade_event, main


def test_build_ross_trade_event_normalizes_symbol_time_and_price() -> None:
    event = build_ross_trade_event(
        symbol="canf",
        action="BUY",
        price="4.25",
        ts="2026-07-02T11:05:00Z",
        note="Ross first pullback scalp",
        visual_evidence_id="vid-canf-chart",
    )

    assert event == {
        "symbol": "CANF",
        "ts": "2026-07-02T11:05:00+00:00",
        "action": "buy",
        "price": 4.25,
        "note": "Ross first pullback scalp",
        "visual_evidence_id": "vid-canf-chart",
    }


def test_build_ross_trade_event_rejects_bad_price() -> None:
    with pytest.raises(ValueError, match="price_must_be_numeric"):
        build_ross_trade_event(symbol="CANF", price="nope")


def test_append_ross_trade_event_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "ross_trade_events.jsonl"
    append_ross_trade_event({"symbol": "CANF", "ts": "2026-07-02T11:05:00+00:00"}, path)
    append_ross_trade_event({"symbol": "JEM", "ts": "2026-07-02T11:06:00+00:00"}, path)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"symbol": "CANF", "ts": "2026-07-02T11:05:00+00:00"},
        {"symbol": "JEM", "ts": "2026-07-02T11:06:00+00:00"},
    ]


def test_review_certified_marker_requires_certifiable_visual_manifest(tmp_path, capsys) -> None:
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-scanner",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]}]}'
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ross_trade_events.jsonl"

    rc = main(
        [
            "TC",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-scanner",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "trade_event_visual_evidence_not_source_before_opportunity" in captured.err
    assert not out.exists()


def test_review_certified_marker_records_valid_visual_certification(tmp_path, capsys) -> None:
    frame = tmp_path / "frames" / "f0123.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-chart",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ross_trade_events.jsonl"

    rc = main(
        [
            "CANF",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-chart",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rc == 0
    assert str(out) in captured.out
    assert rows == [
        {
            "action": "review_certified",
            "certification_reason": "trade_event_visual_evidence_trade_certified",
            "symbol": "CANF",
            "ts": "2026-07-02T11:05:00+00:00",
            "visual_evidence_id": "vid-chart",
        }
    ]


def test_review_certified_dry_run_validates_without_writing(tmp_path, capsys) -> None:
    frame = tmp_path / "frames" / "f0123.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"CANF","evidence_id":"vid-chart",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0123.jpg"]}]}'
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ross_trade_events.jsonl"

    rc = main(
        [
            "CANF",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-chart",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["would_write"] is False
    assert payload["event"]["certification_reason"] == "trade_event_visual_evidence_trade_certified"
    assert not out.exists()


def test_review_certified_dry_run_rejects_noncertifying_visual_manifest(tmp_path, capsys) -> None:
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{"symbol":"TC","evidence_id":"vid-scanner",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]}]}'
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ross_trade_events.jsonl"

    rc = main(
        [
            "TC",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-scanner",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "trade_event_visual_evidence_not_source_before_opportunity" in captured.err
    assert not out.exists()


def test_review_certified_marker_is_symbol_aware_for_shared_video_ids(tmp_path, capsys) -> None:
    frame = tmp_path / "frames" / "f0280.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"frame")
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":['
            '{"symbol":"TC","evidence_id":"vid-shared",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"reviewed_frame_paths":["frames/f0280.jpg"]},'
            '{"symbol":"CANF","evidence_id":"vid-shared",'
            '"evidence_type":"post_opportunity_chart_review_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":false,'
            '"reviewed_frame_paths":["frames/f0529.jpg"]}'
            ']}'
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ross_trade_events.jsonl"

    canf_rc = main(
        [
            "CANF",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-shared",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
            "--dry-run",
        ]
    )
    canf_output = capsys.readouterr()
    tc_rc = main(
        [
            "TC",
            "--action",
            "review_certified",
            "--ts",
            "2026-07-02T11:05:00Z",
            "--visual-evidence-id",
            "vid-shared",
            "--visual-review-manifest",
            str(manifest),
            "--out",
            str(out),
            "--dry-run",
        ]
    )
    tc_output = capsys.readouterr()

    assert canf_rc == 2
    assert "trade_event_visual_evidence_not_source_before_opportunity" in canf_output.err
    assert tc_rc == 0
    assert json.loads(tc_output.out)["event"]["symbol"] == "TC"
    assert not out.exists()
