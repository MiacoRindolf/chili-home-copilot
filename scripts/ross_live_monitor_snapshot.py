from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.trading.momentum_neural.ross_transcript_bridge import (
    DEFAULT_TRANSCRIPT_PATH,
    warrior_session_marker_ok,
)
from scripts.audit_ross_symbol_incidents import (
    DEFAULT_ROSS_TRADE_EVENTS_PATH,
    _db_rows,
    _read_ross_trade_events,
    _sym,
    _transcript_mentions,
    summarize_symbol_incident,
)
from scripts.verify_ross_event_admission_runtime import _recent_events, evaluate_recent_ross_admissions
from scripts.verify_ross_lane_feed_runtime import check_feed_health
from scripts.verify_ross_live_window_readiness import evaluate_live_window_readiness
from scripts.verify_ross_live_window_readiness import READINESS_PROFILES, readiness_requirements_for_profile
from scripts.verify_ross_transcript_runtime import _host_processes, evaluate_transcript_runtime

DEFAULT_OUTPUT_DATE_TIMEZONE = "America/Los_Angeles"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _admission_symbols(events: Sequence[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in events:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        sym = _sym(payload.get("symbol") or payload.get("ticker"))
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _missing_leading_letter_alias(symbol: str, candidates: Sequence[str]) -> str | None:
    """Resolve ASR-clipped symbols only against symbols CHILI already admitted."""
    sym = _sym(symbol)
    if len(sym) < 3 or len(sym) > 4:
        return None
    matches = [
        cand
        for cand in (_sym(row) for row in candidates)
        if cand
        and len(cand) == len(sym) + 1
        and cand.endswith(sym)
        and cand != sym
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_transcript_mentions_to_admissions(
    transcript_mentions: Sequence[dict[str, Any]],
    admission_symbols: Sequence[str],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for row in transcript_mentions:
        item = dict(row)
        raw = _sym(item.get("symbol"))
        alias = _missing_leading_letter_alias(raw, admission_symbols)
        if alias:
            item["original_symbol"] = raw
            item["symbol"] = alias
            item["alias_resolution"] = "missing_leading_letter_to_admitted_symbol"
        resolved.append(item)
    return resolved


_NEGATIVE_TRANSCRIPT_PHRASES = (
    "not interested",
    "nope",
    "no trade",
    "not a trade",
    "pulled back too much",
    "too much pullback",
    "off my radar",
    "was a stop",
)

_PASSIVE_TRANSCRIPT_PHRASES = (
    "let me bring up",
    "let me put the volume",
    "had this",
    "had that line",
    "at the time",
    "could potentially",
    "almost curled",
    "did well",
    "because people",
    "swing trade idea",
    "don't really have another target",
    "would have been",
    "would have preferred",
    "you could have got",
    "kept trading",
    "bitcoin's going up",
    "garbage  crypto plays",
    "garbage crypto plays",
    "didn't do well",
    "possibility  of a curl",
    "possibility of a curl",
    "name stays",
    "give one example",
    "remember ",
    "competing against",
    "had both of them on the chart",
)

_ACTION_TRANSCRIPT_PHRASES = (
    "starter",
    "entry",
    "entered",
    "took",
    "bought",
    "long",
    "breakout",
    "break out",
    "through",
    "adding",
    "scalp",
)


def _transcript_symbol_context(text: str, symbol: str, *, radius: int = 96) -> str:
    low = str(text or "").lower()
    sym = _sym(symbol).lower()
    idx = low.find(sym)
    if idx < 0:
        return low[: radius * 2]
    return low[max(0, idx - radius): idx + len(sym) + radius]


def _negative_transcript_mention(row: dict[str, Any]) -> bool:
    text = str(row.get("text") or row.get("raw_text") or "")
    ctx = _transcript_symbol_context(text, str(row.get("symbol") or ""))
    return any(phrase in ctx for phrase in _NEGATIVE_TRANSCRIPT_PHRASES)


def _passive_transcript_mention(row: dict[str, Any]) -> bool:
    text = str(row.get("text") or row.get("raw_text") or "")
    ctx = _transcript_symbol_context(text, str(row.get("symbol") or ""))
    if any(phrase in ctx for phrase in _ACTION_TRANSCRIPT_PHRASES):
        return False
    return any(phrase in ctx for phrase in _PASSIVE_TRANSCRIPT_PHRASES)


def _actionable_transcript_mentions(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if _negative_transcript_mention(item):
            item["monitor_filtered_reason"] = "negative_transcript_context"
            continue
        if _passive_transcript_mention(item):
            item["monitor_filtered_reason"] = "passive_transcript_context"
            continue
        out.append(item)
    return out


def _snapshot_symbols(
    *,
    symbols_arg: Sequence[str],
    transcript_mentions: Sequence[dict[str, Any]],
    ross_trade_events: Sequence[dict[str, Any]],
    recent_events: Sequence[dict[str, Any]],
    marker_ok: bool,
) -> list[str]:
    symbols = [_sym(s) for s in symbols_arg if _sym(s)]
    if symbols:
        return symbols
    symbols = []
    seen: set[str] = set()
    admission_symbols = _admission_symbols(recent_events)
    resolved_mentions = _actionable_transcript_mentions(
        _resolve_transcript_mentions_to_admissions(transcript_mentions, admission_symbols)
    )
    sources: list[Any] = []
    if marker_ok:
        sources.extend(row.get("symbol") for row in resolved_mentions)
    sources.extend(row.get("symbol") for row in ross_trade_events)
    sources.extend(admission_symbols)
    for sym in sources:
        norm = _sym(sym)
        if norm and norm not in seen:
            seen.add(norm)
            symbols.append(norm)
    return symbols


REVIEW_VERDICTS = {
    "chili_entered_late_for_ross_scalp",
    "chili_entered_too_late_for_ross_scalp",
    "chili_saw_but_did_not_enter",
    "chili_admitted_and_ticked_no_entry",
    "chili_admitted_without_tick",
    "ross_mentioned_chili_missed",
}
STALE_TRANSCRIPT_ONLY_ATTENTION_MAX_LATENCY_S = 15.0 * 60.0


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _incident_attention(row: dict[str, Any]) -> dict[str, Any]:
    verdict = str(row.get("ross_vs_chili_verdict") or "")
    timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
    speed = str(timing.get("ross_entry_speed_class") or "unknown")
    reasons = [str(reason.get("reason") or "") for reason in row.get("latest_reasons", []) if reason.get("reason")]
    needs_review = verdict in REVIEW_VERDICTS or speed in {"late_for_scalp", "too_late_for_ross_scalp"}
    reason = verdict or row.get("classification") or "unknown"
    ross_trades = row.get("ross_trades") if isinstance(row.get("ross_trades"), list) else []
    ross_mentions = row.get("ross_mentions") if isinstance(row.get("ross_mentions"), list) else []
    reference = str(timing.get("ross_reference") or "")
    reference_to_admission_s = _float_or_none(timing.get("ross_reference_to_admission_latency_s"))
    reference_to_entry_s = _float_or_none(timing.get("ross_reference_to_entry_latency_s"))
    freshest_latency_s = min(
        [lat for lat in (reference_to_admission_s, reference_to_entry_s) if lat is not None],
        default=None,
    )
    if (
        needs_review
        and not ross_trades
        and reference == "mention"
        and freshest_latency_s is not None
        and freshest_latency_s > STALE_TRANSCRIPT_ONLY_ATTENTION_MAX_LATENCY_S
    ):
        needs_review = False
        reason = "stale_transcript_only_context"
    if (
        needs_review
        and not ross_trades
        and reference == "mention"
        and freshest_latency_s is not None
        and freshest_latency_s < 0
    ):
        needs_review = False
        reason = "chili_seen_before_transcript_reference"
    if (
        needs_review
        and not ross_trades
        and not ross_mentions
        and not reference
        and verdict
        in {
            "chili_admitted_and_ticked_no_entry",
            "chili_admitted_without_tick",
            "chili_saw_but_did_not_enter",
        }
    ):
        needs_review = False
        reason = "autonomous_chili_watch_no_ross_reference"
    return {
        "needs_review": bool(needs_review),
        "reason": reason,
        "speed": speed,
        "latest_reasons": reasons[:3],
    }


def build_monitor_snapshot(
    *,
    symbols: Sequence[str],
    readiness_ok: bool,
    readiness_reason: str,
    readiness_detail: dict[str, Any],
    incidents: Sequence[dict[str, Any]],
    since_minutes: float,
    readiness_since_minutes: float,
    mode: str,
    profile: str = "quiet",
) -> dict[str, Any]:
    incident_rows: list[dict[str, Any]] = []
    for row in incidents:
        incident = dict(row)
        incident["operator_attention"] = _incident_attention(incident)
        incident_rows.append(incident)
    attention_symbols = [
        str(row.get("symbol") or "")
        for row in incident_rows
        if (row.get("operator_attention") or {}).get("needs_review")
    ]
    return {
        "ok": bool(readiness_ok),
        "read_only": True,
        "as_of_utc": _utc_now_iso(),
        "since_minutes": float(since_minutes),
        "readiness_since_minutes": float(readiness_since_minutes),
        "mode": mode,
        "profile": str(profile or "quiet"),
        "readiness": {
            "ok": bool(readiness_ok),
            "reason": readiness_reason,
            "feed_reason": readiness_detail.get("feed_reason"),
            "feed_severity": readiness_detail.get("feed_severity"),
            "admission_reason": readiness_detail.get("admission_reason"),
            "admission_checked": (readiness_detail.get("admission") or {}).get("checked"),
            "admission_min_checked": (readiness_detail.get("admission") or {}).get("min_checked"),
            "warrior_session_reason": (readiness_detail.get("transcript") or {}).get("warrior_session_reason"),
            "running_daemons": len((readiness_detail.get("transcript") or {}).get("running_daemons") or []),
        },
        "symbols_requested": [_sym(s) for s in symbols if _sym(s)],
        "attention_count": len(attention_symbols),
        "attention_symbols": attention_symbols,
        "incidents": incident_rows,
    }


def _compact(snapshot: dict[str, Any]) -> str:
    lines = [
        (
            f"readiness={snapshot['readiness']['reason']} "
            f"feed={snapshot['readiness']['feed_reason']} "
            f"warrior={snapshot['readiness']['warrior_session_reason']} "
            f"admissions={snapshot['readiness']['admission_checked']}/{snapshot['readiness']['admission_min_checked']}"
        )
    ]
    for row in snapshot.get("incidents") or []:
        reasons = ", ".join(str(r.get("reason")) for r in row.get("latest_reasons", [])[:3])
        timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
        r2e = timing.get("ross_reference_to_entry_latency_s")
        r2a = timing.get("ross_reference_to_admission_latency_s")
        ref = timing.get("ross_reference") or "ross"
        timing_text = f"{ref}2e={r2e}s" if r2e is not None else f"{ref}2a={r2a}s" if r2a is not None else "r2e=n/a"
        speed = str(timing.get("ross_entry_speed_class") or "unknown")
        attention = row.get("operator_attention") if isinstance(row.get("operator_attention"), dict) else {}
        attention_text = "review" if attention.get("needs_review") else "ok"
        lines.append(
            f"{row['symbol']:<6} {row['classification']:<32} "
            f"verdict={row.get('ross_vs_chili_verdict', ''):<32} "
            f"{timing_text:<12} speed={speed:<24} "
            f"attention={attention_text:<6} "
            f"sessions={row['session_count']:<2} admissions={row['admission_count']:<2} "
            f"entries={row['entry_count']:<2} exits={row['exit_count']:<2} reasons={reasons}"
        )
    return "\n".join(lines)


def collect_monitor_snapshot(
    *,
    symbols_arg: Sequence[str],
    since_minutes: float,
    readiness_since_minutes: float,
    mode: str,
    transcript_path: str,
    ross_trades_path: str = DEFAULT_ROSS_TRADE_EVENTS_PATH,
    require_warrior_session: bool = False,
    require_live_event_evidence: bool = False,
    marker_path: str | None = None,
    marker_max_age_seconds: float | None = None,
    max_iqfeed_age_hot_s: float = 60.0,
    include_risk_snapshot: bool = False,
    profile: str = "quiet",
) -> dict[str, Any]:
    requirements = readiness_requirements_for_profile(
        profile,
        require_warrior_session=require_warrior_session,
        require_live_event_evidence=require_live_event_evidence,
    )
    feed = check_feed_health(max_iqfeed_age_hot_s=max_iqfeed_age_hot_s)
    recent_events = _recent_events(since_minutes=readiness_since_minutes)
    min_checked = 1 if requirements["require_live_event_evidence"] else 0
    admission_ok, admission_reason, admission_detail = evaluate_recent_ross_admissions(
        recent_events,
        min_ticks=1,
        min_checked=min_checked,
    )
    marker_ok, marker_reason, marker_detail = warrior_session_marker_ok(
        marker_path,
        max_age_seconds=marker_max_age_seconds,
    )
    transcript_ok, transcript_reason, transcript_detail = evaluate_transcript_runtime(
        marker_ok=marker_ok,
        marker_reason=marker_reason,
        marker_detail=marker_detail,
        processes=_host_processes(),
    )
    readiness_ok, readiness_reason, readiness_detail = evaluate_live_window_readiness(
        feed=feed,
        admission_ok=admission_ok,
        admission_reason=admission_reason,
        admission_detail=admission_detail,
        transcript_ok=transcript_ok,
        transcript_reason=transcript_reason,
        transcript_detail=transcript_detail,
        require_warrior_session=requirements["require_warrior_session"],
    )

    transcript_mentions = _transcript_mentions(
        transcript_path,
        since_minutes=since_minutes,
        max_symbols=max(8, len(symbols_arg) * 2 or 8),
    )
    admission_symbols = _admission_symbols(recent_events)
    transcript_mentions = _resolve_transcript_mentions_to_admissions(
        transcript_mentions,
        admission_symbols,
    )
    transcript_mentions = _actionable_transcript_mentions(transcript_mentions)
    ross_trade_events = _read_ross_trade_events(ross_trades_path, since_minutes=since_minutes)
    symbols = _snapshot_symbols(
        symbols_arg=symbols_arg,
        transcript_mentions=transcript_mentions,
        ross_trade_events=ross_trade_events,
        recent_events=recent_events,
        marker_ok=marker_ok,
    )

    sessions, events = _db_rows(
        symbols,
        since_minutes=since_minutes,
        limit_events=1000,
        mode=mode,
    )
    incidents = [
        summarize_symbol_incident(
            sym,
            sessions=sessions,
            events=events,
            transcript_mentions=transcript_mentions,
            ross_trade_events=ross_trade_events,
            include_risk_snapshot=include_risk_snapshot,
        )
        for sym in symbols
    ]
    return build_monitor_snapshot(
        symbols=symbols,
        readiness_ok=readiness_ok,
        readiness_reason=readiness_reason,
        readiness_detail=readiness_detail,
        incidents=incidents,
        since_minutes=since_minutes,
        readiness_since_minutes=readiness_since_minutes,
        mode=mode,
        profile=profile,
    )


def iter_monitor_snapshots(
    *,
    max_iterations: int,
    interval_seconds: float,
    snapshot_fn,
    sleep_fn=time.sleep,
    on_snapshot=None,
) -> list[dict[str, Any]]:
    count = max(1, int(max_iterations))
    interval = max(0.5, float(interval_seconds))
    snapshots: list[dict[str, Any]] = []
    for idx in range(count):
        snapshot = snapshot_fn()
        snapshots.append(snapshot)
        if on_snapshot is not None:
            on_snapshot(snapshot)
        if idx + 1 < count:
            sleep_fn(interval)
    return snapshots


def resolve_snapshot_output_path(
    path: str | Path,
    *,
    now_utc: datetime | None = None,
    date_timezone: str = DEFAULT_OUTPUT_DATE_TIMEZONE,
) -> Path:
    raw = str(path)
    if "{date}" in raw:
        tz_name = str(date_timezone or DEFAULT_OUTPUT_DATE_TIMEZONE)
        try:
            date_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            date_tz = ZoneInfo(DEFAULT_OUTPUT_DATE_TIMEZONE)
        dt = (now_utc or datetime.now(timezone.utc)).astimezone(date_tz)
        raw = raw.replace("{date}", dt.strftime("%Y%m%d"))
    return Path(raw)


def append_snapshots_jsonl(path: str | Path, snapshots: Sequence[dict[str, Any]]) -> Path:
    out = resolve_snapshot_output_path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for snapshot in snapshots:
            fh.write(json.dumps(snapshot, default=str, sort_keys=True) + "\n")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Ross live monitor snapshot: readiness plus CHILI symbol incidents.")
    parser.add_argument("symbols", nargs="*", help="Optional symbols to include. If absent, uses recent transcript/admission symbols.")
    parser.add_argument(
        "--profile",
        choices=READINESS_PROFILES,
        default="quiet",
        help="quiet=off-hours, prestream=Warrior stream/session required, live=stream plus live Ross admission evidence required.",
    )
    parser.add_argument("--since-minutes", type=float, default=30.0)
    parser.add_argument("--readiness-since-minutes", type=float, default=30.0)
    parser.add_argument("--mode", choices=("live", "paper", "all"), default="live")
    parser.add_argument("--transcript-path", default=DEFAULT_TRANSCRIPT_PATH)
    parser.add_argument("--ross-trades-path", default=DEFAULT_ROSS_TRADE_EVENTS_PATH)
    parser.add_argument("--require-warrior-session", action="store_true")
    parser.add_argument("--require-live-event-evidence", action="store_true")
    parser.add_argument("--marker-path", default=None)
    parser.add_argument("--marker-max-age-seconds", type=float, default=None)
    parser.add_argument("--max-iqfeed-age-hot-s", type=float, default=60.0)
    parser.add_argument("--include-risk-snapshot", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Repeat snapshots until --seconds expires or --iterations is reached.")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--out", default="", help="Optional JSONL file to append snapshots for post-stream audit.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def _snapshot() -> dict[str, Any]:
        return collect_monitor_snapshot(
            symbols_arg=args.symbols,
            since_minutes=args.since_minutes,
            readiness_since_minutes=args.readiness_since_minutes,
            mode=args.mode,
            transcript_path=args.transcript_path,
            ross_trades_path=args.ross_trades_path,
            require_warrior_session=args.require_warrior_session,
            require_live_event_evidence=args.require_live_event_evidence,
            marker_path=args.marker_path,
            marker_max_age_seconds=args.marker_max_age_seconds,
            max_iqfeed_age_hot_s=args.max_iqfeed_age_hot_s,
            include_risk_snapshot=args.include_risk_snapshot,
            profile=args.profile,
        )

    iterations = max(1, int(args.iterations or 1))
    if args.watch and args.seconds and args.seconds > 0:
        iterations = max(1, int(float(args.seconds) // max(0.5, float(args.interval_seconds or 0.5))) + 1)
    snapshots = iter_monitor_snapshots(
        max_iterations=iterations if args.watch else 1,
        interval_seconds=args.interval_seconds,
        snapshot_fn=_snapshot,
        on_snapshot=(lambda snapshot: append_snapshots_jsonl(args.out, [snapshot])) if args.out else None,
    )
    if args.json:
        payload: Any = snapshots[-1] if len(snapshots) == 1 else {"ok": all(s.get("ok") for s in snapshots), "read_only": True, "snapshots": snapshots}
        print(json.dumps(payload, default=str, indent=2, sort_keys=True))
    else:
        for idx, snapshot in enumerate(snapshots):
            if len(snapshots) > 1:
                print(f"--- snapshot {idx + 1}/{len(snapshots)} {snapshot.get('as_of_utc')} ---")
            print(_compact(snapshot))
    return 0 if all(bool(s.get("ok")) for s in snapshots) else 1


if __name__ == "__main__":
    raise SystemExit(main())
