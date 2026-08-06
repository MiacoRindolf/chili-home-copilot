"""Saan eksaktong humihinto ang paper lane? — isang utos, isang sagot.

BAKIT ITO UMIIRAL (2026-08-05). Tinanong: "bakit walang fill?" Umabot ng ilang
oras ang forensics bago malaman ang sagot para sa ISANG araw, at ang sagot pala
ay simple: sa 13 generation ngayong araw (r94-r106), DALAWA lang ang
nakapag-emit ng kahit anong event, at ang nag-iisang buhay habang bukas ang
market (r102) ay 8 bootstrap event sa kalahating segundo lang bago tumahimik.
Hindi problema ng estratehiya — hindi buhay ang serbisyo nang mahalaga.

Ang gastos ay wala sa pag-aayos kundi sa PAGTUKLAS. Kaya ang script na ito ay
binabasa ang capture store at ang DB at iniuulat ang bawat yugto ng kadena, na
may KAILAN at ILAN — para ang susunod na "bakit walang fill?" ay minuto, hindi
maghapon.

READ-ONLY. Walang isinusulat, walang order, walang binabago. Ligtas patakbuhin
habang buhay ang serbisyo.

Gamit:
    python scripts/paper_lane_chain_probe.py                # ngayong araw
    python scripts/paper_lane_chain_probe.py --date 2026-08-05
    python scripts/paper_lane_chain_probe.py --cp-root D:/cp
"""
from __future__ import annotations

import argparse
import collections
import glob
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

# Ang RTH sa UTC (EDT = UTC-4): 13:30-20:00. Premarket mula 08:00.
_RTH_START_UTC = (13, 30)
_RTH_END_UTC = (20, 0)
_PREMARKET_START_UTC = (8, 0)

# Ang mga yugto ng kadena, ayon sa pagkakasunod. Ang unang WALA ang sagot.
_CHAIN = [
    ("bootstrap", ("code_build", "config_snapshot", "feature_flag_snapshot",
                   "capture_health", "read_receipt", "account_risk_snapshot")),
    ("selection", ("captured_viability_input", "captured_paper_selection",
                   "selection_queue", "captured_selection_input")),
    ("arm", ("arm", "armed", "auto_arm", "watch")),
    ("entry", ("entry", "trigger", "pullback", "tick_break")),
    ("order", ("order", "submit", "place", "bbo")),
    ("fill", ("fill", "execution", "settlement")),
]


def _stage_for(stream: str) -> str | None:
    s = (stream or "").lower()
    for stage, keys in _CHAIN:
        for k in keys:
            if k in s:
                return stage
    return None


def _iter_events(gen_dir: str):
    """Bawat event sa isang generation. Fail-soft sa sirang file."""
    try:
        import zstandard as zstd
    except ImportError:
        print("KAILANGAN ang `zstandard`: pip install zstandard", file=sys.stderr)
        raise
    pat = os.path.join(gen_dir, "capture-store", "events", "**", "*.jsonl.zst")
    for f in glob.glob(pat, recursive=True):
        try:
            raw = zstd.ZstdDecompressor().stream_reader(open(f, "rb"))
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
        except Exception:
            continue


def _in_market_hours(ts: str) -> str:
    """'rth' / 'premarket' / 'closed' para sa isang UTC timestamp."""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return "?"
    hm = (t.hour, t.minute)
    if _RTH_START_UTC <= hm < _RTH_END_UTC:
        return "rth"
    if _PREMARKET_START_UTC <= hm < _RTH_START_UTC:
        return "premarket"
    return "closed"


def probe_generation(gen_dir: str) -> dict:
    streams: collections.Counter = collections.Counter()
    stages: collections.Counter = collections.Counter()
    symbols: set[str] = set()
    sessions: collections.Counter = collections.Counter()
    tmin = tmax = None
    for d in _iter_events(gen_dir):
        st = d.get("stream") or "?"
        streams[st] += 1
        stage = _stage_for(st)
        if stage:
            stages[stage] += 1
        if d.get("symbol"):
            symbols.add(d["symbol"])
        t = (d.get("clocks") or {}).get("available_at")
        if t:
            tmin = t if tmin is None or t < tmin else tmin
            tmax = t if tmax is None or t > tmax else tmax
            sessions[_in_market_hours(t)] += 1
    return {
        "dir": os.path.basename(gen_dir),
        "events": sum(streams.values()),
        "streams": streams,
        "stages": stages,
        "symbols": symbols,
        "sessions": sessions,
        "first": tmin,
        "last": tmax,
    }


