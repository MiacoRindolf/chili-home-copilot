from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_EVIDENCE_ROOT = Path("project_ws/AgentOps/ross_video_evidence")
DEFAULT_REVIEW_MANIFEST = DEFAULT_EVIDENCE_ROOT / "review_manifest.json"


@dataclass(frozen=True)
class RossVisualEvidenceRow:
    evidence_id: str
    path: str
    has_video: bool
    has_timestamped_transcript: bool
    has_flat_transcript: bool
    frame_count: int
    total_frame_bytes: int
    ready: bool
    missing: tuple[str, ...]


def _frame_files(frames_dir: Path) -> list[Path]:
    if not frames_dir.exists() or not frames_dir.is_dir():
        return []
    return sorted(
        p
        for p in frames_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )


def audit_visual_evidence_folder(path: Path, *, min_frames: int = 3) -> RossVisualEvidenceRow:
    video = path / "video.mp4"
    transcript_ts = path / "transcript_ts.txt"
    transcript_flat = path / "transcript_flat.txt"
    frames = _frame_files(path / "frames")
    total_frame_bytes = sum(max(0, p.stat().st_size) for p in frames)

    missing: list[str] = []
    if not video.exists() or video.stat().st_size <= 0:
        missing.append("video.mp4")
    transcript_ts_ready = transcript_ts.exists() and transcript_ts.stat().st_size > 0
    transcript_flat_ready = transcript_flat.exists() and transcript_flat.stat().st_size > 0
    if not transcript_ts_ready:
        missing.append("transcript_ts.txt")
    if not transcript_ts_ready and not transcript_flat_ready:
        missing.append("transcript_text")
    if len(frames) < min_frames:
        missing.append(f"frames>={min_frames}")
    if frames and total_frame_bytes <= 0:
        missing.append("nonempty_frames")

    return RossVisualEvidenceRow(
        evidence_id=path.name,
        path=str(path),
        has_video=video.exists() and video.stat().st_size > 0,
        has_timestamped_transcript=transcript_ts_ready,
        has_flat_transcript=transcript_flat_ready,
        frame_count=len(frames),
        total_frame_bytes=total_frame_bytes,
        ready=not missing,
        missing=tuple(missing),
    )


def audit_visual_evidence_root(
    root: Path = DEFAULT_EVIDENCE_ROOT,
    *,
    min_frames: int = 3,
) -> dict[str, object]:
    evidence_dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    rows = [audit_visual_evidence_folder(p, min_frames=min_frames) for p in evidence_dirs]
    ready_rows = [r for r in rows if r.ready]
    return {
        "root": str(root),
        "min_frames": min_frames,
        "evidence_count": len(rows),
        "ready_count": len(ready_rows),
        "not_ready_count": len(rows) - len(ready_rows),
        "total_frames": sum(r.frame_count for r in rows),
        "rows": [asdict(r) for r in rows],
    }


def _review_frame_path_exists(path_text: str, *, manifest_path: Path) -> bool:
    normalized = str(path_text or "").strip().replace("\\", "/")
    if not normalized:
        return False
    path = Path(normalized)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, manifest_path.parent / path]
    return any(candidate.exists() for candidate in candidates)


