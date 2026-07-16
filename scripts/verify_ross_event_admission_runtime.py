from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.models.trading import TradingAutomationEvent


EVENT_TYPE = "ross_event_admitted"
LIVE_EVENT_SOURCES = ("iqfeed", "tape", "ignition", "nbbo", "ross_transcript")


def _utc_naive(now: datetime | None = None) -> datetime:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_live_event_source(payload: dict[str, Any]) -> bool:
    source = str(payload.get("source") or "").strip().lower()
    return any(token in source for token in LIVE_EVENT_SOURCES)


def evaluate_recent_ross_admissions(
    events: Sequence[dict[str, Any]],
    *,
    min_ticks: int = 1,
    min_checked: int = 0,
) -> tuple[bool, str, dict[str, Any]]:
    bad: list[dict[str, Any]] = []
    checked = 0
    for row in events:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if not _is_live_event_source(payload):
            continue
        checked += 1
        try:
            ticked = int(payload.get("ticked") or 0)
        except (TypeError, ValueError):
            ticked = 0
        if ticked < int(min_ticks):
            bad.append(
                {
                    "session_id": row.get("session_id"),
                    "ts": row.get("ts"),
                    "symbol": payload.get("symbol"),
                    "source": payload.get("source"),
                    "ticked": ticked,
                    "latency_ms": payload.get("latency_ms"),
                }
            )
    detail = {"checked": checked, "bad": bad, "min_ticks": int(min_ticks), "min_checked": int(min_checked)}
    if checked < int(min_checked):
        return False, "ross_event_admission_no_recent_live_events", detail
    if bad:
        return False, "ross_event_admission_missing_immediate_tick", detail
    return True, "ross_event_admission_runtime_ok", detail


def _recent_events(*, since_minutes: float) -> list[dict[str, Any]]:
    cutoff = _utc_naive() - timedelta(minutes=max(0.0, float(since_minutes)))
    with SessionLocal() as db:
        rows = (
            db.query(TradingAutomationEvent)
            .filter(
                TradingAutomationEvent.event_type == EVENT_TYPE,
                TradingAutomationEvent.ts >= cutoff,
            )
            .order_by(TradingAutomationEvent.ts.desc())
            .limit(200)
            .all()
        )
        return [
            {
                "session_id": int(row.session_id),
                "ts": row.ts.isoformat() if row.ts is not None else None,
                "payload": row.payload_json if isinstance(row.payload_json, dict) else {},
            }
            for row in rows
        ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify recent Ross event admissions immediately ticked the live runner.")
    parser.add_argument("--since-minutes", type=float, default=30.0)
    parser.add_argument("--min-ticks", type=int, default=1)
    parser.add_argument(
        "--min-checked",
        type=int,
        default=0,
        help="Require at least this many recent live-source Ross admissions; use during active Ross monitoring.",
    )
    args = parser.parse_args(argv)

    ok, reason, detail = evaluate_recent_ross_admissions(
        _recent_events(since_minutes=args.since_minutes),
        min_ticks=args.min_ticks,
        min_checked=args.min_checked,
    )
    print(reason)
    print(f"checked={detail['checked']}")
    print(f"min_checked={detail['min_checked']}")
    print(f"bad={len(detail['bad'])}")
    for row in detail["bad"][:20]:
        print(
            "bad_admission="
            f"session_id={row.get('session_id')} symbol={row.get('symbol')} "
            f"source={row.get('source')} ticked={row.get('ticked')} "
            f"latency_ms={row.get('latency_ms')} ts={row.get('ts')}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
