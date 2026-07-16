from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ross_live_monitor_snapshot import resolve_snapshot_output_path


DEFAULT_MONITOR_PATH = r"D:\CHILI-Docker\chili-data\ross_stream\ross_live_monitor_{date}.jsonl"


def read_monitor_snapshots(path: str | Path, *, max_lines: int = 20000) -> list[dict[str, Any]]:
    p = resolve_snapshot_output_path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(max_lines)) :]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarize_monitor_snapshots(snapshots: Sequence[dict[str, Any]]) -> dict[str, Any]:
    readiness_reasons = Counter()
    verdicts = Counter()
    attention_symbols: set[str] = set()
    latest_by_symbol: dict[str, dict[str, Any]] = {}
    first_ts: str | None = None
    last_ts: str | None = None
    ok_count = 0

    for snapshot in snapshots:
        ts = str(snapshot.get("as_of_utc") or "")
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if snapshot.get("ok"):
            ok_count += 1
        readiness = snapshot.get("readiness") if isinstance(snapshot.get("readiness"), dict) else {}
        reason = str(readiness.get("reason") or "unknown")
        readiness_reasons[reason] += 1
        for sym in snapshot.get("attention_symbols") or []:
            if sym:
                attention_symbols.add(str(sym))
        for incident in snapshot.get("incidents") or []:
            if not isinstance(incident, dict):
                continue
            sym = str(incident.get("symbol") or "").strip().upper()
            if not sym:
                continue
            verdict = str(incident.get("ross_vs_chili_verdict") or incident.get("classification") or "unknown")
            verdicts[verdict] += 1
            previous = latest_by_symbol.get(sym)
            if previous is None or ts >= str(previous.get("_snapshot_ts") or ""):
                row = dict(incident)
                row["_snapshot_ts"] = ts
                latest_by_symbol[sym] = row

    latest_symbols: list[dict[str, Any]] = []
    for sym, incident in sorted(latest_by_symbol.items()):
        attention = incident.get("operator_attention") if isinstance(incident.get("operator_attention"), dict) else {}
        timing = incident.get("timing") if isinstance(incident.get("timing"), dict) else {}
        latest_symbols.append(
            {
                "symbol": sym,
                "snapshot_ts": incident.get("_snapshot_ts"),
                "classification": incident.get("classification"),
                "verdict": incident.get("ross_vs_chili_verdict"),
                "needs_review": bool(attention.get("needs_review")),
                "attention_reason": attention.get("reason"),
                "speed": timing.get("ross_entry_speed_class") or attention.get("speed"),
                "ross_reference_to_entry_latency_s": timing.get("ross_reference_to_entry_latency_s"),
                "session_count": incident.get("session_count"),
                "admission_count": incident.get("admission_count"),
                "entry_count": incident.get("entry_count"),
                "exit_count": incident.get("exit_count"),
                "latest_reasons": [
                    str(row.get("reason") or "")
                    for row in incident.get("latest_reasons", [])[:3]
                    if isinstance(row, dict) and row.get("reason")
                ],
            }
        )

    return {
        "ok": bool(snapshots),
        "read_only": True,
        "snapshot_count": len(snapshots),
        "ok_snapshot_count": ok_count,
        "first_snapshot_ts": first_ts,
        "last_snapshot_ts": last_ts,
        "attention_symbols": sorted(attention_symbols),
        "attention_count": len(attention_symbols),
        "readiness_reasons": dict(readiness_reasons.most_common()),
        "verdict_counts": dict(verdicts.most_common()),
        "latest_symbols": latest_symbols,
    }


def _compact(summary: dict[str, Any]) -> str:
    lines = [
        f"snapshots={summary.get('snapshot_count', 0)} ok_snapshots={summary.get('ok_snapshot_count', 0)}",
        f"window={summary.get('first_snapshot_ts')}..{summary.get('last_snapshot_ts')}",
        "attention_symbols=" + (", ".join(summary.get("attention_symbols") or []) or "none"),
        "readiness_reasons=" + json.dumps(summary.get("readiness_reasons") or {}, sort_keys=True),
    ]
    for row in summary.get("latest_symbols") or []:
        reasons = ", ".join(row.get("latest_reasons") or [])
        lines.append(
            f"{row.get('symbol', ''):<6} verdict={str(row.get('verdict') or ''):<36} "
            f"review={str(row.get('needs_review')):<5} speed={row.get('speed') or 'unknown':<24} "
            f"entries={row.get('entry_count')} exits={row.get('exit_count')} reasons={reasons}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize the daily read-only Ross live monitor JSONL.")
    parser.add_argument("--path", default=DEFAULT_MONITOR_PATH)
    parser.add_argument("--max-lines", type=int, default=20000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = resolve_snapshot_output_path(args.path)
    snapshots = read_monitor_snapshots(path, max_lines=args.max_lines)
    summary = summarize_monitor_snapshots(snapshots)
    summary["path"] = str(path)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(_compact(summary))
    return 0 if snapshots else 1


if __name__ == "__main__":
    raise SystemExit(main())