def _parse_manifest_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def audit_visual_review_manifest(
    manifest_path: Path = DEFAULT_REVIEW_MANIFEST,
    *,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    min_frames: int = 3,
) -> dict[str, object]:
    evidence_audit = audit_visual_evidence_root(evidence_root, min_frames=min_frames)
    ready_evidence_ids = {
        str(row.get("evidence_id") or "")
        for row in evidence_audit["rows"]
        if isinstance(row, dict) and row.get("ready")
    }
    if not manifest_path.exists():
        return {
            "manifest_path": str(manifest_path),
            "exists": False,
            "review_count": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "certifying_count": 0,
            "rows": [],
        }
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "manifest_path": str(manifest_path),
            "exists": True,
            "parse_error": str(exc),
            "review_count": 0,
            "valid_count": 0,
            "invalid_count": 1,
            "certifying_count": 0,
            "rows": [],
        }
    reviews = data.get("reviews") if isinstance(data, dict) else None
    rows: list[dict[str, object]] = []
    if isinstance(reviews, list):
        for idx, review in enumerate(reviews):
            if not isinstance(review, dict):
                rows.append({"index": idx, "valid": False, "missing": ["review_object"]})
                continue
            evidence_id = str(review.get("evidence_id") or "").strip()
            frame_paths = [str(p) for p in review.get("reviewed_frame_paths", []) if str(p or "")]
            missing: list[str] = []
            if not str(review.get("symbol") or "").strip():
                missing.append("symbol")
            if not evidence_id:
                missing.append("evidence_id")
            elif evidence_id not in ready_evidence_ids:
                missing.append("ready_evidence_id")
            if "trade_no_trade_certifiable" not in review:
                missing.append("trade_no_trade_certifiable")
            if "ross_trade_outcome_certifiable" not in review:
                missing.append("ross_trade_outcome_certifiable")
            if "source_before_opportunity_certifiable" not in review:
                missing.append("source_before_opportunity_certifiable")
            if (
                bool(review.get("source_before_opportunity_certifiable"))
                and not bool(review.get("trade_no_trade_certifiable"))
            ):
                missing.append("source_before_requires_trade_no_trade_certifiable")
            evidence_type = str(review.get("evidence_type") or "").strip()
            evidence_type_l = evidence_type.lower()
            if not evidence_type:
                missing.append("evidence_type")
            if bool(review.get("source_before_opportunity_certifiable")) and (
                not evidence_type_l
                or "scanner" in evidence_type_l
                or "post_opportunity" in evidence_type_l
                or ("chart" not in evidence_type_l and "trade" not in evidence_type_l)
            ):
                missing.append("source_before_requires_chart_trade_evidence_type")
            if bool(review.get("source_before_opportunity_certifiable")):
                source_ts = _parse_manifest_ts(review.get("source_observed_at"))
                opportunity_ts = _parse_manifest_ts(review.get("opportunity_ts"))
                if source_ts is None:
                    missing.append("source_before_requires_source_observed_at")
                if opportunity_ts is None:
                    missing.append("source_before_requires_opportunity_ts")
                if source_ts is not None and opportunity_ts is not None and source_ts > opportunity_ts:
                    missing.append("source_before_timestamp_after_opportunity")
            if not str(review.get("observation") or "").strip():
                missing.append("observation")
            if not frame_paths:
                missing.append("reviewed_frame_paths")
            missing.extend(
                f"frame_missing:{path}"
                for path in frame_paths
                if not _review_frame_path_exists(path, manifest_path=manifest_path)
            )
            rows.append(
                {
                    "index": idx,
                    "symbol": str(review.get("symbol") or "").strip().upper(),
                    "evidence_id": evidence_id,
                    "evidence_type": str(review.get("evidence_type") or ""),
                    "trade_no_trade_certifiable": bool(review.get("trade_no_trade_certifiable")),
                    "ross_trade_outcome_certifiable": bool(
                        review.get("ross_trade_outcome_certifiable")
                    ),
                    "source_before_opportunity_certifiable": bool(
                        review.get("source_before_opportunity_certifiable")
                    ),
                    "reviewed_frame_count": len(frame_paths),
                    "valid": not missing,
                    "missing": missing,
                }
            )
    valid_count = sum(1 for row in rows if row.get("valid"))
    rows_by_evidence_id: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        evidence_id = str(row.get("evidence_id") or "").strip()
        if evidence_id:
            rows_by_evidence_id.setdefault(evidence_id, []).append(row)
    shared_evidence_ids = [
        {
            "evidence_id": evidence_id,
            "review_count": len(evidence_rows),
            "symbols": sorted(
                {
                    str(row.get("symbol") or "").strip().upper()
                    for row in evidence_rows
                    if str(row.get("symbol") or "").strip()
                }
            ),
            "rows": [
                int(row["index"])
                for row in evidence_rows
                if isinstance(row.get("index"), int)
            ],
        }
        for evidence_id, evidence_rows in sorted(rows_by_evidence_id.items())
        if len(evidence_rows) > 1
    ]
    return {
        "manifest_path": str(manifest_path),
        "exists": True,
        "review_count": len(rows),
        "valid_count": valid_count,
        "invalid_count": len(rows) - valid_count,
        "certifying_count": sum(1 for row in rows if row.get("trade_no_trade_certifiable")),
        "ross_trade_outcome_certifying_count": sum(
            1 for row in rows if row.get("ross_trade_outcome_certifiable")
        ),
        "source_before_opportunity_certifying_count": sum(
            1 for row in rows if row.get("source_before_opportunity_certifiable")
        ),
        "shared_evidence_id_count": len(shared_evidence_ids),
        "shared_evidence_ids": shared_evidence_ids,
        "rows": rows,
    }


