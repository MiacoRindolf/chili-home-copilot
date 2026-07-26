"""Sequential batch benchmark runner over the golden replay library.

Iterates the window manifest (``scripts/derive_replay_windows.py``), replays each
window through the REAL FSM via ``scripts/replay_ab_dark_flags.py`` (GOLDEN=1,
ARM=base, driver-default equity/exec-family for continuity with the banked
canonical numbers), mines the sink between runs (the next driver run DELETEs the
boards), and appends one JSONL record per window — resumable, deadline-guarded.

    python scripts/replay_benchmark_batch.py \
        --manifest D:/CHILI-Docker/chili-data/replay_batch/window_manifest.json \
        --out-dir  D:/CHILI-Docker/chili-data/replay_batch \
        --tiers baseline --stop-at 2026-07-27T03:00:00

Guards (all process-local — nothing touches the sealed Monday activation):
  * sink db name MUST end in ``_test`` (mirrors tests/conftest.py — fixtures TRUNCATE);
  * ``pg_try_advisory_lock(hashtext('chili_replay_batch'))`` held on the sink for the
    batch lifetime — one replay at a time, refuses to double-run;
  * STOP_AT deadline: pre-launch fit check (1.25x estimate + 2min must fit) AND an
    in-flight subprocess timeout capped to the remaining time — the batch can never
    still be running at the Monday premarket line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

BUILD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(BUILD, "scripts", "replay_ab_dark_flags.py")
PROD = "postgresql://chili:chili@localhost:5433/chili"  # golden window stats (read-only)
DEFAULT_SINK = "postgresql://chili:chili@localhost:5433/chili_replay2_test"

SUMMARY_RE = re.compile(r"\[ARM=(\w+)\]\s+(\S+)\s+PnL\s+([+-]?[\d.]+)\s+entries=(\d+)\s+exits=(\d+)")
FILL_RE = re.compile(r"^\s*(BUY|SELL)\s+([\d.]+)\s+@\s+\$?([\d.]+)")
FINAL_RE = re.compile(r"final_state=(\S+)")

MARKET_SQL = """
SELECT count(*),
       max(price),
       min(price),
       (array_agg(price ORDER BY observed_at ASC, id ASC))[1],
       (array_agg(observed_at ORDER BY price DESC, observed_at ASC))[1]::text
FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(a)s AND observed_at < %(b)s AND price > 0
"""


def guard_sink(url: str) -> str:
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name.endswith("_test"):
        raise SystemExit(f"[batch] REFUSING sink '{name}' — must end in _test (conftest rule)")
    return url


def parse_driver_stdout(out: str) -> tuple[str, dict]:
    parsed: dict = {"fills": [], "final_state": None}
    for line in out.splitlines():
        m = FILL_RE.match(line)
        if m:
            parsed["fills"].append({"side": m.group(1).lower(),
                                    "qty": float(m.group(2)), "px": float(m.group(3))})
        fm = FINAL_RE.search(line)
        if fm:
            parsed["final_state"] = fm.group(1)
    sm = None
    for m in SUMMARY_RE.finditer(out):
        sm = m  # last summary line wins (matches the weekend-script convention)
    if sm is None:
        return "parse_fail", parsed
    parsed.update({"arm": sm.group(1), "pnl": float(sm.group(3)),
                   "entries": int(sm.group(4)), "exits": int(sm.group(5))})
    return "ok", parsed


def mine_sink(sink_url: str, symbol: str) -> dict:
    """Read-only sink mining — MUST run before the next driver run (it DELETEs boards)."""
    import psycopg2

    out: dict = {}
    try:
        conn = psycopg2.connect(sink_url)
        cur = conn.cursor()
        cur.execute("SELECT max(id) FROM trading_automation_sessions WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        sid = row[0] if row else None
        out["session_id"] = sid
        if sid is None:
            conn.close()
            return out
        cur.execute("SELECT id, session_id, ts, event_type, payload_json "
                    "FROM trading_automation_events WHERE session_id = %s ORDER BY id ASC", (sid,))
        rows = [{"id": r[0], "session_id": r[1], "ts": r[2], "event_type": r[3],
                 "payload_json": r[4]} for r in cur.fetchall()]
        # the PURE audit fn (not the limit=1000 wrapper) — a 2h window emits thousands of events
        sys.path.insert(0, BUILD)
        from app.services.trading.momentum_neural.setup_trace_audit import audit_setup_trace_events
        rep = audit_setup_trace_events(rows)
        lc = rep.lifecycle_summary if isinstance(rep.lifecycle_summary, dict) else {}
        out["setup_trace"] = {
            "events_seen": rep.events_seen, "traces_seen": rep.traces_seen,
            "findings_count": len(rep.findings),
            "event_type_counts": lc.get("event_type_counts", {}),
            "trace_alias_counts": lc.get("trace_alias_counts", {}),
            "wait_reason_counts": lc.get("wait_reason_counts", {}),
            "issue_counts": lc.get("issue_counts", {}),
        }
        # top binding detector-rejects (SQL from scripts/nightly_replay_report.py:_top_rejects)
        cur.execute("""
            SELECT r.key || ':' || r.value AS reject, count(*)
            FROM trading_automation_events e,
                 jsonb_each_text(e.payload_json->'detector_rejects') r
            WHERE e.session_id = %s AND e.event_type = 'live_entry_trigger_wait'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """, (sid,))
        out["top_rejects"] = [[r[0], int(r[1])] for r in cur.fetchall()]
        # fill events carry broker-time timestamps for within-trade MFE downstream
        cur.execute("SELECT ts::text, event_type, payload_json FROM trading_automation_events "
                    "WHERE session_id = %s AND event_type IN "
                    "('live_entry_filled','live_exit_fill') ORDER BY id ASC", (sid,))
        fills = []
        for ts, et, payload in cur.fetchall():
            p = payload if isinstance(payload, dict) else {}
            fills.append({"ts": ts, "event_type": et,
                          "qty": p.get("qty") or p.get("quantity"),
                          "px": p.get("fill_price") or p.get("price") or p.get("avg_price"),
                          "trigger_reason": p.get("trigger_reason") or p.get("entry_reason"),
                          "exit_reason": p.get("exit_reason") or p.get("reason")})
        out["fill_events"] = fills
        conn.close()
    except Exception as exc:  # mining failure must never kill the batch
        out["mine_error"] = f"{type(exc).__name__}: {exc}"
    return out


def golden_window_stats(win: dict) -> dict:
    import psycopg2

    try:
        conn = psycopg2.connect(PROD)
        cur = conn.cursor()
        cur.execute(MARKET_SQL, {"s": win["symbol"], "a": win["win_start"], "b": win["win_end"]})
        n, hi, lo, first_px, hi_at = cur.fetchone()
        conn.close()
        if not n:
            return {"win_ticks": 0}
        up_pct = round((float(hi) - float(first_px)) / float(first_px) * 100.0, 2) if first_px else None
        return {"win_ticks": int(n), "first_px": float(first_px), "hi": float(hi),
                "lo": float(lo), "hi_at": hi_at, "up_pct": up_pct}
    except Exception as exc:
        return {"stats_error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default="D:/CHILI-Docker/chili-data/replay_batch/window_manifest.json")
    ap.add_argument("--out-dir", default="D:/CHILI-Docker/chili-data/replay_batch")
    ap.add_argument("--results", default=None, help="results JSONL (default <out-dir>/results.jsonl)")
    ap.add_argument("--tiers", default="baseline", help="comma list: baseline,library")
    ap.add_argument("--stop-at", default=os.environ.get("STOP_AT", "2026-07-27T03:00:00"),
                    help="LOCAL naive ISO hard deadline — no window starts unless it fits")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None,
                    help="comma list of SYMBOL|YYYY-MM-DD keys — run just these (smoke)")
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-run windows whose last record is not status=ok")
    args = ap.parse_args()

    sink = guard_sink(os.environ.get("TEST_DATABASE_URL", DEFAULT_SINK))
    stop_at = datetime.fromisoformat(args.stop_at)
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    os.makedirs(args.out_dir, exist_ok=True)
    results_path = args.results or os.path.join(args.out_dir, "results.jsonl")
    logs_dir = os.path.join(args.out_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    specs = [w for w in manifest["windows"] if w["tier"] in tiers]
    if args.only:
        want = {k.strip() for k in args.only.split(",") if k.strip()}
        specs = [w for w in manifest["windows"] if f"{w['symbol']}|{w['day']}" in want]
    if args.limit:
        specs = specs[: args.limit]

    done: set[str] = set()
    if os.path.exists(results_path):
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("status") == "ok" or not args.retry_errors:
                    if rec.get("status") is not None:
                        done.add(rec["key"])

    build_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BUILD,
                               capture_output=True, text=True).stdout.strip()
    meta = {"schema": "chili.golden_replay_run_meta.v1", "build_sha": build_sha,
            "driver": os.path.relpath(DRIVER, BUILD), "golden": True, "arm": "base",
            "flags_json": "", "sink": sink.rsplit("/", 1)[-1],
            "equity_exec": "driver defaults (13000 / robinhood_agentic_mcp) for canonical tie-back",
            "stop_at": args.stop_at, "started_at": datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)

    # ---- advisory lock: one replay batch at a time against this sink ----
    import psycopg2

    lock_conn = psycopg2.connect(sink)
    lock_conn.autocommit = True
    lc = lock_conn.cursor()
    lc.execute("SELECT pg_try_advisory_lock(hashtext('chili_replay_batch'))")
    if not lc.fetchone()[0]:
        raise SystemExit("[batch] another replay batch holds the sink advisory lock — aborting")

    print(f"[batch] {len(specs)} windows queued (tiers={sorted(tiers)}), "
          f"{len(done)} already done, sha={build_sha[:9]}, stop_at={args.stop_at}", flush=True)
    ran = 0
    try:
        for w in specs:
            key = f"{w['symbol']}|{w['day']}|base"
            if key in done:
                continue
            est = int(w.get("est_runtime_s") or 1800)
            remaining = (stop_at - datetime.now()).total_seconds()
            if 1.25 * est + 120 > remaining:
                print(f"[batch] DEADLINE — {key} needs ~{est}s, only {remaining:.0f}s left; "
                      f"stopping clean", flush=True)
                break
            timeout = min(3900, max(60, int(remaining - 120)))
            env = dict(os.environ)
            env.pop("FLAGS_JSON", None)
            env.update({"SYMBOL": w["symbol"], "ARM": "base",
                        "WIN_START": w["win_start"], "WIN_END": w["win_end"],
                        "OHLCV_START": w["ohlcv_start"], "GOLDEN": "1",
                        "PREPEND_OHLCV": "1" if w.get("prepend") else "0",
                        "ENTRY_DIAG": "1", "DATABASE_URL": sink, "TEST_DATABASE_URL": sink,
                        "PYTHONPATH": BUILD, "PYTHONUNBUFFERED": "1",
                        "PYTHONIOENCODING": "utf-8"})
            t0 = time.time()
            print(f"[batch] RUN {key} ({w['ticks']:,}t, est {est // 60}min, timeout {timeout}s)",
                  flush=True)
            status = "ok"
            stdout = stderr = ""
            exit_code = None
            try:
                p = subprocess.run([sys.executable, DRIVER], env=env, cwd=BUILD,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
                stdout, stderr, exit_code = p.stdout or "", p.stderr or "", p.returncode
            except subprocess.TimeoutExpired as e:
                status = "timeout"
                stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
                stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            log_name = f"{w['symbol']}_{w['day']}_base.log"
            with open(os.path.join(logs_dir, log_name), "w", encoding="utf-8") as f:
                f.write(stdout)
                if stderr:
                    f.write("\n===== STDERR =====\n" + stderr)
            if status == "ok":
                pstatus, parsed = parse_driver_stdout(stdout)
                if exit_code != 0:
                    status = "error"
                elif pstatus != "ok":
                    status = "parse_fail"
                    parsed = parsed or {}
            else:
                _, parsed = parse_driver_stdout(stdout)
            rec = {"schema": "chili.golden_replay_window_result.v1", "key": key,
                   "symbol": w["symbol"], "day": w["day"], "class": w["class"],
                   "tier": w["tier"], "win_start": w["win_start"], "win_end": w["win_end"],
                   "ohlcv_start": w["ohlcv_start"], "window_source": w["window_source"],
                   "prepend": bool(w.get("prepend")), "arm": "base", "golden": True,
                   "build_sha": build_sha, "started_at": datetime.fromtimestamp(t0).isoformat(
                       timespec="seconds"),
                   "duration_s": round(time.time() - t0, 1), "exit_code": exit_code,
                   "status": status,
                   "pnl": parsed.get("pnl"), "entries": parsed.get("entries"),
                   "exits": parsed.get("exits"), "final_state": parsed.get("final_state"),
                   "fills": parsed.get("fills", []),
                   "log": f"logs/{log_name}"}
            rec["sink"] = mine_sink(sink, w["symbol"])          # BEFORE the next run's DELETE
            rec["market"] = golden_window_stats(w)
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            ran += 1
            print(f"[batch] DONE {key} status={status} pnl={rec['pnl']} "
                  f"entries={rec['entries']} exits={rec['exits']} "
                  f"({rec['duration_s']:.0f}s)", flush=True)
    finally:
        lc.execute("SELECT pg_advisory_unlock(hashtext('chili_replay_batch'))")
        lock_conn.close()
    print(f"[batch] finished: {ran} windows this invocation; results -> {results_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
