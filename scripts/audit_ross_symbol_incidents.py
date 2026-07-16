from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models.trading import TradingAutomationEvent, TradingAutomationSession
from app.services.trading.momentum_neural.ross_transcript_bridge import (
    DEFAULT_TRANSCRIPT_PATH,
    recent_transcript_mentions,
)
from scripts.audit_ross_visual_evidence import (
    DEFAULT_EVIDENCE_ROOT as DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT,
    audit_visual_evidence_root,
)

DEFAULT_ROSS_TRADE_EVENTS_PATH = r"D:\CHILI-Docker\chili-data\ross_stream\ross_trade_events.jsonl"
DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH = "project_ws/AgentOps/ross_video_evidence/review_manifest.json"
MAX_VISUAL_EVIDENCE_MATCHES_PER_SYMBOL = 5
VISUAL_EVIDENCE_REVIEW_FRAME_RADIUS = 2

ENTRY_EVENTS = {"entry_fill", "live_entry_filled", "live_entry_submitted", "live_entry_submit_ok"}
EXIT_EVENTS = {"exit_fill", "live_exit_filled", "live_exit_submit_ok"}
ADMISSION_EVENTS = {"ross_event_admitted"}
WATCH_EVENTS = {
    "live_entry_wait",
    "live_entry_wait_late_window",
    "live_entry_blocked",
    "entry_candidate",
    "live_entry_candidate",
    "setup_trace",
}


def _sym(value: Any) -> str:
    return str(value or "").strip().upper()


def _jsonable_dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if value is None:
        return None
    return str(value)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _min_dt(rows: Iterable[dict[str, Any]], key: str = "ts") -> datetime | None:
    vals = [_parse_dt(row.get(key)) for row in rows]
    vals = [val for val in vals if val is not None]
    return min(vals) if vals else None


def _latency_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _ross_entry_speed_class(ross_to_entry_latency_s: float | None) -> str:
    if ross_to_entry_latency_s is None:
        return "unknown"
    try:
        latency = float(ross_to_entry_latency_s)
    except (TypeError, ValueError):
        return "unknown"
    if latency < 0:
        return "clock_mismatch"
    if latency <= 10.0:
        return "ross_scalp_window"
    if latency <= 30.0:
        return "late_for_scalp"
    return "too_late_for_ross_scalp"


def _read_ross_trade_events(path: str | Path, *, since_minutes: float, max_lines: int = 2000) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0.0, float(since_minutes)))
    rows: list[dict[str, Any]] = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-max_lines:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        sym = _sym(row.get("symbol") or row.get("ticker"))
        ts = _parse_dt(row.get("ts") or row.get("time") or row.get("at"))
        if not sym or ts is None or ts < cutoff:
            continue
        rows.append(
            {
                "symbol": sym,
                "ts": ts,
                "action": str(row.get("action") or row.get("side") or "").strip(),
                "price": row.get("price"),
                "note": str(row.get("note") or row.get("text") or "")[:300],
                "visual_evidence_id": str(
                    row.get("visual_evidence_id")
                    or row.get("evidence_id")
                    or row.get("video_id")
                    or row.get("source_video_id")
                    or ""
                ).strip(),
            }
        )
    rows.sort(key=lambda row: (row["symbol"], row["ts"]))
    return rows


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or row.get("payload_json")
    return payload if isinstance(payload, dict) else {}


def _event_symbol(row: dict[str, Any]) -> str:
    payload = _payload(row)
    return _sym(row.get("symbol") or payload.get("symbol") or payload.get("ticker"))


