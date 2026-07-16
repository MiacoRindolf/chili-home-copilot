from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_ross_visual_evidence import (
    DEFAULT_EVIDENCE_ROOT,
    DEFAULT_REVIEW_MANIFEST,
    assert_visual_review_manifest_ready,
    assert_visual_evidence_ready,
    audit_visual_review_manifest,
    audit_visual_evidence_folder,
    audit_visual_evidence_root,
    main as visual_evidence_main,
)


def _write_file(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_visual_evidence_folder_requires_video_transcripts_and_frames(tmp_path: Path) -> None:
    evidence = tmp_path / "abc123"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 JEM break")
    _write_file(evidence / "transcript_flat.txt", b"JEM break")
    for idx in range(3):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    row = audit_visual_evidence_folder(evidence, min_frames=3)

    assert row.ready is True
    assert row.frame_count == 3
    assert row.has_video is True
    assert row.has_timestamped_transcript is True
    assert row.has_flat_transcript is True


def test_visual_evidence_folder_fails_closed_when_frames_are_missing(tmp_path: Path) -> None:
    evidence = tmp_path / "abc123"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 JEM break")
    _write_file(evidence / "transcript_flat.txt", b"JEM break")

    row = audit_visual_evidence_folder(evidence, min_frames=3)

    assert row.ready is False
    assert "frames>=3" in row.missing


def test_visual_evidence_root_strict_mode_rejects_incomplete_dirs(tmp_path: Path) -> None:
    complete = tmp_path / "complete"
    _write_file(complete / "video.mp4", b"video")
    _write_file(complete / "transcript_ts.txt", b"00:01 JEM break")
    _write_file(complete / "transcript_flat.txt", b"JEM break")
    for idx in range(3):
        _write_file(complete / "frames" / f"f{idx:04d}.jpg", b"frame")

    incomplete = tmp_path / "incomplete"
    _write_file(incomplete / "transcript_ts.txt", b"00:01 JEM break")

    audit = audit_visual_evidence_root(tmp_path, min_frames=3)
    assert audit["evidence_count"] == 2
    assert audit["ready_count"] == 1
    assert audit["not_ready_count"] == 1

    with pytest.raises(AssertionError, match="not ready"):
        assert_visual_evidence_ready(tmp_path, min_evidence_dirs=1, min_frames=3)


def test_current_ross_visual_evidence_artifacts_are_readable_when_present() -> None:
    if not DEFAULT_EVIDENCE_ROOT.exists():
        pytest.skip("local Ross visual evidence artifacts are not present")

    audit = assert_visual_evidence_ready(DEFAULT_EVIDENCE_ROOT, min_evidence_dirs=1, min_frames=3)

    assert audit["ready_count"] >= 1
    assert audit["total_frames"] >= 3


def test_visual_review_manifest_validates_reviewed_frame_paths(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-ready"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"TC",'
            '"evidence_id":"vid-ready",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":false,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"scanner only"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["review_count"] == 1
    assert audit["valid_count"] == 1
    assert audit["invalid_count"] == 0
    assert audit["certifying_count"] == 0


def test_visual_review_manifest_accepts_manifest_relative_frame_paths(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-ready"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"TC",'
            '"evidence_id":"vid-ready",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"source_observed_at":"2026-07-01T09:10:00+00:00",'
            '"opportunity_ts":"2026-07-01T09:12:00+00:00",'
            '"reviewed_frame_paths":["evidence/vid-ready/frames/f0001.jpg"],'
            '"observation":"manifest-relative chart frame path"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["valid_count"] == 1
    assert audit["invalid_count"] == 0
    assert audit["source_before_opportunity_certifying_count"] == 1


def test_visual_review_manifest_counts_outcome_and_source_certification_separately(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-canf"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 CANF")
    _write_file(evidence / "transcript_flat.txt", b"CANF")
    frame = evidence / "frames" / "f0529.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 1):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"CANF",'
            '"evidence_id":"vid-canf",'
            '"evidence_type":"post_opportunity_chart_review_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":false,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"post-trade chart and P&L recap, not source-before-opportunity"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["valid_count"] == 1
    assert audit["certifying_count"] == 0
    assert audit["ross_trade_outcome_certifying_count"] == 1
    assert audit["source_before_opportunity_certifying_count"] == 0
    assert audit["rows"][0]["ross_trade_outcome_certifiable"] is True
    assert audit["rows"][0]["source_before_opportunity_certifiable"] is False


def test_visual_review_manifest_reports_shared_evidence_ids(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-shared"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC CANF")
    _write_file(evidence / "transcript_flat.txt", b"TC CANF")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":['
            '{"symbol":"TC",'
            '"evidence_id":"vid-shared",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":false,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"TC scanner context"},'
            '{"symbol":"CANF",'
            '"evidence_id":"vid-shared",'
            '"evidence_type":"post_opportunity_chart_review_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":false,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"CANF outcome context"}'
            "]}"
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["valid_count"] == 2
    assert audit["shared_evidence_id_count"] == 1
    assert audit["shared_evidence_ids"] == [
        {
            "evidence_id": "vid-shared",
            "review_count": 2,
            "symbols": ["CANF", "TC"],
            "rows": [0, 1],
        }
    ]


def test_visual_review_manifest_requires_explicit_source_boundary_fields(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-old"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"TC",'
            '"evidence_id":"vid-old",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"old schema row without source/outcome split"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["invalid_count"] == 1
    assert "ross_trade_outcome_certifiable" in audit["rows"][0]["missing"]
    assert "source_before_opportunity_certifiable" in audit["rows"][0]["missing"]


def test_visual_review_manifest_rejects_source_before_without_trade_context(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-contradiction"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"TC",'
            '"evidence_id":"vid-contradiction",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":false,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":true,'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"contradictory row: source-before true but no trade/no-trade chart context"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["invalid_count"] == 1
    assert "source_before_requires_trade_no_trade_certifiable" in audit["rows"][0]["missing"]


def test_visual_review_manifest_rejects_source_before_scanner_evidence_type(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-scanner"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"TC",'
            '"evidence_id":"vid-scanner",'
            '"evidence_type":"scanner_selection_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":false,'
            '"source_before_opportunity_certifiable":true,'
            '"source_observed_at":"2026-07-01T09:10:00+00:00",'
            '"opportunity_ts":"2026-07-01T09:12:00+00:00",'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"scanner row was incorrectly marked as source-before proof"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["invalid_count"] == 1
    assert "source_before_requires_chart_trade_evidence_type" in audit["rows"][0]["missing"]


def test_visual_review_manifest_rejects_source_after_opportunity_timestamp(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-recap"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 CANF")
    _write_file(evidence / "transcript_flat.txt", b"CANF")
    frame = evidence / "frames" / "f0001.jpg"
    _write_file(frame, b"frame")
    for idx in (0, 2):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")

    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        (
            '{"reviews":[{'
            '"symbol":"CANF",'
            '"evidence_id":"vid-recap",'
            '"evidence_type":"chart_trade_context",'
            '"trade_no_trade_certifiable":true,'
            '"ross_trade_outcome_certifiable":true,'
            '"source_before_opportunity_certifiable":true,'
            '"source_observed_at":"2026-07-01T21:53:58+00:00",'
            '"opportunity_ts":"2026-07-01T13:25:55+00:00",'
            f'"reviewed_frame_paths":["{frame.as_posix()}"],'
            '"observation":"recap chart frame occurs after the replay opportunity"'
            '}]}'
        ),
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["invalid_count"] == 1
    assert "source_before_timestamp_after_opportunity" in audit["rows"][0]["missing"]


def test_visual_review_manifest_fails_closed_for_missing_frames(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid-ready"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"00:01 TC")
    _write_file(evidence / "transcript_flat.txt", b"TC")
    for idx in range(3):
        _write_file(evidence / "frames" / f"f{idx:04d}.jpg", b"frame")
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        '{"reviews":[{"symbol":"TC","evidence_id":"vid-ready","evidence_type":"scanner",'
        '"trade_no_trade_certifiable":false,'
        '"ross_trade_outcome_certifiable":false,'
        '"source_before_opportunity_certifiable":false,'
        '"reviewed_frame_paths":["missing.jpg"],'
        '"observation":"scanner only"}]}',
        encoding="utf-8",
    )

    audit = audit_visual_review_manifest(manifest, evidence_root=evidence_root, min_frames=3)

    assert audit["invalid_count"] == 1
    assert any("frame_missing:missing.jpg" in item for item in audit["rows"][0]["missing"])


def test_current_ross_visual_review_manifest_is_valid_when_present() -> None:
    if not DEFAULT_REVIEW_MANIFEST.exists():
        pytest.skip("local Ross visual review manifest is not present")

    audit = assert_visual_review_manifest_ready(
        DEFAULT_REVIEW_MANIFEST,
        evidence_root=DEFAULT_EVIDENCE_ROOT,
        min_reviews=1,
        min_frames=3,
    )

    assert audit["valid_count"] >= 1
    assert audit["invalid_count"] == 0


def test_visual_evidence_cli_strict_all_checks_manifest_and_evidence(tmp_path: Path, capsys) -> None:
    evidence_root = tmp_path / "evidence"
    evidence = evidence_root / "vid1"
    _write_file(evidence / "video.mp4", b"video")
    _write_file(evidence / "transcript_ts.txt", b"[00:01] TC")
    _write_file(evidence / "frames" / "f0001.jpg", b"frame")
    _write_file(evidence / "frames" / "f0002.jpg", b"frame")
    _write_file(evidence / "frames" / "f0003.jpg", b"frame")
    manifest = tmp_path / "review_manifest.json"
    manifest.write_text(
        "{"
        '"reviews":['
        "{"
        '"symbol":"TC",'
        '"evidence_id":"vid1",'
        '"evidence_type":"chart_trade_context",'
        '"trade_no_trade_certifiable":true,'
        '"ross_trade_outcome_certifiable":false,'
        '"source_before_opportunity_certifiable":true,'
        '"source_observed_at":"2026-07-01T09:10:00+00:00",'
        '"opportunity_ts":"2026-07-01T09:12:00+00:00",'
        '"reviewed_frame_paths":["evidence/vid1/frames/f0001.jpg"],'
        '"observation":"pre-opportunity chart trade context"'
        "}"
        "]"
        "}",
        encoding="utf-8",
    )

    code = visual_evidence_main(
        [
            "--root",
            str(evidence_root),
            "--review-manifest",
            str(manifest),
            "--strict-all",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["strict_all"] is True
    assert payload["not_ready_evidence_count"] == 0
    assert payload["invalid_review_count"] == 0
    assert payload["source_before_opportunity_certifying_count"] == 1
