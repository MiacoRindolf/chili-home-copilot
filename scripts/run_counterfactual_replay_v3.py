"""Run CHILI counterfactual Replay v3 over persisted market tape.

This is not the historical live-session audit. It replays IQFeed/NBBO tape
against current CHILI entry gates and a local simulated broker.

Example:
  python scripts/run_counterfactual_replay_v3.py --date 2026-07-01 --symbols JEM CANF DXF TC LHAI
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.trading.momentum_neural.counterfactual_replay import (  # noqa: E402
    result_to_dict,
    run_counterfactual_replay,
)

# SHIM (main-lineage adoption): the Ross VISUAL-EVIDENCE review helpers live in the
# codex-only ``scripts/audit_ross_symbol_incidents.py`` (a 45KB video-frame-review tool
# NOT part of the 6-file replay suite and not adopted here). They only decorate the
# counterfactual result with Ross-video-frame review status — the replay ENGINE itself
# does not need them. Import the real module if present; else fall back to benign
# no-op/empty stubs so the counterfactual replay + day runner still run on main (the
# visual-review annotations simply come back empty). When the video-review tool lands on
# main as its own PR, this shim transparently picks it up.
try:  # pragma: no cover - exercised only on the fork lineage
    from scripts.audit_ross_symbol_incidents import (  # noqa: E402
        DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT,
        DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
        _absolute_frame_paths,
        _flatten_review_frame_paths,
        _manifest_review_template,
        _visual_evidence_status,
        audit_visual_evidence_root,
        _read_visual_review_manifest,
    )
except ImportError:  # main lineage: visual-evidence review tool not present
    DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT = Path(
        "project_ws/AgentOps/ross_video_evidence"
    )
    DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH = Path(
        "project_ws/AgentOps/ross_video_evidence/review_manifest.json"
    )

    def _absolute_frame_paths(paths):  # type: ignore
        return [str(Path(p).resolve()) for p in (paths or [])]

    def _flatten_review_frame_paths(matches):  # type: ignore
        return []

    def _manifest_review_template(*args, **kwargs):  # type: ignore
        return {}

    def _visual_evidence_status(*args, **kwargs):  # type: ignore
        return {"visual_evidence_available": False, "reason": "visual_review_tool_unavailable_on_main"}

    def audit_visual_evidence_root(*args, **kwargs):  # type: ignore
        return {"root_present": False, "frames": {}, "reason": "visual_review_tool_unavailable_on_main"}

    def _read_visual_review_manifest(*args, **kwargs):  # type: ignore
        return {}


def _parse_dt(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _date_window_pacific(date_text: str) -> tuple[datetime, datetime]:
    # July market replay requests in this repo are normally "trading day in PT".
    # Keep this dependency-free: PDT in July is UTC-7.
    day = datetime.fromisoformat(date_text).date()
    since = datetime(day.year, day.month, day.day, 7, 0, 0, tzinfo=timezone.utc)
    until = since + timedelta(days=1)
    return since, until


def _summary_payload(payload: dict) -> dict:
    summary = payload.get("opportunity_label_summary")
    return {
        "ok": bool(payload.get("ok")),
        "read_only": bool(payload.get("read_only")),
        "since": payload.get("since"),
        "until": payload.get("until"),
        "symbols": payload.get("symbols") or [],
        "total_pnl_usd": payload.get("total_pnl_usd"),
        "total_pnl_r": payload.get("total_pnl_r"),
        "opportunity_label_summary": summary if isinstance(summary, dict) else {},
        "certification_failures": list(payload.get("certification_failures") or []),
    }


def _parse_tick_cap_sweep(values: list[str] | None) -> list[int | None]:
    if not values:
        return []
    caps: list[int | None] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lower()
        if value in {"none", "null", "uncapped", "all"}:
            key = "none"
            cap = None
        else:
            cap = int(value)
            if cap <= 0:
                raise ValueError("tick caps must be positive integers or 'none'")
            key = str(cap)
        if key in seen:
            continue
        seen.add(key)
        caps.append(cap)
    return caps


def _tick_cap_label(cap: int | None) -> str:
    return "uncapped" if cap is None else str(int(cap))


def _tick_cap_sweep_payload(runs: list[tuple[int | None, dict, float]]) -> dict:
    rows: list[dict] = []
    by_symbol: dict[str, list[dict]] = {}
    cap_runtime_seconds: list[dict] = []
    for cap, payload, runtime_seconds in runs:
        cap_label = _tick_cap_label(cap)
        cap_runtime_seconds.append(
            {
                "tick_cap": cap_label,
                "runtime_seconds": round(float(runtime_seconds), 3),
            }
        )
        summary = payload.get("opportunity_label_summary")
        summary = summary if isinstance(summary, dict) else {}
        result_rows = payload.get("results")
        result_rows = result_rows if isinstance(result_rows, list) else []
        summary_rows = {
            str(row.get("symbol") or "").upper(): row
            for row in summary.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        for row in result_rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            summary_row = summary_rows.get(symbol, {})
            first_candidate = row.get("first_candidate") if isinstance(row.get("first_candidate"), dict) else {}
            item = {
                "symbol": symbol,
                "tick_cap": cap_label,
                "runtime_seconds": round(float(runtime_seconds), 3),
                "status": summary_row.get("status"),
                "candidate_count": int(row.get("candidate_count") or 0),
                "first_candidate_ts": first_candidate.get("ts"),
                "first_candidate_reason": first_candidate.get("reason"),
                "confidence": row.get("confidence"),
                "confidence_reasons": list(row.get("confidence_reasons") or []),
            }
            rows.append(item)
            by_symbol.setdefault(symbol, []).append(item)
    stability: list[dict] = []
    for symbol, items in sorted(by_symbol.items()):
        statuses = sorted({str(item.get("status") or "") for item in items})
        candidate_counts = sorted({int(item.get("candidate_count") or 0) for item in items})
        candidate_presence = sorted({int(item.get("candidate_count") or 0) > 0 for item in items})
        first_candidate_ts = sorted({str(item.get("first_candidate_ts") or "") for item in items})
        runtimes = [float(item.get("runtime_seconds") or 0.0) for item in items]
        comparison_ready = len(items) >= 2
        sample_sensitive = comparison_ready and (len(statuses) > 1 or len(candidate_presence) > 1)
        stability.append(
            {
                "symbol": symbol,
                "tested_cap_count": len(items),
                "comparison_ready": comparison_ready,
                "status_stable": len(statuses) <= 1,
                "candidate_count_stable": len(candidate_counts) <= 1,
                "candidate_presence_stable": len(candidate_presence) <= 1,
                "statuses": statuses,
                "candidate_counts": candidate_counts,
                "first_candidate_ts_values": first_candidate_ts,
                "max_runtime_seconds": round(max(runtimes), 3) if runtimes else 0.0,
                "sample_sensitive": sample_sensitive,
            }
        )
    return {
        "ok": True,
        "read_only": True,
        "tick_cap_sweep": [_tick_cap_label(cap) for cap, _payload, _runtime_seconds in runs],
        "cap_runtime_seconds": cap_runtime_seconds,
        "rows": rows,
        "stability": stability,
        "sample_sensitive_symbols": [row["symbol"] for row in stability if row["sample_sensitive"]],
    }


def _quote_command_arg(value: str | Path) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _marker_command_with_manifest(command: object, visual_review_manifest: str | Path) -> object:
    if not isinstance(command, str) or not command.strip():
        return command
    if "--visual-review-manifest" in command:
        return command
    return f"{command} --visual-review-manifest {_quote_command_arg(visual_review_manifest)}"


def _source_certification_queue_payload(
    payload: dict,
    *,
    visual_review_manifest: str | Path = DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
) -> dict:
    summary = payload.get("opportunity_label_summary")
    summary = summary if isinstance(summary, dict) else {}
    queue: list[dict] = []
    for row in list(summary.get("source_certification_queue") or []):
        if not isinstance(row, dict):
            continue
        enriched = dict(row)
        enriched["marker_command_template"] = _marker_command_with_manifest(
            enriched.get("marker_command_template"),
            visual_review_manifest,
        )
        enriched["marker_dry_run_command_template"] = _marker_command_with_manifest(
            enriched.get("marker_dry_run_command_template"),
            visual_review_manifest,
        )
        queue.append(enriched)
    return {
        "ok": bool(payload.get("ok")),
        "read_only": bool(payload.get("read_only")),
        "since": payload.get("since"),
        "until": payload.get("until"),
        "symbols": payload.get("symbols") or [],
        "label_ready_symbol_count": summary.get("label_ready_symbol_count") or 0,
        "pnl_minmax_label_ready": bool(summary.get("pnl_minmax_label_ready")),
        "status_counts": summary.get("status_counts") or {},
        "source_certification_queue": queue,
        "certification_failures": list(payload.get("certification_failures") or []),
    }


def _joined_certification_queue_payload(
    payload: dict,
    *,
    visual_evidence_root: str | Path = DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT,
    visual_review_manifest: str | Path = DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
    visual_evidence_min_frames: int = 3,
) -> dict:
    source_payload = _source_certification_queue_payload(
        payload,
        visual_review_manifest=visual_review_manifest,
    )
    results = payload.get("results")
    results = results if isinstance(results, list) else []
    by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in results
        if isinstance(row, dict) and row.get("symbol")
    }
    visual_audit = audit_visual_evidence_root(
        Path(visual_evidence_root),
        min_frames=max(1, int(visual_evidence_min_frames)),
    )
    visual_manifest = _read_visual_review_manifest(visual_review_manifest)
    joined: list[dict] = []
    for source_row in source_payload["source_certification_queue"]:
        symbol = str(source_row.get("symbol") or "").upper()
        replay_row = by_symbol.get(symbol, {})
        source_events = replay_row.get("source_events")
        source_events = source_events if isinstance(source_events, list) else []
        visual = _visual_evidence_status(
            mentions=[
                {
                    "symbol": symbol,
                    "ts": src.get("ts"),
                    "text": src.get("text"),
                    "visual_evidence_id": (
                        (src.get("signal") or {}).get("visual_evidence_id")
                        if isinstance(src.get("signal"), dict)
                        else ""
                    ),
                }
                for src in source_events
                if isinstance(src, dict)
            ],
            ross_trades=[],
            visual_evidence_audit=visual_audit,
            visual_review_manifest=visual_manifest,
            symbol=symbol,
        )
        visual_candidate_matches = list(visual.get("candidate_evidence_matches") or [])
        visual_review_frame_paths = _flatten_review_frame_paths(visual_candidate_matches)
        next_action, next_action_reason = _joined_next_action(source_row, visual, replay_row)
        joined.append(
            {
                **source_row,
                "source_action_required": source_row.get("action_required"),
                "next_action": next_action,
                "next_action_reason": next_action_reason,
                "replay_confidence": replay_row.get("confidence"),
                "replay_confidence_reasons": list(replay_row.get("confidence_reasons") or []),
                "gate_reason_counts": dict(replay_row.get("gate_reason_counts") or {}),
                "first_candidate": replay_row.get("first_candidate"),
                "visual_status": visual.get("status"),
                "visual_reason": visual.get("reason"),
                "visual_trade_no_trade_certifiable": bool(visual.get("trade_no_trade_certifiable")),
                "visual_candidate_evidence_count": len(visual.get("candidate_evidence_matches") or []),
                "visual_reviewed_evidence_count": len(visual.get("reviewed_visual_evidence") or []),
                "visual_review_frame_paths": visual_review_frame_paths,
                "visual_review_frame_paths_absolute": _absolute_frame_paths(visual_review_frame_paths),
                "visual_manifest_review_template": _manifest_review_template(
                    symbol,
                    visual_review_frame_paths,
                    visual_candidate_matches,
                ),
                "visual_candidate_evidence_matches": visual_candidate_matches[:3],
                "visual_reviewed_evidence": list(visual.get("reviewed_visual_evidence") or [])[:3],
            }
        )
    return {
        **source_payload,
        "source_visual_joined_queue": joined,
    }


def _joined_next_action(source_row: dict, visual: dict, replay_row: dict | None = None) -> tuple[str, str]:
    status = str(source_row.get("status") or "")
    visual_status = str(visual.get("status") or "")
    replay_row = replay_row if isinstance(replay_row, dict) else {}
    replay_reasons = [str(reason) for reason in replay_row.get("confidence_reasons") or []]
    sampled_cap = any(reason.startswith("sampled_tape_max_ticks_") for reason in replay_reasons)
    if bool(visual.get("trade_no_trade_certifiable")):
        return (
            "rerun_replay_strict_labels_with_certified_visual_source",
            "visual_trade_no_trade_certifiable_true",
        )
    if visual_status == "reviewed_frame_evidence_noncertifying":
        if status == "cert_source_after_opportunity":
            return (
                "find_pre_opportunity_certifying_source_or_keep_unlabeled",
                "reviewed_frames_are_after_or_nonentry_context",
            )
        if int(source_row.get("candidate_count") or 0) <= 0:
            if sampled_cap:
                return (
                    "rerun_replay_with_higher_or_uncapped_ticks_before_gate_shape_claim",
                    "no_candidate_under_sampled_tape_cap",
                )
            return (
                "audit_entry_gate_shape_after_noncertifying_source_review",
                "visual_review_done_but_no_current_gate_candidate",
            )
        return (
            "find_different_pre_opportunity_chart_trade_source_or_keep_noncertifying",
            "reviewed_local_frames_do_not_certify_positive_entry_context",
        )
    if visual_status == "candidate_frame_artifacts_symbol_matched_not_linked":
        return (
            "review_candidate_frame_paths_and_update_manifest_if_chart_context_certifies",
            "candidate_frame_artifacts_available",
        )
    if visual_status in {"frame_artifacts_available_but_not_linked", "linked_frame_evidence_ready"}:
        return (
            "link_reviewed_chart_context_frames_to_source_if_certifying",
            "frame_artifacts_available_but_not_certifying_yet",
        )
    return (
        str(source_row.get("action_required") or "locate_or_review_source_evidence"),
        visual_status or "missing_visual_status",
    )


def _joined_queue_text(payload: dict) -> str:
    queue = payload.get("source_visual_joined_queue")
    queue = queue if isinstance(queue, list) else []
    any_sample_limited = any(bool(row.get("sample_limited")) for row in queue if isinstance(row, dict))
    lines = [
        "SYMBOL | REPLAY_STATUS | VISUAL_STATUS | SAMPLE | CANDIDATES | REVIEWED | ACTION",
    ]
    if any_sample_limited:
        lines.append(
            "NOTE | capped_replay_rows_need_higher_or_uncapped_replay_before_gate_shape_claim | "
            "n/a | sample_limited_present | 0 | 0 | do_not_use_capped_absence_as_final_no_candidate"
        )
    if not queue:
        lines.append("NONE | label_ready_or_no_source_blockers | n/a | complete | 0 | 0 | no_source_review_needed")
        return "\n".join(lines)
    for row in queue:
        sample_state = "limited" if bool(row.get("sample_limited")) else "complete"
        sampled_cap = str(row.get("sampled_tape_cap") or "").strip()
        if sampled_cap:
            sample_state = f"{sample_state}:{sampled_cap}"
        lines.append(
            " | ".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("status") or ""),
                    str(row.get("visual_status") or ""),
                    sample_state,
                    str(row.get("visual_candidate_evidence_count") or 0),
                    str(row.get("visual_reviewed_evidence_count") or 0),
                    str(row.get("next_action") or row.get("action_required") or ""),
                ]
            )
        )
        dry_run_command = str(row.get("marker_dry_run_command_template") or "").strip()
        if dry_run_command:
            lines.append(f"  PREFLIGHT: {dry_run_command}")
    return "\n".join(lines)


def _visual_certification_boundary_payload(
    symbols: list[str],
    *,
    visual_evidence_root: str | Path = DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT,
    visual_review_manifest: str | Path = DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
    visual_evidence_min_frames: int = 3,
) -> dict:
    visual_audit = audit_visual_evidence_root(
        Path(visual_evidence_root),
        min_frames=max(1, int(visual_evidence_min_frames)),
    )
    visual_manifest = _read_visual_review_manifest(visual_review_manifest)
    rows: list[dict] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").upper()
        visual = _visual_evidence_status(
            mentions=[],
            ross_trades=[],
            visual_evidence_audit=visual_audit,
            visual_review_manifest=visual_manifest,
            symbol=symbol,
        )
        rows.append(
            {
                "symbol": symbol,
                "visual_status": visual.get("status"),
                "visual_reason": visual.get("reason"),
                "trade_no_trade_certifiable": bool(visual.get("trade_no_trade_certifiable")),
                "ross_trade_outcome_certifiable": any(
                    bool(item.get("ross_trade_outcome_certifiable"))
                    for item in visual.get("reviewed_visual_evidence") or []
                    if isinstance(item, dict)
                ),
                "source_before_opportunity_certifiable": any(
                    bool(item.get("source_before_opportunity_certifiable"))
                    for item in visual.get("reviewed_visual_evidence") or []
                    if isinstance(item, dict)
                ),
                "reviewed_evidence_count": len(visual.get("reviewed_visual_evidence") or []),
                "candidate_evidence_count": len(visual.get("candidate_evidence_matches") or []),
                "reviewed_visual_evidence": list(visual.get("reviewed_visual_evidence") or [])[:5],
                "candidate_evidence_matches": list(visual.get("candidate_evidence_matches") or [])[:5],
            }
        )
    return {
        "ok": True,
        "read_only": True,
        "replay_executed": False,
        "boundary": "visual_review_only_no_pnl_claim",
        "visual_evidence_root": str(visual_evidence_root),
        "visual_review_manifest": str(visual_review_manifest),
        "evidence_ready_count": visual_audit.get("ready_count", 0),
        "evidence_not_ready_count": visual_audit.get("not_ready_count", 0),
        "total_frame_count": visual_audit.get("total_frames", 0),
        "symbols": [str(symbol or "").upper() for symbol in symbols],
        "trade_no_trade_certifying_symbol_count": sum(
            1 for row in rows if row["trade_no_trade_certifiable"]
        ),
        "certifying_symbol_count": sum(1 for row in rows if row["trade_no_trade_certifiable"]),
        "certifying_symbol_count_semantics": (
            "legacy_trade_no_trade_only_not_source_before_or_pnl_certification"
        ),
        "noncertifying_symbol_count": sum(1 for row in rows if not row["trade_no_trade_certifiable"]),
        "source_before_certifying_symbol_count": sum(
            1 for row in rows if row["source_before_opportunity_certifiable"]
        ),
        "pnl_source_certifying_symbol_count": sum(
            1 for row in rows if row["source_before_opportunity_certifiable"]
        ),
        "ross_outcome_certifying_symbol_count": sum(
            1 for row in rows if row["ross_trade_outcome_certifiable"]
        ),
        "rows": rows,
    }


def _visual_certification_boundary_text(payload: dict) -> str:
    rows = payload.get("rows")
    rows = rows if isinstance(rows, list) else []
    lines = [
        "SYMBOL | VISUAL_STATUS | REVIEWED | CANDIDATES | TRADE_NO_TRADE | SOURCE_BEFORE | OUTCOME | BOUNDARY",
    ]
    if not rows:
        lines.append("NONE | no_symbols | 0 | 0 | false | false | false | visual_review_only_no_pnl_claim")
        return "\n".join(lines)
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            " | ".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("visual_status") or ""),
                    str(row.get("reviewed_evidence_count") or 0),
                    str(row.get("candidate_evidence_count") or 0),
                    str(bool(row.get("trade_no_trade_certifiable"))).lower(),
                    str(bool(row.get("source_before_opportunity_certifiable"))).lower(),
                    str(bool(row.get("ross_trade_outcome_certifiable"))).lower(),
                    str(payload.get("boundary") or "visual_review_only_no_pnl_claim"),
                ]
            )
        )
    return "\n".join(lines)


def _certification_failures(
    payload: dict,
    *,
    require_opportunity_labels: bool = False,
    require_pnl_minmax_labels: bool = False,
) -> list[str]:
    summary = payload.get("opportunity_label_summary")
    summary = summary if isinstance(summary, dict) else {}
    failures: list[str] = []
    if require_opportunity_labels and int(summary.get("label_ready_symbol_count") or 0) <= 0:
        failures.append(
            "counterfactual_opportunity_labels_not_ready:"
            f"ready={summary.get('label_ready_symbol_count') or 0}:"
            f"symbols={summary.get('symbol_count') or 0}:"
            f"statuses={summary.get('status_counts') or {}}"
        )
    if require_pnl_minmax_labels and not bool(summary.get("pnl_minmax_label_ready")):
        failures.append(
            "counterfactual_pnl_minmax_labels_not_ready:"
            f"ready={summary.get('label_ready_symbol_count') or 0}:"
            f"symbols={summary.get('symbol_count') or 0}:"
            f"statuses={summary.get('status_counts') or {}}"
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="Pacific trading date, e.g. 2026-07-01.")
    parser.add_argument("--since", default=None, help="UTC ISO lower bound.")
    parser.add_argument("--until", default=None, help="UTC ISO upper bound.")
    parser.add_argument(
        "--eval-since",
        default=None,
        help=(
            "UTC ISO entry-evaluation lower bound. Use with an earlier --since "
            "to warm VWAP/HOD/pullback context without allowing pre-window entries."
        ),
    )
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to replay.")
    parser.add_argument("--bar-seconds", type=int, default=15, help="Microbar size for bar gates.")
    parser.add_argument("--bar-eval-stride", type=int, default=1, help="Evaluate every Nth microbar.")
    parser.add_argument("--max-ticks-per-symbol", type=int, default=None, help="Optional performance cap.")
    parser.add_argument("--max-trades-per-symbol", type=int, default=3)
    parser.add_argument(
        "--allow-pre-source-entries",
        action="store_true",
        help="Diagnostic only: allow gate candidates before local Ross/source rows.",
    )
    parser.add_argument(
        "--require-certified-source",
        action="store_true",
        help="Require certifiable Ross/admission rows before entry, not transcript-only mentions.",
    )
    parser.add_argument("--risk-usd", type=float, default=None, help="Override structural risk dollars.")
    parser.add_argument(
        "--max-notional-usd",
        type=float,
        default=None,
        help=(
            "Extra flat notional ceiling layered ABOVE the live equity-relative/liquidity "
            "caps (see --live-admission-mode). Does not disable those caps."
        ),
    )
    parser.add_argument(
        "--no-live-admission-mode",
        dest="live_admission_mode",
        action="store_false",
        help=(
            "Diagnostic/opportunity-labeling only: disable D3 live-parity admission "
            "(re-enables the tick_first_pullback / tick_vwap_reclaim_burst harness-only "
            "gate families and the market_certified synthetic-source bypass) and D4 live "
            "sizing (equity-relative + liquidity notional caps). DEFAULT is live-admission ON."
        ),
    )
    parser.set_defaults(live_admission_mode=True)
    parser.add_argument(
        "--account-equity-usd",
        type=float,
        default=None,
        help="Account equity/buying-power basis (USD) for D4 live sizing caps. Default ~$13k.",
    )
    parser.add_argument(
        "--fixed-shares",
        type=float,
        default=None,
        help="Diagnostic/Ross-comparison only: simulate a fixed share size instead of CHILI risk sizing.",
    )
    parser.add_argument(
        "--cash-usd",
        type=float,
        default=None,
        help="Diagnostic sizing: simulate cash-fraction notional sizing from this cash/buying-power basis for A/A+ candidates only.",
    )
    parser.add_argument(
        "--cash-fraction",
        type=float,
        default=None,
        help="Fraction of --cash-usd to allocate per entry; defaults to CHILI's notional fraction setting.",
    )
    parser.add_argument("--reward-risk", type=float, default=None, help="Override target R multiple.")
    parser.add_argument("--max-hold-seconds", type=float, default=None, help="Override max hold.")
    parser.add_argument(
        "--exit-model",
        choices=("adaptive", "fixed_target", "momentum_trail", "live_runner_trail"),
        default="adaptive",
        help=(
            "Replay exit model. adaptive routes A+ VWAP/reclaim bursts to runner/trail "
            "and lower-quality starter/scalp entries to target-first; fixed_target exits "
            "the whole position at target; momentum_trail/live_runner_trail arms a 1R "
            "trail at target and extends the inactivity timer on new highs."
        ),
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print compact opportunity-label/PnL summary instead of full replay JSON.",
    )
    parser.add_argument(
        "--source-certification-queue-only",
        action="store_true",
        help="Print only the source/frame review queue needed for counterfactual opportunity labels.",
    )
    parser.add_argument(
        "--joined-certification-queue-only",
        action="store_true",
        help="Print source-certification queue joined with local visual/frame evidence status.",
    )
    parser.add_argument(
        "--joined-certification-queue-text",
        action="store_true",
        help="Print the joined certification queue as compact text for frame-review work.",
    )
    parser.add_argument(
        "--visual-certification-boundary-only",
        action="store_true",
        help=(
            "Fast visual/frame certification boundary only. Does not run tick replay "
            "and therefore cannot certify PnL/min-max labels."
        ),
    )
    parser.add_argument(
        "--visual-certification-boundary-text",
        action="store_true",
        help="Print the fast visual/frame certification boundary as compact text.",
    )
    parser.add_argument(
        "--tick-cap-sweep",
        nargs="+",
        default=None,
        metavar="CAP",
        help="Diagnostic: replay each requested symbol at multiple max-tick caps; use 'none' for uncapped.",
    )
    parser.add_argument("--visual-evidence-root", default=str(DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT))
    parser.add_argument("--visual-review-manifest", default=DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH)
    parser.add_argument("--visual-evidence-min-frames", type=int, default=3)
    parser.add_argument(
        "--require-opportunity-labels",
        action="store_true",
        help="Exit nonzero unless at least one symbol has a counterfactual opportunity label.",
    )
    parser.add_argument(
        "--require-pnl-minmax-labels",
        action="store_true",
        help="Exit nonzero unless every requested symbol is counterfactual opportunity-label ready.",
    )
    args = parser.parse_args(argv)

    if args.visual_certification_boundary_only or args.visual_certification_boundary_text:
        out = _visual_certification_boundary_payload(
            args.symbols,
            visual_evidence_root=args.visual_evidence_root,
            visual_review_manifest=args.visual_review_manifest,
            visual_evidence_min_frames=args.visual_evidence_min_frames,
        )
        if args.visual_certification_boundary_text:
            print(_visual_certification_boundary_text(out))
        else:
            print(json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0

    if args.date:
        since, until = _date_window_pacific(args.date)
    else:
        if not args.since or not args.until:
            parser.error("provide --date or both --since and --until")
        since = _parse_dt(args.since)
        until = _parse_dt(args.until)
    eval_since = _parse_dt(args.eval_since) if args.eval_since else None

    db = SessionLocal()
    try:
        if args.tick_cap_sweep:
            try:
                caps = _parse_tick_cap_sweep(args.tick_cap_sweep)
            except ValueError as exc:
                parser.error(str(exc))
            runs: list[tuple[int | None, dict, float]] = []
            for cap in caps:
                started = perf_counter()
                result = run_counterfactual_replay(
                    db,
                    symbols=args.symbols,
                    since=since,
                    until=until,
                    bar_seconds=args.bar_seconds,
                    bar_eval_stride=args.bar_eval_stride,
                    max_ticks=cap,
                    max_trades_per_symbol=args.max_trades_per_symbol,
                    require_source_before_entry=not args.allow_pre_source_entries,
                    require_certifiable_source=args.require_certified_source,
                    risk_usd=args.risk_usd,
                    max_notional_usd=args.max_notional_usd,
                    fixed_qty=args.fixed_shares,
                    reward_risk=args.reward_risk,
                    max_hold_seconds=args.max_hold_seconds,
                    live_admission_mode=args.live_admission_mode,
                    account_equity_usd=args.account_equity_usd,
                )
                runs.append((cap, result_to_dict(result), perf_counter() - started))
            out = _tick_cap_sweep_payload(runs)
            print(json.dumps(out, indent=2, sort_keys=True, default=str))
            return 0
        result = run_counterfactual_replay(
            db,
            symbols=args.symbols,
            since=since,
            until=until,
            eval_since=eval_since,
            bar_seconds=args.bar_seconds,
            bar_eval_stride=args.bar_eval_stride,
            max_ticks=args.max_ticks_per_symbol,
            max_trades_per_symbol=args.max_trades_per_symbol,
            require_source_before_entry=not args.allow_pre_source_entries,
            require_certifiable_source=args.require_certified_source,
            risk_usd=args.risk_usd,
            max_notional_usd=args.max_notional_usd,
            fixed_qty=args.fixed_shares,
            cash_usd=args.cash_usd,
            cash_fraction=args.cash_fraction,
            reward_risk=args.reward_risk,
            max_hold_seconds=args.max_hold_seconds,
            exit_model=args.exit_model,
            live_admission_mode=args.live_admission_mode,
            account_equity_usd=args.account_equity_usd,
        )
        payload = result_to_dict(result)
        failures = _certification_failures(
            payload,
            require_opportunity_labels=args.require_opportunity_labels,
            require_pnl_minmax_labels=args.require_pnl_minmax_labels,
        )
        if failures:
            payload = dict(payload)
            payload["ok"] = False
            payload["certification_failures"] = failures
        if args.joined_certification_queue_text:
            out_payload = _joined_certification_queue_payload(
                payload,
                visual_evidence_root=args.visual_evidence_root,
                visual_review_manifest=args.visual_review_manifest,
                visual_evidence_min_frames=args.visual_evidence_min_frames,
            )
            print(_joined_queue_text(out_payload))
            return 0 if payload.get("ok") else 2
        if args.joined_certification_queue_only:
            out = _joined_certification_queue_payload(
                payload,
                visual_evidence_root=args.visual_evidence_root,
                visual_review_manifest=args.visual_review_manifest,
                visual_evidence_min_frames=args.visual_evidence_min_frames,
            )
        elif args.source_certification_queue_only:
            out = _source_certification_queue_payload(
                payload,
                visual_review_manifest=args.visual_review_manifest,
            )
        elif args.summary_only:
            out = _summary_payload(payload)
        else:
            out = payload
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0 if payload.get("ok") else 2
    finally:
        try:
            db.rollback()
        finally:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