def _reason_from_event(row: dict[str, Any]) -> str | None:
    payload = _payload(row)
    for key in (
        "reason",
        "skipped",
        "wait_reason",
        "entry_wait_reason",
        "blocked_reason",
        "entry_trigger_reason",
        "ross_universe_reason",
        "ross_evidence_reason",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _session_live_execution(row: dict[str, Any]) -> dict[str, Any]:
    snap = row.get("risk_snapshot") or row.get("risk_snapshot_json")
    if not isinstance(snap, dict):
        return {}
    le = snap.get("momentum_live_execution")
    return le if isinstance(le, dict) else {}


def _session_has_entry_evidence(row: dict[str, Any]) -> bool:
    le = _session_live_execution(row)
    return bool(
        le.get("entry_submitted")
        or le.get("entry_order_id")
        or le.get("entry_order_ids_all")
        or le.get("position")
        or le.get("last_exit_entry_price") is not None
        or le.get("realized_pnl_usd") is not None
    )


def _session_has_exit_evidence(row: dict[str, Any]) -> bool:
    le = _session_live_execution(row)
    return bool(
        le.get("exit_order_id")
        or le.get("exit_execution_intents")
        or le.get("last_exit_price") is not None
        or le.get("realized_pnl_usd") is not None
    )


def _session_reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    le = _session_live_execution(row)
    out: list[dict[str, Any]] = []
    for key in ("last_exit_reason", "entry_trigger_reason", "entry_source_wait_reason"):
        value = le.get(key)
        if value not in (None, ""):
            out.append(
                {
                    "ts": row.get("updated_at"),
                    "event_type": "session_snapshot",
                    "reason": str(value),
                    "session_id": row.get("id"),
                }
            )
    return out


COMPACT_LIVE_EXECUTION_KEYS = (
    "entry_submitted",
    "entry_order_id",
    "entry_order_ids_all",
    "entry_trigger_reason",
    "entry_source_wait_reason",
    "entry_micro_frame",
    "entry_pre_submit_internal_latency_s",
    "entry_pre_submit_internal_latency_max_s",
    "last_wait_reason",
    "last_quote_quality_gate",
    "last_exit_reason",
    "last_exit_price",
    "last_exit_quantity",
    "realized_pnl_usd",
    "tick_count",
)


def _compact_session(row: dict[str, Any], *, include_risk_snapshot: bool = False) -> dict[str, Any]:
    compact = {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "state": row.get("state"),
        "mode": row.get("mode"),
        "execution_family": row.get("execution_family"),
        "created_at": _jsonable_dt(row.get("created_at")),
        "updated_at": _jsonable_dt(row.get("updated_at")),
    }
    live_execution = {
        key: value
        for key in COMPACT_LIVE_EXECUTION_KEYS
        if (value := _session_live_execution(row).get(key)) is not None
    }
    if live_execution:
        compact["live_execution"] = live_execution
    if include_risk_snapshot:
        compact["risk_snapshot"] = row.get("risk_snapshot") or row.get("risk_snapshot_json") or {}
    return compact


def _ross_vs_chili_verdict(
    *,
    classification: str,
    entry_count: int,
    exit_count: int,
    ross_entry_speed_class: str,
    admission_count: int,
    session_count: int,
    mention_count: int,
    latest_reasons: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    if entry_count > 0:
        if ross_entry_speed_class == "too_late_for_ross_scalp":
            return (
                "chili_entered_too_late_for_ross_scalp",
                "CHILI entered, but after the Ross scalp window; treat this as a late or different setup.",
            )
        if ross_entry_speed_class == "late_for_scalp":
            return (
                "chili_entered_late_for_ross_scalp",
                "CHILI entered, but late for a Ross scalp; review whether the entry matched the same play.",
            )
        if exit_count > 0:
            return "chili_entered_and_exited", "CHILI entered and has exit evidence."
        return "chili_entered_open_or_unresolved", "CHILI entered; exit evidence is not present in this window."
    if classification == "admitted_watched_or_blocked":
        reasons = [str(row.get("reason") or "") for row in latest_reasons if row.get("reason")]
        reason_text = ", ".join(reasons[:3]) if reasons else "no reason captured"
        return "chili_saw_but_did_not_enter", f"CHILI saw the Ross-lane symbol but did not enter; latest reasons: {reason_text}."
    if classification == "admitted_ticked":
        return "chili_admitted_and_ticked_no_entry", "CHILI admitted and ticked the symbol but no entry evidence is present."
    if classification == "admitted_not_ticked":
        return "chili_admitted_without_tick", "CHILI admitted the symbol but did not tick it in this window."
    if classification == "ross_mentioned_no_chili_session":
        return "ross_mentioned_chili_missed", "Ross transcript mentioned the symbol, but CHILI has no session/admission evidence."
    if session_count > 0:
        return "chili_session_without_ross_admission", "CHILI has a session, but no Ross admission evidence was found."
    if mention_count > 0:
        return "ross_mentioned_chili_missed", "Ross transcript mentioned the symbol, but CHILI has no evidence."
    return "no_ross_or_chili_evidence", "No Ross mention or CHILI evidence found in this window."


def _source_visual_evidence_ids(rows: Iterable[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        value = (
            row.get("visual_evidence_id")
            or row.get("evidence_id")
            or row.get("video_id")
            or row.get("source_video_id")
        )
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _visual_evidence_status(
    *,
    mentions: Sequence[dict[str, Any]],
    ross_trades: Sequence[dict[str, Any]],
    visual_evidence_audit: dict[str, Any] | None,
    visual_review_manifest: dict[str, Any] | None = None,
    symbol: str = "",
) -> dict[str, Any]:
    source_rows = list(mentions) + list(ross_trades)
    linked_ids = _source_visual_evidence_ids(source_rows)
    reviewed_rows = _reviewed_visual_evidence_for_symbol(symbol, visual_review_manifest)
    rows = (
        visual_evidence_audit.get("rows", [])
        if isinstance(visual_evidence_audit, dict)
        else []
    )
    by_id = {
        str(row.get("evidence_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("evidence_id")
    }
    ready_count = int(visual_evidence_audit.get("ready_count") or 0) if isinstance(visual_evidence_audit, dict) else 0
    total_frames = int(visual_evidence_audit.get("total_frames") or 0) if isinstance(visual_evidence_audit, dict) else 0
    linked_rows = [by_id.get(eid) for eid in linked_ids]
    missing_linked = [eid for eid, row in zip(linked_ids, linked_rows) if not row or not row.get("ready")]
    candidate_matches = _candidate_visual_evidence_matches(symbol, rows) if symbol else []

    if linked_ids and not missing_linked:
        status = "linked_frame_evidence_ready"
        certifiable = True
        reason = "Ross source rows link to ready extracted video/chart frames."
    elif linked_ids:
        status = "linked_frame_evidence_missing_or_not_ready"
        certifiable = False
        reason = "Ross source rows reference frame evidence that is absent or not ready."
    elif reviewed_rows:
        certifying_reviews = [
            row for row in reviewed_rows if bool(row.get("trade_no_trade_certifiable"))
        ]
        if certifying_reviews:
            status = "reviewed_frame_evidence_trade_certified"
            certifiable = True
            reason = "Reviewed frame manifest marks this symbol's evidence as trade/no-trade certifying."
        else:
            status = "reviewed_frame_evidence_noncertifying"
            certifiable = False
            reason = "Reviewed frame manifest exists, but it does not certify chart/trade context."
    elif source_rows and ready_count > 0:
        status = (
            "candidate_frame_artifacts_symbol_matched_not_linked"
            if candidate_matches
            else "frame_artifacts_available_but_not_linked"
        )
        certifiable = False
        reason = (
            "Candidate frame evidence mentions the symbol, but source rows are not linked to reviewed frames."
            if candidate_matches
            else "Ross source rows are transcript/trade-index evidence only until linked to reviewed frames."
        )
    elif source_rows:
        status = "transcript_or_trade_index_only_no_frame_artifacts"
        certifiable = False
        reason = "Ross source rows exist, but no ready frame evidence artifacts were audited."
    else:
        status = "no_ross_source_evidence"
        certifiable = False
        reason = "No Ross source row was available for this symbol in the audit window."

    return {
        "status": status,
        "trade_no_trade_certifiable": certifiable,
        "reason": reason,
        "linked_evidence_ids": linked_ids,
        "missing_or_not_ready_evidence_ids": missing_linked,
        "candidate_evidence_matches": candidate_matches,
        "reviewed_visual_evidence": reviewed_rows,
        "ready_evidence_count": ready_count,
        "total_frame_count": total_frames,
    }


def _reviewed_visual_evidence_for_symbol(
    symbol: str,
    manifest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    sym = _sym(symbol)
    if not sym or not isinstance(manifest, dict):
        return []
    rows = manifest.get("reviews")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or _sym(row.get("symbol")) != sym:
            continue
        reviewed: dict[str, Any] = {
            "symbol": sym,
            "evidence_id": str(row.get("evidence_id") or ""),
            "evidence_type": str(row.get("evidence_type") or ""),
            "trade_no_trade_certifiable": bool(row.get("trade_no_trade_certifiable")),
            "reviewed_frame_paths": [
                str(p) for p in row.get("reviewed_frame_paths", []) if str(p or "")
            ],
            "observation": str(row.get("observation") or "")[:500],
            "review_doc": str(row.get("review_doc") or ""),
        }
        for key in (
            "ross_trade_outcome_certifiable",
            "source_before_opportunity_certifiable",
        ):
            if key in row:
                reviewed[key] = bool(row.get(key))
        out.append(reviewed)
    return out


def _read_visual_review_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_VISUAL_SYMBOL_TEXT_ALIASES: dict[str, tuple[str, ...]] = {
    "CANF": ("CANF", "C-A-N-F", "C A N F", "CANE"),
    "JEM": ("JEM", "GEM"),
    "LHAI": ("LHAI", "LH AI", "LH-AI", "LH.AI"),
}

_VISUAL_SYMBOL_REGEX_ALIASES: dict[str, tuple[str, ...]] = {
    "CANF": (
        r"(?<![A-Z0-9])CF(?![A-Z0-9]).{0,120}(RUNNING UP SCANNER|24 MILLION SHARES|2 MILLION SHARE)",
        r"(RUNNING UP SCANNER|24 MILLION SHARES|2 MILLION SHARE).{0,120}(?<![A-Z0-9])CF(?![A-Z0-9])",
    ),
}


def _symbol_text_patterns(symbol: str) -> list[re.Pattern[str]]:
    sym = _sym(symbol)
    if not sym:
        return []
    aliases = _VISUAL_SYMBOL_TEXT_ALIASES.get(sym, (sym,))
    patterns: list[re.Pattern[str]] = []
    seen: set[str] = set()
    for alias in aliases:
        alias_s = str(alias or "").strip().upper()
        if not alias_s or alias_s in seen:
            continue
        seen.add(alias_s)
        patterns.append(re.compile(rf"(?<![A-Z0-9]){re.escape(alias_s)}(?![A-Z0-9])"))
    return patterns


def _symbol_context_patterns(symbol: str) -> list[re.Pattern[str]]:
    sym = _sym(symbol)
    return [re.compile(pattern) for pattern in _VISUAL_SYMBOL_REGEX_ALIASES.get(sym, ()) if str(pattern or "").strip()]


def _line_has_context_alias(symbol: str, line_upper: str, context_upper: str) -> bool:
    sym = _sym(symbol)
    if sym == "CANF" and not re.search(r"(?<![A-Z0-9])CF(?![A-Z0-9])", line_upper):
        return False
    return any(pattern.search(context_upper) for pattern in _symbol_context_patterns(sym))


def _transcript_line_offset_seconds(line: str) -> float | None:
    m = re.match(r"\[[^|\]]+\|([0-9]+(?:\.[0-9]+)?)\]", str(line or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _candidate_visual_evidence_matches(symbol: str, rows: Sequence[Any]) -> list[dict[str, Any]]:
    sym = _sym(symbol)
    if not sym:
        return []
    patterns = _symbol_text_patterns(sym)
    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("ready"):
            continue
        evidence_id = str(row.get("evidence_id") or "")
        path = Path(str(row.get("path") or ""))
        transcript = path / "transcript_ts.txt"
        if not evidence_id or not transcript.exists():
            continue
        try:
            lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        snippets: list[dict[str, Any]] = []
        for idx, line in enumerate(lines):
            line_upper = str(line).upper()
            context_upper = " ".join(str(part).upper() for part in lines[idx : idx + 3])
            simple_match = any(pattern.search(line_upper) for pattern in patterns)
            context_match = _line_has_context_alias(
                sym,
                line_upper,
                context_upper,
            )
            if not simple_match and not context_match:
                continue
            sec = _transcript_line_offset_seconds(line)
            frame_paths = _nearest_review_frame_paths(
                path,
                offset_seconds=sec,
                radius=VISUAL_EVIDENCE_REVIEW_FRAME_RADIUS,
            )
            if context_match:
                seen_paths = set(frame_paths)
                for context_line in lines[idx + 1 : idx + 3]:
                    for frame_path in _nearest_review_frame_paths(
                        path,
                        offset_seconds=_transcript_line_offset_seconds(context_line),
                        radius=VISUAL_EVIDENCE_REVIEW_FRAME_RADIUS,
                    ):
                        if frame_path not in seen_paths:
                            seen_paths.add(frame_path)
                            frame_paths.append(frame_path)
            snippets.append(
                {
                    "offset_seconds": sec,
                    "text": line[:240],
                    "review_frame_paths": frame_paths,
                }
            )
            if len(snippets) >= MAX_VISUAL_EVIDENCE_MATCHES_PER_SYMBOL:
                break
        if snippets:
            matches.append(
                {
                    "evidence_id": evidence_id,
                    "frame_count": row.get("frame_count"),
                    "snippets": snippets,
                }
            )
    return matches


def _nearest_review_frame_paths(
    evidence_path: Path,
    *,
    offset_seconds: float | None,
    radius: int = VISUAL_EVIDENCE_REVIEW_FRAME_RADIUS,
) -> list[str]:
    if offset_seconds is None:
        return []
    frames_dir = evidence_path / "frames"
    if not frames_dir.exists() or not frames_dir.is_dir():
        return []
    try:
        center = int(round(float(offset_seconds)))
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for idx in range(max(0, center - max(0, radius)), center + max(0, radius) + 1):
        frame = frames_dir / f"f{idx:04d}.jpg"
        if frame.exists() and frame.is_file():
            out.append(str(frame))
    return out


def summarize_symbol_incident(
    symbol: str,
    *,
    sessions: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    transcript_mentions: Sequence[dict[str, Any]] = (),
    ross_trade_events: Sequence[dict[str, Any]] = (),
    visual_evidence_audit: dict[str, Any] | None = None,
    visual_review_manifest: dict[str, Any] | None = None,
    include_risk_snapshot: bool = False,
) -> dict[str, Any]:
    sym = _sym(symbol)
    symbol_sessions = [row for row in sessions if _sym(row.get("symbol")) == sym]
    session_ids = {int(row["id"]) for row in symbol_sessions if row.get("id") is not None}
    symbol_events = [
        row
        for row in events
        if (row.get("session_id") in session_ids) or _event_symbol(row) == sym
    ]
    symbol_events.sort(key=lambda row: str(row.get("ts") or ""))
    event_types = Counter(str(row.get("event_type") or "") for row in symbol_events)
    event_types.pop("", None)

    admissions = [row for row in symbol_events if str(row.get("event_type")) in ADMISSION_EVENTS]
    entries = [row for row in symbol_events if str(row.get("event_type")) in ENTRY_EVENTS]
    exits = [row for row in symbol_events if str(row.get("event_type")) in EXIT_EVENTS]
    session_entry_count = sum(1 for row in symbol_sessions if _session_has_entry_evidence(row))
    session_exit_count = sum(1 for row in symbol_sessions if _session_has_exit_evidence(row))
    waits_or_blocks = [
        row
        for row in symbol_events
        if str(row.get("event_type")) in WATCH_EVENTS or (_reason_from_event(row) or "").startswith("waiting_for_")
    ]
    latest_reasons: list[dict[str, Any]] = []
    seen_reason: set[str] = set()
    for row in reversed(symbol_events):
        reason = _reason_from_event(row)
        if not reason or reason in seen_reason:
            continue
        seen_reason.add(reason)
        latest_reasons.append(
            {
                "ts": _jsonable_dt(row.get("ts")),
                "event_type": row.get("event_type"),
                "reason": reason,
                "session_id": row.get("session_id"),
            }
        )
        if len(latest_reasons) >= 8:
            break
    for row in reversed(symbol_sessions):
        for reason_row in _session_reasons(row):
            reason = str(reason_row.get("reason") or "")
            if not reason or reason in seen_reason:
                continue
            seen_reason.add(reason)
            latest_reasons.append(reason_row)
            if len(latest_reasons) >= 8:
                break
        if len(latest_reasons) >= 8:
            break

    effective_entry_count = max(len(entries), session_entry_count)
    effective_exit_count = max(len(exits), session_exit_count)
    if effective_entry_count:
        classification = "entered"
    elif admissions and waits_or_blocks:
        classification = "admitted_watched_or_blocked"
    elif admissions:
        ticked = 0
        for row in admissions:
            try:
                ticked = max(ticked, int(_payload(row).get("ticked") or 0))
            except (TypeError, ValueError):
                pass
        classification = "admitted_ticked" if ticked > 0 else "admitted_not_ticked"
    elif symbol_sessions:
        classification = "session_without_ross_admission"
    elif transcript_mentions:
        classification = "ross_mentioned_no_chili_session"
    else:
        classification = "no_evidence"

    mentions = [
        {
            "ts": _jsonable_dt(row.get("ts")),
            "text": str(row.get("text") or "")[:300],
            "visual_evidence_id": str(
                row.get("visual_evidence_id")
                or row.get("evidence_id")
                or row.get("video_id")
                or row.get("source_video_id")
                or ""
            ).strip(),
        }
        for row in transcript_mentions
        if _sym(row.get("symbol")) == sym
    ]
    ross_trades = [
        {
            "ts": _jsonable_dt(row.get("ts")),
            "action": str(row.get("action") or ""),
            "price": row.get("price"),
            "note": str(row.get("note") or "")[:300],
            "visual_evidence_id": str(
                row.get("visual_evidence_id")
                or row.get("evidence_id")
                or row.get("video_id")
                or row.get("source_video_id")
                or ""
            ).strip(),
        }
        for row in ross_trade_events
        if _sym(row.get("symbol")) == sym
    ]
    first_ross_mention_ts = _min_dt(mentions)
    first_ross_trade_ts = _min_dt(ross_trades)
    ross_reference_ts = first_ross_trade_ts or first_ross_mention_ts
    first_chili_admission_ts = _min_dt(admissions)
    first_chili_entry_ts = _min_dt(entries)
    first_chili_exit_ts = _min_dt(exits)
    timing = {
        "first_ross_mention_ts": _jsonable_dt(first_ross_mention_ts),
        "first_ross_trade_ts": _jsonable_dt(first_ross_trade_ts),
        "ross_reference": "trade" if first_ross_trade_ts is not None else "mention" if first_ross_mention_ts is not None else None,
        "first_chili_admission_ts": _jsonable_dt(first_chili_admission_ts),
        "first_chili_entry_ts": _jsonable_dt(first_chili_entry_ts),
        "first_chili_exit_ts": _jsonable_dt(first_chili_exit_ts),
        "ross_to_admission_latency_s": _latency_seconds(first_ross_mention_ts, first_chili_admission_ts),
        "ross_to_entry_latency_s": _latency_seconds(first_ross_mention_ts, first_chili_entry_ts),
        "ross_trade_to_admission_latency_s": _latency_seconds(first_ross_trade_ts, first_chili_admission_ts),
        "ross_trade_to_entry_latency_s": _latency_seconds(first_ross_trade_ts, first_chili_entry_ts),
        "ross_reference_to_admission_latency_s": _latency_seconds(ross_reference_ts, first_chili_admission_ts),
        "ross_reference_to_entry_latency_s": _latency_seconds(ross_reference_ts, first_chili_entry_ts),
        "admission_to_entry_latency_s": _latency_seconds(first_chili_admission_ts, first_chili_entry_ts),
        "entry_to_exit_latency_s": _latency_seconds(first_chili_entry_ts, first_chili_exit_ts),
    }
    timing["ross_entry_speed_class"] = _ross_entry_speed_class(timing["ross_reference_to_entry_latency_s"])
    verdict, operator_summary = _ross_vs_chili_verdict(
        classification=classification,
        entry_count=effective_entry_count,
        exit_count=effective_exit_count,
        ross_entry_speed_class=str(timing["ross_entry_speed_class"]),
        admission_count=len(admissions),
        session_count=len(symbol_sessions),
        mention_count=len(mentions),
        latest_reasons=latest_reasons,
    )

    return {
        "symbol": sym,
        "classification": classification,
        "ross_vs_chili_verdict": verdict,
        "operator_summary": operator_summary,
        "timing": timing,
        "session_count": len(symbol_sessions),
        "session_ids": sorted(session_ids),
        "states": dict(sorted(Counter(str(row.get("state") or "") for row in symbol_sessions).items())),
        "ross_mentions": mentions,
        "ross_trades": ross_trades,
        "visual_evidence": _visual_evidence_status(
            symbol=sym,
            mentions=mentions,
            ross_trades=ross_trades,
            visual_evidence_audit=visual_evidence_audit,
            visual_review_manifest=visual_review_manifest,
        ),
        "admission_count": len(admissions),
        "entry_count": effective_entry_count,
        "exit_count": effective_exit_count,
        "entry_event_count": len(entries),
        "exit_event_count": len(exits),
        "entry_session_evidence_count": session_entry_count,
        "exit_session_evidence_count": session_exit_count,
        "event_type_counts": dict(sorted(event_types.items())),
        "latest_reasons": latest_reasons,
        "latest_session": (
            _compact_session(symbol_sessions[-1], include_risk_snapshot=include_risk_snapshot)
            if symbol_sessions
            else None
        ),
    }


def summarize_visual_evidence_status(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    certifiable_symbols: list[str] = []
    uncertified_symbols: list[str] = []
    candidate_symbols: list[str] = []
    reviewed_noncertifying_symbols: list[str] = []
    no_source_symbols: list[str] = []

    for row in rows:
        symbol = _sym(row.get("symbol"))
        visual = row.get("visual_evidence") if isinstance(row.get("visual_evidence"), dict) else {}
        status = str(visual.get("status") or "missing_visual_evidence_status")
        status_counts[status] += 1
        if bool(visual.get("trade_no_trade_certifiable")):
            certifiable_symbols.append(symbol)
        else:
            uncertified_symbols.append(symbol)
        if status == "candidate_frame_artifacts_symbol_matched_not_linked":
            candidate_symbols.append(symbol)
        if status == "reviewed_frame_evidence_noncertifying":
            reviewed_noncertifying_symbols.append(symbol)
        if status == "no_ross_source_evidence":
            no_source_symbols.append(symbol)

    return {
        "symbol_count": len(rows),
        "certifiable_count": len(certifiable_symbols),
        "uncertified_count": len(uncertified_symbols),
        "status_counts": dict(sorted(status_counts.items())),
        "certifiable_symbols": certifiable_symbols,
        "uncertified_symbols": uncertified_symbols,
        "candidate_symbols": candidate_symbols,
        "reviewed_noncertifying_symbols": reviewed_noncertifying_symbols,
        "no_source_symbols": no_source_symbols,
    }


def visual_certification_failures(rows: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for row in rows:
        symbol = _sym(row.get("symbol"))
        visual = row.get("visual_evidence") if isinstance(row.get("visual_evidence"), dict) else {}
        if bool(visual.get("trade_no_trade_certifiable")):
            continue
        failures.append(
            {
                "symbol": symbol,
                "status": str(visual.get("status") or "missing_visual_evidence_status"),
                "reason": str(visual.get("reason") or "missing_certifying_frame_evidence"),
            }
        )
    return failures


def _flatten_review_frame_paths(candidate_matches: Sequence[Any], *, limit: int = 12) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in candidate_matches:
        if not isinstance(match, dict):
            continue
        snippets = match.get("snippets")
        snippets = snippets if isinstance(snippets, list) else []
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            frame_paths = snippet.get("review_frame_paths")
            frame_paths = frame_paths if isinstance(frame_paths, list) else []
            for raw_path in frame_paths:
                path_s = str(raw_path or "").strip()
                if not path_s or path_s in seen:
                    continue
                seen.add(path_s)
                paths.append(path_s)
                if len(paths) >= max(1, int(limit)):
                    return paths
    return paths


def _absolute_frame_paths(frame_paths: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw_path in frame_paths:
        path_s = str(raw_path or "").strip()
        if not path_s:
            continue
        path = Path(path_s)
        if not path.is_absolute():
            path = ROOT / path
        out.append(str(path))
    return out


def _manifest_review_template(symbol: str, frame_paths: Sequence[str], candidate_matches: Sequence[Any]) -> dict[str, Any]:
    evidence_id = "EVIDENCE_ID"
    for match in candidate_matches:
        if isinstance(match, dict) and str(match.get("evidence_id") or "").strip():
            evidence_id = str(match.get("evidence_id")).strip()
            break
    return {
        "symbol": symbol,
        "evidence_id": evidence_id,
        "evidence_type": "chart_trade_context",
        "trade_no_trade_certifiable": False,
        "ross_trade_outcome_certifiable": False,
        "source_before_opportunity_certifiable": False,
        "reviewed_frame_paths": list(frame_paths),
        "observation": "FILL_AFTER_REVIEWING_CHART_VWAP_HOD_PULLBACK_CANDLES_TAPE_L2_CONTEXT",
        "review_doc": "docs/STRATEGY/CC_REPORTS/FILL_REVIEW_DOC.md",
    }


def visual_review_queue(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return operator-facing frame review work for uncertified symbols."""

    queue: list[dict[str, Any]] = []
    for row in rows:
        symbol = _sym(row.get("symbol"))
        visual = row.get("visual_evidence") if isinstance(row.get("visual_evidence"), dict) else {}
        if bool(visual.get("trade_no_trade_certifiable")):
            continue
        candidate_matches = visual.get("candidate_evidence_matches")
        candidate_matches = candidate_matches if isinstance(candidate_matches, list) else []
        reviewed_rows = visual.get("reviewed_visual_evidence")
        reviewed_rows = reviewed_rows if isinstance(reviewed_rows, list) else []
        status = str(visual.get("status") or "missing_visual_evidence_status")
        if status == "reviewed_frame_evidence_noncertifying":
            action = "find_chart_trade_context_frames_or_keep_noncertifying"
        elif candidate_matches:
            action = "review_candidate_frame_paths_and_update_manifest_if_chart_context_certifies"
        elif status == "no_ross_source_evidence":
            action = "locate_or_mark_ross_source_before_visual_certification"
        else:
            action = "link_reviewed_chart_context_frames_to_ross_source"
        review_frame_paths = _flatten_review_frame_paths(candidate_matches)
        queue.append(
            {
                "symbol": symbol,
                "status": status,
                "reason": str(visual.get("reason") or "missing_certifying_frame_evidence"),
                "action_required": action,
                "candidate_evidence_count": len(candidate_matches),
                "reviewed_evidence_count": len(reviewed_rows),
                "review_frame_paths": review_frame_paths,
                "review_frame_paths_absolute": _absolute_frame_paths(review_frame_paths),
                "manifest_review_template": _manifest_review_template(
                    symbol,
                    review_frame_paths,
                    candidate_matches,
                ),
                "candidate_evidence_matches": candidate_matches[:3],
                "reviewed_visual_evidence": reviewed_rows[:3],
            }
        )
    return queue


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def filter_sessions_for_incident_window(
    sessions: Sequence[dict[str, Any]],
    *,
    cutoff: datetime,
    mode: str = "live",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mode_s = str(mode or "live").strip().lower()
    for row in sessions:
        if mode_s != "all" and str(row.get("mode") or "").lower() != mode_s:
            continue
        created_raw = row.get("created_at")
        created = created_raw if isinstance(created_raw, datetime) else None
        if created is None and isinstance(created_raw, str) and created_raw.strip():
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created = None
        if created is not None:
            if created.tzinfo is not None:
                created = created.astimezone(timezone.utc).replace(tzinfo=None)
            if created < cutoff:
                continue
        out.append(dict(row))
    return out


def _db_rows(
    symbols: Sequence[str],
    *,
    since_minutes: float,
    limit_events: int,
    mode: str = "live",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    syms = [_sym(s) for s in symbols if _sym(s)]
    cutoff = _utc_naive_now() - timedelta(minutes=max(0.0, float(since_minutes)))
    with SessionLocal() as db:
        session_query = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.symbol.in_(syms),
            TradingAutomationSession.created_at >= cutoff,
        )
        mode_s = str(mode or "live").strip().lower()
        if mode_s != "all":
            session_query = session_query.filter(TradingAutomationSession.mode == mode_s)
        sessions = session_query.order_by(TradingAutomationSession.created_at.asc()).limit(200).all()
        session_ids = [int(row.id) for row in sessions]
        events: list[TradingAutomationEvent] = []
        if session_ids:
            events = (
                db.query(TradingAutomationEvent)
                .filter(TradingAutomationEvent.session_id.in_(session_ids))
                .order_by(TradingAutomationEvent.ts.asc())
                .limit(max(1, int(limit_events)))
                .all()
            )
        admission_events = (
            db.query(TradingAutomationEvent)
            .filter(
                TradingAutomationEvent.event_type == "ross_event_admitted",
                TradingAutomationEvent.ts >= cutoff,
                TradingAutomationEvent.payload_json["symbol"].astext.in_(syms),
            )
            .order_by(TradingAutomationEvent.ts.asc())
            .limit(200)
            .all()
        )
        by_event_id = {int(ev.id): ev for ev in events + admission_events}
        events = [by_event_id[k] for k in sorted(by_event_id)]
        session_rows = [
            {
                "id": int(row.id),
                "symbol": row.symbol,
                "state": row.state,
                "mode": row.mode,
                "execution_family": row.execution_family,
                "created_at": _jsonable_dt(row.created_at),
                "updated_at": _jsonable_dt(row.updated_at),
                "risk_snapshot": row.risk_snapshot_json if isinstance(row.risk_snapshot_json, dict) else {},
            }
            for row in sessions
        ]
        event_rows = [
            {
                "id": int(row.id),
                "session_id": int(row.session_id),
                "ts": _jsonable_dt(row.ts),
                "event_type": row.event_type,
                "payload": row.payload_json if isinstance(row.payload_json, dict) else {},
            }
            for row in events
        ]
    return session_rows, event_rows


def _transcript_mentions(path: str, *, since_minutes: float, max_symbols: int) -> list[dict[str, Any]]:
    mentions = recent_transcript_mentions(
        path,
        lookback_seconds=max(1.0, float(since_minutes) * 60.0),
        max_symbols=max(1, int(max_symbols)),
        max_lines=2000,
    )
    return [{"symbol": m.symbol, "ts": m.ts, "text": m.text} for m in mentions]


def _compact(row: dict[str, Any]) -> str:
    reasons = ", ".join(str(r.get("reason")) for r in row.get("latest_reasons", [])[:3])
    timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
    r2e = timing.get("ross_reference_to_entry_latency_s")
    r2a = timing.get("ross_reference_to_admission_latency_s")
    ref = timing.get("ross_reference") or "ross"
    timing_text = f"{ref}2e={r2e}s" if r2e is not None else f"{ref}2a={r2a}s" if r2a is not None else "r2e=n/a"
    speed = str(timing.get("ross_entry_speed_class") or "unknown")
    visual = row.get("visual_evidence") if isinstance(row.get("visual_evidence"), dict) else {}
    visual_status = str(visual.get("status") or "visual_unknown")
    return (
        f"{row['symbol']:<6} {row['classification']:<32} "
        f"verdict={row.get('ross_vs_chili_verdict', ''):<32} "
        f"{timing_text:<12} speed={speed:<24} "
        f"visual={visual_status:<40} "
        f"sessions={row['session_count']:<2} admissions={row['admission_count']:<2} "
        f"entries={row['entry_count']:<2} exits={row['exit_count']:<2} reasons={reasons}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Ross-vs-CHILI named-symbol incident audit.")
    parser.add_argument("symbols", nargs="+", help="Symbols to audit, e.g. CANF JEM DXTS DXF.")
    parser.add_argument("--since-minutes", type=float, default=240.0)
    parser.add_argument("--transcript-path", default=DEFAULT_TRANSCRIPT_PATH)
    parser.add_argument("--ross-trades-path", default=DEFAULT_ROSS_TRADE_EVENTS_PATH)
    parser.add_argument("--visual-evidence-root", default=str(DEFAULT_ROSS_VISUAL_EVIDENCE_ROOT))
    parser.add_argument("--visual-review-manifest", default=DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH)
    parser.add_argument("--visual-evidence-min-frames", type=int, default=3)
    parser.add_argument("--max-events", type=int, default=1000)
    parser.add_argument("--mode", choices=("live", "paper", "all"), default="live")
    parser.add_argument("--include-risk-snapshot", action="store_true")
    parser.add_argument(
        "--require-visual-certification",
        action="store_true",
        help="Exit nonzero when any requested symbol lacks certifying reviewed/linked frame evidence.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    symbols = [_sym(s) for s in args.symbols if _sym(s)]
    sessions, events = _db_rows(
        symbols,
        since_minutes=args.since_minutes,
        limit_events=args.max_events,
        mode=args.mode,
    )
    mentions = _transcript_mentions(
        args.transcript_path,
        since_minutes=args.since_minutes,
        max_symbols=max(8, len(symbols) * 2),
    )
    ross_trade_events = _read_ross_trade_events(args.ross_trades_path, since_minutes=args.since_minutes)
    visual_evidence_audit = audit_visual_evidence_root(
        Path(args.visual_evidence_root),
        min_frames=max(1, int(args.visual_evidence_min_frames)),
    )
    visual_review_manifest = _read_visual_review_manifest(args.visual_review_manifest)
    rows = [
        summarize_symbol_incident(
            sym,
            sessions=sessions,
            events=events,
            transcript_mentions=mentions,
            ross_trade_events=ross_trade_events,
            visual_evidence_audit=visual_evidence_audit,
            visual_review_manifest=visual_review_manifest,
            include_risk_snapshot=args.include_risk_snapshot,
        )
        for sym in symbols
    ]
    summary = summarize_visual_evidence_status(rows)
    review_queue = visual_review_queue(rows)
    failures = visual_certification_failures(rows) if args.require_visual_certification else []
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "read_only": True,
                    "mode": args.mode,
                    "visual_evidence_summary": summary,
                    "visual_review_queue": review_queue,
                    "visual_certification_failures": failures,
                    "symbols": rows,
                },
                default=str,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for row in rows:
            print(_compact(row))
        print(
            "VISUAL "
            f"certifiable={summary['certifiable_count']} "
            f"uncertified={summary['uncertified_count']} "
            f"review_queue={len(review_queue)} "
            f"statuses={summary['status_counts']}"
        )
    if failures:
        print(
            "VISUAL_CERTIFICATION_FAILED "
            f"uncertified={','.join(f['symbol'] for f in failures)}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