def _print_admission_census() -> None:
    """Ang admission-census sidecar — kung BAKIT tumanggi ang sealed admitter.

    Ang capture store ay nagsasabi kung SAAN huminto ang kadena; ito ang
    nagsasabi kung BAKIT. Hiwalay itong file dahil ang census ay diagnostic at
    hindi kasya sa non-spoofable na capture-health envelope (tingnan ang
    paliwanag sa live_runner_loop._append_admission_census_sidecar).
    """
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "chili_admission_census.jsonl")
    print()
    print("=== ADMISSION CENSUS (bakit tumanggi ang admitter) ===")
    if not os.path.isfile(path):
        print("  walang sidecar — hindi pa naaabot ng lane ang admitter,")
        print("  o hindi pa naka-deploy ang build na may census.")
        return
    last = None
    lines = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                    lines += 1
                except Exception:
                    continue
    except Exception as exc:  # pragma: no cover
        print(f"  hindi mabasa: {exc}")
        return
    if last is None:
        print("  may file pero walang mabasang record")
        return
    # Cumulative ang counters, kaya ang HULING linya ang buong larawan.
    print(f"  {lines} snapshot | huli {last.get('at')} (pid {last.get('pid')})")
    print(f"  {last.get('total')} attempt, {last.get('admitted')} admitted")
    for k, v in sorted(
        (last.get("outcomes") or {}).items(), key=lambda kv: -kv[1]
    ):
        mark = "  " if k == "admitted" else ">>"
        print(f"    {mark} {k:44s} {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cp-root", default=r"D:\cp")
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    args = ap.parse_args()

    pat = os.path.join(args.cp_root, f"one-shot-*{args.date}*")
    gens = sorted(
        (d for d in glob.glob(pat) if os.path.isdir(d)),
        key=lambda d: re.sub(r".*-r(\d+)-.*", r"\1", os.path.basename(d)).zfill(6),
    )
    if not gens:
        print(f"WALANG generation na tumutugma sa {pat}")
        return 1

    print(f"=== PAPER LANE CHAIN PROBE — {args.date} ===")
    print(f"{len(gens)} generation\n")
    print(f"{'generation':<50} {'events':>7}  {'RTH':>5}  {'yugto na naabot':<26} syms")
    print("-" * 108)

    live_rth = []
    for g in gens:
        r = probe_generation(g)
        reached = [s for s, _ in _CHAIN if r["stages"].get(s)]
        rth = r["sessions"].get("rth", 0)
        if rth:
            live_rth.append(r)
        label = " -> ".join(reached) if reached else "(WALA)"
        print(f"{r['dir']:<50} {r['events']:>7}  {rth:>5}  {label:<26} {len(r['symbols'])}")

    _print_admission_census()

    print()
    print("=== HATOL ===")
    if not live_rth:
        print("  🔴 WALANG generation na nag-emit ng event habang BUKAS ang market (RTH).")
        print("     Ang lane ay hindi buhay nang mahalaga — LIVENESS ito, hindi estratehiya.")
        print("     Wala pang masasabi tungkol sa conversion hangga't walang RTH na takbo.")
        return 0

    print(f"  {len(live_rth)} generation ang may RTH na aktibidad.")
    for r in live_rth:
        reached = [s for s, _ in _CHAIN if r["stages"].get(s)]
        last_stage = reached[-1] if reached else None
        print(f"\n  {r['dir']}")
        print(f"    bintana : {r['first']} -> {r['last']}")
        print(f"    RTH events: {r['sessions'].get('rth', 0)}  | symbols: {len(r['symbols'])}")
        for s, _ in _CHAIN:
            n = r["stages"].get(s, 0)
            mark = "OK " if n else "-- "
            print(f"      {mark}{s:<12} {n}")
        if last_stage and last_stage != _CHAIN[-1][0]:
            nxt = _CHAIN[[s for s, _ in _CHAIN].index(last_stage) + 1][0]
            print(f"    >>> HUMINTO PAGKATAPOS NG '{last_stage}'; walang '{nxt}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
