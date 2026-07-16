"""Rollback-only Ross transcript admission audit.

This watches Ross transcript rows, extracts trading-context ticker mentions
with the production parser, and asks the normal Ross admission path whether
CHILI would admit each ticker right now. It never creates live sessions or
orders because admission runs with dry_run=True and the DB session is rolled
back.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.config import settings
from app.services.trading.momentum_neural.ross_event_admission import admit_ross_event
from app.services.trading.momentum_neural.ross_transcript_bridge import (
    DEFAULT_TRANSCRIPT_PATH,
    TranscriptMention,
    recent_transcript_mentions,
)

DEFAULT_OUT = Path(r"D:\CHILI-Docker\chili-data\ross_stream\ross_transcript_admission_audit.jsonl")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rollback-only Ross transcript admission audit.")
    p.add_argument("--path", default="", help="Transcript JSONL path. Defaults to Ross transcript path.")
    p.add_argument("--out", default="", help="Audit JSONL path. Use '-' to disable file output.")
    p.add_argument("--lookback-seconds", type=float, default=90.0)
    p.add_argument("--max-symbols", type=int, default=8)
    p.add_argument("--max-lines", type=int, default=400)
    p.add_argument("--refresh-viability", action="store_true", default=True)
    p.add_argument("--no-refresh-viability", action="store_false", dest="refresh_viability")
    p.add_argument("--ignore-market-hours", action="store_true")
    p.add_argument("--assume-live", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval-seconds", type=float, default=2.0)
    p.add_argument("--seconds", type=float, default=0.0)
    return p.parse_args()


def _transcript_path(args: argparse.Namespace) -> str:
    return str(
        args.path
        or getattr(settings, "chili_momentum_ross_transcript_path", DEFAULT_TRANSCRIPT_PATH)
        or DEFAULT_TRANSCRIPT_PATH
    )


def _audit_mentions(args: argparse.Namespace, mentions: list[TranscriptMention]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        if args.assume_live:
            settings.chili_momentum_live_runner_enabled = True
        audit_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for mention in mentions:
            out = admit_ross_event(
                db,
                symbol=mention.symbol,
                source="ross_transcript_admission_audit",
                refresh_viability=bool(args.refresh_viability),
                dry_run=True,
                ignore_cooldown=True,
                market_open_fn=(lambda _s: True) if args.ignore_market_hours else None,
            )
            out.update(
                {
                    "audit_ts": audit_ts,
                    "audit_rollback_only": True,
                    "transcript_ts": mention.ts.isoformat(),
                    "transcript_text": mention.text[:500],
                    "transcript_key": mention.key,
                }
            )
            rows.append(out)
        return rows
    finally:
        try:
            db.rollback()
        finally:
            db.close()


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def _compact(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "")
    would = "YES" if row.get("would_admit") or row.get("admitted") else "NO"
    skipped = str(row.get("skipped") or "")
    universe = str(row.get("ross_universe_reason") or "")
    evidence = str(row.get("ross_evidence_reason") or "")
    text = str(row.get("transcript_text") or "").replace("\n", " ")[:80]
    return f"{sym:<8} would={would:<3} skip={skipped:<34} universe={universe:<30} evidence={evidence:<24} text={text}"


def run_once(args: argparse.Namespace, processed_keys: set[str] | None = None) -> list[dict[str, Any]]:
    mentions = recent_transcript_mentions(
        _transcript_path(args),
        lookback_seconds=float(args.lookback_seconds),
        max_lines=int(args.max_lines),
        max_symbols=int(args.max_symbols),
    )
    if processed_keys is not None:
        mentions = [m for m in mentions if m.key not in processed_keys]
    rows = _audit_mentions(args, mentions) if mentions else []
    if processed_keys is not None:
        processed_keys.update(m.key for m in mentions)
    if rows and args.out != "-":
        _append_jsonl(Path(args.out) if args.out else DEFAULT_OUT, rows)
    return rows


def main() -> int:
    args = _parse_args()
    processed_keys: set[str] = set()
    deadline = time.monotonic() + float(args.seconds) if args.seconds and args.seconds > 0 else None
    while True:
        rows = run_once(args, processed_keys=processed_keys)
        for row in rows:
            print(json.dumps(row, default=str, sort_keys=True) if args.json else _compact(row))
        if args.once:
            break
        interval = max(0.5, float(args.interval_seconds or 2.0))
        if deadline is not None and time.monotonic() + interval > deadline:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
