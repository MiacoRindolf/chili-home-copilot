"""Dry-run Ross event admission for current Ross-profile equity candidates.

This is an audit tool, not a trading runner. It calls the same Ross admission
path with ``dry_run=True`` and always rolls back the DB session before exit, so
it can answer "would CHILI arm this?" without creating sessions or orders.
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
from app.services.trading.momentum_neural.universe import EQUITY_ROSS_SMALLCAP, build_equity_universe

DEFAULT_OUT = Path(r"D:\CHILI-Docker\chili-data\ross_stream\ross_admission_dry_run.jsonl")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dry-run Ross admission for symbols or current Ross universe.")
    p.add_argument("symbols", nargs="*", help="Optional symbols. Defaults to the current Ross universe.")
    p.add_argument("--limit", type=int, default=20, help="Max symbols from the Ross universe when symbols omitted.")
    p.add_argument(
        "--refresh-viability",
        action="store_true",
        help="Run the one-symbol momentum pipeline inside the rollback-only transaction.",
    )
    p.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Audit setup logic even when the market is closed.",
    )
    p.add_argument(
        "--assume-live",
        action="store_true",
        help="Audit as if the Ross live runner flag is enabled; still dry-run and rollback-only.",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON lines instead of a compact table.")
    p.add_argument(
        "--out",
        default="",
        help="Optional JSONL path to append audit rows. Use '-' to disable file output.",
    )
    p.add_argument(
        "--source",
        default="iqfeed_l1_dry_run_audit",
        help="Admission source label. Default uses IQFeed semantics for tape-seeded evidence.",
    )
    p.add_argument("--interval-seconds", type=float, default=0.0, help="Repeat audit every N seconds when > 0.")
    p.add_argument("--seconds", type=float, default=0.0, help="Bounded monitor duration when interval is set.")
    return p.parse_args()


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [str(s or "").strip().upper() for s in args.symbols if str(s or "").strip()]
    return build_equity_universe(EQUITY_ROSS_SMALLCAP)[: max(1, int(args.limit))]


def _compact(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "")
    skipped = str(row.get("skipped") or "")
    would = "YES" if row.get("would_admit") or row.get("admitted") else "NO"
    universe = str(row.get("ross_universe_reason") or "")
    evidence = str(row.get("ross_evidence_reason") or "")
    score = row.get("viability_score")
    score_s = f"{float(score):.3f}" if isinstance(score, (int, float)) else ""
    return f"{sym:<8} would={would:<3} skip={skipped:<34} universe={universe:<32} evidence={evidence:<24} score={score_s}"


def _audit_rows(args: argparse.Namespace, symbols: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        if args.assume_live:
            settings.chili_momentum_live_runner_enabled = True
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for sym in symbols:
            out = admit_ross_event(
                db,
                symbol=sym,
                source=str(args.source),
                refresh_viability=bool(args.refresh_viability),
                dry_run=True,
                ignore_cooldown=True,
                market_open_fn=(lambda _s: True) if args.ignore_market_hours else None,
            )
            out["audit_ts"] = ts
            out["audit_rollback_only"] = True
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


def main() -> int:
    args = _parse_args()
    deadline = time.monotonic() + float(args.seconds) if args.seconds and args.seconds > 0 else None
    while True:
        symbols = _symbols(args)
        if not symbols:
            print("No symbols to audit.")
            return 0

        rows = _audit_rows(args, symbols)
        if args.out != "-":
            _append_jsonl(Path(args.out) if args.out else DEFAULT_OUT, rows)
        for out in rows:
            if args.json:
                print(json.dumps(out, default=str, sort_keys=True))
            else:
                print(_compact(out))

        interval = float(args.interval_seconds or 0.0)
        if interval <= 0:
            break
        if deadline is not None and time.monotonic() + interval > deadline:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