def assert_visual_evidence_ready(
    root: Path = DEFAULT_EVIDENCE_ROOT,
    *,
    min_evidence_dirs: int = 1,
    min_frames: int = 3,
) -> dict[str, object]:
    audit = audit_visual_evidence_root(root, min_frames=min_frames)
    if int(audit["evidence_count"]) < min_evidence_dirs:
        raise AssertionError(
            f"expected at least {min_evidence_dirs} Ross visual evidence dirs, "
            f"found {audit['evidence_count']} under {root}"
        )
    bad_rows = [row for row in audit["rows"] if not row["ready"]]
    if bad_rows:
        raise AssertionError(f"Ross visual evidence dirs not ready: {bad_rows}")
    return audit


def assert_visual_review_manifest_ready(
    manifest_path: Path = DEFAULT_REVIEW_MANIFEST,
    *,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    min_reviews: int = 1,
    min_frames: int = 3,
) -> dict[str, object]:
    audit = audit_visual_review_manifest(
        manifest_path,
        evidence_root=evidence_root,
        min_frames=min_frames,
    )
    if not audit.get("exists"):
        raise AssertionError(f"Ross visual review manifest missing: {manifest_path}")
    if int(audit.get("review_count") or 0) < min_reviews:
        raise AssertionError(
            f"expected at least {min_reviews} Ross visual reviews, found {audit.get('review_count')}"
        )
    bad_rows = [row for row in audit["rows"] if not row.get("valid")]
    if bad_rows:
        raise AssertionError(f"Ross visual review manifest invalid rows: {bad_rows}")
    return audit


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local Ross video/chart-frame evidence artifacts.")
    parser.add_argument("--root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--min-evidence-dirs", type=int, default=1)
    parser.add_argument("--min-frames", type=int, default=3)
    parser.add_argument("--review-manifest", type=Path, default=DEFAULT_REVIEW_MANIFEST)
    parser.add_argument("--audit-review-manifest", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless the selected evidence-dir or review-manifest audit is ready.",
    )
    parser.add_argument(
        "--strict-all",
        action="store_true",
        help="Exit non-zero unless both evidence dirs and the review manifest are ready.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.strict_all:
        evidence_audit = assert_visual_evidence_ready(
            args.root,
            min_evidence_dirs=args.min_evidence_dirs,
            min_frames=args.min_frames,
        )
        manifest_audit = assert_visual_review_manifest_ready(
            args.review_manifest,
            evidence_root=args.root,
            min_reviews=1,
            min_frames=args.min_frames,
        )
        audit = {
            "root": str(args.root),
            "strict_all": True,
            "evidence": evidence_audit,
            "review_manifest": manifest_audit,
            "source_before_opportunity_certifying_count": manifest_audit.get(
                "source_before_opportunity_certifying_count", 0
            ),
            "ross_trade_outcome_certifying_count": manifest_audit.get(
                "ross_trade_outcome_certifying_count", 0
            ),
            "invalid_review_count": manifest_audit.get("invalid_count", 0),
            "not_ready_evidence_count": evidence_audit.get("not_ready_count", 0),
        }
    elif args.audit_review_manifest and args.strict:
        audit = assert_visual_review_manifest_ready(
            args.review_manifest,
            evidence_root=args.root,
            min_reviews=1,
            min_frames=args.min_frames,
        )
    elif args.audit_review_manifest:
        audit = audit_visual_review_manifest(
            args.review_manifest,
            evidence_root=args.root,
            min_frames=args.min_frames,
        )
    elif args.strict:
        audit = assert_visual_evidence_ready(
            args.root,
            min_evidence_dirs=args.min_evidence_dirs,
            min_frames=args.min_frames,
        )
    else:
        audit = audit_visual_evidence_root(args.root, min_frames=args.min_frames)
    print(json.dumps(audit, indent=2, sort_keys=True, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
